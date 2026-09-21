"""Durable, database-backed job queue for listing video renders.

WHY THIS EXISTS
Before this module, a render ran inside whichever uvicorn worker happened to receive
the HTTP request:

  * uvicorn picks the worker by accept()-race, not by who is idle. Three workers could
    sit empty while four agents queued behind the fourth.
  * video_renderer's slot semaphore is a threading.Semaphore -- PER PROCESS. With
    --workers 4 the real fleet ceiling was 4, and which worker you landed on decided
    whether you waited or ran.
  * Past a 240s wait the render RAISED and the agent was told "the video service is
    busy, try again in a few minutes". Busyness was a user-visible failure.
  * The job lived in a module-level set of asyncio tasks. Every Railway deploy killed
    every in-flight render, and we deploy to main routinely. The agent's listing sat at
    'rendering' until a 10-minute stale lock let them retry.

The fix is to stop treating "who received the request" as "who does the work". The
endpoint now writes a ROW and returns. Any worker with spare capacity claims that row.
The row is in Postgres, so a redeploy loses nothing: the new process finds the job still
marked running with a dead heartbeat and picks it up again.

THE CLAIM: HOW TWO WORKERS CANNOT TAKE THE SAME JOB
We reach Postgres through PostgREST (supabase-py), which cannot express
`SELECT ... FOR UPDATE SKIP LOCKED`. It can express a conditional UPDATE, and that is
enough, because of how Postgres executes one:

    PATCH /video_jobs?id=eq.<id>&status=eq.queued&attempts=eq.<n>

becomes exactly one statement, in its own transaction:

    UPDATE video_jobs SET status='running', claimed_by=..., attempts=<n>+1
     WHERE id=<id> AND status='queued' AND attempts=<n> RETURNING *;

Under READ COMMITTED (Postgres' default, and Supabase's), when two transactions target
the same row the second one BLOCKS on the first's row lock, and when the first commits
the second RE-EVALUATES its WHERE clause against the newly committed version of the row
before acting (Postgres docs, "Transaction Isolation" -- Read Committed). The first
claimer has by then set status='running', so the second's `status=eq.queued` predicate
no longer holds and it updates ZERO rows. Zero rows updated IS "someone beat me to it".
There is no window between the check and the write, because they are the same statement.

Two belts beyond that brace, because the cost of being wrong here is a double render:

  1. attempts is part of the predicate as well as the assignment, so the claim is a true
     compare-and-swap on (status, attempts) rather than on status alone. A job that was
     requeued and reclaimed between our SELECT and our UPDATE fails the predicate.
  2. We do not trust the returned row count alone (PostgREST only returns rows when
     `Prefer: return=representation` is in play). After the UPDATE we READ THE ROW BACK
     and confirm claimed_by is us. Ownership is then a fact we observed, not a header
     behaviour we assumed.

If the database is ever moved to REPEATABLE READ, the second updater gets a
serialization error instead of zero rows. That is also safe -- we catch it and treat it
as a lost race -- so the claim is correct under either isolation level.

STALE CLAIMS, WITHOUT DOUBLE-RENDERING
A claimed job heartbeats every HEARTBEAT_SECONDS from its own thread (the render itself
is mostly ffmpeg subprocess time, so the beat keeps ticking). A job whose heartbeat is
older than STALE_AFTER_SECONDS is assumed abandoned and CAS'd back to 'queued'.

The danger is the worker that was not dead, only stalled: it wakes up after the sweeper
has handed its job to someone else and writes a result. Every terminal write is FENCED
on (claimed_by = me AND attempts = the attempt number I claimed). A reclaim bumps
attempts, so the stalled worker's completion matches zero rows, is logged as superseded,
and is discarded. The worst outcome is wasted CPU, never a corrupted row -- and even the
wasted work is harmless, because the video upload targets a fixed path per listing and
both renders would write the same bytes to the same place.

CONCURRENCY IS PER JOB TYPE, NOT ONE GLOBAL NUMBER
Classic is CPU/memory bound: one render peaks around 800MB RSS, so the limit is
per-process and stays at 1 (4 across the fleet), exactly the number video_renderer's
comment was measured for. Signature will be bound by something else entirely --
Replicate allows ONE prediction at a time on the whole account, regardless of how many
processes we run -- so it carries an ACCOUNT-WIDE limit of 1 that is checked against the
database, not against a local counter. Both live in JOB_TYPES; adding a third type means
adding a row there, not re-architecting.

BUSYNESS IS NEVER A FAILURE
A worker that claims a job and then finds it cannot run it (no local render slot, or the
account-wide limit is already taken) RELEASES the job back to 'queued' and restores its
attempts counter, so a deferral never burns a retry. Only genuine errors -- unreadable
photos, ffmpeg crashing, a listing that no longer has photos -- can fail a job, and
those still travel through video_renderer's degradation ladder untouched.

WHY POLLING
PostgREST gives us no way to be woken (no LISTEN/NOTIFY over REST). Workers poll. The
interval is deliberately short -- POLL_SECONDS below -- because a Classic render is ~50s,
so up to two seconds of pickup latency is under 4% of the wait and invisible to an agent,
while four workers at roughly one query every two seconds is negligible load on
PostgREST. The interval is jittered so four workers restarting together after a deploy do
not synchronise into a thundering herd.

NO CREDENTIALS IN JOB ROWS
The payload column carries render OPTIONS only -- template id, photo index. The agent is
referenced by id and re-read at run time. The agent record contains Facebook and
Instagram page tokens, and those must never be copied into a queue table.
"""
import asyncio
import logging
import os
import random
import socket
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

