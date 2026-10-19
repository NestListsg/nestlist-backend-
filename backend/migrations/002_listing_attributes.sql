-- NestList Home, Phase 1 (condo family): one optional `attributes` jsonb column on
-- `listings` for the condo fields -- project_name, tenure, tenure_detail, floor_level,
-- top_year, lease_start_year. One flexible column instead of six, so Phase 2 (HDB) can add
-- its own keys without another migration.
--
-- Run this in the Supabase SQL editor. Safe in either order with the deploy, safe to run
-- twice, and it touches NO existing rows -- it only adds a nullable column.
--
--   * BEFORE the deploy: the column sits unused.
--   * AFTER the deploy and before this runs: condo listings still save and still get a
--     condo-aware write-up; only the condo fields themselves are not stored yet (the
--     listing response says attributes_saved: false). Landed listings are unaffected.
--   * The backend checks for the column once at startup, so after running this, restart
--     (or redeploy) the Railway service to switch storage on.
--
-- PRIVACY: this column holds the condo PROJECT NAME, which pins a listing's location the
-- way a street name does. It must stay readable only through the backend (service role).
-- `listings` is already locked (RLS on, anon/authenticated have zero privileges); a new
-- column inherits the table's privileges, but the REVOKEs below re-assert that explicitly
-- in case a column-level grant was ever added by hand.

ALTER TABLE listings
  ADD COLUMN IF NOT EXISTS attributes jsonb;

REVOKE ALL ON TABLE listings FROM anon;
REVOKE ALL ON TABLE listings FROM authenticated;
ALTER TABLE listings ENABLE ROW LEVEL SECURITY;

-- Sanity check 1: one row, data_type = jsonb.
SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name = 'listings'
   AND column_name = 'attributes';

-- Sanity check 2: must return ZERO rows (no table- or column-level privilege for anon or
-- authenticated on listings).
SELECT grantee, privilege_type, 'table' AS level
  FROM information_schema.role_table_grants
 WHERE table_name = 'listings' AND grantee IN ('anon', 'authenticated')
UNION ALL
SELECT grantee, privilege_type, 'column:' || column_name
  FROM information_schema.column_privileges
 WHERE table_name = 'listings' AND grantee IN ('anon', 'authenticated');
