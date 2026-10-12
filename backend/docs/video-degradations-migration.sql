-- NestList -- tell the agent when their video did not use exactly what they asked for
-- ============================================================================
-- WHAT THIS IS FOR
-- When an agent picks which photos go in their video and one of them has since been
-- deleted (or cannot be read), the renderer falls back to the automatic selection so the
-- agent still gets a video. This column keeps the plain-language note about that, so the
-- app can show it next to the finished video instead of the agent assuming the video
-- shows exactly their pick.
--
-- Shape: a JSON array of short sentences, e.g.
--   ["the photos you picked are no longer on this listing, so the automatic photo selection was used"]
-- An empty array means a clean render.
--
-- HOW TO RUN IT
-- Supabase dashboard -> SQL Editor -> New query -> paste -> Run. Safe to run twice.
-- No restart needed.
--
-- BEFORE running it: nothing breaks. With the video queue on (the normal state) the
-- notes are still shown, read from the finished render job. This column adds a second,
-- durable copy and is the only copy the old fallback renderer (used only if the
-- video_jobs table is unavailable) can have.
-- ============================================================================

alter table public.listings
    add column if not exists video_degradations jsonb;

comment on column public.listings.video_degradations is
    'Plain-language notes from the render that produced video_url (what it could not do '
    'exactly as asked, e.g. the agent''s photo pick not honoured). [] = clean render. '
    'Rewritten on every render.';
