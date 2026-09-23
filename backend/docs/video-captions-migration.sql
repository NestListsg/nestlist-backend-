-- NestList -- agent-editable video captions
-- ============================================================================
-- WHAT THIS IS FOR
-- The room captions burned into a Classic video are written by a vision model from
-- the photos alone. It can be wrong in a way only someone who has stood in the
-- property can catch: calling a room "the kitchen" with no worktop in frame, or
-- "the dining hall" when the table is a work desk. A caption is a CLAIM about the
-- property, so a wrong one is the agent's problem, not a cosmetic one.
--
-- This column stores what the video actually says, so the agent can read it back and
-- correct a line. Shape: {"<photo url>": "the terrace ... where golden hours drift by"}
--
-- Keyed on the photo's URL rather than its position on purpose. An agent who
-- reorders or deletes a photo must never silently inherit another photo's caption.
--
-- HOW TO RUN IT
-- Supabase dashboard -> SQL Editor -> New query -> paste -> Run. Safe to run twice.
-- No restart needed: nothing caches this.
--
-- BEFORE the deploy: harmless, the column simply sits empty.
-- AFTER the deploy: also harmless -- an absent column reads as "no overrides", so
-- videos keep rendering with model-written captions until the column exists.
-- ============================================================================

alter table public.listings
    add column if not exists video_captions jsonb;

comment on column public.listings.video_captions is
    'Room captions for the Classic video, keyed by photo URL. Written by the renderer '
    'after a successful render; an agent may overwrite any entry via '
    'PATCH /api/listings/{id}/captions. Agent edits win on the next render.';