TABLE = "video_jobs"

JOB_TYPE_CLASSIC = "classic"
JOB_TYPE_SIGNATURE = "signature"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)


class JobTypeConfig:
    """Everything that differs between one kind of render and another.

    per_process_limit  -- how many of this type one uvicorn worker may run at once.
                          The right lever when the constraint is local (CPU, RSS).
    account_limit      -- how many may run across the ENTIRE fleet at once, enforced
                          against the database. None means "no fleet-wide cap, the
                          per-process limit times the worker count is the ceiling".
                          The right lever when the constraint is an external service.
    default_seconds    -- ETA fallback before we have measured any real durations.
    max_attempts       -- how many times a job may be CLAIMED before we give up on it.
                          Deferrals do not count; crashes and genuine errors do.
    """

    def __init__(self, per_process_limit, account_limit, default_seconds, max_attempts=3):
        self.per_process_limit = per_process_limit
        self.account_limit = account_limit
        self.default_seconds = default_seconds
        self.max_attempts = max_attempts


JOB_TYPES = {
    # Classic: CPU and memory bound. One render peaks at ~800MB RSS (measured, see
    # video_renderer.MAX_CONCURRENT_RENDERS), so one per worker, four across the fleet,
    # is the same ceiling production already runs at -- the queue changes WHO waits and
    # for how long, not how much runs at once. Single-agent behaviour is unchanged.
    JOB_TYPE_CLASSIC: JobTypeConfig(per_process_limit=1, account_limit=None, default_seconds=60),
    # Signature: bound by Replicate, which allows ONE prediction at a time on Jane's
    # whole account (the same constraint photo_upscale.py documents). Three avatar shots
    # at ~2 minutes means ~6 minutes per video, serialised platform-wide, so the account
    # limit is 1 and the ETA default reflects the real number rather than a hopeful one.
    # The renderer is deliberately not built yet; see register_renderer().
    JOB_TYPE_SIGNATURE: JobTypeConfig(per_process_limit=1, account_limit=1, default_seconds=360),
}

# How often a running job proves it is still alive, and how long silence lasts before
# another worker may take the job. Six missed beats is deliberately generous: reclaiming
# a job that is merely slow costs a duplicate render, so the bias is towards patience.
HEARTBEAT_SECONDS = 20
STALE_AFTER_SECONDS = 120

# Poll cadence. See "WHY POLLING" above for why this number.
POLL_SECONDS = 2.0
POLL_JITTER = 0.4          # +/- fraction, so restarting workers do not synchronise
POLL_SECONDS_BUSY = 5.0    # slower poll when this process has no free slots anyway
SWEEP_SECONDS = 30.0       # stale-claim sweep cadence

# A deferred job (no slot, account limit taken) comes back after this, jittered.
DEFER_BACKOFF_SECONDS = 5.0
# A job that crashed and has retries left waits this long times its attempt count.
RETRY_BACKOFF_SECONDS = 30.0

# Finished rows are kept long enough to be useful when debugging a complaint, then
# pruned by the sweeper so the table does not grow without bound. This also clears out
# rows whose listing has since been deleted, which is why there is no foreign key.
PRUNE_AFTER_DAYS = 7
PRUNE_EVERY_SECONDS = 3600

# How many worker processes we assume when turning a queue position into an ETA.
# Railway runs `uvicorn --workers 4`; WEB_CONCURRENCY is the conventional override.
def _worker_count():
    try:
        return max(1, int(os.environ.get("WEB_CONCURRENCY", "4")))
    except ValueError:
        return 4


# This process's identity in claimed_by. Host plus pid plus a random suffix, so two
# processes that reuse a pid after a restart are still told apart. Deliberately carries
# nothing sensitive -- it lands in a database column and in logs.
WORKER_ID = "%s:%d:%s" % (socket.gethostname()[:40], os.getpid(), uuid.uuid4().hex[:6])


class DeferJob(Exception):
    """Not now -- but nothing is wrong. Put the job back and try again shortly.

    This is the replacement for the old "the video service is busy, try again in a few
    minutes" error. Raised when a job cannot run for a capacity reason: no local render
    slot, an external service reporting it is at its limit. It rewinds the attempt
    counter, so no amount of waiting for capacity can ever push a job into 'failed'.
    Signature will raise this on a Replicate 429 for exactly the same reason."""


class PermanentJobError(Exception):
    """An error no amount of retrying will fix -- the agent has to change something.

    "This listing has no photos any more" is permanent. "ffmpeg was killed" is not.
    Raised by a job's run() to skip the retry ladder and fail immediately with a
    message the agent can act on."""


