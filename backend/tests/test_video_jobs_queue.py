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
print("\n" + "=" * 70)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
if FAIL:
    print("\nFailures:")
    for name in FAIL:
        print("  - " + name)
print("=" * 70)
sys.exit(1 if FAIL else 0)
