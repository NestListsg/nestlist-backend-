-- NestList -- let the render queue accept the Dramatic video, and make "one video job
--             per listing" a rule the database enforces
-- ============================================================================
-- WHAT THIS IS FOR
-- Agents can now choose between two video styles, Classic and Dramatic ("From Old to
-- New"). The render queue table was built when there was only one style, and two things
-- in it still assume that. This fixes both.
--
-- RUN THIS BEFORE AGENTS CAN CHOOSE THE DRAMATIC STYLE. Section 1 is the one that
-- matters most: without it, every Dramatic video falls back to the older, less reliable
-- way of rendering (see "WHAT HAPPENS IF YOU DO NOT RUN IT" below).
--
-- HOW TO RUN IT
-- Supabase dashboard -> SQL Editor -> New query -> paste the whole file -> Run.
-- Safe to run twice. No restart needed, and no deploy has to wait for it.
--
-- WHAT HAPPENS IF YOU DO NOT RUN IT
--   * Section 1: the table refuses to accept a Dramatic job at all. Nothing is lost --
--     the backend notices, warns you on Telegram, and renders the video the old way,
--     inside the web request. But that old way is exactly what the queue was built to
--     replace: those renders die on every redeploy, do not queue politely when several
--     agents generate at once, and show no position in line.
--   * Section 2: an agent who presses Generate on one style and then switches to the
--     other within the same moment can, rarely, start two renders of one listing. Both
--     write to the same video file and the listing can end up labelled with the style it
--     did not get. The backend checks for this before it writes a job, so this is the
--     last narrow gap rather than the main defence -- but the gap is about a tenth of a
--     second wide, and switching style mid-render is a normal thing to do.
-- ============================================================================


-- ============================================================================
-- SECTION 1 -- allow the new job type
--
-- The table was created with a rule allowing only 'classic' and 'signature' (a tier that
-- was withdrawn on 2026-10-21). 'dramatic' has to be added to that list before a
-- Dramatic render can be queued.
--
-- 'signature' is KEPT in the list. Nothing creates such a row any more, but a leftover
-- one may still exist, and a rule that an existing row breaks cannot be added at all.
-- A job type the backend does not recognise is never picked up, so a leftover row is
-- inert. 'elegant' is added now as well: it is the next style, not built yet, and
-- nothing can create one (the backend has no renderer for it) -- having the slot ready
-- means this file does not have to be run a second time later.
-- ============================================================================

alter table public.video_jobs
    drop constraint if exists video_jobs_type_check;

alter table public.video_jobs
    add constraint video_jobs_type_check
    check (job_type in ('classic', 'signature', 'dramatic', 'elegant'));


-- ============================================================================
-- SECTION 2 -- one video job per listing, whatever its style
--
-- The old rule was "one active job per listing PER STYLE", which was exactly right while
-- there was one style. With two, an agent who starts a Classic and then switches to
-- Dramatic has two jobs that are each perfectly legal by that rule -- and they race to
-- write the same videos/<listing id>.mp4. Whichever finishes last wins the file while
-- the listing row may record the other one, so the agent is shown the wrong style with
-- no error anywhere.
--
-- The new rule is "one active job per listing, full stop". A second press is then
-- refused by the database rather than by a check in the app, so it holds no matter how
-- two requests interleave or which two workers they land on. The backend already treats
-- that refusal as "you already have a render going, here is where it has got to" rather
-- than as an error, which is the behaviour agents see today.
--
-- Finished jobs are not covered by either rule (that is what the WHERE clause is for),
-- so regenerating a video still works exactly as before.
--
-- NOTE ON ORDER: the new index is created BEFORE the old one is dropped, so there is no
-- instant with no rule in force. If the CREATE fails because two active jobs for one
-- listing already exist, the old index is still there and nothing has been weakened --
-- clear the duplicate (the newer of the two) and run the file again.
-- ============================================================================

create unique index if not exists video_jobs_one_active_per_listing
    on public.video_jobs (listing_id)
    where status in ('queued', 'running');

drop index if exists public.video_jobs_one_active_per_listing_type;


-- ============================================================================
-- AFTERWARDS -- how to tell it worked
--
-- This should return the one new index and no rows for the old one:
--
--     select indexname from pg_indexes
--      where tablename = 'video_jobs'
--        and indexname like 'video_jobs_one_active_per_listing%';
--
-- And this should list all four job types:
--
--     select pg_get_constraintdef(oid) from pg_constraint
--      where conname = 'video_jobs_type_check';
--
-- Then generate a Dramatic video. If the Telegram warning about the render queue does
-- not arrive, the job was queued properly.
-- ============================================================================
