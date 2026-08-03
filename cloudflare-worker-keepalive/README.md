# Vidioma Supabase keep-alive (Cloudflare Worker cron)

A Supabase free-plan project **pauses after 7 consecutive days without
activity**, and un-pausing is a manual click in the dashboard. While paused,
auth, saved progress, classes, and assignments are all down for users. This
Worker calls the backend's `/api/db-keepalive` every 2 days, which touches the
database and resets Supabase's idle clock.

## The ping must be a WRITE

The first version of this read one row (`GET /videos?select=id&limit=1`) on the
assumption that any real query counts as activity. **It does not.** Cloudflare
analytics showed the cron running 7 times across 8 days with zero errors, and
Supabase still sent an "unused project will be paused" notice. A `limit=1` read
of a single indexed column is exactly the shape of request that can be answered
without meaningful database work.

So `/api/db-keepalive` now upserts a row in `public.keepalive`
(`backend/db/keepalive_schema.sql`) — bumping `pinged_at` and incrementing
`ping_count`. A write has to reach Postgres, produce WAL and change on-disk
state, so it cannot be served from a cache.

If you are setting this up fresh, **run `backend/db/keepalive_schema.sql` in the
Supabase SQL editor first** — the endpoint returns 502 until that table exists.

Every 2 days rather than every 6 is deliberate: two consecutive runs can fail
(Cloudflare incident, backend down, Supabase blip) and the project still stays
awake with a day to spare.

## No credentials required

This Worker holds **no secrets**. It calls a public backend endpoint, and the
already-deployed backend does the authenticated DB write using the
`SUPABASE_SERVICE_KEY` it already has in its host env vars. So there is no key to
copy, no second place to rotate it, and no Supabase dashboard access needed to
set this up.

`/api/db-keepalive` is unauthenticated for the same reason: the caller is a cron
job with no user identity. It is safe to leave open because it reveals nothing
(the response is just `{ok, pinged_at, ping_count}`), writes only to a
single-row sentinel table that no user-facing query reads, and is rate-limited
to 60 requests per hour.

The limit is loose for a caller that runs ~15 times a month because behind
Render's proxy the limiter's buckets are effectively global rather than per-IP
(`get_remote_address` sees the proxy, not the client). A tight limit here would
let any unrelated traffic lock out the cron, and a locked-out tick means waiting
2 days for the next one.

## Setup

**0. Create the heartbeat table.** Run `backend/db/keepalive_schema.sql` in the
Supabase SQL editor. It is additive and idempotent (`create table if not
exists`), so re-running it is harmless.

**1. Deploy the backend change.** The `/api/db-keepalive` endpoint must be live
first. It ships with the backend, so push `main` and let the normal deploy run.
Confirm it answers:

```sh
curl -s https://<your-backend-host>/api/db-keepalive
# {"ok":true,"pinged_at":"...","ping_count":1}
```

A `503 Database not configured` means the backend has no Supabase env vars set.
A `502` means it has them but the write was refused — most likely step 0 was
skipped, so `public.keepalive` does not exist yet.

`ping_count` climbing across calls is the proof the write landed; a fresh
`pinged_at` with a stuck `ping_count` would mean the row is not being updated.

**2. Point this Worker at your backend.** Edit `BACKEND_URL` under `[vars]` in
`wrangler.toml` to your backend's base URL. It is a plain var, not a secret,
because a public URL is not a credential.

**3. Deploy.**

```sh
npm i -g wrangler        # or use npx
wrangler login           # one-time, needs a Cloudflare account (free)
wrangler deploy
```

Confirm the schedule registered under Workers & Pages >
`vidioma-supabase-keepalive` > Settings > Trigger Events.

**4. Verify without waiting for the cron.** `wrangler deploy` prints the Worker
URL; hit it directly:

```sh
curl -s -X POST https://vidioma-supabase-keepalive.<you>.workers.dev
# {"ok":true,"backend":{"ok":true,"pinged_at":"..."}}
```

A `502` here returns the backend's own error verbatim, so you can tell a Worker
misconfiguration (`BACKEND_URL must be set`) from a backend problem
(`503 Database not configured`).

## Checking on it later

Failed cron runs appear under Workers & Pages > `vidioma-supabase-keepalive` >
Logs, and `wrangler tail` streams them live. A successful run logs
`keepalive ok (cron ...) pinged_at=...`.

The honest limitation: nothing here *alerts* you. If the cron silently stops, you
find out when the project pauses. If you want a real alarm, point a free uptime
monitor (e.g. UptimeRobot) at `https://<your-backend-host>/api/db-keepalive` on a
2-day interval. It emails on failure, and its own polling doubles as a second
independent pinger.

## Why not GitHub Actions

GitHub disables scheduled workflows after 60 days of repository inactivity and
requires a manual click to re-enable. That failure mode correlates exactly with
the quiet period this job exists to protect against, so it would go silent right
when it is needed. Cloudflare cron triggers have no such rule.

## What this does not cover

- **An already-paused project.** This prevents a pause; it cannot un-pause one.
  Restore from the Supabase dashboard first, then deploy this.
- **A backend that is itself down.** The ping goes through the backend, so if
  that deployment is broken for more than ~6 days the DB stops being touched.
  The uptime-monitor option above covers this, since it would alert you.
- **Other free-tier limits.** Supabase also pauses/limits on storage and egress
  overages. Activity pings do nothing for those.
- **A read-only ping.** Documented above, but worth restating: if you ever
  "optimize" this endpoint back into a `select`, the project will start being
  flagged as unused again while the cron keeps reporting success. That failure is
  silent for up to 7 days. `backend/test_db_keepalive.py` pins the write.
