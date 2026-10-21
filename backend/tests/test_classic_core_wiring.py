"""_render_video_core in main.py: what a Classic render stores, and where.

This is the Classic's wiring guard. main.py cannot be imported here (no supabase,
anthropic or fitz on this machine), so these checks EXECUTE the real source of
_render_video_core against stubs -- the same technique tests/test_video_photo_choice.py
uses for generate_video -- and assert what it actually writes.

What is being protected:

1. The finished mp4 goes to videos/{listing_id}.mp4 and NOWHERE else. Both callers
   (the queue and the legacy in-process path) share this one function, so this is the
   single place the object path is decided.
2. It writes the listing's video_* columns and nothing else.
3. The degradations column is written only when the row HAS it, so there is no failing
   write on a database where the column has not been added.
4. Ownership is fenced TWICE -- before the upload and before the listing write -- and a
   reclaimed render raises instead of writing.

(Carved out of tests/test_signature_wiring.py when the Signature tier was withdrawn on
2026-10-21. That file's section [1] was the Classic's guard, which is this; the rest of
it tested the tier and went with it. The renderer's own output is pinned separately and
command-for-command by tests/test_classic_render.py.)
"""
import ast
import asyncio
import io
import logging
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, BACKEND)

import video_renderer as REAL_VR          # noqa: E402  (real validators, real notes)

MAIN = os.path.join(BACKEND, "main.py")
SRC = io.open(MAIN, encoding="utf-8").read()
LINES = SRC.splitlines()
TREE = ast.parse(SRC)

