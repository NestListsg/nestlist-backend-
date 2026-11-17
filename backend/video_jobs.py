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
There is one type today, 'classic'. It is CPU/memory bound: one render peaks around
1.6GB RSS counting the ffmpeg children (see the measured table at JOB_TYPES -- the
long-quoted 800MB counted only the python process), so its limit is per-process and
stays at 1 (4 across the fleet).

The structure is kept per type rather than collapsed to one global number because the
right lever depends on what the constraint IS. A CPU- or memory-bound render is capped
per PROCESS. A type bound by an external service -- one that allows, say, a single
prediction at a time across the whole account however many processes we run -- needs an
ACCOUNT-WIDE cap checked against the database instead, and JobTypeConfig carries both.
Adding a type is a row in JOB_TYPES plus one register_renderer call, not re-architecting;
that is how the Signature tier was added, and how it was removed again on 2026-10-21.

BUSYNESS IS NEVER A FAILURE
A worker that claims a job and then finds it cannot run it (no local render slot, or the
account-wide limit is already taken) RELEASES the job back to 'queued' and restores its
attempts counter, so a deferral never burns a retry. Only genuine errors -- unreadable
photos, ffmpeg crashing, a listing that no longer has photos -- can fail a job, and
those still travel through video_renderer's degradation ladder untouched.

WHY POLLING -- AND WHAT THE OLD CADENCE ACTUALLY COST (CORRECTED 2026-11-17)
PostgREST gives us no way to be woken (no LISTEN/NOTIFY over REST), so workers poll.

The paragraph that used to sit here said "four workers at roughly one query every two
seconds is negligible load on PostgREST". As a judgement about LOAD that was right, and
it still is: PostgREST never noticed. It was wrong about the two things that are METERED
on Jane's plan, and it undercounted the queries by about a factor of five.

The undercount: it was one query PER REGISTERED TYPE per cycle, not one per cycle, plus
a separate running-count query for every type that has an account_limit (dramatic,
elegant). main.py registers the Classic and the Dramatic unconditionally and the Elegant
only when ELEGANT_VIDEO_ENABLED is set, so that is THREE requests per worker per two
seconds today and FIVE with the Elegant switched on -- times four uvicorn workers, which
is where ~7-8 a second comes from rather than the "one every two seconds" above.

What was measured in Supabase's Edge Logs on 2026-11-17, with essentially nobody using
the platform:
  * ALL of the 100 most recent requests were GET /rest/v1/video_jobs. Nothing else
    appeared at all.
  * ~7-8 requests a second, continuously, on a dead-flat hourly chart -- it ran around
    the clock whether or not anyone was using the app. About 600,000 requests a day,
    ~18 million a month, against a queue that is almost always empty.
  * Egress 0.91 GB in the first two days of a cycle that allows 5 GB, and log ingestion
    already at 133% of the monthly allowance after two days.

So the cost that bites is not CPU. It is BYTES AND LOG LINES. Every poll is an HTTPS
round trip with headers both ways -- about 750 bytes of egress and about 950 bytes of log
ingestion each, from dividing those totals by the request count -- and both are metered
monthly whether the response carries a job or an empty array. An idle poll is not free;
it is only not slow.

What the loop does now, and why each piece is there:
  * ONE query per cycle covering every type this worker may take (`job_type=in.(...)`)
    instead of one per type, reading only the four columns a claim actually needs. The
    round-robin that stopped one type starving another is kept: it now orders the
    CANDIDATES of a single read rather than the reads themselves.
  * The account_limit running-count is asked LAZILY, only when a candidate of that type
    actually turned up. An empty queue costs zero count queries.
  * The interval RAMPS while the queue keeps coming back empty -- POLL_IDLE_LADDER,
    2s up to 30s -- and snaps back to POLL_SECONDS the moment anything is claimed.
  * TWO DOORBELLS keep that ramp from costing an agent anything, because half a minute
    of dead air before a ~100s render even starts would be a real regression. enqueue()
    sets an in-process asyncio event -- the worker that served the request is usually
    the one with a free render slot -- and it also replaces a tiny file in a private
    /tmp directory that the sibling workers in the same container read once a second.
    Reading a file is not a request: no egress, no log line. If that file cannot be used
    (read-only disk, or workers spread over more than one container) nothing breaks; the
    ramp is then the only thing left and the worst case is one rung of it.
  * The jitter is unchanged, so four workers restarting together after a deploy still do
    not synchronise into a thundering herd.

