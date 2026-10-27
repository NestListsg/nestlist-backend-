-- NestList -- the money controls for the ELEGANT video (the one with real AI motion)
-- ============================================================================
-- JANE: THIS IS THE EXPENSIVE ONE. Please read the next twelve lines before running it.
--
-- WHAT THE ELEGANT VIDEO IS. The Classic is a slideshow. The Dramatic is a slideshow
-- with the old furniture flying out, built on our own machine for about 24 US cents of
-- AI pictures a listing. The ELEGANT is different in kind: the old house actually comes
-- down on screen and the new one rises out of it, with no cut, the camera then walks in
-- from the driveway, through the front door and across the living room in one move, and
-- each room comes apart and reassembles -- and all of that motion is bought from an AI
-- video service, by the second.
--
-- WHAT IT COSTS. About US$3.85 for one listing's video -- roughly FIFTEEN TIMES a
-- Dramatic. This file adds the two things that keep that under control: a memory of the
-- clips already bought (so looking at a video twice costs nothing the second time) and a
-- monthly allowance per agent, counted in seconds of video, which is what is billed.
--
-- RUNNING THIS FILE DOES NOT SWITCH THE FEATURE ON. Nothing changes for any agent until
-- the backend also has ELEGANT_VIDEO_ENABLED=1 set in Railway, which is a separate,
-- deliberate step for you to take when you have watched a real Elegant film and are
-- happy with it. Until then the style is not offered in the app, the backend refuses it
-- if asked, and no worker will run one even if a row appears by hand.
--
-- RUN THE OTHER TWO FILES FIRST. The full order is:
--
--   1. old-to-new-images-migration.sql        (the pictures: cache + monthly allowance)
--   2. video-jobs-dramatic-migration.sql      (the render queue accepts the new styles)
--   3. THIS FILE (old-to-new-elegant-migration.sql)
--
-- If 1 and 2 have already been run -- they have, for the Dramatic -- this file is all
-- that is left. Running this one out of order cannot do any harm: the Elegant stays off
-- until all of its conditions are true, and one of them is the columns the first file
-- adds.
--
-- HOW TO RUN IT
-- Supabase dashboard -> SQL Editor -> New query -> paste the whole file -> Run.
-- Safe to run twice. No restart needed for the columns; switching the feature ON later
-- does need a Railway restart, because the backend reads that switch once at startup.
--
-- WHAT HAPPENS IF YOU DO NOT RUN IT
-- Nothing breaks and nothing is lost. The Elegant style simply cannot be switched on:
-- with no memory of the clips every press would buy them again, and with no allowance
-- nothing would be counting the spending, which at US$3.85 a press is the one state this
-- feature must never be in. The backend checks for these columns at startup and sends a
-- Telegram warning if the switch is on and they are missing, and keeps the style off.
-- ============================================================================


-- ----------------------------------------------------------------------------
-- 1. The clip memory, per listing.
--
-- Each entry is one bought clip: the key says which treatment it was made for (the
-- tear-down or the gentle makeover), which shot it is, how many seconds long it is, and
-- a fingerprint of the two pictures it was made from. So a regenerate with the same
-- pictures reuses it and pays nothing, while genuinely changing the photos buys new
-- clips -- which is the honest behaviour in both directions.
--
-- Shape:
--   {"version": 1,
--    "clips": {"c1:tear-down-and-rebuild:teardown:5s:<fingerprint>": "https://.../x.mp4",
--              "c1:tear-down-and-rebuild:walkin:5s:<fingerprint>":   "https://.../y.mp4"}}
--
-- SAFE TO CLEAR, with one consequence worth knowing: clearing it means the next Elegant
-- render of that listing BUYS ITS CLIPS AGAIN, about US$3.85. That is a much more
-- expensive clear than the picture cache next door.
-- ----------------------------------------------------------------------------
alter table public.listings
    add column if not exists old_to_new_clips jsonb;

comment on column public.listings.old_to_new_clips is
    'Where the AI-generated video clips for this listing''s Elegant video are stored, '
    'keyed by treatment, shot, duration and a fingerprint of the two source pictures. '
    'Lets a regenerate reuse them instead of buying them again. Clearing it costs about '
    'US$3.85 on the next Elegant render of this listing.';


-- ----------------------------------------------------------------------------
-- 2. Which treatment this listing's Elegant video used.
--
-- There are two ways to show the transformation and they produce completely different
-- films from the same photographs:
--
--   'tear-down-and-rebuild'  the house comes down and the new one rises, then each room
--                            comes apart and reassembles. The headline effect.
--   'gentle-makeover'        the old furniture is carried out piece by piece and the new
--                            design moves in, camera still. The quieter film, and two
--                            clips cheaper.
--
-- It is remembered here FOR A MONEY REASON, not for tidiness: the clip memory above is
-- keyed on the treatment, because the same room and the same two pictures make a
-- different clip under each one. Without this column a regenerate would quietly fall
-- back to the default treatment and buy a whole second film.
-- ----------------------------------------------------------------------------
alter table public.listings
    add column if not exists old_to_new_treatment text;

