-- NestList Home: the agent's CEA details, shown on condo-family (Home) listings only --
-- posters, the video end slate, the write-up closing and the buyer page. Prestige/landed
-- listings never show them (Jane's decision, 2026-10-20).
--
--   cea_reg_no         e.g. R123456A   (CEA salesperson registration number)
--   agency_name        e.g. ERA Realty Network Pte Ltd
--   agency_licence_no  e.g. L3008899K  (estate agent licence number)
--
-- Run in the Supabase SQL editor. Safe in either order with the deploy, safe to run twice,
-- touches NO existing rows (three nullable text columns).
--
--   * BEFORE the deploy: the columns sit unused.
--   * AFTER the deploy and before this runs: profile saves ignore the CEA fields (PATCH
--     /api/profile answers 503 "can't be saved yet"), condo materials go out without the
--     CEA block and the agent is told their details are missing. Nothing else changes.
--   * The backend checks for the columns once at startup, so after running this, restart
--     (or redeploy) the Railway service to switch storage on.
--
-- PRIVACY: `agents` holds password hashes and tokens; it must stay readable only through
-- the backend (service role). The REVOKEs below re-assert the existing lock in case a
-- column-level grant was ever added by hand.

ALTER TABLE agents
  ADD COLUMN IF NOT EXISTS cea_reg_no text,
  ADD COLUMN IF NOT EXISTS agency_name text,
  ADD COLUMN IF NOT EXISTS agency_licence_no text;

REVOKE ALL ON TABLE agents FROM anon;
REVOKE ALL ON TABLE agents FROM authenticated;
ALTER TABLE agents ENABLE ROW LEVEL SECURITY;

-- Sanity check 1: three rows, all data_type = text.
SELECT column_name, data_type
  FROM information_schema.columns
 WHERE table_name = 'agents'
   AND column_name IN ('cea_reg_no', 'agency_name', 'agency_licence_no');

-- Sanity check 2: must return ZERO rows (no table- or column-level privilege for anon or
-- authenticated on agents).
SELECT grantee, privilege_type, 'table' AS level
  FROM information_schema.role_table_grants
 WHERE table_name = 'agents' AND grantee IN ('anon', 'authenticated')
UNION ALL
SELECT grantee, privilege_type, 'column:' || column_name
  FROM information_schema.column_privileges
 WHERE table_name = 'agents' AND grantee IN ('anon', 'authenticated');

-- Sanity check 3: RLS on (relrowsecurity = true).
SELECT relname, relrowsecurity FROM pg_class WHERE relname = 'agents';