# ---------------------------------------------------------------------------
# Wiring. main.py owns the database helpers and the renderers; this module must not
# import main (main imports it), so both are injected -- the same shape photo_upscale
# uses for its storage client.
# ---------------------------------------------------------------------------
_db_execute = None
_RENDERERS = {}
_table_available = None    # None = not probed yet, False = fall back to inline renders
_shutting_down = False


class _Renderer:
    def __init__(self, run, on_final_failure=None):
        self.run = run
        self.on_final_failure = on_final_failure


def configure(db_execute):
    """Hand this module main.py's db_execute (retry + poisoned-pool discipline)."""
    global _db_execute
    _db_execute = db_execute


def register_renderer(job_type, run, on_final_failure=None):
    """Attach the code that actually performs one job of this type.

    run(job) -> (result_url, degradations). It may raise: PermanentJobError to fail
    without retrying, anything else to burn one attempt and be retried.

    on_final_failure(job, error_text) is called once, only when the job has exhausted
    its attempts, so the owning module can mirror the failure onto the listing row.

    A job type with no registered renderer is never claimed -- which is exactly how
    'signature' stays defined-but-inert until its pipeline is built. Wiring it up later
    is one register_renderer() call, not a change to this queue.
    """
    if job_type not in JOB_TYPES:
        raise ValueError("unknown job type %r" % job_type)
    _RENDERERS[job_type] = _Renderer(run, on_final_failure)


def available():
    """True once we know the video_jobs table is there and usable."""
    return _table_available is True


def _now():
    return datetime.now(timezone.utc)


def _iso(dt):
    return dt.isoformat()


def _exec(build, what, idempotent=True, attempts=3):
    if _db_execute is None:
        raise RuntimeError("video_jobs.configure() was never called")
    return _db_execute(build, what=what, idempotent=idempotent, attempts=attempts)


def probe():
    """Detect whether the video_jobs table exists AND this key may use it.

    Deliberately probes with a real SELECT rather than reading a catalogue: a table that
    exists but is invisible to our key (RLS with no policy for it, a typo in the grant)
    must fail this probe too, because the queue would be just as unusable. Failing the
    probe is safe -- generate-video falls back to today's inline render path, so the
    code can deploy before or after Jane runs the migration, in either order."""
    global _table_available
    try:
        _exec(lambda db: db.table(TABLE).select("id").limit(1), what="video_jobs probe")
        _table_available = True
        logger.info("video_jobs table present -- renders run on the durable queue "
                    "(worker id %s)", WORKER_ID)
    except Exception as e:
        _table_available = False
        logger.warning(
            "video_jobs table NOT usable (%s: %s) -- video renders fall back to the "
            "in-process path, which does not survive a redeploy. Run the migration in "
            "docs/video-jobs-migration.sql to enable the queue.", type(e).__name__, e)
    return _table_available


# ---------------------------------------------------------------------------
# Enqueue
# ---------------------------------------------------------------------------
def enqueue(job_type, listing_id, agent_id, payload):
    """Write one job row and return it, or return the active job that already exists.

    IDEMPOTENCY IS ENFORCED IN THE DATABASE, not here and not in the UI. The migration
    carries a partial unique index over (listing_id, job_type) WHERE status IN
    ('queued','running'), so a double-clicked Generate button cannot produce two rows no
    matter how the two requests interleave, or which two workers they land on. The
    second insert raises a uniqueness violation and we hand back the job already in
    flight, which is what the agent wanted anyway.

    That index also makes the insert's retry story safe. db_execute only ever retries a
    write when the request provably never reached the database, and even if that
    judgement were ever wrong, a duplicate insert is rejected by the index rather than
    silently double-queueing the render. idempotent=False is still passed, because
    defence in depth is the point.
    """
    if job_type not in JOB_TYPES:
        raise ValueError("unknown job type %r" % job_type)

    now = _iso(_now())
    row = {
        "job_type": job_type,
        "listing_id": str(listing_id),
        "agent_id": str(agent_id),
        "status": STATUS_QUEUED,
        # Options only. Never the agent record -- it carries Facebook/Instagram tokens.
        "payload": payload or {},
        "attempts": 0,
        "max_attempts": JOB_TYPES[job_type].max_attempts,
        "run_after": now,
        "created_at": now,
        "updated_at": now,
    }
    try:
        res = _exec(lambda db: db.table(TABLE).insert(row),
                    what="video_jobs enqueue insert", idempotent=False)
        if res.data:
            return res.data[0], True
    except Exception as e:
        if not _is_unique_violation(e):
            raise
        logger.info("video_jobs: %s job for listing %s already queued/running; "
                    "reusing it", job_type, listing_id)

    existing = find_active(job_type, listing_id)
    if existing:
        return existing, False
    # The unique index fired but the row is gone already (it finished in the microsecond
    # between). Re-read failed to find anything active, so let the caller treat this as
    # "nothing in flight" and try again rather than inventing a job id.
    raise RuntimeError("could not enqueue or find an active %s job for listing %s"
                       % (job_type, listing_id))


