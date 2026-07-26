-- ============================================================================
-- Vidioma — Row Level Security (defense in depth)
-- ============================================================================
-- Run this in the Supabase SQL editor. It is additive: no table shapes change,
-- and no application code has to change.
--
-- WHY, given the backend already checks authorization in Python:
--
-- The backend talks to Supabase with the SERVICE ROLE key, which BYPASSES RLS
-- entirely. So enabling RLS does not alter how the app behaves today. What it
-- does is remove the assumption that every one of the ~40 hand-written
-- ownership checks in app.py is, and forever remains, correct. Today a single
-- missing `user_id: eq.<caller>` filter on any query is a cross-tenant data
-- leak with nothing behind it. With these policies, the same mistake is
-- contained the moment anything talks to the database as the end user instead of
-- as the service role.
--
-- This matters here because the data is student data: names, class rosters, and
-- per-assignment progress that a teacher can see for their own students only.
--
-- It also makes a future direct-from-browser query (with the anon key) safe by
-- default rather than a new audit surface, which is exactly the migration the
-- note at the bottom of assignments_schema.sql anticipated.
--
-- WHAT EACH POLICY GRANTS: only ever "rows that are yours". A teacher reaches
-- rows belonging to classes they own; a student reaches their own rows and the
-- classes they are enrolled in. Nothing here grants a wider view than the
-- corresponding endpoint in app.py already enforces, so turning RLS on cannot
-- expose something that was previously hidden.
-- ----------------------------------------------------------------------------

-- ── Idempotency: drop every policy this file manages, FIRST ────────────────
-- All drops are hoisted here rather than sitting next to each create. The
-- Supabase SQL editor runs only the HIGHLIGHTED text when there is a selection,
-- so a partial run could previously create policies while skipping their drops,
-- and the next run then failed with "policy ... already exists". With the drops
-- in one leading block, re-running (or running any later portion) is always safe.
drop policy if exists own_profile_select                on public.user_profiles;
drop policy if exists own_profile_insert                on public.user_profiles;
drop policy if exists own_profile_update                on public.user_profiles;
drop policy if exists teacher_classes_all               on public.classes;
drop policy if exists student_reads_enrolled_class      on public.classes;
drop policy if exists own_enrolment_select              on public.student_classes;
drop policy if exists own_enrolment_insert              on public.student_classes;
drop policy if exists enrolment_delete                  on public.student_classes;
drop policy if exists own_progress_all                  on public.user_progress;
drop policy if exists videos_read                       on public.videos;
drop policy if exists teacher_assignments_all           on public.assignments;
drop policy if exists student_reads_targeted_assignment on public.assignments;
drop policy if exists targets_select                    on public.assignment_targets;
drop policy if exists targets_teacher_write             on public.assignment_targets;
drop policy if exists own_assignment_progress_select    on public.assignment_progress;
drop policy if exists own_assignment_progress_write     on public.assignment_progress;
drop policy if exists own_assignment_progress_update    on public.assignment_progress;

-- Enable RLS everywhere. With RLS on and NO policy matching, the default is deny,
-- so each table below gets explicit policies for the access the app really needs.
alter table public.user_profiles      enable row level security;
alter table public.classes            enable row level security;
alter table public.student_classes    enable row level security;
alter table public.user_progress      enable row level security;
alter table public.videos             enable row level security;
alter table public.assignments        enable row level security;
alter table public.assignment_targets enable row level security;
alter table public.assignment_progress enable row level security;

-- Helper: is the current user the teacher who owns this class?
-- SECURITY DEFINER so the function itself can read classes without recursing
-- through the policy that calls it (a policy that queries its own table would
-- otherwise deadlock into infinite recursion).
create or replace function public.owns_class(cls uuid)
returns boolean
language sql
security definer
set search_path = public
stable
as $$
  select exists (
    select 1 from public.classes c
    where c.class_id = cls and c.teacher_id = auth.uid()
  );
$$;

-- Helper: is the current user enrolled in this class?
create or replace function public.is_enrolled(cls uuid)
returns boolean
language sql
security definer
set search_path = public
stable
as $$
  select exists (
    select 1 from public.student_classes sc
    where sc.class_id = cls and sc.student_id = auth.uid()
  );
