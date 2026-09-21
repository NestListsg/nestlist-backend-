-- NestList -- durable video render queue
-- ============================================================================
-- WHAT THIS IS FOR
-- Today a video render happens inside whichever web worker received the request, and
-- lives only in that worker's memory. Every redeploy kills renders that are half done,
-- and when several agents generate at once some of them are told "the video service is
-- busy" instead of being queued. This table turns a render into a durable row: the
-- request writes the row and returns immediately, any worker with spare capacity picks
-- it up, and a redeploy mid-render just means another worker finishes the job.
--
-- HOW TO RUN IT
-- Supabase dashboard -> SQL Editor -> New query -> paste ALL of section 1 -> Run.
-- It is safe to run more than once (every statement is IF NOT EXISTS).
--
-- ORDER DOES NOT MATTER. The backend probes for this table at startup. Until it exists,
-- video generation keeps working exactly as it does today, on the old in-process path.
-- So you can run this before or after the deploy, and nothing breaks either way.
-- ============================================================================


-- ============================================================================
-- SECTION 1 -- run this
-- ============================================================================

create table if not exists public.video_jobs (
    id                uuid primary key default gen_random_uuid(),

    -- Which kind of video. 'classic' is what every agent generates today.
    -- 'signature' is defined now so the avatar pipeline can be plugged in later
    -- without another migration; nothing creates one yet.
    job_type          text        not null default 'classic',

    -- Stored as text rather than uuid, and with no foreign key, on purpose: it keeps
    -- this migration safe to paste whatever type listings.id happens to be, and it
    -- means deleting a listing can never fail because a finished job row still points
    -- at it. Orphaned rows are cleaned up by the backend's own pruning (see below).
    listing_id        text        not null,
    agent_id          text        not null,

    status            text        not null default 'queued',

    -- Render OPTIONS ONLY (template id, photo index). Never the agent record: that
    -- carries Facebook and Instagram access tokens and they must not be copied here.
    payload           jsonb       not null default '{}'::jsonb,

    attempts          integer     not null default 0,
    max_attempts      integer     not null default 3,

    -- Which worker process holds this job, and when it last proved it was alive.
    -- A job whose heartbeat goes quiet is handed to another worker -- this is what
    -- makes a redeploy mid-render recoverable instead of fatal.
    claimed_by        text,
    claimed_at        timestamptz,
    heartbeat_at      timestamptz,

    -- Not runnable before this moment. Used for retry backoff, and for politely
    -- deferring a job when there is no capacity right now.
    run_after         timestamptz not null default now(),

    created_at        timestamptz not null default now(),
    updated_at        timestamptz not null default now(),
    started_at        timestamptz,
    finished_at       timestamptz,

    -- Feeds the "about N minutes" estimate agents see while they wait.
    duration_seconds  double precision,

    result_url        text,
    degradations      jsonb,
    error             text,

    constraint video_jobs_status_check
        check (status in ('queued', 'running', 'done', 'failed')),
    constraint video_jobs_type_check
        check (job_type in ('classic', 'signature'))
);


-- THE IMPORTANT ONE. This is what stops a double-clicked Generate button from
-- producing two renders of the same listing. It is a rule in the database, not a check
-- in the app, so it holds no matter how two requests interleave or which two workers
-- they land on. Once a job finishes, a new one can be created -- regenerating a video
-- still works exactly as before.
create unique index if not exists video_jobs_one_active_per_listing_type
    on public.video_jobs (listing_id, job_type)
    where status in ('queued', 'running');


-- Lets a worker find the next job to run without scanning the table.
create index if not exists video_jobs_claimable
    on public.video_jobs (job_type, status, run_after, created_at);

-- Lets the stale-job sweeper find abandoned renders cheaply.
create index if not exists video_jobs_heartbeat
    on public.video_jobs (status, heartbeat_at)
    where status = 'running';

-- Backs the "how long do these usually take" estimate and the cleanup of old rows.
create index if not exists video_jobs_finished
    on public.video_jobs (job_type, finished_at desc);

-- Backs the My Listings screen asking "what is this agent waiting on right now".
create index if not exists video_jobs_agent_active
    on public.video_jobs (agent_id, status);


-- Lock the table down. The backend connects with the service key, which bypasses row
-- level security, so it keeps full access. Turning RLS on with no policies means the
-- public anon key -- the one the browser holds -- gets nothing at all. Agents never
-- talk to this table directly; they only ever see it through the API.
alter table public.video_jobs enable row level security;