def _is_unique_violation(exc):
    """PostgREST reports a unique-index violation as SQLSTATE 23505.

    Matched on the code rather than the message so a reworded PostgREST error does not
    silently turn a handled duplicate into a 500."""
    code = getattr(exc, "code", None)
    if code and str(code) == "23505":
        return True
    text = str(exc)
    return "23505" in text or "duplicate key value" in text


def find_active(job_type, listing_id):
    res = _exec(
        lambda db: db.table(TABLE).select("*")
        .eq("listing_id", str(listing_id)).eq("job_type", job_type)
        .in_("status", list(ACTIVE_STATUSES))
        .order("created_at", desc=True).limit(1),
        what="video_jobs find_active")
    return (res.data or [None])[0]


def get_job(job_id):
    res = _exec(lambda db: db.table(TABLE).select("*").eq("id", str(job_id)).limit(1),
                what="video_jobs get_job")
    return (res.data or [None])[0]


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------
def claim_next(job_type):
    """Take the oldest runnable job of this type, or return None.

    Candidates are read first and then claimed one at a time by conditional UPDATE. The
    SELECT is only a hint -- it may be stale the instant it returns, and that is fine,
    because nothing is decided by it. The UPDATE decides, and it decides atomically (see
    the module docstring). Losing the race just means trying the next candidate.
    """
    cfg = JOB_TYPES[job_type]

    # Fleet-wide gate, for types bound by an external service rather than by our CPU.
    # This is only a cheap pre-filter; the authoritative check happens after the claim,
    # in confirm_account_slot(), because a count read can always be stale.
    if cfg.account_limit is not None and _running_count(job_type) >= cfg.account_limit:
        return None

    now = _iso(_now())
    res = _exec(
        lambda db: db.table(TABLE).select("*")
        .eq("job_type", job_type).eq("status", STATUS_QUEUED)
        .lte("run_after", now)
        .order("created_at").limit(10),
        what="video_jobs claim candidates")
    for candidate in (res.data or []):
        claimed = _try_claim(candidate)
        if claimed is not None:
            return claimed
    return None


def _try_claim(candidate):
    """One compare-and-swap attempt. Returns the claimed row, or None if we lost."""
    job_id = candidate["id"]
    attempts = int(candidate.get("attempts") or 0)
    now = _now()
    patch = {
        "status": STATUS_RUNNING,
        "claimed_by": WORKER_ID,
        "claimed_at": _iso(now),
        "heartbeat_at": _iso(now),
        # Incremented as part of the same statement, so (status, attempts) together are
        # the compare-and-swap key and the attempt number we hold is unique to us.
        "attempts": attempts + 1,
        "started_at": candidate.get("started_at") or _iso(now),
        "updated_at": _iso(now),
        "error": None,
    }
    try:
        res = _exec(
            lambda db: db.table(TABLE).update(patch)
            .eq("id", job_id).eq("status", STATUS_QUEUED).eq("attempts", attempts),
            what="video_jobs claim")
    except Exception as e:
        # Under REPEATABLE READ a losing claimer gets a serialization failure rather
        # than zero rows. Same meaning, so same handling: someone else has the job.
        if _is_serialization_failure(e):
            return None
        raise

    if res.data:
        return res.data[0]

    # Empty data means either "we lost" or "this deployment is not returning the updated
    # representation". Read the row back and let the database tell us which, rather than
    # inferring ownership from a header we did not set.
    fresh = get_job(job_id)
    if (fresh and fresh.get("claimed_by") == WORKER_ID
            and fresh.get("status") == STATUS_RUNNING
            and int(fresh.get("attempts") or 0) == attempts + 1):
        return fresh
    return None


def _is_serialization_failure(exc):
    code = str(getattr(exc, "code", "") or "")
    if code in ("40001", "40P01"):
        return True
    text = str(exc)
    return "40001" in text or "could not serialize" in text or "deadlock detected" in text


def _running_count(job_type):
    res = _exec(
        lambda db: db.table(TABLE).select("id", count="exact")
        .eq("job_type", job_type).eq("status", STATUS_RUNNING),
        what="video_jobs running count")
    if getattr(res, "count", None) is not None:
        return int(res.count)
    return len(res.data or [])


_running_count_cache = {}


def _running_count_cached(job_type, max_age=2.0):
    """_running_count for DISPLAY only, with a couple of seconds of slack.

    My Listings polls every few seconds and every agent polls independently, so an
    uncached count here would multiply straight into PostgREST load for a number whose
    only job is to render "3rd in line". Two seconds of staleness cannot change that
    string meaningfully. Admission control deliberately does NOT use this -- claim_next
    reads the live count, and confirm_account_slot is the authority regardless."""
    with _duration_cache_lock:
        cached = _running_count_cache.get(job_type)
        if cached and time.monotonic() - cached[0] < max_age:
            return cached[1]
    value = _running_count(job_type)
    with _duration_cache_lock:
        _running_count_cache[job_type] = (time.monotonic(), value)
    return value