NODES = {n.name: n for n in ast.walk(TREE)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def seg(name):
    n = NODES[name]
    return "\n".join(LINES[n.lineno - 1:n.end_lineno])


def load(names, ns):
    for name in names:
        exec(compile(seg(name), "main.py:%s" % name, "exec"), ns)
    return ns


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
LOG = logging.getLogger("core-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class FakeVideoJobs:
    """Only what the code under test touches."""
    JOB_TYPE_CLASSIC = "classic"

    class SupersededError(Exception):
        pass

    class PermanentJobError(Exception):
        pass

    class DeferJob(Exception):
        pass

    def __init__(self, available=True, enqueue_result=None, enqueue_raises=None,
                 active=None, latest_done=None, describe=None):
        self._available = available
        self._enqueue_result = enqueue_result
        self._enqueue_raises = enqueue_raises
        self._active = active
        self._latest_done = latest_done
        self._describe = describe if describe is not None else {}
        self.enqueued = []
        self.heartbeats = 0
        self.recorded = []

    def available(self):
        return self._available

    def enqueue(self, job_type, listing_id, agent_id, payload):
        if self._enqueue_raises:
            raise self._enqueue_raises
        self.enqueued.append((job_type, listing_id, agent_id, payload))
        job, created = self._enqueue_result
        if created and not job.get("payload"):
            # A real insert returns the row it wrote, payload and all. The honesty rule
            # reads the job's payload rather than the request's, so the fake has to
            # behave the same way or the test would be testing itself.
            job = dict(job, payload=dict(payload))
        return job, created

    def describe(self, job):
        return dict(self._describe)

    def find_active(self, job_type, listing_id):
        return self._active

    def find_latest_done(self, job_type, listing_id):
        return self._latest_done

    def heartbeat(self, job):
        self.heartbeats += 1
        return True

    def record_degradations(self, job, degs):
        self.recorded.append(list(degs))


def renderer_stub(result=None, raises=None):
    """The real video_renderer, with only render_property_video replaced."""
    keep = ("MAX_VIDEO_PHOTOS", "is_auto_music_choice", "resolve_pickable_track",
            "is_auto_photo_choice", "resolve_photo_choice", "photo_choice_note",
            "RenderSlotBusy", "PHOTO_NOTE_GONE", "PHOTO_NOTE_PARTIAL")
    stub = types.SimpleNamespace(**{k: getattr(REAL_VR, k) for k in keep})
    stub.calls = []

    def render_property_video(**kwargs):
        stub.calls.append(kwargs)
        if raises:
            raise raises
        return result or (b"mp4-bytes", [], {})

    stub.render_property_video = render_property_video
    return stub


class Chain:
    """Records one postgrest call chain without touching a database."""

    def __init__(self, rec):
        self.rec = rec

    def table(self, name):
        self.rec["table"] = name
        return self

    def select(self, *a, **k):
        self.rec["select"] = a[0] if a else "*"
        return self

    def update(self, payload):
        self.rec["update"] = payload
        return self

    def insert(self, payload):
        self.rec["insert"] = payload
        return self

    def eq(self, key, value):
        self.rec.setdefault("eq", []).append((key, value))
        return self

    def neq(self, key, value):
        return self

    def order(self, *a, **k):
        return self

    def limit(self, n):
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.rec.get("_rows", []))


def recorder(rows_for=None, raises_for=None):
    """A db_execute/db_execute_async pair that records every write.

    rows_for maps a `what=` string to the rows that select should return; anything not
    named returns no rows. raises_for maps a `what=` to an exception to raise.
    """
    calls = []
    rows_for = rows_for or {}
    raises_for = raises_for or {}

    def run(build, what="", **kw):
        rec = {}
        build(Chain(rec))
        calls.append(dict(rec, what=what))
        if what in raises_for:
            raise raises_for[what]
        return types.SimpleNamespace(data=list(rows_for.get(what, [])))

    async def run_async(build, what="", **kw):
        return run(build, what=what, **kw)

    return calls, run, run_async


def base_ns(**extra):
    ns = dict(
        logger=LOG, asyncio=asyncio, os=os, io=io, re=__import__("re"),
        time=__import__("time"), tempfile=__import__("tempfile"),
        threading=__import__("threading"),
        uuid=__import__("uuid"), requests=None,
        datetime=__import__("datetime").datetime,
        timezone=__import__("datetime").timezone,
        timedelta=__import__("datetime").timedelta,
        Optional=__import__("typing").Optional, List=__import__("typing").List,
        DEFAULT_VIDEO_TEMPLATE_ID="classic",
        _resolve_video_template_id=lambda v: v if v in ("classic", "driftcut") else None,
        _to_number=lambda v: float(v or 0),
        _format_price_millions=lambda v: "4.2M",
        _listing_district_label=lambda listing: "District 15",
        # Studio rule (condo-family "0" bedrooms). The fixture is a Semi-Detached, so the
        # real helper would say False too; stubbed because main.py is executed piecemeal.
        _listing_is_studio=lambda listing: False,
        # NestList Home end-slate overlay: a landed fixture gets the plain contact back.
        _home_video_overlay=lambda listing, agent, price_text, stats: (
            (agent or {}).get("contact", ""), price_text, stats),
        # NestList Home CEA keys on profile responses (not what this file tests).
        _with_cea_fields=lambda out, agent: out,
        apply_listing_copy_guards=lambda text, context=None: text,
        _agent_caption_overrides=lambda stored: {},
        get_current_agent=lambda: None,
    )
    from fastapi import Depends, HTTPException, Query
    ns.update(Depends=Depends, HTTPException=HTTPException, Query=Query)
    ns.update(extra)
    return ns

LISTING = {
    "id": "L1", "images": ["https://img/1.jpg", "https://img/2.jpg"],
    "price": 4200000, "built_up": 3000, "bedrooms": "4", "bathrooms": "3",
    "property_type": "Semi-Detached", "video_template_id": "classic",
    "video_captions": None,
}
AGENT = {"id": "a1", "name": "Janel", "contact": "9123 4567",
         "photo_url": "https://img/face.jpg"}


def migrated(listing, with_degradations=True):
    """A listing row as it reads with (or without) the video_degradations column."""
    row = dict(listing)
    if with_degradations:
        row["video_degradations"] = []
    return row


def run_core(listing_row, ownership=None, render_result=None, jobs=None, vr=None):
    """Execute the real _render_video_core against stubs."""
    jobs = jobs or FakeVideoJobs()
    vr = vr or renderer_stub(result=render_result)
    calls, run, run_async = recorder(
        rows_for={"_render_video_job listings select": [listing_row]})
    stored = []

    def store(filename, video_bytes):
        stored.append((filename, video_bytes))
        return "https://cdn/%s?v=abcd1234" % filename

    ns = base_ns(db_execute=run, db_execute_async=run_async, video_jobs=jobs,
                 video_renderer=vr, _store_rendered_video=store)
    load(["_render_video_core"], ns)
    result = ns["_render_video_core"](
        "L1", AGENT, listing_row, "classic", False, 0, ownership_check=ownership)
    return result, stored, calls, vr, jobs


def writes(calls):
    return [c for c in calls if "update" in c]


def written_keys(calls):
    keys = set()
    for c in writes(calls):
        keys.update(c["update"].keys())
    return keys


# ===========================================================================
print("\n[1] a Classic render stores one film, in one place, under its own columns")
# ===========================================================================
(result, stored, calls, vr, _) = run_core(migrated(LISTING))
check("renders and returns (url, degradations)",
      result[0].startswith("https://cdn/videos/L1.mp4") and result[1] == [], str(result))
check("stored at videos/{listing_id}.mp4, and that is the only upload",
      stored and len(stored) == 1 and stored[0][0] == "videos/L1.mp4", str(stored[:1]))
check("the finished write names the video_* columns",
      {"video_url", "video_status", "video_error"} <= written_keys(calls),
      str(sorted(written_keys(calls))))
check("done is recorded on video_status",
      any(c["update"].get("video_status") == "done" for c in writes(calls)))
check("degradations go to video_degradations",
      any(c["update"].get("video_degradations") == [] for c in writes(calls)))
check("nothing else is written to the listing",
      written_keys(calls) <= {"video_url", "video_status", "video_error",
                              "video_degradations", "video_captions",
                              "video_template_id"},
      str(sorted(written_keys(calls))))
check("the renderer is handed this listing's photos and the agent's details",
      vr.calls and vr.calls[0].get("image_urls") == LISTING["images"]
      and vr.calls[0].get("agent_name") == "Janel", str(vr.calls[:1]))

# ===========================================================================
print("\n[2] a row WITHOUT the degradations column gets no degradations write")
# ===========================================================================
# The state a database is in before that column is added. A failing write here would
# cost the agent the video they just waited for, so the write is conditional on the
# freshly-read row actually having the key.
(_, _, calls, _, _) = run_core(migrated(LISTING, with_degradations=False))
check("no degradations write at all", not any("video_degradations" in c["update"]
                                              for c in writes(calls)),
      str(sorted(written_keys(calls))))
check("but the video is still recorded",
      any(c["update"].get("video_status") == "done" for c in writes(calls)))

# ===========================================================================
print("\n[3] ownership is fenced twice, before anything the agent can see")
# ===========================================================================
order = []


def ownership_true():
    order.append("check")
    return True


(_, stored, calls, _, _) = run_core(migrated(LISTING), ownership=ownership_true)
check("both guards ran", order.count("check") == 2, str(order))

lost = []


def ownership_lost():
    lost.append(1)
    return False


try:
    run_core(migrated(LISTING), ownership=ownership_lost)
    check("a reclaimed render raises instead of writing", False, "it returned")
except Exception as e:
    check("a reclaimed render raises SupersededError before the upload",
          type(e).__name__ == "SupersededError", type(e).__name__)

# ===========================================================================
print("\n[4] the legacy in-process path reaches the same core")
# ===========================================================================
legacy = seg("_render_video_job")
check("it calls _render_video_core", "_render_video_core(" in legacy)
check("and deliberately supplies no ownership check (nothing to race with)",
      "ownership_check" not in legacy)
check("the queued path does supply one",
      "ownership_check=lambda: video_jobs.heartbeat(job)" in seg("_classic_job_run"))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