NO CREDENTIALS IN JOB ROWS
The payload column carries render OPTIONS only -- template id, photo index, music track, chosen video photos (URLs). The agent is
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
# "From Old to New", the flying-furniture variation. Same id as its video template, so
# there is one word for one thing from the request through to the job row.
JOB_TYPE_DRAMATIC = "dramatic"
# PHASE TWO. The Kling makeover. Declared nowhere else yet: it is not in JOB_TYPES and
# has no renderer, so nothing can enqueue it -- adding it is a JOB_TYPES entry and one
# register_renderer call, which is the seam this module was built to have.
JOB_TYPE_ELEGANT = "elegant"

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

    AND FOUR THAT EXIST BECAUSE OF THE ELEGANT, whose render is 10-15 minutes where
    every other type's is one or two. A quarter-hour job breaks three assumptions this
    module was built on, and each of them needed designing rather than patching:

    counts_toward_render_limit -- whether one of these occupying this process should stop
                          it taking any other render. TRUE for anything bound by OUR cpu:
                          a Classic or a Dramatic is ffmpeg running flat out from start to
                          finish, and two in one process is two ffmpeg trees at 1.6GB each.
                          FALSE for a type that spends almost all of its life ASLEEP
                          WAITING ON SOMEBODY ELSE'S API. An Elegant render is 10-15
                          minutes of which maybe one is ffmpeg; counting it as a render
                          would take a quarter of this worker's Classic throughput offline
                          for a quarter of an hour to protect a cpu nobody is using.
                          IT IS SAFE BECAUSE THE CPU IS GUARDED SOMEWHERE ELSE AS WELL:
                          video_renderer._RENDER_SLOTS is a per-process semaphore of ONE
                          that BOTH renderers acquire around their ffmpeg phase, so a
                          waiting Elegant and a running Classic can coexist but two
                          ffmpeg phases in one process cannot. The admission control here
                          stops the queue handing a worker work it cannot do; that
                          semaphore stops two renders being in ffmpeg at once. The
                          Elegant needs the second guarantee, not the first.
    queued_escape_seconds -- how long one of these may sit in 'queued' before the
                          abandoned-queue sweep concludes nothing is draining the queue.
                          Must be a large multiple of how long the type takes to RUN,
                          or a healthy queue of them looks dead. None means the global
                          QUEUED_ESCAPE_SECONDS.
    min_quoted_seconds -- never tell an agent less than this. The ETA is a rolling mean
                          of recent renders, which is honest for a type whose renders are
                          all about the same length and misleading for one whose cached
                          regenerate takes a minute and whose cold render takes fifteen.
                          Under-quoting a long wait is the specific failure that makes an
                          agent press Generate again, so the floor is a floor and not an
                          average.
    rate_limit_patience_seconds -- how long a job may keep going back in the queue because
                          the external service has no room, before we give the agent the
                          film we can make instead. Has to be longer than the type's own
                          render, or the first deferral is already out of patience.
    """

    def __init__(self, per_process_limit, account_limit, default_seconds,
                 max_attempts=3, counts_toward_render_limit=True,
                 queued_escape_seconds=None, min_quoted_seconds=0,
                 rate_limit_patience_seconds=600):
        self.per_process_limit = per_process_limit
        self.account_limit = account_limit
        self.default_seconds = default_seconds
        self.max_attempts = max_attempts
        self.counts_toward_render_limit = counts_toward_render_limit
        self.queued_escape_seconds = queued_escape_seconds
        self.min_quoted_seconds = min_quoted_seconds
        self.rate_limit_patience_seconds = rate_limit_patience_seconds


# MEASURED COST OF ONE CLASSIC RENDER (2026-10-19, real ffmpeg, 3200x1800 sources).
# This replaces a long-standing "~800MB RSS" figure that counted only the PYTHON
# process. ffmpeg is a separate process and the instance pays for both, so the number
# that matters for sizing is the peak of the SUM:
#
#     photos   peak tree RSS   python   ffmpeg   wall    ffmpeg CPU
#        6        1521 MB      307 MB   1214 MB  11.7s      79.1s
#       10        1613 MB      379 MB   1234 MB  18.0s     127.0s
#
# (An independent audit measured ~1.85GB on different hardware. Treat 1.6-1.9GB as the
# range, not 800MB.)
#
# TWO THINGS TO READ OFF THIS. First, peak memory is dominated by ffmpeg and is nearly
# FLAT in the photo count -- 1214MB at six photos, 1234MB at ten -- because clips are
# encoded one at a time, so raising the photo cap costs CPU and bytes, not memory.
# Second, the real fleet ceiling at four workers is ~6.4GB (or ~7.4GB on the audit's
# figure), not the ~3.3GB derived further down from the old 800MB. Size the Railway
# instance from THIS table.
#
# The per-process limit of 1 is unchanged and is still right -- if anything more
# clearly right, since each render is twice the footprint previously believed.
JOB_TYPES = {
    # Classic: CPU and memory bound. One per worker, four across the fleet, is the same
    # ceiling production already runs at -- the queue changes WHO waits and for how
    # long, not how much runs at once. Single-agent behaviour is unchanged.
    #
    # default_seconds was 60, calibrated when a film was six photos. Ten photos cost
    # 1.6x the CPU (79s -> 127s measured above), so the cold-start quote was about half
    # the truth; scaled by that same ratio. It is ONLY the fallback until real
    # duration_seconds rows exist, after which the rolling mean takes over.
    JOB_TYPE_CLASSIC: JobTypeConfig(per_process_limit=1, account_limit=None, default_seconds=100),

    # Dramatic ("From Old to New"). Two reasons its numbers differ from the Classic's.
    #
    # account_limit=1 FOR THE ROLLOUT, where the Classic has none. The Classic is bound by
    # OUR cpu, and PER_PROCESS_RENDER_LIMIT plus the worker count is the right ceiling for
    # that. This type is also bound by an EXTERNAL service -- it generates up to six images
    # through fal before a frame is rendered -- and the fleet-wide lever is the one that
    # fits an external constraint. A second render is DEFERRED (which rewinds its attempt
    # counter) rather than failed.
    #
    # IT WAS TWO, AND TWO WAS THE LIMIT WITH NO HEADROOM. fal starts a NEW ACCOUNT at two
    # concurrent requests, rising as credits are spent. Two renders, each with one request
    # in flight, is exactly that -- and Jane's own experiments on ~/.nestlist_fal_key share
    # the key, so a third request from her laptop collides with a real agent's render. An
    # earlier version of this comment called a collision "a wait of a few seconds". A
    # second audit on 2026-10-22 measured the wait: ONE collision costs about 37 seconds
    # (four collect attempts at POLL_MAX + RETRY_BACKOFF, then DEFER_BACKOFF_SECONDS), not
    # "seconds". Worse, a collision that lands AFTER fal has accepted a submit is billed
    # and used to be invisible to the per-agent tally -- see old_to_new.RateLimited.
    #
    # So for the rollout this is 1. Deferring costs a wait and nothing else, so halving
    # throughput buys headroom inside fal's limit for the price of time: at one render at a
    # time, Jane's laptop and an agent's render no longer collide at all, and nothing is
    # ever billed for a picture lost to a 429. REVISIT once real 429 rates are known from
    # live renders (or once fal raises the account's concurrency with spend), because 1 is
    # a deliberately conservative number for a feature whose failure modes have only been
    # measured in a test harness, not a permanent judgement about throughput.
    #
    # default_seconds=300, against the Classic's 100. A cold Dramatic render pays for up
    # to six generations before any ffmpeg runs: at fal's healthy 10-25s an image that
    # is 1-2.5 minutes of waiting, then the local render. The quote an agent sees has to
    # include it, and this is only the fallback until real duration_seconds rows exist.
    # A REGENERATE is far quicker -- every image comes from the listing's cache -- so the
    # rolling mean will sit well below this once both kinds have run.
    JOB_TYPE_DRAMATIC: JobTypeConfig(per_process_limit=1, account_limit=1,
                                     default_seconds=300,
                                     rate_limit_patience_seconds=600),

    # Elegant ("From Old to New", the Kling film). EVERY NUMBER HERE DIFFERS FROM THE
    # OTHER TWO, because this is the first job type that is not a render at all for most
    # of its life -- it is a quarter of an hour of waiting on somebody else's API with a
    # minute of ffmpeg on the end. The first reliability audit's wording was right: "a
    # quarter-hour job breaks the queue's assumptions ... that needs designing, not
    # patching."
    #
    # account_limit=1. Same lever as the Dramatic's and for a sharper version of the same
    # reason. One Elegant render asks fal for two clips at a time
    # (old_to_new.ELEGANT_CLIP_CONCURRENCY), which is exactly what fal allows a new
    # account; a second render would 429 against the first. A 429 here does not cost a
    # retry -- DeferJob rewinds the attempt counter -- but it costs a deferral cycle, and
    # a 429 that lands AFTER an accepted submit costs seventy cents. So one at a time
    # across the fleet, and the agents behind it WAIT VISIBLY, which is what the queue is
    # for.
    #
    # counts_toward_render_limit=False. The change the audit asked for, and the one that
    # keeps this feature from taxing the other two: see the field's documentation above.
    # A worker waiting on Kling still renders Classics.
    #
    # default_seconds=1020 (17 minutes) against the Dramatic's 300. A cold Elegant render
    # buys up to six clips two at a time -- three waves of two to six minutes -- then up to
    # seven stills, then runs ffmpeg over six 1080p shots. Fifteen minutes is the honest
    # middle of that and seventeen is the honest quote, because an ETA that is late is a
    # complaint and an ETA that is early is a second press of Generate. It is only the
    # fallback until real duration_seconds rows exist.
    #
    # min_quoted_seconds=420 (7 minutes). The rolling mean WILL mislead here in a way it
    # does not for the other types: a fully cached regenerate pays for no clips at all and
    # finishes in about a minute, while a cold render takes fifteen, so a mean over a mix
    # of the two describes neither. Rather than invent a second statistic, the quote has a
    # floor: whatever the mean says, an Elegant is never quoted at less than seven minutes.
    #
    # rate_limit_patience_seconds=1800. The Dramatic's 600 would be exhausted before a
    # single Elegant render had finished, so the first 429 would immediately give the
    # agent the plainer film. Half an hour is two renders' worth of waiting for capacity,
    # and every clip already bought is cached, so the waiting is free.
    #
    # queued_escape_seconds=3600. At one render at a time, three agents queued is 45
    # minutes of entirely healthy waiting -- past the global 30-minute escape hatch, which
    # would fail the third agent's job and tell them the rendering service was not picking
    # up work, which would be false. _queue_is_draining() already guards against that, but
    # it guards with a question about the whole table and the honest fix is a window sized
    # to this type's own duration.
    JOB_TYPE_ELEGANT: JobTypeConfig(
        per_process_limit=1, account_limit=1, default_seconds=1020,
        counts_toward_render_limit=False, queued_escape_seconds=3600,
        min_quoted_seconds=420, rate_limit_patience_seconds=1800),
}

# How many renders of ANY type one worker process may have in flight at once.
#
# WHY ONE IS THE CEILING, which is the part not to lose: a single render peaks at
# 1.6-1.9GB RSS counting its ffmpeg children (the measured table above). Four worker
# processes at one render each is already ~6.4-7.4GB, which is what the Railway instance
# is sized for. Two renders in one process would double that process's footprint, and
# video_renderer only hands out ONE render slot per process anyway -- so the second
# would sit inside a claimed job for RENDER_SLOT_WAIT_SECONDS before deferring,
# heartbeating all the while so nothing could reclaim it, while the agent's queue
# position quietly meant nothing. This makes the queue honour the ceiling when CHOOSING
# work rather than discovering it while rendering.
#
# It is a SEPARATE limit from the per-type ones because it is a limit on the SUM. The
# per-type limits cannot express it: two types registered at 1 each would let one worker
# claim one of each. That combination was real while the Signature tier existed
# (withdrawn 2026-10-21), was inert for a day with only 'classic' registered, and is
# LIVE AGAIN since 'dramatic' arrived on 2026-10-22 -- a worker may be rendering a
# Classic or a Dramatic, never one of each.
PER_PROCESS_RENDER_LIMIT = 1

# How many jobs that are NOT renders -- types with counts_toward_render_limit False, which
# today means the Elegant -- one worker may have in flight at once.
#
# A SEPARATE CEILING, because they are a different resource. A waiting Elegant costs this
# process one python thread, one heartbeat thread and two sockets to fal for a quarter of
# an hour; it does not cost the 1.6GB of ffmpeg that PER_PROCESS_RENDER_LIMIT exists to
# bound. But it is not free either, and "it is only waiting" is exactly the reasoning that
# would let a worker accumulate twenty of them, so it gets a number rather than no number.
#
# ONE, which with four workers is four -- and is therefore not the binding constraint
# anyway: the Elegant's account_limit of 1 means only one runs across the whole fleet. It
# is here so that if that fleet-wide cap is ever raised (once fal raises the account's
# concurrency with spend), the per-worker consequence is a deliberate decision rather
# than a discovery.
PER_PROCESS_WAITING_LIMIT = 1

# How often a running job proves it is still alive, and how long silence lasts before
# another worker may take the job. Six missed beats is deliberately generous: reclaiming
# a job that is merely slow costs a duplicate render, so the bias is towards patience.
#
# UNCHANGED FOR THE ELEGANT, AND THAT IS A DECISION RATHER THAN AN OVERSIGHT. The obvious
# worry about a quarter-hour job is that a 120-second silence window will reclaim it
# mid-render. It cannot: the beat comes from its OWN THREAD (_heartbeat_loop), not from
# the render, and the Elegant's long waits are time.sleep inside the render thread while
# fal generates -- which is exactly the shape the beater was built for, and the same shape
# as ffmpeg subprocess time. The beat also keeps ticking through the clip DOWNLOADS, which
# are read under slow_read's watchdog in the render thread.
#
# What the long job does change is volume: 45 beats per Elegant render instead of 5, each
# a PostgREST write. At one Elegant at a time across the fleet (account_limit=1) that is
# one write every 20 seconds, which is nothing. If the fleet-wide cap is ever raised, this
# is the number to look at again.
HEARTBEAT_SECONDS = 20
STALE_AFTER_SECONDS = 120

# Poll cadence. See "WHY POLLING" above for why these numbers, and for what the old
# flat two seconds cost on a metered plan.
POLL_SECONDS = 2.0
POLL_JITTER = 0.4          # +/- fraction, so restarting workers do not synchronise
POLL_SECONDS_BUSY = 5.0    # slower poll when this process has no free slots anyway

# How the interval ramps while the queue keeps coming back empty: (how many consecutive
# empty polls this rung covers, the interval to use on it). The last rung's count is
# None, meaning "and thereafter". Any claim, and any doorbell, puts us back on rung one.
#
# The shape is deliberate rather than a plain doubling. The first rung keeps the brisk
# two seconds for a few polls, so the moment just after a render finishes is still
# brisk; the middle rungs cover an agent working through a few listings in one sitting;
# the ceiling is only reached after about six minutes in which nothing at all happened,
# which is the state this app is in for most of the day. At the ceiling one worker asks
# twice a minute, so four workers ask eight times a minute.
POLL_IDLE_LADDER = ((3, 2.0), (10, 5.0), (20, 15.0), (None, 30.0))

SWEEP_SECONDS = 30.0       # stale-claim sweep cadence while there is work about
# ...and while there is not. The two sweeps are two more reads of the same table on the
# same timer, so leaving them at 30s would have left them as the LARGEST remaining
# source of requests once the claim poll backed off (about 23,000 a day against 11,500).
#
# Stretching them costs recovery time in exactly one case: a worker that dies mid-render
# leaves a 'running' row, which no claim poll can see, so only the stale sweep frees it.
# Worst case that job now waits STALE_AFTER_SECONDS + 90s instead of + 30s -- about a
# minute more, for one agent, in a situation where a render had already been lost. And
# when the sweep does requeue something it rings the doorbell, so the rest of the fleet
# takes it immediately instead of waiting out its own ramp. Everything else the sweeps
# do is measured in tens of minutes (QUEUED_ESCAPE_SECONDS) and cannot tell the
# difference.
SWEEP_SECONDS_IDLE = 90.0

# The in-container doorbell. enqueue() replaces this file; every waiting consumer reads
# it once a second, which costs no request, no egress and no log line. /tmp because that
# is what the container gives us and this is per-deploy state that may vanish at any
# time -- losing it only means falling back to the ramp. 0700, like the URA snapshot
# directory next door, so nothing else on the box can write our doorbell.
VIDEO_QUEUE_RUNTIME_DIR = os.environ.get("VIDEO_QUEUE_RUNTIME_DIR",
                                         "/tmp/nestlist_video_queue")
DOORBELL_READ_SECONDS = 1.0

# How long a job may sit in 'queued' untouched before we conclude nothing is draining
# the queue and set the listing free again.
#
# This exists because a queued job that nobody claims is otherwise IMMORTAL: the stale
# sweep only looks at 'running', and the partial unique index means the agent gets
# handed that same dead row every time they press Generate. The old in-process path had
# a 10-minute stale-lock escape; without this the queue would be a regression on it.
#
# Deliberately much longer than any plausible real wait -- at four concurrent Classic
# renders of ~100s, 30 minutes is roughly 70 jobs deep, far beyond anything this
# product will see -- and guarded further by _queue_is_draining(), so a genuinely deep
# but healthy queue is never mistaken for a dead one.
QUEUED_ESCAPE_SECONDS = float(os.environ.get("VIDEO_QUEUE_ESCAPE_SECONDS", "1800"))


def _escape_seconds(job_type):
    """The escape window for one job type.

    PER TYPE SINCE THE ELEGANT, and it had to become per type rather than stay one
    number. The global 30 minutes was sized against "four concurrent Classic renders of
    ~100s, roughly 70 jobs deep" -- a window that could not plausibly be filled by honest
    waiting. One Elegant render at a time at a quarter of an hour each fills it with
    THREE AGENTS: the third would be failed and told the rendering service was not
    picking up work, which would be a lie told to an agent whose video was simply third
    in line. So a type that runs for a quarter of an hour gets a window measured against
    its own duration.
    """
    cfg = JOB_TYPES.get(job_type)
    if cfg is not None and cfg.queued_escape_seconds:
        return float(cfg.queued_escape_seconds)
    return QUEUED_ESCAPE_SECONDS

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
    counter, so no amount of waiting for capacity can ever push a job into 'failed'. A
    type bound by an external service would raise it on that service's 429 for exactly
    the same reason."""


