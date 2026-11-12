-- The "Potential" poster style: an AI makeover concept of the property shown beside the
-- real photograph, labelled BEFORE, under a disclosure band.
--
-- It is the only poster style that spends money: one generated image per listing photo,
-- about 4 US cents, from fal.ai. This migration adds the two things that make that safe
-- to switch on -- somewhere to COUNT what has been spent, and somewhere to REMEMBER a
-- concept so a second press of Generate is free.
--
-- Run this in the Supabase SQL editor. Safe in either order with the deploy, safe to run
-- twice, and it touches NO existing rows -- it only adds three nullable columns.
--
--   * BEFORE the deploy: the columns sit unused. Nothing reads or writes them.
--   * AFTER the deploy and before this runs: the Potential style is OFFERED TO NOBODY.
--     It does not appear in the poster picker, and asking for it directly answers 400
--     "The Potential style is not switched on for this account yet." The other ten
--     poster styles are completely unaffected. The backend checks for these columns once
--     at startup, so after running this, redeploy (or restart) the Railway service.
--   * The style ALSO needs POTENTIAL_POSTER_ENABLED=1 in the Railway variables and a
--     FAL_KEY. Running this migration on its own switches nothing on -- which is
--     deliberate: nobody has looked at a real generated makeover yet.
--
-- WHY ITS OWN COUNTER RATHER THAN SHARING old_to_new_images_used. Both count the same
-- 4-cent fal picture, so sharing would work. It is kept separate because a poster press
-- would otherwise quietly eat an agent's VIDEO allowance, and an agent who could not
-- make a video because of posters they made last week would have no way to discover that
-- from anything the product shows them.
--
-- PRIVACY: listings.potential_images holds public storage URLs of generated images for a
-- listing, and the keys are hashes of the source photo URL. `listings` is already locked
-- (RLS on, anon/authenticated have zero privileges); a new column inherits the table's
-- privileges, but the REVOKEs below re-assert that explicitly in case a column-level
-- grant was ever added by hand. The same for `agents`.


-- ----------------------------------------------------------------------------
-- 1. The concept cache, per listing.
--
-- Keyed by prompt version plus a hash of the source photo, so re-rendering the poster --
-- a different accent colour, a reworded headline, a typo in the price -- reuses the
-- picture instead of buying it again. This is the cost control that matters most in
-- everyday use: without it, every press of Generate costs another 4 cents.
--
-- CLEAR THE WHOLE COLUMN, NEVER PART OF IT. Setting potential_images to null is safe:
-- the next Potential render pays four cents again and the agent reviews the new concept
-- as usual. But this value holds BOTH the generated pictures and the list of which ones
-- a human has approved, and the approvals are keyed on the source photo. So emptying
-- 'images' by hand while leaving 'approved' in place would let a REGENERATED picture --
-- one nobody has looked at -- inherit yesterday's yes and go straight onto a published
-- advert. No code does this; it is reachable only with a SQL editor. Null the column.
-- ----------------------------------------------------------------------------
alter table public.listings
    add column if not exists potential_images jsonb;

comment on column public.listings.potential_images is
    'Where the AI "makeover concept" images for this listing''s Potential poster are '
    'stored, keyed by prompt version plus a hash of the source photo. Lets a re-render '
    'reuse them instead of paying again. Safe to clear: the next render regenerates.';


-- ----------------------------------------------------------------------------
-- 2. The monthly allowance, per agent.
--
-- Same shape as old_to_new_month / old_to_new_images_used, and for the same reasons: the
-- total lives on the agent row because nothing prunes it, and it counts PICTURES because
-- that is what is billed -- not posters and not listings, since re-picking which photo
-- leads a poster buys another picture on the same listing.
--
-- The month is stored beside the count ('2026-11'), so the reset is a comparison rather
-- than a scheduled job: a count from last month reads as zero.
--
-- SAFE TO CLEAR, with one consequence: zeroing these gives every agent a fresh
-- allowance for the current month.
-- ----------------------------------------------------------------------------
alter table public.agents
    add column if not exists potential_makeovers_month text;

alter table public.agents
    add column if not exists potential_makeovers_used integer;

comment on column public.agents.potential_makeovers_month is
    'Which calendar month (YYYY-MM) potential_makeovers_used is counting. A value from a '
    'past month means this month''s count is zero.';

comment on column public.agents.potential_makeovers_used is
    'How many AI makeover concept images this agent has had generated in the month named '
    'by potential_makeovers_month. About 4 US cents each; the backend stops generating at '
    '20 a month (about US$0.80). Counts what fal CHARGED for, which includes pictures '
    'that were paid for and never arrived, because that is what is on the bill.';


-- ----------------------------------------------------------------------------
-- 3. Re-assert that these tables stay backend-only.
-- ----------------------------------------------------------------------------
revoke all on table public.listings from anon;
revoke all on table public.listings from authenticated;
alter table public.listings enable row level security;

revoke all on table public.agents from anon;
revoke all on table public.agents from authenticated;
alter table public.agents enable row level security;


-- ----------------------------------------------------------------------------
-- Sanity checks. Run these after the statements above.
-- ----------------------------------------------------------------------------

-- Check 1: three rows -- potential_images (jsonb), potential_makeovers_month (text),
-- potential_makeovers_used (integer).
select table_name, column_name, data_type
from information_schema.columns
where table_schema = 'public'
  and (
    (table_name = 'listings' and column_name = 'potential_images')
    or (table_name = 'agents' and column_name in ('potential_makeovers_month',
                                                  'potential_makeovers_used'))
  )
order by table_name, column_name;

-- Check 2: no existing rows were touched. Run BEFORE the feature is used: month_set and
-- used_set should both be 0. (After agents start making concepts these climb, which is
-- correct -- this check only means something on the day the migration runs.)
select count(*) as agents_total,
       count(potential_makeovers_month) as month_set,
       count(potential_makeovers_used) as used_set
from public.agents;

-- Check 3: the REVOKEs above actually hold. THIS IS THE ONE THAT MATTERS, and it is here
-- because of this project's own history on these exact two tables: a policy of
-- `USING true` once made "RLS enabled" mean nothing at all, and the anon key was public.
-- Asserting the grants without checking them would repeat that mistake in a quieter way.
-- EXPECTED: zero rows. Any row is a way in for the anon or signed-in browser key.
select grantee, table_name, privilege_type
from information_schema.role_table_grants
where table_schema = 'public'
  and table_name in ('agents', 'listings')
  and grantee in ('anon', 'authenticated')
order by table_name, grantee, privilege_type;

-- Check 4: RLS is on AND forced, and no permissive policy quietly re-opens either table.
-- EXPECTED: two rows, both with rowsecurity = true.
select relname as table_name,
       relrowsecurity as rowsecurity,
       relforcerowsecurity as forced
from pg_class
where oid in ('public.agents'::regclass, 'public.listings'::regclass);

-- EXPECTED: zero rows. A policy here is only safe if you can say exactly who it lets in
-- and why -- read the `qual` column, and treat `true` as a finding, not a default.
select tablename, policyname, roles, cmd, qual, with_check
from pg_policies
where schemaname = 'public'
  and tablename in ('agents', 'listings')
order by tablename, policyname;
