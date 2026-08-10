# Vidioma Supabase keep-alive (Cloudflare Worker cron)

A Supabase free-plan project **pauses when it sees too little activity over a
7-day period**, and un-pausing is a manual click in the dashboard. While paused,
auth, saved progress, classes, and assignments are all down for users. This
Worker calls the backend's `/api/db-keepalive` every 4 hours, which touches the
database and keeps it above Supabase's activity threshold.

## The threshold is DAILY VOLUME, not one clever request

This got flagged twice before the real cause was found, and both earlier fixes
misread the problem the same way, as a question of whether a single request
*qualifies* as activity. It is not. From Supabase's
[Project Pausing](https://supabase.com/docs/guides/platform/free-project-pausing)
doc:

> A Free plan project is considered inactive if it does not receive sufficient
> user database activity over the past week. Projects with **too few user
> queries** during that window are the clearest candidates for pausing.
> Typically **a few user requests to the database each day over the previous
> week** is enough to keep the project from being paused.

A few requests *each day*, for seven days. The cron used to run every 2 days,
which left **5 of every 7 days with zero activity**, averaging about one
request a day. That is the whole bug.

Confirmed from the project's own API logs (2026-08-10): the only traffic to the
database in 24 hours was the keepalive itself, and the auth log was empty. There
is no organic user traffic to fall back on, so the cron's cadence *is* the
project's activity level.

### What the two earlier fixes got wrong

1. **First version** read one row (`GET /videos?select=id&limit=1`) assuming any
   real query counts. It got flagged anyway, and the conclusion drawn was that a
   `limit=1` read of an indexed column was too cheap to count.
2. **Second version** (`ce6c693`) therefore switched to an upsert, on the theory
   that a write must produce WAL and change on-disk state so it cannot be served
   from cache. It got flagged **again**, with four upserts landing in the final
   7 days.

The doc counts "user requests to the database" and draws no read/write
distinction, so the original read would have been fine at a daily cadence. Both
fixes changed the request shape and left the every-2-days schedule alone, which
is why neither worked. **If this ever gets flagged again, suspect frequency and
check the API logs before touching the request.**

The upsert is kept anyway: it costs nothing extra, and `ping_count` is a genuinely
useful diagnostic (it distinguishes "the cron has run 40 times" from "one manual
curl ran once", which a timestamp alone cannot).

If you are setting this up fresh, **run `backend/db/keepalive_schema.sql` in the
Supabase SQL editor first** — the endpoint returns 502 until that table exists.

Six ticks a day is deliberate overkill: 12 DB requests a day, and six
independent chances, so one failed tick (Cloudflare incident, backend down,
Supabase blip) never costs a whole day of activity. It stays free: ~180
invocations a month against Cloudflare's 100k/day allowance, and ~100-byte
responses, so backend egress is well under a megabyte a month.

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

The direct check on whether the activity is actually *counting* is the project's
API log (Supabase dashboard > Logs > API, or the `get_logs` MCP tool). If the
only entries are `/rest/v1/keepalive`, this cron is the sole thing keeping the
project awake and its cadence is the whole safety margin.

The honest limitation: nothing here *alerts* you. If the cron silently stops, you
find out when the project pauses. If you want a real alarm, point a free uptime
monitor (e.g. UptimeRobot) at `https://<your-backend-host>/api/db-keepalive` on a
daily interval. It emails on failure, and its own polling doubles as a second
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
  that deployment is broken the DB stops being touched, and a few days of that is
  enough to fall under the threshold. The uptime-monitor option above covers
  this, since it would alert you.
- **Other free-tier limits.** Supabase also pauses/limits on storage and egress
  overages. Activity pings do nothing for those.
- **Stretching the schedule back out.** The failure mode that bit this twice is
  cadence, not request shape. If you ever "optimize" the cron to run less often,
  the project starts getting flagged again while every run still reports success,
  and that failure is silent until the warning email arrives. The guarantee to
  preserve is activity on **every** day of the trailing week, not a low total.