def confirm_account_slot(job):
    """Second, authoritative check of a fleet-wide concurrency limit.

    The pre-filter in claim_next() reads a count that may be stale by the time we act on
    it, so for a limit of 1 two workers could in principle both claim. This closes that:
    after claiming, list every running job of this type in a total order -- oldest claim
    first, job id as the tie-break -- and proceed only if we are inside the first
    `account_limit` of them. Anyone outside releases and comes back.

    The ordering is a deterministic function of committed rows, so the worker that
    claimed first always wins and the queue keeps moving. In the worst case both
    workers yield on the same pass and one of them re-claims two seconds later; that
    costs latency, never correctness, and never a failure shown to the agent.
    """
    cfg = JOB_TYPES[job["job_type"]]
    if cfg.account_limit is None:
        return True
    res = _exec(
        lambda db: db.table(TABLE).select("id,claimed_at")
        .eq("job_type", job["job_type"]).eq("status", STATUS_RUNNING)
        .order("claimed_at").limit(cfg.account_limit + 5),
        what="video_jobs confirm account slot")
    rows = sorted((r for r in (res.data or [])),
                  key=lambda r: (str(r.get("claimed_at") or ""), str(r.get("id"))))
    return str(job["id"]) in [str(r["id"]) for r in rows[:cfg.account_limit]]


# ---------------------------------------------------------------------------
# Terminal and near-terminal writes -- every one fenced on (claimed_by, attempts)
# ---------------------------------------------------------------------------
def _fenced_update(job, patch, what):
    """Write only if we still own this job at the attempt number we claimed.

    Returns True if the write landed. False means another worker has since reclaimed the
    job (our claim went stale and the sweeper gave it away), so whatever we were about
    to record is out of date and must be dropped on the floor."""
    job_id = job["id"]
    attempt = int(job.get("attempts") or 0)
    body = dict(patch)
    body["updated_at"] = _iso(_now())
    try:
        res = _exec(
            lambda db: db.table(TABLE).update(body)
            .eq("id", job_id).eq("claimed_by", WORKER_ID).eq("attempts", attempt),
            what=what)
    except Exception as e:
        if _is_serialization_failure(e):
            return False
        raise
    if res.data:
        return True
    # Same belt-and-braces as _try_claim: an empty result may mean "we lost the row" or
    # may mean this PostgREST deployment did not return the updated representation, so
    # read the row back and check it reflects what WE wrote rather than what a reclaim
    # would have written. Timestamps are skipped in the comparison because Postgres
    # normalises them on the way back; status, claimed_by and attempts together are
    # already enough to tell our write apart from anyone else's.
    fresh = get_job(job_id)
    if not fresh:
        return False
    if "attempts" in body and int(fresh.get("attempts") or 0) != int(body["attempts"]):
        return False
    if "status" in body and fresh.get("status") != body["status"]:
        return False
    if "claimed_by" in body and fresh.get("claimed_by") != body["claimed_by"]:
        return False
    if "attempts" not in body:
        # A heartbeat-shaped patch changes nothing we can compare, so fall back to
        # "do we still own this job at the attempt we claimed".
        return (fresh.get("claimed_by") == WORKER_ID
                and int(fresh.get("attempts") or 0) == attempt)
    return True


def heartbeat(job):
    """Prove this job is still alive. False means we have been superseded."""
    return _fenced_update(job, {"heartbeat_at": _iso(_now())}, "video_jobs heartbeat")


def complete(job, result_url, degradations, duration_seconds):
    ok = _fenced_update(job, {
        "status": STATUS_DONE,
        "result_url": result_url,
        "degradations": list(degradations or []),
        "duration_seconds": round(float(duration_seconds), 2),
        "finished_at": _iso(_now()),
        "error": None,
    }, "video_jobs complete")
    if not ok:
        logger.warning("video_jobs: job %s finished but was already reclaimed by "
                       "another worker -- discarding this result", job["id"])
    return ok


def defer(job, reason, backoff_seconds=DEFER_BACKOFF_SECONDS):
    """Put a claimed job back because we cannot run it RIGHT NOW -- never a failure.

    attempts is rewound to its pre-claim value, so waiting for capacity can never use up
    a job's retries and can never push it into the failed state. This is the mechanism
    that replaces the old 240-second fuse: a job with nowhere to run waits, visibly, and
    the agent is told where they are in the queue instead of being told no."""
    target = _iso(_now() + timedelta(seconds=backoff_seconds * (0.7 + random.random() * 0.6)))
    ok = _fenced_update(job, {
        "status": STATUS_QUEUED,
        "claimed_by": None,
        "claimed_at": None,
        "attempts": max(0, int(job.get("attempts") or 1) - 1),
        "run_after": target,
    }, "video_jobs defer")
    if ok:
        logger.info("video_jobs: deferred %s job %s (%s)", job["job_type"], job["id"], reason)
    return ok


