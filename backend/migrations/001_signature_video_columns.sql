-- Signature video tier: its own columns on `listings`, mirroring the Classic's.
--
-- Run this in the Supabase SQL editor. Safe in either order with the deploy, safe to
-- run twice, and it touches NO existing rows -- it only adds columns.
--
--   * BEFORE the deploy: the columns sit unused. Nothing reads or writes them.
--   * AFTER the deploy and before this runs: POST /generate-signature answers 503
--     "Signature videos are not switched on yet.", GET /signature-status answers
--     status "none", `signature_available` is false so the button is never offered,
--     and the Classic tier is untouched. Nothing fails at import or at startup.
--
-- `signature_video_url` ALREADY EXISTS in production and is already populated: it holds
-- the hand-made Signature that the demo buyer page serves today, and it predates this
-- tier. That is why every line below is ADD COLUMN IF NOT EXISTS (so this does not fail
-- on the column that is already there), and why the backend decides whether the tier is
-- on by looking for `signature_video_status`, not for the url.
--
-- The backend notices the new columns from the keys of any listing row it reads, so the
-- tier switches itself on within seconds of this running -- no redeploy, no restart.
--
-- Why separate columns at all: a Signature must never overwrite a Classic. The agent
-- may already have posted the Classic, and the two films are different deliverables.
-- The finished mp4s are likewise kept apart: videos/{id}.mp4 for the Classic,
-- videos/signature/{id}.mp4 for the Signature. Neither path can collide with the
-- hand-made film at videos/signature_silent_v40.mp4, and nothing in this change
-- deletes or rewrites that object.
--
-- NOTE FOR JANE, which is a decision and not a migration step: on the one listing that
-- already has a hand-made Signature, the first automated Signature render will replace
-- what `signature_video_url` points at, so the buyer page would start serving the new
-- film instead of the approved one. The old file stays in storage and can be pointed
-- back to, but the swap would happen the moment someone presses the button.
--
-- `video_captions` is deliberately NOT duplicated: both tiers caption the same photos,
-- so a caption the agent corrects should hold for both.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS signature_video_url               text,
  ADD COLUMN IF NOT EXISTS signature_video_status            text,
  ADD COLUMN IF NOT EXISTS signature_video_error             text,
  ADD COLUMN IF NOT EXISTS signature_video_render_started_at timestamptz,
  ADD COLUMN IF NOT EXISTS signature_video_degradations      jsonb;

-- Sanity check: five rows, all five columns.
SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name = 'listings'
   AND column_name LIKE 'signature_video%'
 ORDER BY column_name;
