"""Verification for the durable video render queue.

Each simulated worker is a SEPARATE import of video_jobs.py, so each gets its own
WORKER_ID, its own local state and its own registry -- the same isolation four uvicorn
worker processes have. All of them are pointed at one shared FakeDB, which is the only
thing they have in common, exactly as in production.
"""
import importlib.util
import os
import random
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
from fake_postgrest import FakeDB, make_db_execute

MODULE_PATH = os.path.join(os.path.dirname(_HERE), "video_jobs.py")

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def load_worker(db, index):
    spec = importlib.util.spec_from_file_location("video_jobs_w%d" % index, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.configure(make_db_execute(db))
    module._table_available = True
    return module


def iso(dt):
    return dt.isoformat()


def now():
    return datetime.now(timezone.utc)


# ===========================================================================
print("\n[1] Two workers cannot claim the same job")
# ===========================================================================
db = FakeDB()
workers = [load_worker(db, i) for i in range(8)]
workers[0].enqueue(workers[0].JOB_TYPE_CLASSIC, "listing-solo", "agent-1", {"photo_index": 0})

winners = []
winners_lock = threading.Lock()
barrier = threading.Barrier(len(workers))


def race(worker):
    barrier.wait()
    job = worker.claim_next(worker.JOB_TYPE_CLASSIC)
    if job is not None:
        with winners_lock:
            winners.append((worker.WORKER_ID, job["id"], job["attempts"]))


threads = [threading.Thread(target=race, args=(w,)) for w in workers]
for t in threads:
    t.start()
for t in threads:
    t.join()

check("exactly one of 8 workers claimed the job", len(winners) == 1,
      "winners=%d" % len(winners))
row = db.rows[0]
check("the row shows exactly one holder and attempts == 1",
      row["status"] == "running" and row["attempts"] == 1 and row["claimed_by"] == winners[0][0],
      "status=%s attempts=%s" % (row["status"], row["attempts"]))

# Repeat the race many times over fresh jobs to make sure the single winner was not luck.
multi_ok = True
for trial in range(40):
    db2 = FakeDB()
    ws = [load_worker(db2, 100 + i) for i in range(6)]
    ws[0].enqueue(ws[0].JOB_TYPE_CLASSIC, "listing-%d" % trial, "agent-1", {})
    got = []
    lock = threading.Lock()
    bar = threading.Barrier(len(ws))

    def race2(worker, bar=bar, got=got, lock=lock):
        bar.wait()
        j = worker.claim_next(worker.JOB_TYPE_CLASSIC)
        if j is not None:
            with lock:
                got.append(j["id"])

    ts = [threading.Thread(target=race2, args=(w,)) for w in ws]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    if len(got) != 1:
        multi_ok = False
        break
check("40 further 6-way races each produced exactly one winner", multi_ok)


# ===========================================================================
print("\n[2] Idempotency: a double-click cannot produce two renders")
# ===========================================================================
db = FakeDB()
w = load_worker(db, 200)
job_a, created_a = w.enqueue(w.JOB_TYPE_CLASSIC, "listing-dbl", "agent-1", {})
job_b, created_b = w.enqueue(w.JOB_TYPE_CLASSIC, "listing-dbl", "agent-1", {})
check("first enqueue created a job", created_a is True)
check("second enqueue did NOT create a job", created_b is False)
check("both calls refer to the same job id", job_a["id"] == job_b["id"])
check("only one row exists", len(db.rows) == 1, "rows=%d" % len(db.rows))

# And under genuine concurrency, not just sequentially.
db = FakeDB()
ws = [load_worker(db, 210 + i) for i in range(10)]
bar = threading.Barrier(len(ws))
errors = []


def double_click(worker):
    bar.wait()
    try:
        worker.enqueue(worker.JOB_TYPE_CLASSIC, "listing-storm", "agent-1", {})
    except Exception as e:
        errors.append(repr(e))


ts = [threading.Thread(target=double_click, args=(x,)) for x in ws]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("10 simultaneous Generate presses produced exactly one job row",
      len(db.rows) == 1, "rows=%d errors=%s" % (len(db.rows), errors[:2]))
check("none of the 10 raised an error at the agent", not errors, str(errors[:2]))

# Regeneration must still work once the first job is finished.
db.rows[0]["status"] = "done"
db.rows[0]["finished_at"] = iso(now())
_, created_again = ws[0].enqueue(ws[0].JOB_TYPE_CLASSIC, "listing-storm", "agent-1", {})
check("a new job can be queued once the previous one finished", created_again is True)


# ===========================================================================
print("\n[3] A job survives a simulated restart (deploy mid-render)")
# ===========================================================================
db = FakeDB()
w_old = load_worker(db, 300)       # the worker that will be "killed"
w_new = load_worker(db, 301)       # the worker that comes up after the deploy
w_old.enqueue(w_old.JOB_TYPE_CLASSIC, "listing-deploy", "agent-1", {"photo_index": 2})
claimed = w_old.claim_next(w_old.JOB_TYPE_CLASSIC)
check("the job was claimed before the restart", claimed is not None)

# Railway SIGKILLs the process: no shutdown hook, no further heartbeats.
db.rows[0]["heartbeat_at"] = iso(now() - timedelta(seconds=w_new.STALE_AFTER_SECONDS + 30))

# A brand-new process starts up and sweeps.
reclaimed = w_new.sweep_stale()
check("the abandoned job was returned to the queue", reclaimed == 1, "reclaimed=%d" % reclaimed)
check("its status is queued again with no holder",
      db.rows[0]["status"] == "queued" and db.rows[0]["claimed_by"] is None)
check("the render options survived the restart",
      db.rows[0]["payload"] == {"photo_index": 2})
again = w_new.claim_next(w_new.JOB_TYPE_CLASSIC)
check("the new worker picked the job up", again is not None and again["attempts"] == 2)

# The fast path: a graceful shutdown hands the job back immediately.
db = FakeDB()
w_a = load_worker(db, 310)
w_b = load_worker(db, 311)
w_a.enqueue(w_a.JOB_TYPE_CLASSIC, "listing-graceful", "agent-1", {})
held = w_a.claim_next(w_a.JOB_TYPE_CLASSIC)
w_a._locally_claimed[held["id"]] = held
w_a.release_local_claims()
check("a graceful shutdown requeues the job at once",
      db.rows[0]["status"] == "queued" and db.rows[0]["claimed_by"] is None)
check("the shutdown release did not burn an attempt",
      db.rows[0]["attempts"] == 0, "attempts=%s" % db.rows[0]["attempts"])


# ===========================================================================
print("\n[4] A resurrected worker cannot double-write a job someone else now owns")
# ===========================================================================
db = FakeDB()
w_stalled = load_worker(db, 400)
w_taker = load_worker(db, 401)
w_stalled.enqueue(w_stalled.JOB_TYPE_CLASSIC, "listing-zombie", "agent-1", {})
stalled_job = w_stalled.claim_next(w_stalled.JOB_TYPE_CLASSIC)

# It was not dead, only stalled -- long enough for the sweeper to give the job away.
db.rows[0]["heartbeat_at"] = iso(now() - timedelta(seconds=w_taker.STALE_AFTER_SECONDS + 5))
w_taker.sweep_stale()
taker_job = w_taker.claim_next(w_taker.JOB_TYPE_CLASSIC)
check("the second worker now owns the job", taker_job is not None and taker_job["attempts"] == 2)

# The stalled worker wakes up and tries to record its result.
landed = w_stalled.complete(stalled_job, "https://example/stale.mp4", [], 51.0)
check("the stalled worker's completion was rejected", landed is False)
check("the row still belongs to the new owner and is still running",
      db.rows[0]["claimed_by"] == w_taker.WORKER_ID and db.rows[0]["status"] == "running",
      "status=%s" % db.rows[0]["status"])
check("no stale result_url was written", db.rows[0]["result_url"] is None)

# The same fence protects failure writes, so a zombie cannot fail a live job either.
failed_landed = w_stalled.fail_or_retry(stalled_job, "ffmpeg died")
check("the stalled worker could not fail the live job", failed_landed is False)
check("the live job is untouched by the zombie's failure",
      db.rows[0]["status"] == "running" and db.rows[0]["error"] is None)

# And its heartbeat tells it it has been superseded, so it can stop early.
check("the stalled worker's heartbeat reports supersession",
      w_stalled.heartbeat(stalled_job) is False)


# ===========================================================================
print("\n[5] Busyness never fails a job")
# ===========================================================================
db = FakeDB()
w = load_worker(db, 500)
w.enqueue(w.JOB_TYPE_CLASSIC, "listing-busy", "agent-1", {})
job = w.claim_next(w.JOB_TYPE_CLASSIC)
before = db.rows[0]["attempts"]
w.defer(job, "no local slot")
check("a deferred job goes back to queued", db.rows[0]["status"] == "queued")
check("a deferral rewinds the attempt counter so it can never exhaust retries",
      db.rows[0]["attempts"] == before - 1,
      "before=%s after=%s" % (before, db.rows[0]["attempts"]))
check("a deferred job is held back briefly rather than spun on",
      db.rows[0]["run_after"] > iso(now()))

# Defer 50 times: a job waiting for capacity must never reach 'failed'.
for _ in range(50):
    j = w.claim_next(w.JOB_TYPE_CLASSIC)
    if j is None:
        db.rows[0]["run_after"] = iso(now())
        j = w.claim_next(w.JOB_TYPE_CLASSIC)
    w.defer(j, "still no slot", backoff_seconds=0)
check("50 consecutive deferrals left the job queued, never failed",
      db.rows[0]["status"] == "queued", "status=%s" % db.rows[0]["status"])


# ===========================================================================
print("\n[6] Genuine errors still fail -- after retrying")
# ===========================================================================
db = FakeDB()
w = load_worker(db, 600)
final_failures = []
w.register_renderer(w.JOB_TYPE_CLASSIC,
                    run=lambda job: (_ for _ in ()).throw(RuntimeError("ffmpeg crashed")),
                    on_final_failure=lambda job, err: final_failures.append(err))
w.enqueue(w.JOB_TYPE_CLASSIC, "listing-broken", "agent-1", {})
for attempt in range(3):
    db.rows[0]["run_after"] = iso(now())
    j = w.claim_next(w.JOB_TYPE_CLASSIC)
    w.execute_job(j)
    if attempt < 2:
        check("attempt %d was retried, not failed" % (attempt + 1),
              db.rows[0]["status"] == "queued", "status=%s" % db.rows[0]["status"])
check("the job failed only after exhausting its attempts",
      db.rows[0]["status"] == "failed" and db.rows[0]["attempts"] == 3,
      "status=%s attempts=%s" % (db.rows[0]["status"], db.rows[0]["attempts"]))
check("the listing was told exactly once", len(final_failures) == 1,
      "calls=%d" % len(final_failures))

# A permanent error skips the ladder entirely.
db = FakeDB()
w = load_worker(db, 610)
told = []
w.register_renderer(
    w.JOB_TYPE_CLASSIC,
    run=lambda job: (_ for _ in ()).throw(w.PermanentJobError("This listing has no photos any more.")),
    on_final_failure=lambda job, err: told.append(err))
w.enqueue(w.JOB_TYPE_CLASSIC, "listing-nophotos", "agent-1", {})
w.execute_job(w.claim_next(w.JOB_TYPE_CLASSIC))
check("an agent-fixable error fails immediately without burning retries",
      db.rows[0]["status"] == "failed" and db.rows[0]["attempts"] == 1)
check("the agent gets the actionable message",
      told and "no photos" in told[0], str(told[:1]))


# ===========================================================================
print("\n[7] Ten simultaneous requests: all complete, none refused")
# ===========================================================================
db = FakeDB()
WORKERS = 4
workers = [load_worker(db, 700 + i) for i in range(WORKERS)]

rendered = []
rendered_lock = threading.Lock()


def fake_render(job):
    # Stands in for a ~50s Classic render, scaled down so the test runs in seconds.
    time.sleep(0.25 + random.random() * 0.1)
    with rendered_lock:
        rendered.append(str(job["listing_id"]))
    return "https://example/%s.mp4" % job["listing_id"], []


refused = []
for wk in workers:
    wk.register_renderer(wk.JOB_TYPE_CLASSIC, run=fake_render,
                         on_final_failure=lambda job, err: refused.append(err))

# Ten agents press Generate at the same instant, spread over the four web workers the
# way uvicorn would spread them -- i.e. not evenly.
enqueue_errors = []
bar = threading.Barrier(10)


def agent_presses_generate(n):
    wk = workers[random.randrange(WORKERS)]      # whichever worker uvicorn happened to pick
    bar.wait()
    try:
        wk.enqueue(wk.JOB_TYPE_CLASSIC, "listing-%02d" % n, "agent-%02d" % n, {})
    except Exception as e:
        enqueue_errors.append(repr(e))


ts = [threading.Thread(target=agent_presses_generate, args=(n,)) for n in range(10)]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("all 10 requests were accepted", len(db.rows) == 10 and not enqueue_errors,
      "rows=%d errors=%s" % (len(db.rows), enqueue_errors[:1]))

# Each worker runs its consumer loop: one Classic render at a time, as in production.
stop = threading.Event()


def consumer(worker):
    while not stop.is_set():
        job = worker.claim_next(worker.JOB_TYPE_CLASSIC)
        if job is None:
            time.sleep(0.02)
            continue
        worker.execute_job(job)


consumers = [threading.Thread(target=consumer, args=(wk,), daemon=True) for wk in workers]
started_at = time.time()
for c in consumers:
    c.start()
while time.time() - started_at < 30:
    if all(r["status"] in ("done", "failed") for r in db.rows):
        break
    time.sleep(0.05)
stop.set()
for c in consumers:
    c.join(timeout=5)

done = [r for r in db.rows if r["status"] == "done"]
failed = [r for r in db.rows if r["status"] == "failed"]
check("all 10 renders completed", len(done) == 10, "done=%d failed=%d" % (len(done), len(failed)))
check("not one was refused for busyness", len(failed) == 0 and not refused)
check("each listing was rendered exactly once", sorted(rendered) == sorted(set(rendered)),
      "renders=%d distinct=%d" % (len(rendered), len(set(rendered))))
check("every job recorded a result url and a duration",
      all(r["result_url"] and r["duration_seconds"] for r in done))

# Scale it up: 30 at once, the "50 agents" shape.
db = FakeDB()
workers = [load_worker(db, 750 + i) for i in range(WORKERS)]
rendered = []
for wk in workers:
    wk.register_renderer(wk.JOB_TYPE_CLASSIC, run=fake_render)
for n in range(30):
    workers[n % WORKERS].enqueue(workers[0].JOB_TYPE_CLASSIC, "big-%02d" % n, "agent-%02d" % n, {})
stop = threading.Event()
consumers = [threading.Thread(target=consumer, args=(wk,), daemon=True) for wk in workers]
started_at = time.time()
for c in consumers:
    c.start()
while time.time() - started_at < 60:
    if all(r["status"] in ("done", "failed") for r in db.rows):
        break
    time.sleep(0.05)
stop.set()
for c in consumers:
    c.join(timeout=5)
check("30 simultaneous renders all completed",
      all(r["status"] == "done" for r in db.rows),
      "done=%d" % len([r for r in db.rows if r["status"] == "done"]))
check("30 renders, none duplicated", sorted(rendered) == sorted(set(rendered)))


# ===========================================================================
print("\n[8] Per-type concurrency: Signature is capped account-wide at 1")
# ===========================================================================
db = FakeDB()
workers = [load_worker(db, 800 + i) for i in range(4)]
check("classic has no account-wide cap (CPU bound, per-process)",
      workers[0].JOB_TYPES["classic"].account_limit is None)
check("signature is capped at 1 across the whole fleet (Replicate)",
      workers[0].JOB_TYPES["signature"].account_limit == 1)

for n in range(4):
    workers[0].enqueue(workers[0].JOB_TYPE_SIGNATURE, "sig-%d" % n, "agent-1", {})

claims = []
bar = threading.Barrier(4)


def claim_signature(worker):
    bar.wait()
    job = worker.claim_next(worker.JOB_TYPE_SIGNATURE)
    if job and worker.confirm_account_slot(job):
        claims.append(job["id"])
    elif job:
        worker.defer(job, "account slot taken", backoff_seconds=0)


ts = [threading.Thread(target=claim_signature, args=(wk,)) for wk in workers]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("at most one Signature job was allowed to proceed", len(claims) <= 1,
      "proceeded=%d" % len(claims))
check("the others were deferred, not failed",
      not any(r["status"] == "failed" for r in db.rows),
      str([r["status"] for r in db.rows]))

# Signature has no renderer registered, so no worker will ever pick one up in production.
fresh = load_worker(FakeDB(), 890)
check("signature has no renderer registered -- defined but inert",
      "signature" not in fresh._RENDERERS)


# ===========================================================================
print("\n[9] Queue position and ETA are honest")
# ===========================================================================
db = FakeDB()
w = load_worker(db, 900)
jobs = []
for n in range(5):
    j, _ = w.enqueue(w.JOB_TYPE_CLASSIC, "pos-%d" % n, "agent-1", {})
    jobs.append(j)
    time.sleep(0.005)   # distinct created_at values
first = w.claim_next(w.JOB_TYPE_CLASSIC)
positions = [w.describe(w.get_job(j["id"])).get("queue_position") for j in jobs]
check("the running job reports position 0", positions[0] == 0, str(positions))
check("the queued jobs report 2,3,4,5 behind it", positions[1:] == [2, 3, 4, 5], str(positions))
etas = [w.describe(w.get_job(j["id"])).get("eta_seconds") for j in jobs[1:]]
check("ETAs are real numbers, not zero", all(e and e > 0 for e in etas), str(etas))
check("with 4 slots free, the next 4 in line all wait one render (~120s)",
      etas == [120, 120, 120, 120], str(etas))

# A deeper queue must produce a visibly growing estimate, not one flat number.
db = FakeDB()
w = load_worker(db, 910)
deep = []
for n in range(13):
    j, _ = w.enqueue(w.JOB_TYPE_CLASSIC, "deep-%02d" % n, "agent-1", {})
    deep.append(j)
    time.sleep(0.004)
w.claim_next(w.JOB_TYPE_CLASSIC)
deep_etas = [w.describe(w.get_job(j["id"])).get("eta_seconds") for j in deep[1:]]
check("ETA grows as the queue deepens", deep_etas[-1] > deep_etas[0],
      "first=%s last=%s" % (deep_etas[0], deep_etas[-1]))
check("ETA never decreases as you go further back",
      all(b >= a for a, b in zip(deep_etas, deep_etas[1:])), str(deep_etas))
check("the 13th in line is told minutes, not seconds", deep_etas[-1] >= 240,
      "eta=%s" % deep_etas[-1])

agent_view = w.describe_active_for_agent("agent-1")
check("the agent's in-flight jobs are reported in one call", len(agent_view) == 13,
      "entries=%d" % len(agent_view))
check("each entry carries the position the UI needs",
      all("queue_position" in v for v in agent_view.values()))


# ===========================================================================
print("\n[10] The queue is skipped entirely when the table is missing")
# ===========================================================================
class BrokenDB:
    def table(self, name):
        raise RuntimeError('relation "public.video_jobs" does not exist')


w = load_worker(BrokenDB(), 950)
w._table_available = None
w.configure(make_db_execute(BrokenDB()))
result = w.probe()
check("a missing table fails the probe rather than raising", result is False)
check("available() is False, so generate-video uses the old inline path",
      w.available() is False)
check("agent-facing queue metadata degrades to empty, not an error",
      w.describe_active_for_agent("agent-1") == {})


# ===========================================================================
print("\n[11] S3(a): a shutdown cannot resurrect a job that already finished")
# ===========================================================================
db = FakeDB()
w = load_worker(db, 1100)
w.enqueue(w.JOB_TYPE_CLASSIC, "listing-sigterm", "agent-1", {})
job = w.claim_next(w.JOB_TYPE_CLASSIC)
w._locally_claimed[job["id"]] = job          # execute_job records it here
w.complete(job, "https://example/done.mp4", [], 50.0)
check("the job is done with its video recorded",
      db.rows[0]["status"] == "done" and db.rows[0]["result_url"])

# SIGTERM lands in the window between complete() committing and execute_job's finally.
w.release_local_claims()
check("the finished job was NOT pushed back to queued",
      db.rows[0]["status"] == "done", "status=%s" % db.rows[0]["status"])
check("its attempt count was not rewound", db.rows[0]["attempts"] == 1,
      "attempts=%s" % db.rows[0]["attempts"])
check("the delivered video url survived", db.rows[0]["result_url"] == "https://example/done.mp4")

# Same fence on the failure writes.
db = FakeDB()
w = load_worker(db, 1110)
w.enqueue(w.JOB_TYPE_CLASSIC, "listing-sigterm2", "agent-1", {})
job = w.claim_next(w.JOB_TYPE_CLASSIC)
w.complete(job, "https://example/done2.mp4", [], 50.0)
check("fail_permanently cannot overwrite a completed job",
      w.fail_permanently(job, "late error") is False and db.rows[0]["status"] == "done")
check("defer cannot overwrite a completed job",
      w.defer(job, "late defer") is False and db.rows[0]["status"] == "done")


# ===========================================================================
print("\n[12] S1: a queued job nothing will ever run stops holding the listing")
# ===========================================================================
db = FakeDB()
w = load_worker(db, 1200)
told = []
w.register_renderer(w.JOB_TYPE_CLASSIC, run=lambda job: ("u", []),
                    on_final_failure=lambda job, err: told.append(err))
w.enqueue(w.JOB_TYPE_CLASSIC, "listing-stuck", "agent-1", {})
check("a fresh queued job is left alone", w.sweep_abandoned_queue() == 0)

# Nobody ever claimed it, and nothing has finished -- the consumers are not running.
db.rows[0]["updated_at"] = iso(now() - timedelta(seconds=w.QUEUED_ESCAPE_SECONDS + 60))
freed = w.sweep_abandoned_queue()
check("the abandoned job was released", freed == 1, "freed=%d" % freed)
check("it failed rather than lingering", db.rows[0]["status"] == "failed")
check("the agent is told something actionable",
      "try generating it again" in db.rows[0]["error"], db.rows[0]["error"])
check("the listing was notified once", len(told) == 1)

# The listing is free again: the partial unique index no longer blocks a new job.
_, created = w.enqueue(w.JOB_TYPE_CLASSIC, "listing-stuck", "agent-1", {})
check("the agent can generate again without anyone running SQL", created is True)

# A DEEP BUT HEALTHY queue must not be mistaken for a dead one.
db = FakeDB()
w = load_worker(db, 1210)
for n in range(5):
    w.enqueue(w.JOB_TYPE_CLASSIC, "deepq-%d" % n, "agent-1", {})
for r in db.rows:
    r["updated_at"] = iso(now() - timedelta(seconds=w.QUEUED_ESCAPE_SECONDS + 60))
# ...but the queue IS draining: something finished a moment ago.
db.rows.append({
    "id": "recent-done", "job_type": "classic", "listing_id": "other", "agent_id": "a",
    "status": "done", "attempts": 1, "max_attempts": 3, "payload": {},
    "created_at": iso(now()), "updated_at": iso(now()), "finished_at": iso(now()),
    "run_after": iso(now()), "claimed_by": None, "duration_seconds": 50.0,
})
check("a deep but draining queue is left strictly alone",
      w.sweep_abandoned_queue() == 0,
      "statuses=%s" % [r["status"] for r in db.rows[:5]])


# ===========================================================================
print("\n[13] S2(d): the probe detects a key that can read but not write")
# ===========================================================================
from fake_postgrest import RlsFakeDB

rls = RlsFakeDB()
w = load_worker(rls, 1300)
w._table_available = None
w.configure(make_db_execute(rls))
check("a read-only SELECT against RLS succeeds and returns nothing (the trap)",
      rls.table("video_jobs").select("id").limit(1).execute().data == [])
check("the probe nonetheless FAILS, because it proves it can write",
      w.probe() is False)
check("so generate-video falls back instead of enqueuing into a black hole",
      w.available() is False)

# And on a healthy database it passes and leaves nothing behind.
db = FakeDB()
w = load_worker(db, 1310)
w._table_available = None
check("the probe passes on a writable table", w.probe() is True)
check("the probe row was cleaned up", len(db.rows) == 0, "rows=%d" % len(db.rows))

# Even if cleanup were skipped, a probe row must be invisible to everything that counts.
db = FakeDB()
w = load_worker(db, 1320)
db.rows.append({
    "id": "leftover-probe", "job_type": "classic",
    "listing_id": w.PROBE_LISTING_ID, "agent_id": w.PROBE_LISTING_ID,
    "status": "failed", "attempts": 0, "max_attempts": 3, "payload": {},
    "created_at": iso(now()), "updated_at": iso(now()), "finished_at": iso(now()),
    "run_after": iso(now()), "claimed_by": None, "duration_seconds": None,
    "error": "startup write probe",
})
w.enqueue(w.JOB_TYPE_CLASSIC, "real-listing", "agent-1", {})
desc = w.describe(w.get_job([r for r in db.rows if r["listing_id"] == "real-listing"][0]["id"]))
check("a leftover probe row does not affect queue position",
      desc["queue_position"] == 1, "position=%s" % desc.get("queue_position"))
check("a leftover probe row is not claimable",
      w.claim_next(w.JOB_TYPE_CLASSIC)["listing_id"] == "real-listing")


# ===========================================================================
print("\n[14] S2(c): a superseded render writes nothing the agent can see")
# ===========================================================================
db = FakeDB()
w_slow = load_worker(db, 1400)
w_new = load_worker(db, 1401)

uploads = []          # stands in for the storage bucket
listing_writes = []   # stands in for listings.video_url / video_status

def render_with_ownership_checks(job):
    """Mirrors _render_video_core: check ownership immediately before the upload, and
    again immediately before the listing write."""
    time.sleep(0.05)                                   # the render itself
    if not w_slow.heartbeat(job):                      # check before storing
        raise w_slow.SupersededError("reclaimed before the video was stored")
    uploads.append(job["listing_id"])
    if not w_slow.heartbeat(job):                      # check before the listing write
        raise w_slow.SupersededError("reclaimed before the listing was updated")
    listing_writes.append(job["listing_id"])
    return "https://example/%s.mp4" % job["listing_id"], []

w_slow.register_renderer(w_slow.JOB_TYPE_CLASSIC, run=render_with_ownership_checks)
w_slow.enqueue(w_slow.JOB_TYPE_CLASSIC, "listing-super", "agent-1", {})
stolen = w_slow.claim_next(w_slow.JOB_TYPE_CLASSIC)

# While it renders, its heartbeat dies and the job is handed to another worker.
db.rows[0]["heartbeat_at"] = iso(now() - timedelta(seconds=w_new.STALE_AFTER_SECONDS + 5))
w_new.sweep_stale()
new_owner = w_new.claim_next(w_new.JOB_TYPE_CLASSIC)
check("another worker now owns the job", new_owner is not None and new_owner["attempts"] == 2)

w_slow.execute_job(stolen)
check("the superseded render never uploaded a video", uploads == [], str(uploads))
check("the superseded render never touched the listing", listing_writes == [], str(listing_writes))
check("the job row still belongs to the new owner, still running",
      db.rows[0]["claimed_by"] == w_new.WORKER_ID and db.rows[0]["status"] == "running",
      "status=%s" % db.rows[0]["status"])
check("no result url was recorded by the loser", db.rows[0]["result_url"] is None)
check("the supersession did not count as a failure", db.rows[0]["error"] is None)

# The owner's own render completes normally, checks and all.
w_new.register_renderer(w_new.JOB_TYPE_CLASSIC, run=render_with_ownership_checks)
uploads.clear()
listing_writes.clear()

def render_as_owner(job):
    if not w_new.heartbeat(job):
        raise w_new.SupersededError("x")
    uploads.append(job["listing_id"])
    if not w_new.heartbeat(job):
        raise w_new.SupersededError("x")
    listing_writes.append(job["listing_id"])
    return "https://example/owner.mp4", []

w_new.register_renderer(w_new.JOB_TYPE_CLASSIC, run=render_as_owner)
w_new.execute_job(new_owner)
check("the rightful owner's render did complete and deliver",
      db.rows[0]["status"] == "done" and uploads == ["listing-super"]
      and listing_writes == ["listing-super"],
      "status=%s uploads=%s" % (db.rows[0]["status"], uploads))


# ===========================================================================
print("\n[15] A deferred job is not re-claimed inside its own backoff")
# ===========================================================================
db = FakeDB()
w = load_worker(db, 1500)
w.enqueue(w.JOB_TYPE_CLASSIC, "listing-backoff", "agent-1", {})
job = w.claim_next(w.JOB_TYPE_CLASSIC)
stale_candidate = dict(job)                      # a candidate list read before the defer
w.defer(job, "no slot", backoff_seconds=60)
check("the job is queued with a future run_after", db.rows[0]["status"] == "queued")
check("claim_next will not pick it up during the backoff",
      w.claim_next(w.JOB_TYPE_CLASSIC) is None)
# Even a worker acting on a candidate list read BEFORE the defer must be refused.
stale_candidate["attempts"] = 0
check("a stale candidate cannot bypass the backoff via the CAS",
      w._try_claim(stale_candidate) is None)


# ===========================================================================
print("\n[16] Two spellings of one uuid cannot become two renders")
# ===========================================================================
db = FakeDB()
w = load_worker(db, 1600)
upper = "A1B2C3D4-0000-0000-0000-00000000FFFF"
lower = upper.lower()
w.enqueue(w.JOB_TYPE_CLASSIC, upper, "agent-1", {})
_, created = w.enqueue(w.JOB_TYPE_CLASSIC, lower, "agent-1", {})
check("the second spelling did not create a second job", created is False)
check("only one row exists", len(db.rows) == 1, "rows=%d" % len(db.rows))
check("find_active resolves either spelling",
      w.find_active(w.JOB_TYPE_CLASSIC, upper) is not None
      and w.find_active(w.JOB_TYPE_CLASSIC, lower) is not None)


# ===========================================================================
print("\n" + "=" * 70)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("\nFailures:")
    for name in FAIL:
        print("  - " + name)
print("=" * 70)
sys.exit(1 if FAIL else 0)
