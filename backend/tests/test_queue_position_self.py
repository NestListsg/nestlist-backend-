"""A job must never count itself as being ahead of itself.

Seen in production on the first two Signatures an agent ever generated: both said
"2nd in line, about 3 minutes" with an empty queue behind them. `describe()` is handed
the job row as it was at enqueue, and a worker can claim it between that insert and the
call -- so counting RUNNING jobs by type counted the job against itself, putting the
agent who is genuinely first into second place and roughly doubling the quoted wait.

Not a stuck queue, which is what it looked like. Just an off-by-one that only appears
when the queue is empty, which is exactly the common case for the first agent.
"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_jobs as vj

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))


class Res:
    def __init__(self, count): self.count = count; self.data = []

def fake_exec(fn, what=None):
    """Stands in for the database. `fn` is applied to a recorder that notes the filters."""
    class Q:
        def __init__(self): self.f = {}
        def table(self, *a): return self
        def select(self, *a, **k): return self
        def eq(self, col, val): self.f[col] = val; return self
        def lt(self, col, val): self.f["lt_" + col] = val; return self
        def neq(self, col, val): self.f["neq_" + col] = val; return self
    q = Q(); fn(q)
    STATE["queries"].append(dict(q.f))
    if q.f.get("status") == vj.STATUS_QUEUED:
        return Res(STATE["queued_ahead"])
    if q.f.get("status") == vj.STATUS_RUNNING:
        # The real table: count running jobs, honouring an id exclusion if asked for.
        n = STATE["running"]
        if "neq_id" in q.f and STATE["self_is_running"]:
            n -= 1
        return Res(max(0, n))
    return Res(0)

STATE = {}
def describe(queued_ahead, running, self_is_running):
    STATE.clear()
    STATE.update(queued_ahead=queued_ahead, running=running,
                 self_is_running=self_is_running, queries=[])
    saved = (vj._exec, vj._average_duration, vj._worker_count)
    vj._exec = fake_exec
    vj._average_duration = lambda t: 100.0
    vj._worker_count = lambda: 4
    try:
        return vj.describe({"id": "me", "job_type": vj.JOB_TYPE_SIGNATURE,
                            "status": vj.STATUS_QUEUED, "created_at": "2026-10-20T00:00:00Z"})
    finally:
        vj._exec, vj._average_duration, vj._worker_count = saved


print("\n[1] the production case: empty queue, this job already claimed")
out = describe(queued_ahead=0, running=1, self_is_running=True)
check("first in line, not second", out["queue_position"] == 1, str(out.get("queue_position")))
check("the running count excluded this job by id",
      any("neq_id" in q for q in STATE["queries"]), str(STATE["queries"]))
check("eta is one render, not two", out["eta_seconds"] == 100, str(out.get("eta_seconds")))

print("\n[2] still counts OTHER running jobs")
out = describe(queued_ahead=0, running=3, self_is_running=True)
check("two others running -> 3rd", out["queue_position"] == 3, str(out.get("queue_position")))

print("\n[3] and jobs genuinely queued ahead")
out = describe(queued_ahead=2, running=1, self_is_running=True)
check("two queued ahead, none else running -> 3rd",
      out["queue_position"] == 3, str(out.get("queue_position")))

print("\n[4] a job not yet claimed is unaffected")
out = describe(queued_ahead=0, running=0, self_is_running=False)
check("alone and unclaimed -> 1st", out["queue_position"] == 1, str(out.get("queue_position")))
out = describe(queued_ahead=0, running=2, self_is_running=False)
check("two others running -> 3rd", out["queue_position"] == 3, str(out.get("queue_position")))

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
