-- NestList -- the money controls for Old-to-New videos: the picture cache and the
--             per-agent monthly allowance
-- ============================================================================
-- RUN THIS BEFORE ANY AGENT MAKES AN OLD-TO-NEW VIDEO. It is the only thing that stops
-- the same pictures being paid for over and over, and the only thing that counts an
-- agent's monthly allowance. An earlier version of this file said it was "not a
-- blocker"; on the money that was wrong, and this is the corrected version.
--
-- WHAT THIS IS FOR
-- The "From Old to New" (Dramatic) video needs a second picture of each room: the room
-- redesigned, from the same camera position. We make those with AI from the agent's own
-- photos, and each one costs real money (about 4 US cents, roughly 24 cents for a whole
-- listing). This column remembers where they were stored, so an agent who regenerates
-- their video -- different music, different photo order, a second look -- does not pay
-- for the same pictures again, and does not wait for them again either.
--
-- Shape:
--   {"version": 1, "images": {"v1:room_new:<hash of the photo>": "https://.../x.jpg"}}
-- The key carries a prompt version, so if we deliberately improve the wording the next
-- render makes fresh pictures; an incidental change does not.
--
-- HOW TO RUN IT
-- Supabase dashboard -> SQL Editor -> New query -> paste -> Run. Safe to run twice.
-- No restart needed.
--
-- BEFORE running it, nothing CRASHES -- but two cost controls are simply off, and both
-- of them cost real money:
--
--   1. NO CACHE. The backend notices the column is absent and does not cache, so every
--      regeneration makes the pictures again and pays again. Measured: five presses of
--      the same unchanged listing cost US$1.20 instead of US$0.24.
--   2. NO MONTHLY ALLOWANCE. The per-agent ceiling (240 pictures a month, about
--      US$9.60) is counted in the two agents columns below. Without them nothing is
--      counted, so nothing but fal's own billing spend limit caps what a bug or an
--      enthusiastic agent can spend. The backend sends a Telegram warning on startup
--      when the pictures are switched on and these columns are missing.
--
-- So: videos work either way, and this is still a blocker for opening the feature to
-- agents. (The earlier wording here said it was not. On the reliability axis it was
-- true; on the money axis it was not, and money is what this file is about.)
-- ============================================================================

-- ----------------------------------------------------------------------------
-- 1. The picture cache, per listing.
-- ----------------------------------------------------------------------------
alter table public.listings
    add column if not exists old_to_new_images jsonb;

comment on column public.listings.old_to_new_images is
    'Where the AI-generated "after" photos for this listing''s Old-to-New video are '
    'stored, keyed by prompt version plus a hash of the source photo. Lets a regenerate '
    'reuse them instead of paying again. Safe to clear: the next render regenerates.';


-- ----------------------------------------------------------------------------
-- 2. The monthly allowance, per agent.
--
-- WHY IT LIVES HERE AND NOT IN THE JOBS TABLE. It used to be counted by reading rows out
-- of video_jobs, which was wrong twice over. Finished job rows are DELETED after seven
-- days, so from the 8th of the month the count could only see a week and the allowance
-- reset itself about weekly. And job rows count listings, where the bill is per picture
-- per press -- re-picking which photo leads a video costs three more pictures on the
-- same listing. A running total on the agent, in pictures, has neither problem, and it
-- also counts renders made on the backend's fallback path, which writes no job row.
--
-- The month is stored beside the count ('2026-10'), so the reset is a comparison rather
-- than a scheduled job: a count from last month reads as zero.
--
-- SAFE TO CLEAR, with one consequence: zeroing these gives every agent a fresh
-- allowance for the current month.
-- ----------------------------------------------------------------------------
alter table public.agents
    add column if not exists old_to_new_month text;

alter table public.agents
    add column if not exists old_to_new_images_used integer;

comment on column public.agents.old_to_new_month is
    'Which calendar month (YYYY-MM) old_to_new_images_used is counting. A value from a '
    'past month means this month''s count is zero.';

comment on column public.agents.old_to_new_images_used is
    'How many AI "after" pictures this agent has had generated in the month named by '
    'old_to_new_month. About 4 US cents each; the backend stops generating at 240 a '
    'month (about US$9.60). Counts pictures, not videos or listings, because that is '
    'what is billed.';