class SupersededError(Exception):
    """This job now belongs to another worker -- stop, and write nothing.

    Raised from inside a renderer at the points where it is about to do something the
    agent can actually see (store the video, update the listing). The job row is fenced
    against a late write, but the storage bucket and the listings row are not, so a
    renderer has to check ownership itself before touching either."""


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

    A job type with no registered renderer is never claimed, which is what lets a type
    be declared in JOB_TYPES before its renderer exists -- and what makes a leftover job
    row of a withdrawn type harmless: no worker will ever pick it up. main.py's startup
    registers 'classic'.
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


# ---------------------------------------------------------------------------
# Waking up. PostgREST cannot wake us, so the poll interval backs off when the queue is
# empty -- and these two doorbells are what stop that backoff costing an agent anything.
# See "WHY POLLING" in the module docstring. BOTH ARE BEST EFFORT BY DESIGN: a ring that
# never arrives costs one rung of the ladder, never a lost job, because the poll that
# follows finds the row regardless. Nothing here may raise into an enqueue.
# ---------------------------------------------------------------------------
_wake_loop = None            # the event loop run_consumer() is running on
_wake_event = None           # set from any thread to cut an idle sleep short
_doorbell_dir = None
_doorbell_dir_checked = False


def _runtime_dir():
    """The private per-container directory for the doorbell, or None if unusable.

    Decided once: if /tmp is read-only, or the directory is somebody else's, there is no
    point asking again on every poll."""
    global _doorbell_dir, _doorbell_dir_checked
    if _doorbell_dir_checked:
        return _doorbell_dir
    _doorbell_dir_checked = True
    try:
        os.makedirs(VIDEO_QUEUE_RUNTIME_DIR, mode=0o700, exist_ok=True)
        st = os.stat(VIDEO_QUEUE_RUNTIME_DIR)
        if (os.path.isdir(VIDEO_QUEUE_RUNTIME_DIR)
                and (not hasattr(os, "getuid") or st.st_uid == os.getuid())):
            _doorbell_dir = VIDEO_QUEUE_RUNTIME_DIR
    except Exception:
        _doorbell_dir = None
    if _doorbell_dir is None:
        logger.info(
            "video_jobs: no usable doorbell directory (%s) -- a render enqueued by one "
            "worker is picked up by its siblings on the poll interval instead of at "
            "once. Nothing breaks; pickup is simply slower when the enqueuing worker "
            "is already busy.", VIDEO_QUEUE_RUNTIME_DIR)
    return _doorbell_dir


