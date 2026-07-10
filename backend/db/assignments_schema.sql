-- ============================================================================
-- Vidioma — Teacher → Student video assignments
-- ============================================================================
-- Run this in the Supabase SQL editor to add assignment support. It is additive
-- only (no changes to existing tables). Assignments let a teacher assign a
-- YouTube video to whole classes and/or specific students, with a due date, and
-- track each student's progress separately from their personal dashboard.
--
-- Tables:
--   assignments          one row per assignment a teacher creates
--   assignment_targets   who an assignment is for (a whole class, or one
--                        student within a class); an assignment can have many
--   assignment_progress  per-student progress on an assignment (kept separate
--                        from user_progress so assignments never show up in the
--                        personal "My Dashboard")
-- ----------------------------------------------------------------------------

-- 1. assignments -------------------------------------------------------------
create table if not exists public.assignments (
    assignment_id        uuid primary key default gen_random_uuid(),
    teacher_id           uuid not null references auth.users(id) on delete cascade,
    video_id             uuid not null references public.videos(id) on delete cascade,
    youtube_id           text not null,               -- denormalized for quick client launch
    title                text,                        -- video title (teacher-visible label)
    transcript_language  text not null default 'en',  -- language the student practices FROM
    translation_language text not null default 'es',  -- language they translate INTO
    instructions         text,                        -- optional note from the teacher
    due_date             timestamptz,                 -- null => no due date
    total_lines          integer default 0,           -- filled in once known (for % display)
    is_active            boolean not null default true,
    created_at           timestamptz not null default now()
);

create index if not exists idx_assignments_teacher on public.assignments(teacher_id);
create index if not exists idx_assignments_video   on public.assignments(video_id);

-- 2. assignment_targets ------------------------------------------------------
-- Each row targets EITHER an entire class (student_id null) OR a single student
-- within a class (student_id set). The app expands class targets to the class
-- roster at read time, so late-joining students also see class-wide assignments.
create table if not exists public.assignment_targets (
    target_id      uuid primary key default gen_random_uuid(),
    assignment_id  uuid not null references public.assignments(assignment_id) on delete cascade,
    class_id       uuid not null references public.classes(class_id) on delete cascade,
    student_id     uuid references auth.users(id) on delete cascade,  -- null => whole class
    created_at     timestamptz not null default now()
);

create index if not exists idx_targets_assignment on public.assignment_targets(assignment_id);
create index if not exists idx_targets_class       on public.assignment_targets(class_id);
create index if not exists idx_targets_student     on public.assignment_targets(student_id);
-- Prevent duplicate identical targets (treats null student as a distinct value).
create unique index if not exists uniq_target
    on public.assignment_targets(assignment_id, class_id, coalesce(student_id, '00000000-0000-0000-0000-000000000000'::uuid));

-- 3. assignment_progress -----------------------------------------------------
-- One row per (assignment, student). Kept separate from user_progress so
-- assignment work never appears on the personal dashboard.
--   current_line_index : resume point
--   max_line_reached   : furthest line the student has legitimately reached;
--                        the no-skip player never lets them jump past this
--   completed          : set true when they finish the last line
--   active_seconds     : accumulated active practice time; the client sends a
--                        per-save elapsed delta (capped, so idle gaps where the
--                        student walked away don't inflate it) and the backend
--                        adds it here
create table if not exists public.assignment_progress (
    id                  uuid primary key default gen_random_uuid(),
    assignment_id       uuid not null references public.assignments(assignment_id) on delete cascade,
    student_id          uuid not null references auth.users(id) on delete cascade,
    current_line_index  integer not null default 0,
    max_line_reached    integer not null default 0,
    total_lines         integer not null default 0,
    active_seconds      integer not null default 0,
    completed           boolean not null default false,
    completed_at        timestamptz,
    last_accessed_at    timestamptz not null default now(),
    unique (assignment_id, student_id)
);

create index if not exists idx_aprogress_student on public.assignment_progress(student_id);

-- If assignment_progress predates active practice-time tracking, add the column:
alter table public.assignment_progress
    add column if not exists active_seconds integer not null default 0;

-- ----------------------------------------------------------------------------
-- NOTE ON RLS: the backend uses the Supabase SERVICE ROLE key and enforces
-- authorization in application code (teacher-owns-assignment, student-is-a-
-- target), mirroring how the existing classes/user_progress endpoints work.
-- If you later expose these tables to the anon/auth client directly, add RLS
-- policies accordingly.
-- ----------------------------------------------------------------------------