def fail_or_retry(job, error_text):
    """A genuine error. Retry if attempts remain, otherwise fail for good.

    A retry leaves the listing alone -- from the agent's side the video is still on its
    way, because it is. Only the final failure is mirrored onto the listing row, through
    the owning module's on_final_failure hook, so a transient ffmpeg crash never flashes
    an error at an agent whose video then arrives anyway."""
    attempt = int(job.get("attempts") or 1)
    limit = int(job.get("max_attempts") or JOB_TYPES[job["job_type"]].max_attempts)
    text = str(error_text)[:500]

    if attempt < limit:
        target = _iso(_now() + timedelta(
            seconds=RETRY_BACKOFF_SECONDS * attempt * (0.7 + random.random() * 0.6)))
        ok = _fenced_update(job, {
            "status": STATUS_QUEUED,
            "claimed_by": None,
            "claimed_at": None,
            "run_after": target,
            "error": text,
        }, "video_jobs retry")
        if ok:
            logger.warning("video_jobs: %s job %s failed on attempt %d/%d (%s) -- "
                           "retrying", job["job_type"], job["id"], attempt, limit, text)
        return ok

    ok = _fenced_update(job, {
        "status": STATUS_FAILED,
        "error": text,
        "finished_at": _iso(_now()),
    }, "video_jobs fail")
    if ok:
        logger.error("video_jobs: %s job %s failed permanently after %d attempts: %s",
                     job["job_type"], job["id"], attempt, text)
        _notify_final_failure(job, text)
    return ok


def fail_permanently(job, error_text):
    """Skip the retry ladder -- nothing about running this again would go better."""
    text = str(error_text)[:500]
    ok = _fenced_update(job, {
        "status": STATUS_FAILED,
        "error": text,
        "finished_at": _iso(_now()),
    }, "video_jobs fail permanently")
    if ok:
        logger.error("video_jobs: %s job %s failed (not retryable): %s",
                     job["job_type"], job["id"], text)
        _notify_final_failure(job, text)
    return ok


def _notify_final_failure(job, text):
    entry = _RENDERERS.get(job["job_type"])
    if entry is None or entry.on_final_failure is None:
        return
    try:
        entry.on_final_failure(job, text)
    except Exception as e:
        logger.warning("video_jobs: on_final_failure hook for job %s raised: %s",
                       job["id"], e)


# ---------------------------------------------------------------------------
# Stale-claim recovery and pruning
# ---------------------------------------------------------------------------
def sweep_stale():
    """Return abandoned jobs to the queue. Safe to run on every worker at once.

    Every reclaim is the same compare-and-swap used for a normal claim, so two sweepers
    racing is a non-event: one wins, the other updates zero rows. That is why no leader
    election is needed here -- running it everywhere is simpler AND more available than
    electing one worker that might be the one that died."""
    cutoff = _iso(_now() - timedelta(seconds=STALE_AFTER_SECONDS))
    try:
        res = _exec(
            lambda db: db.table(TABLE).select("*")
            .eq("status", STATUS_RUNNING).lt("heartbeat_at", cutoff).limit(50),
            what="video_jobs sweep select")
    except Exception as e:
        logger.warning("video_jobs: stale sweep could not read (%s)", e)
        return 0

    reclaimed = 0
    for job in (res.data or []):
        holder = job.get("claimed_by")
        if not holder:
            # A running row with no holder is not something this code can produce, and
            # `eq` on a null would not match anyway. Leave it for a human to look at.
            logger.warning("video_jobs: job %s is running with no claimed_by; skipping",
                           job.get("id"))
            continue
        attempt = int(job.get("attempts") or 0)
        limit = int(job.get("max_attempts") or 3)
        now = _now()
        if attempt >= limit:
            patch = {
                "status": STATUS_FAILED,
                "error": ("The render was interrupted and could not be completed after "
                          "several attempts. Please try generating the video again."),
                "finished_at": _iso(now),
                "updated_at": _iso(now),
            }
        else:
            patch = {
                "status": STATUS_QUEUED,
                "claimed_by": None,
                "claimed_at": None,
                "run_after": _iso(now),
                "updated_at": _iso(now),
            }
        try:
            out = _exec(
                lambda db: db.table(TABLE).update(patch)
                .eq("id", job["id"]).eq("status", STATUS_RUNNING)
                .eq("claimed_by", holder).eq("attempts", attempt),
                what="video_jobs sweep reclaim")
        except Exception as e:
            if _is_serialization_failure(e):
                continue
            logger.warning("video_jobs: could not reclaim job %s (%s)", job["id"], e)
            continue
        if out.data or _reclaim_landed(job["id"], attempt, patch["status"]):
            reclaimed += 1
            if patch["status"] == STATUS_FAILED:
                logger.error("video_jobs: job %s abandoned %d times; giving up",
                             job["id"], attempt)
                _notify_final_failure(job, patch["error"])
            else:
                logger.warning("video_jobs: job %s was abandoned by %s (no heartbeat "
                               "for %ds) -- requeued for another worker",
                               job["id"], job.get("claimed_by"), STALE_AFTER_SECONDS)
    return reclaimed


def _reclaim_landed(job_id, attempt, expected_status):
    fresh = get_job(job_id)
    return bool(fresh and fresh.get("status") == expected_status
                and int(fresh.get("attempts") or 0) == attempt)


