// Cloudflare Worker: Supabase keep-alive pinger (cron-triggered).
//
// WHY: a Supabase free-plan project pauses after 7 consecutive days without
// activity, and un-pausing is a manual dashboard click. A paused project takes
// down auth, saved progress, classes, and assignments. This Worker calls the
// backend's /api/db-keepalive on a cron schedule so the project never reaches
// the 7-day mark.
//
// WHY IT CALLS THE BACKEND INSTEAD OF SUPABASE DIRECTLY: the deployed backend
// already holds SUPABASE_SERVICE_KEY in its host env vars, so it can do the
// authenticated DB touch itself. This Worker therefore needs no credentials at
// all -- nothing to store here, nothing to rotate in two places, and no copy of
// a key that bypasses RLS sitting in a second deployment.
//
// WHY A CRON WORKER AND NOT A GITHUB ACTIONS SCHEDULE: GitHub disables
// scheduled workflows after 60 days of repo inactivity and requires a manual
// click to re-enable. That failure mode correlates exactly with the situation
// this job exists to protect (a quiet period), so it would go silent right when
// it is needed. Cloudflare cron triggers have no such rule.
//
// Deploy: see README.md (one `wrangler deploy`, no secrets).

// The backend also sleeps when idle on a free host, so a cold start can take
// ~50s to boot before it answers. This is a background cron with nobody
// waiting, so the timeout is generous: a slow answer is still a successful
// ping, and giving up early would report a false failure.
const REQUEST_TIMEOUT_MS = 90000;

async function ping(env) {
  const base = (env.BACKEND_URL || "").replace(/\/+$/, "");
  if (!base) throw new Error("BACKEND_URL must be set (see wrangler.toml)");

  const resp = await fetch(`${base}/api/db-keepalive`, {
    method: "POST",
    headers: { "User-Agent": "vidioma-keepalive-cron" },
    signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
  });

  // The endpoint returns 503 when the backend has no DB configured and 502 when
  // the DB itself refused -- both mean the project did NOT get touched, so they
  // must fail loudly rather than look like a successful run.
  if (!resp.ok) {
    const detail = (await resp.text().catch(() => "")).slice(0, 200);
    throw new Error(`keepalive ping failed: ${resp.status} ${detail}`);
  }

  // Body is {ok, pinged_at}; tolerate a non-JSON body rather than turning a
  // successful 200 into a failure over a parse error.
  try {
    return await resp.json();
  } catch {
    return { ok: true };
  }
}

export default {
  // Cron entrypoint. A thrown error marks the run as failed in the Cloudflare
  // dashboard (Workers > this worker > Logs), which is the signal to look at.
  async scheduled(event, env, ctx) {
    const body = await ping(env);
    console.log(`keepalive ok (cron ${event.cron}) pinged_at=${body?.pinged_at || "n/a"}`);
  },

  // Manual trigger, so a deploy can be verified immediately instead of waiting
  // for the next cron tick. No auth: it holds no secrets and does strictly less
  // than the public endpoint it calls, which is itself rate-limited.
  async fetch(request, env) {
    try {
      const body = await ping(env);
      return json({ ok: true, backend: body });
    } catch (e) {
      return json({ ok: false, error: String(e.message || e) }, 502);
    }
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
