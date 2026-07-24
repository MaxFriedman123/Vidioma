# Vidioma Supabase keep-alive (Cloudflare Worker cron)

A Supabase free-plan project **pauses after 7 consecutive days without
activity**, and un-pausing is a manual click in the dashboard. While paused,
auth, saved progress, classes, and assignments are all down for users. This
Worker calls the backend's `/api/db-keepalive` every 2 days, which touches the
database and resets Supabase's idle clock.

Every 2 days rather than every 6 is deliberate: two consecutive runs can fail
(Cloudflare incident, backend down, Supabase blip) and the project still stays
awake with a day to spare.

## No credentials required

This Worker holds **no secrets**. It calls a public backend endpoint, and the
already-deployed backend does the authenticated DB read using the
`SUPABASE_SERVICE_KEY` it already has in its host env vars. So there is no key to
copy, no second place to rotate it, and no Supabase dashboard access needed to
set this up.

`/api/db-keepalive` is unauthenticated for the same reason: the caller is a cron
job with no user identity. It is safe to leave open because it reveals nothing
(the response is just `{ok, pinged_at}`), writes nothing, and is rate-limited to
6 requests per hour per IP.

## Setup

**1. Deploy the backend change.** The `/api/db-keepalive` endpoint must be live
first. It ships with the backend, so push `main` and let the normal deploy run.
Confirm it answers:

```sh
curl -s https://<your-backend-host>/api/db-keepalive
# {"ok":true,"pinged_at":"..."}
```

A `503 Database not configured` means the backend has no Supabase env vars set.
A `502` means it has them but the DB refused the read.

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