def prune_finished():
    cutoff = _iso(_now() - timedelta(days=PRUNE_AFTER_DAYS))
    try:
        _exec(lambda db: db.table(TABLE).delete()
              .in_("status", list(TERMINAL_STATUSES)).lt("finished_at", cutoff),
              what="video_jobs prune")
    except Exception as e:
        logger.warning("video_jobs: prune failed (%s)", e)


def release_local_claims():
    """Called on shutdown: hand back everything this process is holding, immediately.

    Without this a redeploy's in-flight jobs would sit 'running' until the stale sweep
    noticed, up to STALE_AFTER_SECONDS later. With it, the next worker picks them up in
    seconds. Best-effort by design -- if the process is killed outright, the heartbeat
    sweep is still there as the backstop, which is the whole point of having both."""
    with _locally_claimed_lock:
        held = list(_locally_claimed.values())
    for job in held:
        try:
            defer(job, "worker shutting down", backoff_seconds=0.5)
        except Exception as e:
            logger.warning("video_jobs: could not release job %s on shutdown (%s)",
                           job.get("id"), e)


# ---------------------------------------------------------------------------
# Queue position and ETA -- "a wait you can see beats a spinner"
# ---------------------------------------------------------------------------
_duration_cache = {}
_duration_cache_lock = threading.Lock()
_DURATION_CACHE_SECONDS = 60


def _average_duration(job_type):
    """Rolling mean of recent successful renders, cached so a polling UI is cheap."""
    with _duration_cache_lock:
        cached = _duration_cache.get(job_type)
        if cached and time.monotonic() - cached[0] < _DURATION_CACHE_SECONDS:
            return cached[1]
    fallback = JOB_TYPES[job_type].default_seconds
    try:
        res = _exec(
            lambda db: db.table(TABLE).select("duration_seconds")
            .eq("job_type", job_type).eq("status", STATUS_DONE)
            .order("finished_at", desc=True).limit(20),
            what="video_jobs average duration")
        values = [float(r["duration_seconds"]) for r in (res.data or [])
                  if r.get("duration_seconds")]
        average = sum(values) / len(values) if values else fallback
    except Exception:
        average = fallback
    with _duration_cache_lock:
        _duration_cache[job_type] = (time.monotonic(), average)
    return average


def describe(job):
    """What the agent is shown: where they are, and roughly how long.

    Position counts everything genuinely ahead of them -- jobs already running plus jobs
    queued earlier -- because "you are 3rd" has to mean the same thing to the agent as
    it does to the queue, or it is worse than saying nothing."""
    if not job:
        return {}
    job_type = job.get("job_type") or JOB_TYPE_CLASSIC
    cfg = JOB_TYPES.get(job_type, JOB_TYPES[JOB_TYPE_CLASSIC])
    status = job.get("status")
    out = {
        "job_id": str(job.get("id")),
        "job_type": job_type,
        "job_status": status,
        "degradations": job.get("degradations") or [],
    }
    if status == STATUS_RUNNING:
        out["queue_position"] = 0
        out["eta_seconds"] = int(_average_duration(job_type))
        return out
    if status != STATUS_QUEUED:
        return out

    try:
        ahead_queued = _exec(
            lambda db: db.table(TABLE).select("id", count="exact")
            .eq("job_type", job_type).eq("status", STATUS_QUEUED)
            .lt("created_at", job["created_at"]),
            what="video_jobs position queued")
        ahead = int(getattr(ahead_queued, "count", None) or len(ahead_queued.data or []))
        ahead += _running_count_cached(job_type)
    except Exception:
        return out

    concurrency = cfg.account_limit or (cfg.per_process_limit * _worker_count())
    concurrency = max(1, concurrency)
    average = _average_duration(job_type)
    # Rounded up: the honest answer to "when will mine start" is the end of the batch
    # it sits behind, not the average across it.
    waves_ahead = (ahead + concurrency - 1) // concurrency
    out["queue_position"] = ahead + 1
    out["eta_seconds"] = int(waves_ahead * average + average)
    return out


def describe_active_for_agent(agent_id):
    """One round trip for everything this agent currently has in flight.

    Used to decorate the My Listings payload. An agent with nothing rendering costs one
    indexed select that returns nothing, which is the common case by a wide margin."""
    if not available():
        return {}
    try:
        res = _exec(
            lambda db: db.table(TABLE).select("*")
            .eq("agent_id", str(agent_id)).in_("status", list(ACTIVE_STATUSES))
            .order("created_at").limit(20),
            what="video_jobs active for agent")
    except Exception as e:
        logger.warning("video_jobs: could not read active jobs for an agent (%s)", e)
        return {}
    out = {}
    for job in (res.data or []):
        try:
            out[str(job["listing_id"])] = describe(job)
        except Exception:
            continue
    return out


# ---------------------------------------------------------------------------
# The consumer: claim, run, heartbeat, record
# ---------------------------------------------------------------------------
_locally_claimed = {}
_locally_claimed_lock = threading.Lock()