comment on column public.listings.old_to_new_treatment is
    'Which Elegant treatment this listing last used: tear-down-and-rebuild or '
    'gentle-makeover. Remembered so a regenerate reuses the clips already bought '
    'instead of buying a different film.';


-- ----------------------------------------------------------------------------
-- 3. The monthly allowance, per agent, in SECONDS OF VIDEO.
--
-- Why seconds and not videos. The AI video service bills per second of generated video
-- (about 7 US cents a second), so seconds are the only unit that can be checked against
-- a bill. One Elegant listing is about 55 seconds -- US$3.85.
--
-- The backend stops generating at 165 seconds an agent a calendar month, which is about
-- US$11.55 an agent, or exactly three Elegant videos. Across fifty agents that is about
-- US$578 a month at the absolute worst -- deliberately the same order as the picture allowance you
-- already agreed to (US$480), so the two together roughly double a figure you know
-- rather than introducing one you do not.
--
-- THIS IS A SAFETY NET AND NOT A PRICE. When you decide what the Elegant tier costs, the
-- number in the backend should be worked out again from that price; it is one constant in
-- one file. And the cap that no bug on our side can outrun is still the spend limit on
-- fal's own billing page -- please keep one set there as well.
--
-- The month is stored beside the count ('2026-10'), so the reset is a comparison rather
-- than a scheduled job: a count from a past month reads as zero.
--
-- SAFE TO CLEAR, with one consequence: zeroing these gives every agent a fresh 165
-- seconds -- about US$11.55 each -- for the current month.
-- ----------------------------------------------------------------------------
alter table public.agents
    add column if not exists old_to_new_video_month text;

alter table public.agents
    add column if not exists old_to_new_video_seconds integer;

comment on column public.agents.old_to_new_video_month is
    'Which calendar month (YYYY-MM) old_to_new_video_seconds is counting. A value from a '
    'past month means this month''s count is zero.';

comment on column public.agents.old_to_new_video_seconds is
    'How many seconds of AI video this agent has had generated in the month named by '
    'old_to_new_video_month. About 7 US cents a second; the backend stops generating at '
    '165 seconds a month (about US$11.55, or three Elegant videos). Counts seconds, not '
    'videos or listings, because seconds are what is billed.';


-- ----------------------------------------------------------------------------
-- 4. Make sure the render queue accepts an Elegant job.
--
-- video-jobs-dramatic-migration.sql already added 'elegant' to the list of allowed job
-- types, so on an up-to-date database this changes nothing at all. It is repeated here
-- so that this file is self-sufficient: if that file was ever run in an older form, the
-- queue would silently refuse every Elegant job and the only symptom would be a Telegram
-- warning.
--
-- 'signature' is kept in the list for the same reason it was kept there: nothing creates
-- such a row any more, a leftover one may still exist, and a rule an existing row breaks
-- cannot be added at all. A job type the backend does not recognise is never picked up,
-- so a leftover row is inert.
-- ----------------------------------------------------------------------------
alter table public.video_jobs
    drop constraint if exists video_jobs_type_check;

alter table public.video_jobs
    add constraint video_jobs_type_check
    check (job_type in ('classic', 'signature', 'dramatic', 'elegant'));


-- ============================================================================
-- AFTERWARDS -- how to tell it worked
--
-- All four new columns should be listed by this:
--
--     select table_name, column_name from information_schema.columns
--      where (table_name = 'listings'
--             and column_name in ('old_to_new_clips', 'old_to_new_treatment'))
--         or (table_name = 'agents'
--             and column_name in ('old_to_new_video_month',
--                                 'old_to_new_video_seconds'));
--
-- And this should list all four job types:
--
--     select pg_get_constraintdef(oid) from pg_constraint
--      where conname = 'video_jobs_type_check';
--
-- THEN, and only then, the feature can be switched on: set ELEGANT_VIDEO_ENABLED=1 in
-- Railway and restart. The backend logs a line at startup saying the Elegant is switched
-- on and what the monthly cap is. If a Telegram warning about the allowance arrives
-- instead, one of the columns above is missing.
--
-- TO SWITCH IT OFF AGAIN: remove ELEGANT_VIDEO_ENABLED (or set it to 0) and restart.
-- Nothing is lost -- the clips already bought stay remembered on their listings, so
-- switching it back on later costs nothing for those listings.
-- ============================================================================