-- ============================================================================
-- SECTION 2 -- checks you can run afterwards (optional, read-only)
-- ============================================================================

-- Confirm the table and, more importantly, the one-active-job rule are in place.
-- Expect one row back, named video_jobs_one_active_per_listing_type.
--
--   select indexname from pg_indexes
--   where tablename = 'video_jobs'
--     and indexname = 'video_jobs_one_active_per_listing_type';

-- Prove the double-click rule actually bites. This inserts two jobs for a fake
-- listing; the SECOND one must fail with "duplicate key value violates unique
-- constraint". That error is the test passing. The rollback cleans up either way.
--
--   begin;
--     insert into public.video_jobs (job_type, listing_id, agent_id)
--     values ('classic', 'migration-self-test', 'migration-self-test');
--     insert into public.video_jobs (job_type, listing_id, agent_id)
--     values ('classic', 'migration-self-test', 'migration-self-test');
--   rollback;

-- What is in the queue right now, newest first.
--
--   select job_type, status, attempts, claimed_by, created_at, duration_seconds, error
--   from public.video_jobs
--   order by created_at desc
--   limit 50;


-- ============================================================================
-- SECTION 2b -- proving two workers cannot claim the same job (for the audit)
-- ============================================================================
-- The whole design rests on one property: when two workers run the same conditional
-- UPDATE against one row, exactly one of them updates a row and the other updates none.
-- Postgres guarantees this at READ COMMITTED -- the second updater waits on the first's
-- row lock and then RE-EVALUATES its WHERE clause against the committed new version of
-- the row, which no longer says status='queued'. That is documented behaviour, but it
-- is worth confirming on THIS database rather than taking on trust.
--
-- (A) TWO-SESSION PROOF. Needs two genuinely concurrent sessions holding open
-- transactions, so it needs psql against the Supabase connection string -- the
-- dashboard SQL editor commits each run and cannot hold a transaction open between
-- them. Run this if you have psql to hand; skip to (B) if not.
--
--   -- setup, either session:
--   insert into public.video_jobs (id, job_type, listing_id, agent_id, status)
--   values ('00000000-0000-0000-0000-0000000000aa', 'classic',
--           'atomicity-test', 'atomicity-test', 'queued');
--
--   -- session A:
--   begin;
--   update public.video_jobs set status = 'running', claimed_by = 'worker-A',
--          attempts = attempts + 1
--    where id = '00000000-0000-0000-0000-0000000000aa'
--      and status = 'queued' and attempts = 0;
--   -- reports UPDATE 1. Leave the transaction OPEN.
--
--   -- session B, now, while A is still open:
--   update public.video_jobs set status = 'running', claimed_by = 'worker-B',
--          attempts = attempts + 1
--    where id = '00000000-0000-0000-0000-0000000000aa'
--      and status = 'queued' and attempts = 0;
--   -- this BLOCKS, which is the point.
--
--   -- session A:
--   commit;
--   -- session B then returns UPDATE 0. Zero rows is the proof: B re-checked the
--   -- predicate after A committed, saw status='running', and did nothing.
--
--   -- cleanup:
--   delete from public.video_jobs where listing_id = 'atomicity-test';
--
-- (B) LIVE PROOF, no psql needed. After deploying, have several agents (or several
-- browser tabs) press Generate at once on DIFFERENT listings, then run:
--
--   select listing_id, count(*) as job_rows, max(attempts) as claims
--   from public.video_jobs
--   where created_at > now() - interval '15 minutes'
--   group by listing_id
--   having count(*) > 1 or max(attempts) > 1;
--
-- An empty result is a pass: one row per listing, claimed once each. Any row returned
-- means either a listing was queued twice (the unique index failed) or a job was
-- claimed more than once (it was retried or reclaimed -- check `error` and the Railway
-- logs to see which). Cross-check against Railway: there must be exactly one
-- "video rendered:" log line per listing.


-- ============================================================================
-- SECTION 3 -- rollback, if this ever needs undoing
-- ============================================================================
-- Dropping the table is safe at any time. The backend's startup probe will find it
-- missing and quietly go back to rendering videos the old way, in-process. Do this
-- while nothing is rendering, or the jobs in flight are simply lost (which is exactly
-- what happens on the old path anyway).
--
--   drop table if exists public.video_jobs;
