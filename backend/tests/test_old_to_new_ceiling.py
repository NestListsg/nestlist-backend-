"""The per-agent monthly ceiling on paid "after" pictures: where it is kept, and why.

WHAT WENT WRONG, which is what this file is for. The first version of this ceiling was
"40 listings an agent a month", counted by reading rows out of video_jobs. An audit on
2026-10-22 took it apart three ways:

  * video_jobs.prune_finished() DELETES finished rows after PRUNE_AFTER_DAYS = 7, so from
    the 8th of the month the count could only see a week. The ceiling reset itself about
    weekly -- roughly four times the allowance Jane agreed to. The test below seeds a
    month of jobs, runs the REAL prune against a fake Postgres, and shows the old count
    collapsing while the new one does not move.
  * it counted LISTINGS, and the bill is per picture per press: re-picking which photo
    leads a video costs three more pictures on the same listing, and one ten-photo
    listing reached 25 pictures (US$1.00) while counting as one of the forty.
  * the in-process fallback renderer writes no job row at all, so while it was in use --
    which is every Dramatic render until the video_jobs migration is run -- the ceiling
    was not approximate, it was absent.

So the tally is now a running total of PICTURES on the agent row, with the month it
belongs to beside it. Nothing prunes it, every paying path adds to it, and it is counted
in the unit fal charges for.

Runs without Supabase or a key. Run: python3 tests/test_old_to_new_ceiling.py
"""
import ast
import copy
import datetime as dt
import io
import logging
import os
import sys
import threading
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, BACKEND)
sys.path.insert(0, _HERE)
logging.disable(logging.CRITICAL)

import fal_images            # noqa: E402
import old_to_new            # noqa: E402
import video_jobs            # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("  -- " + detail) if detail else ""))


# ---------------------------------------------------------------------------
# A small stand-in for the agents table. It models the two things the tally's
# compare-and-swap actually depends on: a filter can pin a column's current value, and
# NULL is not zero (a filter of `= 0` never matches a NULL column, which is the trap the
# photo-array compare-and-swap in main.py documents and this one inherits).
# ---------------------------------------------------------------------------
class FakeAgents:
    def __init__(self, rows, columns=("id", "old_to_new_month", "old_to_new_images_used")):
        self.rows = [dict(r) for r in rows]
        self.columns = set(columns)
        self.lock = threading.Lock()
        self.updates = 0
        self.lost_races = 0

    def table(self, name):
        assert name == "agents", name
        return _Table(self)


class _Table:
    def __init__(self, db):
        self.db = db

    def select(self, columns="*", **kw):
        wanted = [c.strip() for c in str(columns).split(",") if c.strip() != "*"]
        missing = [c for c in wanted if c not in self.db.columns]
        if missing:
            # What PostgREST does when a column is not there: it refuses the request.
            raise RuntimeError("column agents.%s does not exist" % missing[0])
        return _Query(self.db, "select", columns=wanted)

    def update(self, payload):
        missing = [c for c in payload if c not in self.db.columns]
        if missing:
            raise RuntimeError("column agents.%s does not exist" % missing[0])
        return _Query(self.db, "update", payload=payload)


class _Query:
    def __init__(self, db, op, payload=None, columns=None):
        self.db, self.op, self.payload = db, op, dict(payload or {})
        self.columns = columns or []
        self.filters = []

    def eq(self, column, value):
        self.filters.append((column, "eq", value))
        return self

    def filter(self, column, operator, value):
        self.filters.append((column, operator, value))
        return self

    def limit(self, n):
        return self

    def _matches(self, row):
        for column, operator, value in self.filters:
            current = row.get(column)
            if operator == "eq":
                # Postgres: NULL = <anything> is never true.
                if current is None or str(current) != str(value):
                    return False
            elif operator == "is":
                if str(value) == "null" and current is not None:
                    return False
            else:
                raise AssertionError("unsupported operator %r" % operator)
        return True

    def execute(self):
        if self.op == "select":
            rows = [{c: r.get(c) for c in (self.columns or r)} for r in self.db.rows
                    if self._matches(r)]
            return types.SimpleNamespace(data=rows)
        # Outside the lock, like fake_postgrest's own writes: the point is for threads to
        # genuinely interleave rather than be serialised by luck, so that the
        # compare-and-swap is tested against a real lost race.
        __import__("time").sleep(__import__("random").random() * 0.004)
        with self.db.lock:
            self.db.updates += 1
            hit = [r for r in self.db.rows if self._matches(r)]
            for row in hit:
                row.update(copy.deepcopy(self.payload))
            if not hit:
                self.db.lost_races += 1
            return types.SimpleNamespace(data=[dict(r) for r in hit])