$$;

-- 1. user_profiles: a user reads and writes only their own profile row.
--    Teachers legitimately need student NAMES for their roster, which the app
--    serves through the service role; RLS does not need to widen this.
create policy own_profile_select on public.user_profiles
  for select using (user_id = auth.uid());

create policy own_profile_insert on public.user_profiles
  for insert with check (user_id = auth.uid());

create policy own_profile_update on public.user_profiles
  for update using (user_id = auth.uid()) with check (user_id = auth.uid());

-- 2. classes: the owning teacher has full control; enrolled students can read.
create policy teacher_classes_all on public.classes
  for all using (teacher_id = auth.uid()) with check (teacher_id = auth.uid());

create policy student_reads_enrolled_class on public.classes
  for select using (public.is_enrolled(class_id));

-- 3. student_classes (enrolment): a student sees and creates their own
--    enrolment (joining by code); the class's teacher sees and removes any
--    enrolment in their class.
create policy own_enrolment_select on public.student_classes
  for select using (student_id = auth.uid() or public.owns_class(class_id));

create policy own_enrolment_insert on public.student_classes
  for insert with check (student_id = auth.uid());

create policy enrolment_delete on public.student_classes
  for delete using (student_id = auth.uid() or public.owns_class(class_id));

-- 4. user_progress: strictly private to the user. This is the personal
--    dashboard; assignment work is tracked separately in assignment_progress.
create policy own_progress_all on public.user_progress
  for all using (user_id = auth.uid()) with check (user_id = auth.uid());

-- 5. videos: shared, non-sensitive metadata (a YouTube id, title, thumbnail).
--    Readable by any signed-in user; only the service role writes it, since rows
--    are created as a side effect of processing a video.
create policy videos_read on public.videos
  for select using (auth.uid() is not null);

-- 6. assignments: the teacher who created it has full control. A student can
--    read an assignment only if it targets them, either directly or through a
--    class they are enrolled in.
create policy teacher_assignments_all on public.assignments
  for all using (teacher_id = auth.uid()) with check (teacher_id = auth.uid());

create policy student_reads_targeted_assignment on public.assignments
  for select using (
    exists (
      select 1 from public.assignment_targets t
      where t.assignment_id = assignments.assignment_id
        -- student_id null means the whole class is targeted, which mirrors how
        -- the app expands class targets to the roster at read time.
        and (t.student_id = auth.uid()
             or (t.student_id is null and public.is_enrolled(t.class_id)))
    )
  );

-- 7. assignment_targets: visible to the assignment's teacher, and to a student
--    the row applies to.
create policy targets_select on public.assignment_targets
  for select using (
    public.owns_class(class_id)
    or student_id = auth.uid()
    or (student_id is null and public.is_enrolled(class_id))
  );

create policy targets_teacher_write on public.assignment_targets
  for all using (public.owns_class(class_id)) with check (public.owns_class(class_id));

-- 8. assignment_progress: a student reads and writes only their own progress.
--    The assignment's teacher can read it (that is the whole point of the
--    teacher view) but never write it.
create policy own_assignment_progress_select on public.assignment_progress
  for select using (
    student_id = auth.uid()
    or exists (
      select 1 from public.assignments a
      where a.assignment_id = assignment_progress.assignment_id
        and a.teacher_id = auth.uid()
    )
  );

create policy own_assignment_progress_write on public.assignment_progress
  for insert with check (student_id = auth.uid());

create policy own_assignment_progress_update on public.assignment_progress
  for update using (student_id = auth.uid()) with check (student_id = auth.uid());

-- ----------------------------------------------------------------------------
-- VERIFYING: with the service role key nothing changes (RLS is bypassed), which
-- is why this is safe to apply to a live project. To confirm the policies
-- actually constrain an end user, query as an authenticated user, e.g. from the
-- Supabase SQL editor:
--
--   set local role authenticated;
--   set local request.jwt.claims = '{"sub":"<some-user-uuid>"}';
--   select count(*) from public.user_progress;   -- only that user's rows
--
-- reset role;
-- ----------------------------------------------------------------------------