def _doorbell_path():
    d = _runtime_dir()
    return os.path.join(d, "enqueued.doorbell") if d else None


def _doorbell_token():
    """What the doorbell currently reads, or None when there is no readable doorbell."""
    path = _doorbell_path()
    if not path:
        return None
    try:
        with open(path) as fh:
            return fh.read(64)
    except Exception:
        return None


def _wake_local_consumer():
    """Cut THIS process's consumer sleep short. Safe from any thread; never raises."""
    loop, event = _wake_loop, _wake_event
    if loop is None or event is None:
        return
    try:
        loop.call_soon_threadsafe(event.set)
    except Exception:
        # The loop is closed (shutdown) or never ran. The next poll finds the row.
        pass


def ring_doorbell():
    """Tell every consumer in this container that there is work, right now.

    The write is atomic -- temp file, then rename -- so a reader always sees either the
    old token or the new one, never a half-written file. The token is a nanosecond clock
    reading rather than the file's mtime, because mtime granularity varies by
    filesystem and a ring that looks identical to the last one is a ring that is
    missed."""
    _wake_local_consumer()
    path = _doorbell_path()
    if not path:
        return
    tmp = "%s.%d.%s.tmp" % (path, os.getpid(), uuid.uuid4().hex[:8])
    try:
        with open(tmp, "w") as fh:
            fh.write(str(time.time_ns()))
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


# The reason the probe must write, not just read, and what it leaves behind.
PROBE_LISTING_ID = "__nestlist_startup_probe__"


def probe():
    """Detect whether the video_jobs table exists AND this key may actually USE it.

    A SELECT is not sufficient, and the earlier version of this function was wrong to
    rely on one. With RLS enabled and no policies -- exactly what the migration sets up
    -- a key that is subject to RLS does not get an error back. Default-deny FILTERS
    ROWS; it does not revoke the privilege. So `select id limit 1` returns HTTP 200 and
    an empty array, which is indistinguishable from an empty table. The probe would
    pass, available() would go True, and then every single enqueue would fail 42501 and
    fall back to an inline render. Agents would still get their videos, and the exact
    problem the queue exists to fix would carry on happening invisibly.

    So the probe proves the operation the queue actually depends on: an INSERT, followed
    by a DELETE. The service key sails through; a key subject to RLS is refused, which
    is the answer we need. The canary is written in a terminal state under a reserved
    listing id, so it is invisible to every query that matters -- it is not 'active', it
    is not 'running', and it is not 'done', so it cannot appear in a queue position, a
    concurrency count, or the rolling duration average. It is deleted immediately, and
    prune_finished() clears any that a crash leaves behind."""
    global _table_available
    canary_id = None
    try:
        _exec(lambda db: db.table(TABLE).select("id").limit(1), what="video_jobs probe read")
        now = _iso(_now())
        row = {
            "job_type": JOB_TYPE_CLASSIC,
            "listing_id": PROBE_LISTING_ID,
            "agent_id": PROBE_LISTING_ID,
            # Terminal on arrival: never claimable, never counted, never averaged.
            "status": STATUS_FAILED,
            "payload": {},
            "attempts": 0,
            "error": "startup write probe",
            "run_after": now,
            "created_at": now,
            "updated_at": now,
            "finished_at": now,
        }
        res = _exec(lambda db: db.table(TABLE).insert(row),
                    what="video_jobs probe write", idempotent=False)
        if res.data:
            canary_id = res.data[0].get("id")
        _table_available = True
        logger.info("video_jobs table present and writable -- renders run on the "
                    "durable queue (worker id %s)", WORKER_ID)
    except Exception as e:
        _table_available = False
        logger.warning(
            "video_jobs table NOT usable (%s: %s) -- video renders fall back to the "
            "in-process path, which does not survive a redeploy. Either the migration "
            "in docs/video-jobs-migration.sql has not been run, or SUPABASE_KEY is not "
            "the service key and row level security is refusing the write.",
            type(e).__name__, e)
    finally:
        if canary_id:
            try:
                _exec(lambda db: db.table(TABLE).delete().eq("id", canary_id),
                      what="video_jobs probe cleanup")
            except Exception as e:
                # Harmless: it is terminal, invisible to every live query, and pruned.
                logger.warning("video_jobs: could not remove the startup probe row (%s)", e)
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
        # Lower-cased because these are TEXT columns carrying uuids. Postgres compares
        # uuid values case-insensitively, but the partial unique index that stops a
        # double-click becoming a double render compares TEXT -- so two spellings of one
        # uuid would be two different keys. Callers should pass the canonical id from
        # the row; this is the second line of defence.
        "listing_id": str(listing_id).strip().lower(),
        "agent_id": str(agent_id).strip().lower(),
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
            # Wake this container's consumers now rather than let them wait out the idle
            # ramp. This is what keeps the backoff from costing the agent who pressed
            # Generate anything at all -- see "WHY POLLING".
            ring_doorbell()
            return res.data[0], True
    except Exception as e:
        if not _is_unique_violation(e):
            raise
        logger.info("video_jobs: %s job for listing %s already queued/running; "
                    "reusing it", job_type, listing_id)

    existing = find_active(job_type, listing_id)
    if existing:
        if existing.get("status") == STATUS_QUEUED:
            # Usually a double-clicked Generate, where the ring is harmless. But this is
            # also the path a deployment that does not return the inserted
            # representation takes after a SUCCESSFUL insert, and that row does need
            # somebody woken for it.
            ring_doorbell()
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
        .eq("listing_id", str(listing_id).strip().lower()).eq("job_type", job_type)
        .in_("status", list(ACTIVE_STATUSES))
        .order("created_at", desc=True).limit(1),
        what="video_jobs find_active")
    return (res.data or [None])[0]