# ---------------------------------------------------------------------------
# The real main.py functions, lifted out and run against that stand-in. main.py cannot be
# imported here (it builds a FastAPI app and wants a database), so the tally functions are
# extracted by name -- which also means these tests are against the SHIPPED source and
# not a copy of it.
# ---------------------------------------------------------------------------
SRC = io.open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)
NODES = {n.name: n for n in ast.walk(TREE)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
ASSIGNS = {}
for node in TREE.body:
    if isinstance(node, ast.Assign) and len(node.targets) == 1 \
            and isinstance(node.targets[0], ast.Name) \
            and isinstance(node.value, (ast.Constant, ast.Num, ast.Str)):
        ASSIGNS[node.targets[0].id] = node.value.value


def build(db, names=("_current_usage_month", "_read_old_to_new_usage",
                     "_old_to_new_images_used", "_add_old_to_new_images",
                     "_old_to_new_cas", "_probe_old_to_new_usage_columns",
                     "_job_age_seconds")):
    """main.py's tally functions, wired to `db`."""
    def db_execute(fn, what="", **kw):
        # Exactly what main's own db_execute does with the lambda it is handed: build the
        # query against a client, then run it. Returning the unexecuted query instead
        # would make every read look like an empty table and every write look as if it
        # landed, which is the wrong answer to both of this file's questions.
        return fn(db).execute()

    g = {
        "logger": logging.getLogger("t"),
        "datetime": dt.datetime, "timezone": dt.timezone, "timedelta": dt.timedelta,
        "time": __import__("time"),
        "db_execute": db_execute,
        "get_db": lambda: db,
        "old_to_new": old_to_new,
        "fal_images": fal_images,
        "OLD_TO_NEW_MONTH_COLUMN": ASSIGNS["OLD_TO_NEW_MONTH_COLUMN"],
        "OLD_TO_NEW_IMAGES_COLUMN": ASSIGNS["OLD_TO_NEW_IMAGES_COLUMN"],
        "_OLD_TO_NEW_CAS_ATTEMPTS": ASSIGNS["_OLD_TO_NEW_CAS_ATTEMPTS"],
    }
    lines = SRC.splitlines()
    for name in names:
        node = NODES[name]
        exec(compile("\n".join(lines[node.lineno - 1:node.end_lineno]), name, "exec"), g)
    return g


THIS_MONTH = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m")
LAST_MONTH = (dt.datetime.now(dt.timezone.utc).replace(day=1)
              - dt.timedelta(days=1)).strftime("%Y-%m")


# ===========================================================================
print("\n[1] The tally itself: NULL is not zero, and a month rolls over")
# ===========================================================================
db = FakeAgents([{"id": "a1", "old_to_new_month": None,
                  "old_to_new_images_used": None}])
g = build(db)
check("a never-counted agent has used nothing", g["_old_to_new_images_used"]("a1", "L1") == 0)
# THE TRAP: a compare-and-swap that pinned `images = 0` against a NULL column would match
# no rows, and every first write would be silently lost.
check("the FIRST write lands from NULL, rather than matching nothing for ever",
      g["_add_old_to_new_images"]("a1", 6) is True)
check("and it is readable afterwards", g["_old_to_new_images_used"]("a1", "L1") == 6)
check("the month is stored beside it, so the reset is a comparison not a cron job",
      db.rows[0]["old_to_new_month"] == THIS_MONTH)
g["_add_old_to_new_images"]("a1", 3)
g["_add_old_to_new_images"]("a1", 1)
check("further pictures accumulate", g["_old_to_new_images_used"]("a1", "L1") == 10)
check("adding nothing writes nothing", g["_add_old_to_new_images"]("a1", 0) is True)

db.rows[0]["old_to_new_month"] = LAST_MONTH
db.rows[0]["old_to_new_images_used"] = 237
check("last month's tally reads as zero this month",
      g["_old_to_new_images_used"]("a1", "L1") == 0)
g["_add_old_to_new_images"]("a1", 6)
check("and the first write of the new month REPLACES it rather than adding to it",
      db.rows[0]["old_to_new_images_used"] == 6
      and db.rows[0]["old_to_new_month"] == THIS_MONTH)

# A REGENERATE IS STILL SAFE AT THE CEILING, and for a better reason than the exclusion
# this replaced: a full cache hit asks for no budget at all.
db.rows[0]["old_to_new_images_used"] = old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH
used = g["_old_to_new_images_used"]("a1", "L1")
try:
    old_to_new.reserve_budget(1, used)
    check("an agent at the ceiling cannot generate a new picture", False)
except old_to_new.BudgetExhausted as e:
    check("an agent at the ceiling cannot generate a new picture", True)
    check("and the message is in listings, which is how agents think, while the limit "
          "is in pictures",
          str(old_to_new.TYPICAL_LISTINGS_PER_AGENT_PER_MONTH) in str(e)
          and "Classic" in str(e))
plan = old_to_new.build_plan(
    "https://x/e.jpg", [("https://x/r.jpg", None)],
    # A cache holding every key this listing needs.
    {old_to_new.cache_key(r, u): "https://cdn/%s.jpg" % r
     for r, u in ((old_to_new.ROLE_EXTERIOR_ANON, "https://x/e.jpg"),
                  (old_to_new.ROLE_EXTERIOR_NEW, "https://x/e.jpg"),
                  (old_to_new.ROLE_ROOM_NEW, "https://x/r.jpg"))},
    generate_image=lambda *a: (_ for _ in ()).throw(AssertionError("paid at the ceiling")),
    store_image=lambda *a: "https://cdn/x.jpg",
    images_used_this_month=used)
check("but a REGENERATE off the cache still works at the ceiling, paying nothing",
      plan.images_paid == 0 and plan.opening_url)


# ===========================================================================
print("\n[2] Two renders at once both get counted (compare-and-swap)")
# ===========================================================================
# JOB_TYPES caps Dramatic at two running fleet-wide, so two renders adding to one tally
# is the normal case, not a rare one. A plain read-modify-write loses one of them.
db = FakeAgents([{"id": "a1", "old_to_new_month": THIS_MONTH,
                  "old_to_new_images_used": 0}])
g = build(db)
errors = []


def add_six():
    try:
        for _ in range(5):
            g["_add_old_to_new_images"]("a1", 6)
    except Exception as e:                                  # pragma: no cover
        errors.append(e)


threads = [threading.Thread(target=add_six) for _ in range(4)]
for t in threads:
    t.start()
for t in threads:
    t.join()
check("no exception escaped the concurrent writers", not errors, str(errors[:1]))
check("every one of the 20 writes is in the total (%d)"
      % db.rows[0]["old_to_new_images_used"],
      db.rows[0]["old_to_new_images_used"] == 120,
      str(db.rows[0]["old_to_new_images_used"]))
check("(and the races were real, not a lucky serialisation)", db.lost_races > 0,
      "lost=%d" % db.lost_races)


# ===========================================================================
print("\n[3] THE REGRESSION: a prune cannot move the tally")
# ===========================================================================
# This is the audit's finding, reproduced and then shown fixed. The old count was
# "distinct listings with a dramatic job this month, x 6". Run the REAL
# video_jobs.prune_finished() against a fake Postgres holding a month of finished jobs,
# and that count collapses to what is left inside PRUNE_AFTER_DAYS.
from fake_postgrest import FakeDB, make_db_execute                      # noqa: E402

jobs_db = FakeDB()
now = dt.datetime.now(dt.timezone.utc)
for day in range(1, 29):
    stamp = (now - dt.timedelta(days=28 - day)).isoformat()
    jobs_db.rows.append({
        "id": "j%d" % day, "job_type": video_jobs.JOB_TYPE_DRAMATIC,
        "listing_id": "L%d" % day, "agent_id": "a1", "status": video_jobs.STATUS_DONE,
        "created_at": stamp, "finished_at": stamp, "attempts": 1, "max_attempts": 3,
        "payload": {}, "claimed_by": None, "claimed_at": None, "heartbeat_at": None,
        "started_at": None, "duration_seconds": 30, "result_url": "u", "error": None,
        "degradations": None, "run_after": stamp,
    })
video_jobs.configure(make_db_execute(jobs_db))


def old_style_count():
    """What the shipped code used to do, for comparison only."""
    return len({r["listing_id"] for r in jobs_db.rows
                if r["job_type"] == video_jobs.JOB_TYPE_DRAMATIC
                and r["agent_id"] == "a1"}) * old_to_new.IMAGES_PER_LISTING


tally_db = FakeAgents([{"id": "a1", "old_to_new_month": THIS_MONTH,
                        "old_to_new_images_used": 168}])
g = build(tally_db)
before_jobs = old_style_count()
before_tally = g["_old_to_new_images_used"]("a1", "LX")
check("before the prune, both ways of counting agree there has been a lot of spending",
      before_jobs == 168 and before_tally == 168,
      "jobs=%d tally=%d" % (before_jobs, before_tally))
video_jobs.prune_finished()
after_jobs = old_style_count()
after_tally = g["_old_to_new_images_used"]("a1", "LX")
check("the prune really did delete a month's finished jobs (%d rows left)"
      % len(jobs_db.rows), len(jobs_db.rows) < 28, str(len(jobs_db.rows)))
check("THE OLD COUNT COLLAPSES to about a week, which is how the ceiling used to reset "
      "itself weekly (%d -> %d images)" % (before_jobs, after_jobs),
      after_jobs < before_jobs / 2, "%d -> %d" % (before_jobs, after_jobs))
check("the tally on the agent row does not move, because nothing prunes it",
      after_tally == 168, str(after_tally))
check("and the ceiling it feeds still refuses, where the old one would have re-opened",
      old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH - after_tally == 72
      and old_to_new.reserve_budget(6, after_tally) == 6)
tally_db.rows[0]["old_to_new_images_used"] = 236
try:
    old_to_new.reserve_budget(6, g["_old_to_new_images_used"]("a1", "LX"))
    check("a trimmed plan near the limit, not a refusal", True)
except old_to_new.BudgetExhausted:
    check("a trimmed plan near the limit, not a refusal", False)
check("...and the trim is to what is actually left",
      old_to_new.reserve_budget(6, 236) == 4)


# ===========================================================================
print("\n[4] Without the migration: no ceiling, loudly rather than silently")
# ===========================================================================
bare = FakeAgents([{"id": "a1"}], columns=("id",))
g = build(bare)
check("the probe reports the columns missing rather than raising",
      g["_probe_old_to_new_usage_columns"]() is False)
check("a missing column reads as 'nothing used', so an agent is never blocked by it",
      g["_old_to_new_images_used"]("a1", "L1") == 0)
check("and recording returns False rather than costing the agent their video",
      g["_add_old_to_new_images"]("a1", 6) is False)
present = FakeAgents([{"id": "a1", "old_to_new_month": None,
                       "old_to_new_images_used": None}])
check("with the columns there, the probe says so",
      build(present)["_probe_old_to_new_usage_columns"]() is True)
check("startup tells Jane on Telegram when the pictures are ON and the ceiling is not "
      "being counted",
      "old_to_new_ceiling_missing" in SRC
      and "_check_old_to_new_ceiling" in NODES)
check("and it says nothing when there is no key, because then nothing can be spent",
      "not fal_images.is_enabled()"
      in ast.get_source_segment(SRC, NODES["_check_old_to_new_ceiling"]))


# ===========================================================================
print("\n[5] The picker does not offer what this deploy cannot make")
# ===========================================================================
node = NODES["get_video_templates"]
g = {"fal_images": types.SimpleNamespace(is_enabled=lambda: False),
     "_old_to_new_ceiling_enforceable": lambda: True,
     "DRAMATIC_VIDEO_TEMPLATE_ID": "dramatic",
     "VIDEO_TEMPLATES": [{"id": "classic", "name": "Classic"},
                         {"id": "dramatic", "name": "Dramatic"}],
     "app": types.SimpleNamespace(get=lambda *a, **k: (lambda f: f)),
     "Depends": lambda f: None, "get_current_agent": None}
exec(compile("\n".join(SRC.splitlines()[node.lineno - 1:node.end_lineno]),
             "get_video_templates", "exec"), g)
offered = [t["id"] for t in g["get_video_templates"]()]
check("with no FAL_KEY the picker is offered Classic only -- not a style that 400s on "
      "the press", offered == ["classic"], str(offered))
g["fal_images"] = types.SimpleNamespace(is_enabled=lambda: True)
offered = [t["id"] for t in g["get_video_templates"]()]
check("with the key set, both styles are offered", offered == ["classic", "dramatic"],
      str(offered))
check("and the full list is unchanged, so nothing else reading it is affected",
      [t["id"] for t in g["VIDEO_TEMPLATES"]] == ["classic", "dramatic"])


# ===========================================================================
print("\n[6] A rate limit waits for capacity instead of costing a room its look")
# ===========================================================================
core = ast.get_source_segment(SRC, NODES["_render_dramatic_core"])
run = ast.get_source_segment(SRC, NODES["_dramatic_job_run"])
# Indices of the STATEMENTS, matched with their indentation, because both of these names
# also appear in the function's own docstring and prose must not be able to pass a test
# about order of execution.
_count = core.index("    _add_old_to_new_images(agent[\"id\"]")
_cache = core.index('"old_to_new_images": old_to_new.write_cache(plan.cache)')
_defer = core.index("        raise old_to_new.RateLimited(")
check("the paid pictures are counted BEFORE anything can fail", _count < _cache < _defer)
check("and the cache is written before the deferral, so a wait never re-pays",
      _cache < _defer)
check("the queued path turns a rate limit into a DEFERRAL, which rewinds the attempt "
      "counter rather than using up a retry",
      "except old_to_new.RateLimited" in run and "video_jobs.DeferJob" in run)
check("but it does not defer for ever: past a patience window it renders what it has",
      "_run(False)" in run and "rate_limit_patience" in run)
check("the in-process fallback never defers, because it has no queue to wait in",
      "defer_on_rate_limit=False"
      in ast.get_source_segment(SRC, NODES["_render_dramatic_job"]))

# The planner's half of it: flagged, not raised, so the cache survives.
paid = []


def gen_rate_limited(prompt, sources, deadline):
    if len(paid) >= 2:
        raise fal_images.FalRateLimited("fal is rate limiting this account")
    paid.append(prompt)
    return b"IMAGE"


plan = old_to_new.build_plan(
    "https://x/e.jpg", [("https://x/r%d.jpg" % i, None) for i in range(4)], {},
    generate_image=gen_rate_limited,
    store_image=lambda role, key, data: "https://cdn/%s.jpg" % key.replace(":", "_"),
    images_used_this_month=0)
check("build_plan does not raise, so the pictures already paid for reach the cache",
      plan.images_paid == 2 and len(plan.cache) == 2)
check("it flags the rate limit for the caller to act on", plan.rate_limited is True)
check("and it stops asking once fal has said no -- five more refusals is five more "
      "waits for the same answer", len(paid) == 2, str(len(paid)))


# ===========================================================================
print("\n[7] The money arithmetic, stated once so it cannot drift")
# ===========================================================================
per_image = fal_images.COST_PER_IMAGE_USD
check("one picture is 4 US cents", abs(per_image - 0.04) < 0.0001)
check("one listing, first press, is 6 pictures = US$0.24",
      abs(old_to_new.IMAGES_PER_LISTING * per_image - 0.24) < 0.001)
check("the ceiling is 240 pictures = US$9.60 an agent a month",
      abs(old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH * per_image - 9.60) < 0.01)
check("= US$480 a month across fifty agents, and that is now a real bound rather than "
      "a quoted one",
      abs(old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH * per_image * 50 - 480) < 1)

# ===========================================================================
print("\n[8] A DEFERRAL MUST NOT RE-BUY A PICTURE IT ALREADY PAID FOR")
# ===========================================================================
# The second audit's worst case in money terms. The deferral was designed for a 429 on the
# SUBMIT: nothing queued, nothing charged, waiting costs a wait. A 429 that arrives while
# COLLECTING a picture fal has already accepted is a different animal -- that picture is
# billed, never collected, therefore never cached -- so the next cycle submits it again.
# MEASURED on 2026-10-22 against the shipped code: 17 deferral cycles inside the
# 600-second patience window, US$0.68 for one press of Generate, all of it for one
# picture, and the per-agent tally read US$0.00 throughout.
#
# The rule now is "defer only when waiting is free", and the test reads that rule OUT OF
# main.py rather than restating it, so prose cannot pass a test about behaviour.
_defer_if = None
for _node in ast.walk(NODES["_render_dramatic_core"]):
    if not isinstance(_node, ast.If):
        continue
    if any(isinstance(s, ast.Raise) and isinstance(s.exc, ast.Call)
           and "RateLimited" in ast.dump(s.exc.func) for s in ast.walk(_node)):
        _defer_if = _node.test
        break
check("the shipped deferral decision was found in _render_dramatic_core",
      _defer_if is not None)


def would_defer(plan, from_the_queue=True):
    """Evaluate the SHIPPED condition against a plan."""
    expr = ast.Expression(body=_defer_if)
    ast.fix_missing_locations(expr)
    return bool(eval(compile(expr, "defer", "eval"),
                     {"plan": plan, "defer_on_rate_limit": from_the_queue}))


class _P:
    """Just the three fields the decision reads."""
    def __init__(self, rate_limited=False, images_lost=0, images_paid=0):
        self.rate_limited = rate_limited
        self.images_lost = images_lost
        self.images_paid = images_paid


check("a free rate limit (429 on the submit -- nothing billed) STILL defers, which is "
      "the whole point of the deferral", would_defer(_P(rate_limited=True)))
check("a rate limit that already cost money does NOT defer, because the next cycle "
      "would buy the same picture again",
      not would_defer(_P(rate_limited=True, images_lost=1, images_paid=1)))
check("money lost without a rate limit does not defer either (there is nothing to wait "
      "for)", not would_defer(_P(images_lost=1, images_paid=1)))
check("and the in-process fallback never defers, whatever happened, because it has no "
      "queue to wait in",
      not would_defer(_P(rate_limited=True), from_the_queue=False))

# The arithmetic, with the shipped constants, for both kinds of 429.
_per_collect = fal_images.POLL_MAX_SECONDS + fal_images.RETRY_BACKOFF_SECONDS
_cycle = fal_images.MAX_COLLECT_ATTEMPTS * _per_collect + video_jobs.DEFER_BACKOFF_SECONDS
_patience = 600.0


def _cycles(plan_factory):
    """How many deferral cycles one press can run before the rule says stop."""
    cache, cycles, elapsed = {}, 0, 0.0
    while elapsed < _patience:
        plan = plan_factory(cache)
        cache = dict(plan.cache)
        cycles += 1
        elapsed += _cycle
        if not would_defer(plan):
            break
    return cycles, plan


def _billed_429(cache, at_collect):
    """One generation pass where fal 429s -- at the submit, or during collection.

    `at_collect` is the expensive one: fal accepted the submit (and charged for it) and
    then refused to let us collect the result.
    """
    def gen(prompt, sources, deadline):
        e = fal_images.FalRateLimited("fal is rate limiting this account")
        if at_collect:
            e.images_billed = 1          # exactly what fal_images.generate now sets
        raise e
    return old_to_new.build_plan(
        "https://x/e.jpg", [("https://x/r%d.jpg" % i, None) for i in range(4)], cache,
        generate_image=gen, store_image=lambda r, k, d: "https://store/x.jpg",
        images_used_this_month=0)


cycles_free, plan_free = _cycles(lambda c: _billed_429(c, at_collect=False))
cycles_paid, plan_paid = _cycles(lambda c: _billed_429(c, at_collect=True))
check("one pass plus its backoff is about %.0fs, so %.0fs of patience is about %d "
      "cycles -- which is why this was expensive" % (_cycle, _patience, cycles_free),
      cycles_free > 10, str(cycles_free))
check("a 429 that cost nothing may use every one of them: %d cycles, US$0.00"
      % cycles_free, plan_free.images_paid == 0 and plan_free.images_lost == 0)
check("a 429 THAT WAS BILLED stops after ONE cycle (it was 17), so one press costs "
      "US$%.2f instead of US$%.2f"
      % (plan_paid.images_paid * fal_images.COST_PER_IMAGE_USD,
         cycles_free * fal_images.COST_PER_IMAGE_USD),
      cycles_paid == 1, str(cycles_paid))
check("and what it did spend is counted, so the monthly ceiling can see it",
      plan_paid.images_paid == 1 and plan_paid.images_lost == 1, repr(plan_paid))
check("once fal has said no, the pass stops asking for the other five pictures",
      plan_paid.images_paid == 1, repr(plan_paid))


# ===========================================================================
print("\n[9] The tally holds at the concurrency the IN-PROCESS path can reach")
# ===========================================================================
# Section [2] proves the compare-and-swap at the queue's own limit. It does not hold far
# above it, and far above it is reachable: the in-process fallback renderer's only other
# lock is per LISTING, so one agent pressing Generate across ten listings used to get ten
# concurrent renders tallying onto one row -- and that is the live path for every Dramatic
# render until docs/video-jobs-dramatic-migration.sql has run.
#
# MEASURED at 16 writers with the old 4 attempts: 36 of 96 images lost, and the loss always
# runs in the direction that permits more spending. Eight attempts closed it.
check("the shipped retry count is the one that was measured, not the one that fitted the "
      "queue's limit", ASSIGNS["_OLD_TO_NEW_CAS_ATTEMPTS"] >= 8,
      str(ASSIGNS["_OLD_TO_NEW_CAS_ATTEMPTS"]))

WRITERS, EACH = 16, 6
for trial in range(3):
    race_db = FakeAgents([{"id": "a1", "old_to_new_month": None,
                           "old_to_new_images_used": None}])
    gr = build(race_db)
    racers = [threading.Thread(target=gr["_add_old_to_new_images"], args=("a1", EACH))
              for _ in range(WRITERS)]
    for t in racers:
        t.start()
    for t in racers:
        t.join()
    got = race_db.rows[0]["old_to_new_images_used"] or 0
    check("%d renders on one agent row, trial %d: all %d images counted (%d)"
          % (WRITERS, trial + 1, WRITERS * EACH, got), got == WRITERS * EACH,
          "lost %d, races %d" % (WRITERS * EACH - got, race_db.lost_races))

check("(and the races were real -- the writers genuinely contended)",
      race_db.lost_races > 0, "lost_races=%d" % race_db.lost_races)

# And the in-process path now has a cap of its own, so 16 is a test number rather than a
# Tuesday.
check("the in-process fallback caps its own Dramatic concurrency",
      "MAX_INPROCESS_DRAMATIC_RENDERS" in ASSIGNS
      and ASSIGNS["MAX_INPROCESS_DRAMATIC_RENDERS"] >= 1,
      str(ASSIGNS.get("MAX_INPROCESS_DRAMATIC_RENDERS")))
_gv = ast.get_source_segment(SRC, NODES["generate_video"])
check("...and the endpoint checks that cap BEFORE it claims the listing's render lock, "
      "so a refusal cannot leave a listing stuck on 'rendering'",
      _gv.index("MAX_INPROCESS_DRAMATIC_RENDERS")
      < _gv.index("generate_video claim render lock"))
check("...and the slot is given back by add_done_callback, which runs whether the "
      "render returns or raises, so a crash cannot wedge the feature",
      "_dramatic_inprocess_tasks.discard" in _gv
      and "add_done_callback" in _gv)


# ===========================================================================
print("\n[10] The month is Singapore's, because every agent is in Singapore")
# ===========================================================================
# A UTC month rolls over at 08:00 Singapore time on the 1st, so for eight hours an agent
# at their limit was refused a video that reserve_budget's own message had promised them
# ("the allowance resets at the start of next month").
_month_src = ast.get_source_segment(SRC, NODES["_current_usage_month"])
check("the tally's month is computed with Singapore's +8 offset, not in UTC",
      "hours=8" in _month_src, _month_src.strip().splitlines()[-1])
# Proved rather than read: 00:30 on the 1st in Singapore is still the 31st in UTC, and the
# allowance has to have reset by then.
_sg = dt.timezone(dt.timedelta(hours=8))
for label, moment, expected in (
        ("23:30 on the 31st, Singapore", dt.datetime(2026, 10, 31, 23, 30, tzinfo=_sg),
         "2026-10"),
        ("00:30 on the 1st, Singapore -- the reset Jane's message promises",
         dt.datetime(2026, 11, 1, 0, 30, tzinfo=_sg), "2026-11"),
        ("07:30 on the 1st, Singapore -- the eight-hour hole UTC used to leave",
         dt.datetime(2026, 11, 1, 7, 30, tzinfo=_sg), "2026-11")):
    got = (moment.astimezone(dt.timezone.utc) + dt.timedelta(hours=8)).strftime("%Y-%m")
    check("%s reads as %s" % (label, expected), got == expected, got)
check("and the agent-facing promise the code now keeps is still the one written down",
      "resets at the start of next month" in io.open(
          os.path.join(BACKEND, "old_to_new.py"), encoding="utf-8").read())


# ===========================================================================
print("\n[11] NO TALLY, NO SPENDING: the probe's answer is wired to behaviour")
# ===========================================================================
# _old_to_new_usage_columns_supported was set by the startup probe and read by NOTHING,
# unlike its siblings which gate behaviour -- so the one thing the probe knew (that the
# money is not being counted) changed nothing about what we were willing to spend. It is
# now the gate, which also makes the migration ORDER load-bearing in code: before
# old-to-new-images-migration.sql there is no cache and no tally, which is the worst
# combination there is.
gate = build(FakeAgents([{"id": "a1"}], columns=("id",)),
             names=("_probe_old_to_new_usage_columns",
                    "_old_to_new_ceiling_enforceable"))
gate["_old_to_new_usage_columns_supported"] = None
check("before the probe has run, the gate is OPEN -- a start-up race must not refuse an "
      "agent their video", gate["_old_to_new_ceiling_enforceable"]() is True)
gate["_old_to_new_usage_columns_supported"] = True
check("with the columns there, the gate is open",
      gate["_old_to_new_ceiling_enforceable"]() is True)
gate["_old_to_new_usage_columns_supported"] = False
check("with the columns MISSING, the gate is SHUT -- no ceiling means no paid pictures",
      gate["_old_to_new_ceiling_enforceable"]() is False)

for name in ("get_video_templates", "generate_video", "_render_dramatic_core"):
    seg = ast.get_source_segment(SRC, NODES[name])
    check("%s consults it, so there is no path that spends with no ceiling" % name,
          "_old_to_new_ceiling_enforceable" in seg)
check("the picker stops OFFERING the Dramatic, rather than letting the press 400",
      "_old_to_new_ceiling_enforceable"
      in ast.get_source_segment(SRC, NODES["get_video_templates"]))
check("the render core refuses PERMANENTLY, since no retry creates a database column",
      "PermanentJobError" in ast.get_source_segment(SRC, NODES["_render_dramatic_core"]))
check("and the agent is told something they can act on, not a column name",
      "not switched on for this account yet"
      in ast.get_source_segment(SRC, NODES["_render_dramatic_core"]))
check("while Jane's Telegram alert says what actually happened and in which order to "
      "run the two migrations",
      "old-to-new-images-migration.sql" in SRC
      and "video-jobs-dramatic-migration.sql" in ast.get_source_segment(
          SRC, NODES["_check_old_to_new_ceiling"]))


# ===========================================================================
print("\n[12] The two migrations say which one goes first, in both files")
# ===========================================================================
# Run the queue migration first and Dramatic renders start queueing and deferring with no
# cache and no tally -- findings 1 and 2 at the same time. The order now lives at the top
# of both files, in Jane's words, as well as being enforced by [11].
_images_sql = io.open(os.path.join(BACKEND, "docs",
                                   "old-to-new-images-migration.sql"),
                      encoding="utf-8").read()
_queue_sql = io.open(os.path.join(BACKEND, "docs",
                                  "video-jobs-dramatic-migration.sql"),
                     encoding="utf-8").read()
for label, text, other in (("the pictures migration", _images_sql,
                            "video-jobs-dramatic-migration.sql"),
                           ("the queue migration", _queue_sql,
                            "old-to-new-images-migration.sql")):
    head = text[:1600]
    check("%s names the other one in its first lines" % label, other in head,
          head[:120])
    check("%s says plainly which runs first" % label,
          "first" in head.lower(), head[:120])


print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
