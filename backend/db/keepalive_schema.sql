-- ============================================================================
-- Vidioma — Supabase keep-alive heartbeat
-- ============================================================================
-- Run this in the Supabase SQL editor. Additive only: no existing table changes.
--
-- WHY THIS TABLE EXISTS
--
-- A Supabase free-plan project pauses after 7 consecutive days without
-- activity, and un-pausing is a manual dashboard click. While paused, auth,
-- saved progress, classes and assignments are all down for users.
--
-- The cron in cloudflare-worker-keepalive/ has been calling /api/db-keepalive
-- successfully every 2 days (Cloudflare analytics: 7 runs, 0 errors), and that
-- endpoint did a PostgREST read: GET /videos?select=id&limit=1. Supabase still
-- flagged the project as unused. A `limit=1` read of a single indexed column is
-- exactly the shape of request that can be served without meaningful database
-- work, so it evidently does not register as activity.
--
-- A WRITE cannot be satisfied that way. It has to reach Postgres, produce WAL,
-- and change on-disk state. That is why the heartbeat writes here instead of
-- reading from `videos`.
--
-- WHY A DEDICATED TABLE RATHER THAN WRITING TO AN EXISTING ONE
--
-- The heartbeat must never be confusable with real data. Writing a sentinel row
-- into `videos` (or any user table) would put a fake record in front of every
-- query, every count and every teacher-facing list, and would need excluding
-- from all of them forever. This table has exactly one row, is never joined, and
-- nothing else reads it.
-- ----------------------------------------------------------------------------

create table if not exists public.keepalive (
    -- Single-row table by construction. The check constraint makes a second row
    -- impossible rather than merely unlikely, so the endpoint can upsert on a
    -- known primary key and this can never grow.
    id           integer primary key default 1 check (id = 1),

    -- Bumped on every ping. `pinged_at` is what makes the write a real change:
    -- an upsert of an identical value would still be a write, but a moving
    -- timestamp also makes the last successful ping readable in the dashboard,
    -- which is the only place to check it without app access.
    pinged_at    timestamptz not null default now(),

    -- Monotonic counter, purely diagnostic: it distinguishes "the cron has run
    -- 40 times" from "one manual curl ran once", which the timestamp alone
    -- cannot.
    ping_count   bigint not null default 0
);

-- Seed the single row so the endpoint's upsert always has a target.
insert into public.keepalive (id, pinged_at, ping_count)
values (1, now(), 0)
on conflict (id) do nothing;

-- ----------------------------------------------------------------------------
-- RLS
-- ----------------------------------------------------------------------------
-- Enabled with NO policy, which denies every anon/authenticated request. The
-- backend writes with the service-role key, which bypasses RLS, so the
-- heartbeat is unaffected.
--
-- This matters because the anon key is published to the browser: without RLS,
-- any visitor could read or rewrite the heartbeat row directly through
-- PostgREST. Nothing here is sensitive, but a writable public row is free
-- vandalism surface and would let someone fake activity or spam WAL.
alter table public.keepalive enable row level security;

revoke all on public.keepalive from anon, authenticated;

-- ----------------------------------------------------------------------------
-- Verification
-- ----------------------------------------------------------------------------
-- Expect: one row, id = 1, and rowsecurity = true.
--
--   select id, pinged_at, ping_count from public.keepalive;
--   select relname, relrowsecurity from pg_class where relname = 'keepalive';
--
-- After the cron has run at least once, pinged_at should be within ~2 days of
-- now() and ping_count should be climbing.