def find_latest_done(job_type, listing_id):
    """The most recently finished job of this type for the listing, or None. Lets
    /video-status report a finished render's degradations after the job has left the
    active set (find_active only ever returns queued/running jobs)."""
    res = _exec(
        lambda db: db.table(TABLE).select("*")
        .eq("listing_id", str(listing_id).strip().lower()).eq("job_type", job_type)
        .eq("status", STATUS_DONE)
        .order("finished_at", desc=True).limit(1),
        what="video_jobs find_latest_done")
    return (res.data or [None])[0]


def get_job(job_id):
    res = _exec(lambda db: db.table(TABLE).select("*").eq("id", str(job_id)).limit(1),
                what="video_jobs get_job")
    return (res.data or [None])[0]


# ---------------------------------------------------------------------------
# Claiming
# ---------------------------------------------------------------------------
# How many queued rows one poll reads. Per type, capped overall, because the cap is
# there to bound the bytes on the wire and a fairness order needs candidates of more
# than just the first type to order.
CLAIM_CANDIDATES_PER_TYPE = 10
CLAIM_CANDIDATES_MAX = 30


def claim_next(job_type):
    """Take the oldest runnable job of this type, or return None.

    Kept as the single-type spelling, used by callers and tests that mean one type.
    claim_next_any() is what the consumer loop uses, and is the only implementation."""
    return claim_next_any([job_type])