def _heartbeat_loop(job, stop_event, superseded_event):
    while not stop_event.wait(HEARTBEAT_SECONDS):
        try:
            if not heartbeat(job):
                superseded_event.set()
                logger.warning("video_jobs: job %s was taken over by another worker "
                               "while we were still rendering it", job["id"])
                return
        except Exception as e:
            # A transport blip is not proof we lost the job; keep beating. Genuine
            # abandonment is caught by the sweeper on the other side regardless.
            logger.warning("video_jobs: heartbeat for job %s failed (%s)", job["id"], e)


def execute_job(job):
    """Run one claimed job to a terminal state. Blocking -- call it in a thread."""
    job_id = job["id"]
    job_type = job["job_type"]
    with _locally_claimed_lock:
        _locally_claimed[job_id] = job

    stop_event = threading.Event()
    superseded_event = threading.Event()
    beater = threading.Thread(target=_heartbeat_loop,
                              args=(job, stop_event, superseded_event),
                              name="video-job-heartbeat", daemon=True)
    beater.start()
    started = time.monotonic()
    try:
        entry = _RENDERERS.get(job_type)
        if entry is None or entry.run is None:
            # Cannot happen through claim_next (it only looks at registered types), but
            # a job row inserted by hand or left behind by a rollback would land here.
            fail_permanently(job, "This kind of video is not available yet.")
            return

        if not confirm_account_slot(job):
            defer(job, "the account-wide slot for %s is taken" % job_type)
            return

        result_url, degradations = entry.run(job)
        if superseded_event.is_set():
            logger.warning("video_jobs: finished job %s but another worker owns it now; "
                           "not recording our result", job_id)
            return
        complete(job, result_url, degradations, time.monotonic() - started)
    except DeferJob as e:
        defer(job, str(e) or "no capacity right now")
    except PermanentJobError as e:
        fail_permanently(job, e)
    except Exception as e:
        logger.exception("video_jobs: %s job %s raised", job_type, job_id)
        fail_or_retry(job, e)
    finally:
        stop_event.set()
        with _locally_claimed_lock:
            _locally_claimed.pop(job_id, None)


def _poll_delay(has_capacity):
    """Poll briskly while there is somewhere to put work; idle back when there is not."""
    base = POLL_SECONDS if has_capacity else POLL_SECONDS_BUSY
    return base * (1.0 - POLL_JITTER + random.random() * 2 * POLL_JITTER)


async def run_consumer():
    """Poll for work and run it. One of these per uvicorn worker.

    Runs in the event loop but never blocks it: the claim, the render and every database
    call go out to threads. The per-type local limits are counted here, on the loop, so
    the counting itself needs no lock.

    NOTE ON WHERE THIS RUNS. Nothing about the queue requires the consumer to live
    inside a web worker -- it only needs a process with database access. Moving renders
    off the web workers entirely is a one-line Procfile addition (`worker: python -m
    video_jobs_worker`) plus an env var to stop the web workers consuming. That is worth
    doing before agent numbers grow, because an ffmpeg render inside a web worker
    competes for CPU with buyer-facing page requests. It is deliberately NOT done in
    this change: in-process consumers across four workers give redundancy for free,
    whereas a single worker process is a single point of failure, and the rollout gate
    is better served by a smaller change surface. The split is a deployment decision
    now, not an architectural one."""
    if os.environ.get("VIDEO_QUEUE_CONSUMER", "1") not in ("1", "true", "True"):
        logger.info("video_jobs: consumer disabled on this process by VIDEO_QUEUE_CONSUMER")
        return
    if not available():
        return

    running = {}
    tasks = set()
    last_sweep = 0.0
    last_prune = 0.0
    logger.info("video_jobs: consumer started (worker %s)", WORKER_ID)

    while not _shutting_down:
        picked = False
        has_capacity = True
        try:
            now = time.monotonic()
            if now - last_sweep > SWEEP_SECONDS * (0.7 + random.random() * 0.6):
                last_sweep = now
                await asyncio.to_thread(sweep_stale)
            if now - last_prune > PRUNE_EVERY_SECONDS:
                last_prune = now
                await asyncio.to_thread(prune_finished)

            has_capacity = False
            for job_type in sorted(_RENDERERS):
                cfg = JOB_TYPES[job_type]
                if running.get(job_type, 0) >= cfg.per_process_limit:
                    continue
                has_capacity = True
                job = await asyncio.to_thread(claim_next, job_type)
                if job is None:
                    continue
                picked = True
                running[job_type] = running.get(job_type, 0) + 1
                task = asyncio.create_task(asyncio.to_thread(execute_job, job))
                tasks.add(task)

                def _finished(t, job_type=job_type):
                    tasks.discard(t)
                    running[job_type] = max(0, running.get(job_type, 1) - 1)
                    if not t.cancelled() and t.exception() is not None:
                        logger.error("video_jobs: consumer task for %s died: %s",
                                     job_type, t.exception())

                task.add_done_callback(_finished)
        except Exception as e:
            # The consumer loop must never die: if it does, this worker stops taking
            # work for the life of the process and nothing says so.
            logger.warning("video_jobs: consumer poll failed (%s: %s)", type(e).__name__, e)
            has_capacity = True

        await asyncio.sleep(0.05 if picked else _poll_delay(has_capacity))


def begin_shutdown():
    global _shutting_down
    _shutting_down = True
