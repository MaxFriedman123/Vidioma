-- ============================================================================
-- Vidioma — per-attempt answer log
-- ============================================================================
-- Run this in the Supabase SQL editor. Additive only: no existing table changes.
--
-- WHY: the app grades every submitted answer and then discards everything about
-- it. `isError` is transient React state, and `assignment_progress.total_attempts`
-- is a single running sum, so a student who breezed through 28 lines and stalled
-- on one is indistinguishable from a student who fought every line. The client
-- already knows the per-line detail and drops it at the API boundary.
--
-- This records one row per submission, which is what makes error review, per-line
-- teacher insight, and any future difficulty signal possible at all.
-- ----------------------------------------------------------------------------

create table if not exists public.line_attempts (
    attempt_id     bigserial primary key,
    user_id        uuid not null references auth.users(id) on delete cascade,
    video_id       uuid not null references public.videos(id) on delete cascade,

    transcript_language  text not null,
    translation_language text not null,

    -- Which line, and what it said. line_index alone is NOT a stable identity:
    -- caption tracks get re-uploaded and paragraph grouping shifts, so the same
    -- index can mean a different line next week. source_hash is the durable key
    -- for "have I seen this line before", and source_text keeps the row readable
    -- even after the video changes or is taken down.
    line_index     integer not null,
    source_text    text not null,
    source_hash    text not null,

    -- What the learner was graded against, snapshotted. Deliberately stored
    -- rather than re-derived: the translation cache has a 24h TTL and machine
    -- translation is not stable across provider versions, so re-deriving later
    -- would silently change what "correct" meant at the time.
    expected_text  text,
    user_text      text not null,

    -- practice_mode records WHICH task this was, because the same line graded as
    -- a translation and as dictation are not comparable.
    practice_mode  text not null default 'translate',
    score          numeric(4,3) not null,
    passed         boolean not null,
    attempt_no     smallint not null default 1,

    -- Null when practising outside an assignment (the personal dashboard path).
    assignment_id  uuid references public.assignments(assignment_id) on delete set null,
    created_at     timestamptz not null default now(),

    constraint line_attempts_mode_check
        check (practice_mode in ('translate', 'listen', 'dictate')),
    constraint line_attempts_score_range
        check (score >= 0 and score <= 1)
);

-- "My recent attempts", the review-queue read pattern.
create index if not exists idx_line_attempts_user_recent
    on public.line_attempts(user_id, created_at desc);

-- "Have I failed this exact line before", keyed on the durable identity.
create index if not exists idx_line_attempts_user_source
    on public.line_attempts(user_id, source_hash);

-- "Which lines did this class struggle with", for a future teacher view.
create index if not exists idx_line_attempts_assignment
    on public.line_attempts(assignment_id, line_index)
    where assignment_id is not null;

-- ----------------------------------------------------------------------------
-- RLS: a learner's own rows only, plus the assignment's teacher for the rows
-- belonging to their own assignment. This table holds free-text student writing,
-- which is a more sensitive surface than the counters it replaces, so the read
-- policy is deliberately narrower than "anyone in the class".
-- ----------------------------------------------------------------------------
alter table public.line_attempts enable row level security;

drop policy if exists own_attempts_select on public.line_attempts;
drop policy if exists own_attempts_insert on public.line_attempts;
drop policy if exists teacher_reads_assignment_attempts on public.line_attempts;

create policy own_attempts_select on public.line_attempts
  for select using (user_id = auth.uid());

create policy own_attempts_insert on public.line_attempts
  for insert with check (user_id = auth.uid());

create policy teacher_reads_assignment_attempts on public.line_attempts
  for select using (
    assignment_id is not null
    and exists (
      select 1 from public.assignments a
      where a.assignment_id = line_attempts.assignment_id
        and a.teacher_id = auth.uid()
    )
  );

-- ----------------------------------------------------------------------------
-- RETENTION: this is per-keystroke-adjacent student writing about minors in a
-- classroom context, so it must not accumulate forever. Run this periodically
-- (the keepalive cron is a natural home) to purge attempts older than 180 days:
--
--   delete from public.line_attempts where created_at < now() - interval '180 days';
--
-- Volume note: a 400-line video with retries is roughly 600 rows per sitting, so
-- a class of 30 generates ~18k rows per assignment. Fine for Postgres with the
-- indexes above, but do not select * across a whole class.
-- ----------------------------------------------------------------------------