def claim_next_any(job_types):
    """Take the oldest runnable job of ANY of these types, in ONE read.

    `job_types` is in PREFERENCE ORDER -- the consumer's round robin -- and that order
    is what keeps one type from starving another now the reads have been collapsed into
    one. It used to be one read per type, issued in round-robin order, so the order of
    the READS was the fairness. The order of the CANDIDATES is the fairness now: a
    single `job_type=in.(...)` read comes back oldest-first, and the candidates are then
    STABLY re-sorted by preference, so every job of the preferred type is tried before
    any job of the next one and each group keeps its oldest-first order. Same outcome,
    ONE request where there were three today and five with the Elegant switched on --
    see "WHY POLLING" for what the old cadence cost.

    Candidates are a hint and nothing more. The SELECT may be stale the instant it
    returns, and that is fine, because nothing is decided by it: the conditional UPDATE
    in _try_claim decides, atomically (see the module docstring). Losing a race just
    means trying the next candidate.

    ONLY FOUR COLUMNS ARE READ. A queued row carries its render payload -- chosen photo
    urls, music track -- and this read happens every few seconds for the life of the
    process, so there is no reason to pull that down the wire. The claim's own UPDATE
    returns the full row, which is what the caller gets back.
    """
    order = [t for t in job_types if t in JOB_TYPES]
    if not order:
        return None

    now = _iso(_now())
    limit = min(CLAIM_CANDIDATES_MAX, CLAIM_CANDIDATES_PER_TYPE * len(order))
    res = _exec(
        lambda db: db.table(TABLE).select("id,job_type,attempts,started_at")
        .in_("job_type", order).eq("status", STATUS_QUEUED)
        .lte("run_after", now)
        .order("created_at").limit(limit),
        what="video_jobs claim candidates")

    candidates = list(res.data or [])
    if len(order) > 1 and candidates:
        rank = {job_type: i for i, job_type in enumerate(order)}
        # Stable, so the oldest-first order the database returned survives within each
        # type's group.
        candidates.sort(key=lambda c: rank.get(c.get("job_type"), len(order)))

    # Fleet-wide gate, for types bound by an external service rather than by our CPU.
    # ASKED LAZILY -- only once a candidate of that type has actually turned up, and at
    # most once per type per poll. It is a second request, and an empty queue must not
    # pay for it: this count alone was one of the three requests this loop used to make
    # every two seconds, and two of the five with the Elegant switched on. Still only a
    # cheap pre-filter -- confirm_account_slot() after the claim is the authority,
    # because a count read can always be stale.
    gate = {}
    for candidate in candidates:
        job_type = candidate.get("job_type")
        cfg = JOB_TYPES.get(job_type)
        if cfg is None:
            # A leftover row of a withdrawn type cannot appear here (the read filters on
            # the types we were asked for) but a type removed from JOB_TYPES mid-deploy
            # could. Never claim what we cannot configure.
            continue
        if cfg.account_limit is not None:
            if job_type not in gate:
                gate[job_type] = _running_count(job_type) < cfg.account_limit
            if not gate[job_type]:
                # This type is at its fleet-wide limit. Carry on down the list: another
                # type's job must not wait behind it.
                continue
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
    # run_after is in the predicate, not just in the candidate SELECT: between the two,
    # another worker may have deferred this job into a backoff window, and claiming it
    # inside that window would defeat the backoff.
    claim_cutoff = _iso(now)
    try:
        res = _exec(
            lambda db: db.table(TABLE).update(patch)
            .eq("id", job_id).eq("status", STATUS_QUEUED).eq("attempts", attempts)
            .lte("run_after", claim_cutoff),
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
def _fenced_update(job, patch, what, require_status=STATUS_RUNNING):
    """Write only if we still own this job, at the attempt we claimed, in the state we
    expect to find it.

    Returns True if the write landed. False means the job is no longer ours to write:
    another worker reclaimed it after our claim went stale, or it has already reached a
    terminal state. Either way whatever we were about to record is out of date and must
    be dropped on the floor.

    require_status is what stops a shutdown from RESURRECTING A FINISHED JOB. complete()
    leaves claimed_by set (deliberately -- it is the audit trail of who rendered it), so
    without a status predicate the shutdown hook's defer() would match a job that had
    just been marked done, push it back to 'queued' with its attempt rewound, and hand
    another worker a job that was already delivered. The agent would watch a finished
    video revert to a spinner. Pass None only for a write that is genuinely valid in any
    state."""
    job_id = job["id"]
    attempt = int(job.get("attempts") or 0)
    body = dict(patch)
    body["updated_at"] = _iso(_now())

    def _build(db):
        q = (db.table(TABLE).update(body)
             .eq("id", job_id).eq("claimed_by", WORKER_ID).eq("attempts", attempt))
        if require_status is not None:
            q = q.eq("status", require_status)
        return q

    try:
        res = _exec(_build, what=what)
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
    if require_status is not None and "status" not in body:
        # Nothing in the patch moves the status, so the row must still be in the state
        # we required for the write to have been ours.
        if fresh.get("status") != require_status:
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


def record_degradations(job, degradations):
    """Write a running job's degradations onto its row BEFORE the listing is flipped to
    'done', so there is no instant at which the agent can see a finished video whose
    notes are not yet readable. Fenced like every other job write; complete() writes the
    same list again at the end."""
    return _fenced_update(job, {"degradations": list(degradations or [])},
                          "video_jobs record degradations")


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


def _queue_is_draining(window_seconds):
    """Has ANY job, of any type, actually finished recently?

    This is what separates "the queue is deep" from "the queue is dead". A backlog with
    workers chewing through it keeps producing finished jobs; a backlog with no consumer
    produces none. Only the second one justifies failing an agent's job.

    DELIBERATELY NOT PER TYPE. A type can be starved by a busier one, and a starved type
    finishes nothing of its own -- so asking "has anything of THIS type finished" answers
    "the rendering service is dead" and fails the agent's job with a message that is
    simply untrue, while the failure then counts as that type draining and leaves the
    jobs behind it stuck for another window. The question that matters is whether
    anything at all is being drained: if it is, a long-queued job has not had its turn
    yet, which is a wait and not a breakage."""
    since = _iso(_now() - timedelta(seconds=window_seconds))
    try:
        res = _exec(
            lambda db: db.table(TABLE).select("id")
            .in_("status", list(TERMINAL_STATUSES))
            .gt("finished_at", since).limit(1),
            what="video_jobs draining check")
        return bool(res.data)
    except Exception:
        # Cannot tell -- assume healthy. Wrongly failing an agent's job is worse than
        # leaving a stuck one for the next sweep to catch.
        return True


def sweep_abandoned_queue():
    """Release listings held hostage by a queued job that nothing is ever going to run.

    The failure this recovers from is a misconfiguration, not a crash: consumers turned
    off everywhere (VIDEO_QUEUE_CONSUMER=0 on every process) while the endpoint happily
    keeps enqueuing, or a deploy where no process ends up draining. Without an escape
    the agent is stuck forever with 'position 1, about a minute' and no way out short of
    someone running SQL.

    Failing the job is the right recovery rather than silently deleting it: it clears
    the partial unique index, so the agent's next press of Generate works, and it leaves
    a row explaining what happened."""
    # The SHORTEST window of any registered type decides which rows are worth reading;
    # each row is then measured against its OWN type's window below. Reading on the
    # shortest means one query rather than one per type, and no row is acted on early.
    windows = [_escape_seconds(t) for t in JOB_TYPES] or [QUEUED_ESCAPE_SECONDS]
    cutoff = _iso(_now() - timedelta(seconds=min(windows)))
    try:
        res = _exec(
            lambda db: db.table(TABLE).select("*")
            .eq("status", STATUS_QUEUED).lt("updated_at", cutoff).limit(50),
            what="video_jobs abandoned queue select")
    except Exception as e:
        logger.warning("video_jobs: abandoned-queue sweep could not read (%s)", e)
        return 0

    rows = res.data or []
    if not rows:
        return 0

    # One question, asked once, about the whole table: is anything being drained at all?
    # If it is, every one of these rows is waiting rather than abandoned -- including a
    # row of a type that has been waiting behind a busier one.
    #
    # ASKED OVER THE LONGEST WINDOW, not the shortest. The question is "is this queue
    # dead", and the slowest type is the one that proves it is alive least often: at a
    # quarter of an hour an Elegant render finishes four times an hour, so a 30-minute
    # look-back can legitimately see nothing finish at all while one is in progress.
    if _queue_is_draining(max(windows)):
        return 0

    freed = 0
    for job in rows:
        job_type = job.get("job_type") or JOB_TYPE_CLASSIC
        waited = _queued_for_seconds(job)
        if waited is not None and waited < _escape_seconds(job_type):
            # Not this type's turn to be given up on yet.
            continue
        attempt = int(job.get("attempts") or 0)
        now = _now()
        patch = {
            "status": STATUS_FAILED,
            "error": ("This video could not be started -- the rendering service was not "
                      "picking up work. Please try generating it again."),
            "finished_at": _iso(now),
            "updated_at": _iso(now),
        }
        try:
            out = _exec(
                lambda db: db.table(TABLE).update(patch)
                .eq("id", job["id"]).eq("status", STATUS_QUEUED).eq("attempts", attempt),
                what="video_jobs abandoned queue release")
        except Exception as e:
            if _is_serialization_failure(e):
                continue
            logger.warning("video_jobs: could not release stuck job %s (%s)", job["id"], e)
            continue
        if out.data or _reclaim_landed(job["id"], attempt, STATUS_FAILED):
            freed += 1
            logger.error(
                "video_jobs: %s job %s sat queued for over %.0f minutes with nothing "
                "draining the queue -- failed so the listing is not locked out. Check "
                "that at least one process has VIDEO_QUEUE_CONSUMER enabled.",
                job_type, job["id"], _escape_seconds(job_type) / 60.0)
            _notify_final_failure(job, patch["error"])
    return freed


def _queued_for_seconds(job):
    """How long this row has sat untouched, or None when the timestamp is unreadable.

    None means "do not use age to decide anything", and every caller treats it as the
    IMPATIENT answer here on purpose: this function exists to stop a healthy slow job
    being failed early, and a row whose updated_at we cannot read has already passed the
    shortest window (that is how the select found it), so falling through to the old
    behaviour is the conservative choice rather than a new one.
    """
    raw = (job or {}).get("updated_at")
    if not raw:
        return None
    try:
        stamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return max(0.0, (_now() - stamp).total_seconds())
    except Exception:
        return None


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
    # .get(), not [], for the same reason describe() uses it: a row left over from a
    # withdrawn job type must never be able to raise a KeyError at a reader. Describing
    # such a row with the Classic's fallback is a slightly wrong ETA for a job nothing
    # will ever run; indexing would be a 500 on an agent's status poll.
    cfg = JOB_TYPES.get(job_type) or JOB_TYPES[JOB_TYPE_CLASSIC]
    fallback = cfg.default_seconds
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


def _quoted_duration(job_type):
    """What we are willing to TELL an agent one of these takes.

    The rolling mean, with the type's own floor under it. THE FLOOR IS NOT A FUDGE: for a
    type whose cached regenerate is one minute and whose cold render is fifteen, the mean
    over recent jobs describes neither of them, and the error that matters is only in one
    direction. An ETA that is late costs a complaint; an ETA that is early costs a second
    press of Generate -- which, for a paid feature, costs money as well as patience.
    Every existing type has a floor of zero, so this is the mean exactly as before.
    """
    cfg = JOB_TYPES.get(job_type) or JOB_TYPES[JOB_TYPE_CLASSIC]
    return max(_average_duration(job_type), float(cfg.min_quoted_seconds or 0))


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
        out["eta_seconds"] = int(_quoted_duration(job_type))
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
        # Running jobs count as ahead -- EXCEPT this one.
        #
        # `job` is the row as it was when it was enqueued, and a worker can claim it
        # between that insert and this call. When that happens the job is already
        # RUNNING, so counting running jobs by type counts the job against itself: the
        # agent who is first in the queue is told "2nd in line" and quoted roughly
        # double the real wait. Seen in production on the first two renders an agent
        # ever generated, both of which said "2nd in line, about 3 minutes" with an
        # empty queue behind them.
        #
        # Excluding by id rather than re-reading the status also covers the simpler
        # race where it is claimed a moment after we read it. Uncached deliberately:
        # describe() runs once per generate request, not per poll, so the load this
        # cache exists to prevent does not arise here, and a two-second-stale count is
        # exactly what produces the off-by-one.
        running_others = _exec(
            lambda db: db.table(TABLE).select("id", count="exact")
            .eq("job_type", job_type).eq("status", STATUS_RUNNING)
            .neq("id", job["id"]),
            what="video_jobs position running")
        ahead += int(getattr(running_others, "count", None)
                     or len(running_others.data or []))
    except Exception:
        return out

    concurrency = cfg.account_limit or (cfg.per_process_limit * _worker_count())
    concurrency = max(1, concurrency)
    average = _quoted_duration(job_type)
    # Rounded up: the honest answer to "when will mine start" is the end of the batch
    # it sits behind, not the average across it.
    waves_ahead = (ahead + concurrency - 1) // concurrency
    out["queue_position"] = ahead + 1
    out["eta_seconds"] = int(waves_ahead * average + average)
    return out


def describe_active_for_agent(agent_id, job_type=None):
    """One round trip for everything this agent currently has in flight.

    Used to decorate the My Listings payload. An agent with nothing rendering costs one
    indexed select that returns nothing, which is the common case by a wide margin.

    `job_type` narrows the result to one type, and the caller should say which. The
    result is keyed by LISTING, and the unique index is over listing + type -- so if a
    second render type is ever added, one listing could legitimately have two jobs in
    flight and the last row read would silently win, showing one type's queue position
    against the other. Omitting it keeps the original behaviour."""
    if not available():
        return {}
    try:
        res = _exec(
            lambda db: db.table(TABLE).select("*")
            .eq("agent_id", str(agent_id).strip().lower()).in_("status", list(ACTIVE_STATUSES))
            .order("created_at").limit(20),
            what="video_jobs active for agent")
    except Exception as e:
        logger.warning("video_jobs: could not read active jobs for an agent (%s)", e)
        return {}
    out = {}
    for job in (res.data or []):
        if job_type is not None and (job.get("job_type") or JOB_TYPE_CLASSIC) != job_type:
            continue
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
        # Stop beating BEFORE the terminal write. The heartbeat fence now requires the
        # job to still be 'running', so a beat landing after complete() would report a
        # spurious takeover; and a render that is finished has nothing left to prove.
        stop_event.set()
        if superseded_event.is_set():
            logger.warning("video_jobs: finished job %s but another worker owns it now; "
                           "not recording our result", job_id)
            return
        complete(job, result_url, degradations, time.monotonic() - started)
    except SupersededError:
        # The renderer noticed mid-flight that the job had been handed to someone else
        # and stopped before writing anything the agent could see. Nothing to record --
        # the new owner's render is the one that counts.
        logger.warning("video_jobs: job %s abandoned mid-render; another worker owns it",
                       job_id)
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


def claimable_types(order, running):
    """Which of `order` this process may claim one more of, in the order given.

    TWO CEILINGS, NOT ONE, AND THE SPLIT IS THE ELEGANT'S DOING.

    Admission used to be one check on the SUM of everything in flight, and it stopped
    looking at any further type as soon as that sum was reached: one render in this
    process meant this process took no more work of any kind. That was exactly right
    while every type was ffmpeg from start to finish, because the thing being rationed
    was 1.6GB of memory and a cpu.

    It is wrong for a type that is asleep waiting on somebody else's API. An Elegant
    render is 10-15 minutes of which about one is ffmpeg; counting it as a render would
    take a quarter of this worker's Classic throughput offline for a quarter of an hour
    to protect a cpu nobody is using -- and, the other way round, a worker rendering a
    Classic would refuse to even START an Elegant's waiting, which is the work that
    actually takes the time.

    So renders are counted against renders and waiters against waiters, and a full
    render slot cannot hide a type that needs no render slot. The memory guarantee the
    old single check was really providing is still there and is stronger, because it is
    enforced where the memory is spent: video_renderer._RENDER_SLOTS is a per-process
    semaphore of ONE that both renderers hold around their ffmpeg phase, so two ffmpeg
    trees in one process remain impossible however many jobs this worker holds.

    A PURE FUNCTION, deliberately, so the one decision in this module that changed shape
    for the Elegant can be tested without an event loop, a database or a render.
    """
    out = []
    for job_type in order:
        cfg = JOB_TYPES.get(job_type)
        if cfg is None:
            continue
        if running.get(job_type, 0) >= cfg.per_process_limit:
            continue
        in_flight = sum(
            count for kind, count in running.items()
            if kind in JOB_TYPES
            and JOB_TYPES[kind].counts_toward_render_limit
            == cfg.counts_toward_render_limit)
        ceiling = (PER_PROCESS_RENDER_LIMIT if cfg.counts_toward_render_limit
                   else PER_PROCESS_WAITING_LIMIT)
        if in_flight >= ceiling:
            continue
        out.append(job_type)
    return out


def _idle_interval(empty_polls):
    """Where `empty_polls` consecutive empty reads puts us on POLL_IDLE_LADDER.

    A pure function of one number, so the ramp can be read off and tested without a
    loop, a clock or a database."""
    seen = 0
    for count, interval in POLL_IDLE_LADDER:
        if count is None:
            return interval
        seen += count
        if empty_polls < seen:
            return interval
    return POLL_IDLE_LADDER[-1][1]


def _poll_delay(has_capacity, empty_polls=0):
    """How long to wait before asking the queue again.

    Brisk while there is somewhere to put work AND work keeps turning up; backing off up
    the ladder while the queue keeps coming back empty; a flat slower cadence when this
    process has no free slot, which costs nothing anyway because a worker with no free
    slot does not read the queue at all."""
    if not has_capacity:
        base = POLL_SECONDS_BUSY
    else:
        base = max(POLL_SECONDS, _idle_interval(empty_polls))
    return base * (1.0 - POLL_JITTER + random.random() * 2 * POLL_JITTER)


async def _wait_for_work(delay, doorbell_seen):
    """Sleep up to `delay` seconds, but come back the instant work appears.

    Returns True when a doorbell rang -- the caller then drops back to the brisk rung --
    and False when the wait simply ran out.

    The wait is taken in short hops, for three reasons. The in-container doorbell is a
    FILE, which has to be read rather than waited on. Shutdown has to be noticed in
    about a second rather than at the end of a 30-second rung, or a redeploy would hold
    in-flight jobs 'running' for that much longer. And a hop costs nothing: no request,
    no egress, no log line, about four file reads a second across the whole container.
    """
    event = _wake_event
    deadline = time.monotonic() + max(0.0, delay)
    while not _shutting_down:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        hop = min(DOORBELL_READ_SECONDS, remaining)
        if event is None:
            await asyncio.sleep(hop)
        else:
            try:
                await asyncio.wait_for(event.wait(), timeout=hop)
                return True
            except asyncio.TimeoutError:
                pass
        token = _doorbell_token()
        if token is not None and token != doorbell_seen:
            return True
    return False


async def run_consumer():
    """Poll for work and run it. One of these per uvicorn worker.

    Runs in the event loop but never blocks it: the claim, the render and every database
    call go out to threads. The per-type local limits are counted here, on the loop, so
    the counting itself needs no lock.

    NOTE ON WHERE THIS RUNS -- AND A WARNING. Nothing about the queue requires the
    consumer to live inside a web worker; it only needs a process with database access.
    Moving renders off the web workers is worth doing before agent numbers grow, because
    an ffmpeg render inside a web worker competes for CPU with buyer-facing page
    requests. (CPU, not memory: the RSS ceiling quoted in video_renderer.py is measured
    against a limit far below Railway's actual one.)

    DO NOT attempt that split by setting VIDEO_QUEUE_CONSUMER=0 today. There is no
    worker entrypoint in this repo yet, so turning the consumer off on the web workers
    turns it off EVERYWHERE while generate-video happily keeps enqueuing -- every video
    on the platform would stop. The split needs a `video_jobs_worker` module (import the
    app module for its db_execute and renderers, configure, probe, run this loop) that
    is written and tested first, and only then a Procfile `worker:` line plus the env
    var. sweep_abandoned_queue() exists to make that mistake recoverable rather than
    permanent, and startup logs a warning when a process is enqueuing without consuming
    -- but recovery still costs every agent in flight their render."""
    if os.environ.get("VIDEO_QUEUE_CONSUMER", "1") not in ("1", "true", "True"):
        logger.warning(
            "video_jobs: consumer DISABLED on this process by VIDEO_QUEUE_CONSUMER. "
            "This process will still accept video requests and write them to the queue, "
            "so unless another process is draining it, no video will ever be rendered. "
            "There is no separate worker entrypoint in this repo yet -- if you did not "
            "deliberately build one, unset this variable.")
        return
    if not available():
        return

    global _wake_loop, _wake_event

    running = {}
    tasks = set()
    last_sweep = 0.0
    last_prune = 0.0
    # Which registered type gets first refusal on the next pass. Advanced ONLY when a
    # job is actually claimed, so the types take turns; see the loop below.
    rr_start = 0
    # How many reads in a row have come back with nothing. Drives the idle ladder.
    empty_polls = 0
    # The in-process doorbell: enqueue() sets this from the request's thread, which cuts
    # the sleep below short on the one worker most likely to have a free render slot.
    _wake_event = asyncio.Event()
    _wake_loop = asyncio.get_running_loop()
    _runtime_dir()      # decide the cross-worker doorbell once, and log it if unusable
    logger.info("video_jobs: consumer started (worker %s)", WORKER_ID)

    while not _shutting_down:
        picked = False
        polled = False
        has_capacity = True
        # Cleared BEFORE the read, and the doorbell token taken BEFORE it too, so a
        # video enqueued WHILE this read is in flight still cuts the sleep that follows
        # short instead of falling down the gap between the two.
        _wake_event.clear()
        doorbell = _doorbell_token()
        try:
            now = time.monotonic()
            # The sweeps are reads of this same table on their own timer, so they back
            # off with everything else once nothing has turned up for a few polls. See
            # SWEEP_SECONDS_IDLE for the one recovery case that pays for it.
            sweep_every = (SWEEP_SECONDS_IDLE
                           if empty_polls >= (POLL_IDLE_LADDER[0][0] or 1)
                           else SWEEP_SECONDS)
            if now - last_sweep > sweep_every * (0.7 + random.random() * 0.6):
                last_sweep = now
                reclaimed = await asyncio.to_thread(sweep_stale)
                await asyncio.to_thread(sweep_abandoned_queue)
                if reclaimed:
                    # Those rows are queued again. Ring, so whichever worker has a free
                    # slot takes them now instead of waiting out its own ramp -- the
                    # agent whose render was lost has already waited once.
                    ring_doorbell()
            if now - last_prune > PRUNE_EVERY_SECONDS:
                last_prune = now
                await asyncio.to_thread(prune_finished)

            # TAKE TURNS, so no type can starve another.
            #
            # WHY THIS EXISTS, because with one type registered it is inert and looks
            # pointless: the loop used to run in alphabetical order, which with
            # PER_PROCESS_RENDER_LIMIT = 1 is absolute priority and not a queue at all
            # -- it breaks as soon as it claims one job, so the type that sorts first
            # took every slot and the other was never claimed while any of the first
            # remained. It then got worse than waiting: the abandoned-queue sweep
            # eventually failed the starved job and told the agent the rendering service
            # was not picking up work, which was false. (Measured on two real types; the
            # second was the Signature tier, withdrawn 2026-10-21.)
            #
            # Round-robin instead: whichever type was served last goes to the back, and
            # rr_start only moves when something is actually claimed, so a type with an
            # empty queue never costs another its turn. With one type registered this is
            # a single-element loop and behaves exactly as it did before -- kept so that
            # adding a second type does not reintroduce the starvation.
            #
            # THE ORDER IS NOW PASSED TO ONE READ rather than deciding the order of
            # several. claim_next_any() applies it to the candidates of a single
            # `job_type=in.(...)` query, which is the same fairness for one request
            # instead of one per type; see "WHY POLLING".
            types = sorted(_RENDERERS)
            rr_start %= max(1, len(types))
            order = types[rr_start:] + types[:rr_start]
            # Which types this process may take one more of. See claimable_types: a full
            # render slot no longer stops this worker starting a job that only waits on
            # somebody else's API, and a waiting job no longer stops it rendering.
            #
            # RE-ASKED AFTER EVERY CLAIM, against the CURRENT `running`, rather than once
            # for the whole pass. Claiming a job changes what else this process may take
            # -- a worker that has just claimed a Classic must not then also claim a
            # Dramatic -- and a list computed before the first claim would not know that.
            claimable = claimable_types(order, running)
            has_capacity = bool(claimable)
            # Bounded by what one process is allowed to hold at once, so a pass can
            # never turn into an unbounded claiming spree however the limits are retuned.
            claims_left = max(1, PER_PROCESS_RENDER_LIMIT + PER_PROCESS_WAITING_LIMIT)
            while claimable and claims_left and not _shutting_down:
                polled = True
                claims_left -= 1
                job = await asyncio.to_thread(claim_next_any, claimable)
                if job is None:
                    break
                job_type = job.get("job_type")
                picked = True
                if job_type in types:
                    rr_start = types.index(job_type) + 1
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
                claimable = claimable_types(order, running)

            if picked:
                # Work is about: back to the brisk rung at once.
                empty_polls = 0
            elif polled:
                empty_polls += 1
            # A pass that never READ the queue -- no free slot -- leaves the ladder
            # exactly where it was. Being busy is not evidence that the queue is empty,
            # and a worker must not come out of a render to find itself on the
            # 30-second rung.
        except Exception as e:
            # The consumer loop must never die: if it does, this worker stops taking
            # work for the life of the process and nothing says so.
            #
            # The ladder is deliberately left ALONE here. A failed poll is not an empty
            # queue -- backing off on the strength of an error would slow pickup exactly
            # when the database is already unhappy -- and it is not a claim either, so
            # it does not reset the ramp.
            logger.warning("video_jobs: consumer poll failed (%s: %s)", type(e).__name__, e)
            has_capacity = True

        if await _wait_for_work(_poll_delay(has_capacity, empty_polls), doorbell):
            # Somebody rang: a video has just been enqueued, or a sweep has just put one
            # back. Come back to the brisk rung immediately.
            empty_polls = 0


def begin_shutdown():
    global _shutting_down
    _shutting_down = True
