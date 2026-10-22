-- NestList -- remember the "after" photos an Old-to-New video has already paid for
-- ============================================================================
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
-- BEFORE running it: nothing breaks and nothing fails. Old-to-New videos still render
-- correctly -- the backend notices the column is absent and simply does not cache, which
-- means each regeneration generates the pictures again and costs again. So this is worth
-- running before the feature is opened to agents, but it is not a blocker for deploying
-- the code.
-- ============================================================================

alter table public.listings
    add column if not exists old_to_new_images jsonb;

comment on column public.listings.old_to_new_images is
    'Where the AI-generated "after" photos for this listing''s Old-to-New video are '
    'stored, keyed by prompt version plus a hash of the source photo. Lets a regenerate '
    'reuse them instead of paying again. Safe to clear: the next render regenerates.';
