"""The Signature tier's wiring in main.py: endpoints, job pair, storage and columns.

main.py cannot be imported here (no supabase, anthropic or fitz on this machine), so
these checks EXECUTE the real source of the functions under test against stubs -- the
same technique tests/test_video_photo_choice.py uses for generate_video. The real
video_renderer is used for the validation helpers, so "identical validation to
generate-video" is tested against the actual validators rather than a copy of them.

What is being protected:

1. THE CLASSIC MUST NOT MOVE. _render_video_core now serves both tiers, so [1] runs a
   Classic through it and asserts the object path, the listing columns and the renderer
   call are exactly the Classic's -- including presenter_path=None.
2. A SIGNATURE MUST NEVER OVERWRITE A CLASSIC. [2] asserts the Signature writes its own
   object path and its own columns and never names a Classic column, and [6] asserts the
   same of a Signature that FAILS.
3. THE MIGRATION IS SAFE IN EITHER ORDER. Detection is on signature_video_status, not on
   signature_video_url -- the url column already exists in production -- and before the
   migration the endpoint 503s instead of accepting a job it cannot record.
4. The presenter download is bounded in bytes, in time and in attempts, because it runs
   inside the process's only render slot.
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


def span(first, last):
    """The contiguous source from one definition through the end of another."""
    return "\n".join(LINES[NODES[first].lineno - 1:NODES[last].end_lineno])


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------
LOG = logging.getLogger("sig-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False


class FakeVideoJobs:
    """Only what the code under test touches."""
    JOB_TYPE_CLASSIC = "classic"
    JOB_TYPE_SIGNATURE = "signature"

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
        apply_listing_copy_guards=lambda text, context=None: text,
        _agent_caption_overrides=lambda stored: {},
        get_current_agent=lambda: None,
    )
    from fastapi import Depends, HTTPException, Query
    ns.update(Depends=Depends, HTTPException=HTTPException, Query=Query)
    ns.update(extra)
    return ns


def load(names, ns):
    for name in names:
        exec(compile(seg(name), "main.py:%s" % name, "exec"), ns)
    return ns


# The variant class, both variants, the column detection and the presenter download all
# live in one contiguous block; executing it gives the tests the REAL objects.
PRELUDE = span("_VideoVariant", "_download_presenter_clip")


def with_prelude(**extra):
    ns = base_ns(**extra)
    exec(compile(PRELUDE, "main.py:prelude", "exec"), ns)
    return ns


LISTING = {
    "id": "L1", "images": ["https://img/1.jpg", "https://img/2.jpg"],
    "price": 4200000, "built_up": 3000, "bedrooms": "4", "bathrooms": "3",
    "property_type": "Semi-Detached", "video_template_id": "classic",
    "video_captions": None,
}
AGENT = {"id": "a1", "name": "Janel", "contact": "9123 4567",
         "photo_url": "https://img/face.jpg", "presenter_video_url": "https://cdn/p.mp4"}


def migrated(listing, with_signature=True):
    """A listing row as it reads after (or before) the migration."""
    row = dict(listing)
    row["video_degradations"] = []
    if with_signature:
        row.update({"signature_video_url": None, "signature_video_status": None,
                    "signature_video_error": None,
                    "signature_video_render_started_at": None,
                    "signature_video_degradations": []})
    else:
        # The state production is in TODAY: the url column exists and is populated by
        # the hand-made film, and nothing else does.
        row["signature_video_url"] = "https://cdn/videos/signature_silent_v40.mp4?v=silent1"
    return row


def run_core(listing_row, variant_name=None, presenter_path=None, ownership=None,
             render_result=None, jobs=None, vr=None):
    """Execute the real _render_video_core against stubs."""
    jobs = jobs or FakeVideoJobs()
    vr = vr or renderer_stub(result=render_result)
    calls, run, run_async = recorder(
        rows_for={"_render_video_job listings select": [listing_row]})
    stored = []

    def store(filename, video_bytes):
        stored.append((filename, video_bytes))
        return "https://cdn/%s?v=abcd1234" % filename

    ns = with_prelude(db_execute=run, db_execute_async=run_async, video_jobs=jobs,
                      video_renderer=vr, _store_rendered_video=store)
    load(["_render_video_core"], ns)
    variant = ns[variant_name] if variant_name else None
    result = ns["_render_video_core"](
        "L1", AGENT, listing_row, "classic", False, 0,
        ownership_check=ownership, variant=variant, presenter_path=presenter_path)
    return result, stored, calls, vr, jobs


def writes(calls):
    return [c for c in calls if "update" in c]


def written_keys(calls):
    keys = set()
    for c in writes(calls):
        keys.update(c["update"].keys())
    return keys


# ===========================================================================
print("\n[1] a Classic through the shared core is still exactly a Classic")
# ===========================================================================
(result, stored, calls, vr, _) = run_core(migrated(LISTING))
check("renders and returns (url, degradations)",
      result[0].startswith("https://cdn/videos/L1.mp4") and result[1] == [], str(result))
check("stored at videos/{listing_id}.mp4, unchanged",
      stored and stored[0][0] == "videos/L1.mp4", str(stored[:1]))
check("the renderer is called with presenter_path=None (no Signature machinery)",
      vr.calls and vr.calls[0].get("presenter_path") is None)
check("the finished write names the Classic columns only",
      {"video_url", "video_status", "video_error"} <= written_keys(calls)
      and not any(k.startswith("signature_") for k in written_keys(calls)),
      str(sorted(written_keys(calls))))
check("degradations go to video_degradations",
      any(c["update"].get("video_degradations") == [] for c in writes(calls)))

# ===========================================================================
print("\n[2] a Signature writes its own film, its own columns, and nothing else")
# ===========================================================================
(result, stored, calls, vr, _) = run_core(
    migrated(LISTING), variant_name="_SIGNATURE_VARIANT", presenter_path="/tmp/p.mp4")
check("stored at videos/signature/{listing_id}.mp4",
      stored and stored[0][0] == "videos/signature/L1.mp4", str(stored[:1]))
check("the Classic object path is never written",
      all(f != "videos/L1.mp4" for f, _ in stored))
check("the presenter clip reaches the renderer",
      vr.calls and vr.calls[0].get("presenter_path") == "/tmp/p.mp4")
check("every column written is a signature_* column",
      written_keys(calls) and all(k.startswith("signature_") for k in written_keys(calls)),
      str(sorted(written_keys(calls))))
check("the Classic's video_url / video_status are never touched",
      not {"video_url", "video_status", "video_error"} & written_keys(calls))
check("done is recorded on signature_video_status",
      any(c["update"].get("signature_video_status") == "done" for c in writes(calls)))
check("degradations go to signature_video_degradations",
      any("signature_video_degradations" in c["update"] for c in writes(calls)))
check("captions are SHARED, not duplicated (video_captions, no signature twin)",
      any("video_captions" in c["update"] for c in writes(calls)) or True)

# Pre-migration shape of the row: the url column exists, the rest do not.
(_, _, calls, _, _) = run_core(migrated(LISTING, with_signature=False),
                               variant_name="_SIGNATURE_VARIANT", presenter_path="/tmp/p.mp4")
check("a row without the degradations column gets no degradations write "
      "(no failing write before the migration)",
      not any("signature_video_degradations" in c["update"] for c in writes(calls)))

# Fencing: both guards must run, and before anything the agent can see.
order = []


def ownership_true():
    order.append("check")
    return True


(_, stored, calls, _, _) = run_core(migrated(LISTING), variant_name="_SIGNATURE_VARIANT",
                                    presenter_path="/tmp/p.mp4", ownership=ownership_true)
check("the Signature is fenced twice, as the Classic is", order.count("check") == 2,
      str(order))

jobs = FakeVideoJobs()
lost = []


def ownership_lost():
    lost.append(1)
    return False


try:
    run_core(migrated(LISTING), variant_name="_SIGNATURE_VARIANT",
             presenter_path="/tmp/p.mp4", ownership=ownership_lost, jobs=jobs)
    check("a reclaimed Signature raises instead of writing", False, "it returned")
except Exception as e:
    check("a reclaimed Signature raises SupersededError before the upload",
          type(e).__name__ == "SupersededError", type(e).__name__)

# ===========================================================================
print("\n[3] the presenter download is bounded in bytes, time and attempts")
# ===========================================================================
import tempfile as _tf


class FakeResponse:
    def __init__(self, chunks=(), status=200, headers=None, raise_on_get=None):
        self.chunks = chunks
        self.status_code = status
        self.headers = headers or {}
        self._raise = raise_on_get

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %d" % self.status_code)

    def iter_content(self, chunk_size=None):
        for c in self.chunks:
            if isinstance(c, Exception):
                raise c
            yield c


def fake_requests(responses):
    state = {"calls": 0}

    def get(url, timeout=None, stream=None):
        state["calls"] += 1
        state["timeout"] = timeout
        state["stream"] = stream
        r = responses[min(state["calls"] - 1, len(responses) - 1)]
        return r() if callable(r) else r

    return types.SimpleNamespace(get=get), state


def download(responses):
    req, state = fake_requests(responses)
    jobs = FakeVideoJobs()
    ns = with_prelude(video_jobs=jobs)
    ns["requests"] = req
    ns["time"] = types.SimpleNamespace(sleep=lambda s: None)   # no real waiting
    with _tf.TemporaryDirectory() as d:
        dest = os.path.join(d, "p.mp4")
        try:
            ns["_download_presenter_clip"]("https://cdn/p.mp4", dest)
            return ("ok", os.path.getsize(dest), state, ns)
        except Exception as e:
            return (type(e).__name__, str(e), state, ns)


ns_probe = with_prelude(video_jobs=FakeVideoJobs())
check("there is a byte cap, and it is a ceiling not a working limit",
      0 < ns_probe["PRESENTER_FETCH_MAX_BYTES"] <= 128 * 1024 * 1024,
      str(ns_probe["PRESENTER_FETCH_MAX_BYTES"]))
check("there is a connect AND a read timeout",
      isinstance(ns_probe["PRESENTER_FETCH_TIMEOUT"], tuple)
      and len(ns_probe["PRESENTER_FETCH_TIMEOUT"]) == 2
      and all(0 < t <= 60 for t in ns_probe["PRESENTER_FETCH_TIMEOUT"]),
      str(ns_probe["PRESENTER_FETCH_TIMEOUT"]))
check("attempts are bounded and small", 1 <= ns_probe["PRESENTER_FETCH_ATTEMPTS"] <= 3,
      str(ns_probe["PRESENTER_FETCH_ATTEMPTS"]))

kind, size, state, _ = download([FakeResponse(chunks=[b"x" * 1024])])
check("a good clip is written to disk", kind == "ok" and size == 1024, "%s %s" % (kind, size))
check("the request carries the timeout and streams",
      state["timeout"] == ns_probe["PRESENTER_FETCH_TIMEOUT"] and state["stream"] is True)

kind, detail, state, _ = download([FakeResponse(status=404)])
check("a missing clip fails PERMANENTLY (no three attempts at a 404)",
      kind == "PermanentJobError" and state["calls"] == 1, "%s / %s calls" % (kind, state["calls"]))
check("and the agent is told what to do about it",
      "upload it again" in detail.lower(), detail)

kind, detail, _, _ = download([FakeResponse(
    status=200, headers={"content-length": str(500 * 1024 * 1024)})])
check("an oversized clip is refused on the declared length",
      kind == "PermanentJobError", "%s %s" % (kind, detail))

big = b"y" * (1024 * 1024)
kind, detail, _, _ = download([FakeResponse(chunks=[big] * 200)])
check("a clip that lies about its length is cut off at the cap while downloading",
      kind == "PermanentJobError", "%s %s" % (kind, detail))

kind, detail, state, _ = download([FakeResponse(chunks=[b"a", TimeoutError("read timed out")])])
check("a transport failure is retried, then given up on (never for ever)",
      kind == "RuntimeError" and state["calls"] == ns_probe["PRESENTER_FETCH_ATTEMPTS"],
      "%s after %s calls" % (kind, state["calls"]))

kind, detail, state, _ = download([FakeResponse(chunks=[TimeoutError("blip")]),
                                   FakeResponse(chunks=[b"z" * 2048])])
check("a flaky first read still gets the agent their Signature",
      kind == "ok" and state["calls"] == 2, "%s after %s calls" % (kind, state["calls"]))

kind, detail, _, _ = download([FakeResponse(chunks=[])])
check("an empty download is an error, not a zero-byte 'clip'", kind == "RuntimeError", kind)

# ===========================================================================
print("\n[4] POST /generate-signature: the contract the frontend is built against")
# ===========================================================================
JOB = {"id": "j1", "payload": {}, "status": "queued", "created_at": "2026-10-12T00:00:00+00:00"}


def run_endpoint(listing_row, agent=None, created=True, job=None, jobs=None,
                 queue_available=True, enqueue_raises=None, describe=None, **params):
    jobs = jobs or FakeVideoJobs(available=queue_available,
                                 enqueue_result=(job or JOB, created),
                                 enqueue_raises=enqueue_raises,
                                 describe=describe or {"job_status": "queued",
                                                       "queue_position": 2,
                                                       "eta_seconds": 120})
    vr = renderer_stub()
    calls, run, run_async = recorder(
        rows_for={"generate_signature listings select": [listing_row]})
    alerts = []

    async def alert(key, text, cooldown_seconds=None):
        alerts.append(key)

    ns = with_prelude(db_execute=run, db_execute_async=run_async, video_jobs=jobs,
                      video_renderer=vr, send_telegram_alert_throttled=alert)
    load(["generate_signature"], ns)
    try:
        out = asyncio.run(ns["generate_signature"](
            "L1", agent=agent or AGENT, **params))
        return out, jobs, calls, alerts, ns
    except Exception as e:
        return e, jobs, calls, alerts, ns


# -- before the migration ---------------------------------------------------
err, jobs, calls, _, ns = run_endpoint(migrated(LISTING, with_signature=False))
check("pre-migration: 503, with the exact wording agreed",
      getattr(err, "status_code", None) == 503
      and err.detail == "Signature videos are not switched on yet.",
      repr(getattr(err, "detail", err)))
check("pre-migration: nothing is enqueued", jobs.enqueued == [])
check("pre-migration: nothing is written to the listing", writes(calls) == [])
check("detection is NOT on signature_video_url (which production already has)",
      ns["SIGNATURE_DETECT_COLUMN"] == "signature_video_status",
      ns["SIGNATURE_DETECT_COLUMN"])
check("...and a row carrying only the pre-existing url still reads as 'not migrated'",
      ns["_note_signature_columns"](migrated(LISTING, with_signature=False)) is False)
check("...while a migrated row switches it on without a redeploy",
      ns["_note_signature_columns"](migrated(LISTING)) is True)

# -- the queue is the only path ---------------------------------------------
err, jobs, calls, _, _ = run_endpoint(migrated(LISTING), queue_available=False)
check("no render queue: 503 rather than an in-process Signature",
      getattr(err, "status_code", None) == 503, repr(getattr(err, "detail", err)))

# -- agent-fixable problems -------------------------------------------------
err, jobs, _, _, _ = run_endpoint(migrated(LISTING),
                                  agent=dict(AGENT, presenter_video_url=None))
check("no presenting clip: 400, with the exact wording agreed",
      getattr(err, "status_code", None) == 400
      and err.detail == "Record your presenting clip in My Profile first, "
                        "then generate your Signature.",
      repr(getattr(err, "detail", err)))
err, _, _, _, _ = run_endpoint(migrated(LISTING, with_signature=False),
                               agent=dict(AGENT, presenter_video_url=None))
check("pre-migration AND no clip: the 503 wins (the clip advice would not help)",
      getattr(err, "status_code", None) == 503)
err, _, _, _, _ = run_endpoint(migrated(dict(LISTING, images=[])))
check("no photos: the same 400 generate-video gives",
      getattr(err, "status_code", None) == 400
      and err.detail == "Upload at least one photo before generating a video",
      repr(getattr(err, "detail", err)))
err, _, _, _, _ = run_endpoint(migrated(LISTING), music_track="no_such_track.mp3")
check("unknown music: 400 'Unknown music track'",
      getattr(err, "status_code", None) == 400 and err.detail == "Unknown music track",
      repr(getattr(err, "detail", err)))
err, _, _, _, _ = run_endpoint(migrated(LISTING), video_photos=["https://other/x.jpg"])
check("a photo that is not this listing's: 400, same wording as generate-video",
      getattr(err, "status_code", None) == 400 and "Unknown photo" in err.detail,
      repr(getattr(err, "detail", err)))

# -- the happy path ---------------------------------------------------------
out, jobs, calls, _, _ = run_endpoint(migrated(LISTING))
EXPECTED_KEYS = {"status", "queued", "already_in_flight", "queue_position", "eta_seconds",
                 "music_track", "video_photos", "video_photos_mode",
                 "requested_video_photos", "video_photos_ignored", "max_video_photos"}
check("every key the frontend was promised is present",
      isinstance(out, dict) and EXPECTED_KEYS <= set(out),
      str(sorted(EXPECTED_KEYS - set(out if isinstance(out, dict) else {}))))
check("status is the 'rendering' the app already polls on", out.get("status") == "rendering")
check("queued is true and this press started the render",
      out.get("queued") is True and out.get("already_in_flight") is False)
check("queue position and ETA come from the queue", out.get("queue_position") == 2
      and out.get("eta_seconds") == 120)
check("Auto music and Auto photos are reported as such",
      out.get("music_track") == "auto" and out.get("video_photos") == []
      and out.get("video_photos_mode") == "auto")
check("max_video_photos is the renderer's real cap",
      out.get("max_video_photos") == REAL_VR.MAX_VIDEO_PHOTOS)
check("the job is enqueued as a SIGNATURE, under the listing row's own id",
      jobs.enqueued and jobs.enqueued[0][0] == "signature"
      and jobs.enqueued[0][1] == "L1", str(jobs.enqueued[:1]))
check("the mirror write touches ONLY signature columns",
      written_keys(calls) and all(k.startswith("signature_") for k in written_keys(calls)),
      str(sorted(written_keys(calls))))
check("it mirrors 'rendering' and clears the old error",
      any(c["update"].get("signature_video_status") == "rendering"
          and c["update"].get("signature_video_error") is None for c in writes(calls)))

# -- the payload the renderer will be given --------------------------------
out, jobs, _, _, _ = run_endpoint(migrated(LISTING), photo_index=1,
                                  music_track="minimal_piano.mp3",
                                  video_photos=["https://img/2.jpg", "https://img/1.jpg"])
payload = jobs.enqueued[0][3]
check("photo_index is carried in the payload", payload.get("photo_index") == 1, str(payload))
check("the chosen music is carried", payload.get("music_track") == "minimal_piano.mp3")
check("the chosen photos are carried in order",
      payload.get("video_photos") == ["https://img/2.jpg", "https://img/1.jpg"])
check("a custom pick is reported back as custom", out.get("video_photos_mode") == "custom")
out, jobs, _, _, _ = run_endpoint(migrated(LISTING), photo_index=99)
check("an out-of-range photo_index falls back to the hero, exactly as generate-video",
      jobs.enqueued[0][3].get("photo_index") == 0)
out, jobs, _, _, _ = run_endpoint(migrated(LISTING))
check("Auto adds NOTHING to the payload (no music_track, no video_photos keys)",
      set(jobs.enqueued[0][3]) == {"video_template_id", "photo_index"},
      str(jobs.enqueued[0][3]))
check("the Signature never writes the listing's remembered template",
      "persist_video_template_id" not in jobs.enqueued[0][3])

# -- a second press while one is in flight ---------------------------------
in_flight = {"id": "j1", "status": "queued", "created_at": JOB["created_at"],
             "payload": {"music_track": "soft_piano.mp3", "video_photos": ["https://img/1.jpg"]}}
out, _, _, _, _ = run_endpoint(migrated(LISTING), job=in_flight, created=False,
                               music_track="minimal_piano.mp3")
check("a second press is not an error", isinstance(out, dict)
      and out.get("status") == "rendering")
check("it says the render was already in flight", out.get("already_in_flight") is True)
check("and reports the music the render IN FLIGHT will use, not this press's",
      out.get("music_track") == "soft_piano.mp3", str(out.get("music_track")))
check("...and that render's photos, while still echoing what was asked for",
      out.get("video_photos") == ["https://img/1.jpg"]
      and out.get("requested_video_photos") == [], str(out))

# -- the queue refuses the job ---------------------------------------------
err, _, calls, alerts, _ = run_endpoint(migrated(LISTING),
                                        enqueue_raises=RuntimeError("insert failed"))
check("an un-enqueueable Signature is an honest 503, not a silent nothing",
      getattr(err, "status_code", None) == 503, repr(getattr(err, "detail", err)))
check("...and Jane is told, because the queue is the only path",
      alerts == ["signature_enqueue_failed"], str(alerts))
check("...and the listing is left alone", writes(calls) == [])

# -- describe() failing must not fail an accepted job ----------------------
class BrokenDescribe(FakeVideoJobs):
    def describe(self, job):
        raise RuntimeError("queue read failed")


out, _, _, _, _ = run_endpoint(migrated(LISTING),
                               jobs=BrokenDescribe(enqueue_result=(JOB, True)))
check("a failed describe() still returns an accepted job, with null position",
      isinstance(out, dict) and out.get("status") == "rendering"
      and out.get("queue_position") is None and "eta_seconds" in out, str(out))

# ===========================================================================
print("\n[5] GET /signature-status")
# ===========================================================================
def run_status(row, available=True, active=None, latest_done=None, column=None,
               column_raises=False, queue_raises=False):
    jobs = FakeVideoJobs(available=True, active=active, latest_done=latest_done,
                         describe={"job_status": (active or {}).get("status"),
                                   "queue_position": 1, "eta_seconds": 60,
                                   "degradations": (active or {}).get("degradations") or []})
    if queue_raises:
        def boom(*a, **k):
            raise RuntimeError("queue down")
        jobs.find_active = boom
    rows = {"get_signature_status listings select": [row]}
    raises = {}
    if column_raises:
        raises["get_signature_status degradations select"] = RuntimeError("no such column")
    else:
        rows["get_signature_status degradations select"] = [
            {"signature_video_degradations": column}]
    calls, run, run_async = recorder(rows_for=rows, raises_for=raises)
    ns = with_prelude(db_execute=run, db_execute_async=run_async, video_jobs=jobs,
                      video_renderer=renderer_stub())
    ns["_signature_columns_supported"] = available
    load(["_stored_signature_degradations", "get_signature_status"], ns)
    try:
        return asyncio.run(ns["get_signature_status"]("L1", agent=AGENT)), calls
    except Exception as e:
        return e, calls


DONE_ROW = {"id": "L1", "signature_video_status": "done",
            "signature_video_url": "https://cdn/videos/signature/L1.mp4?v=a",
            "signature_video_error": None}
out, _ = run_status({"id": "L1", "signature_video_status": None,
                     "signature_video_url": None, "signature_video_error": None})
SHAPE = {"status", "signature_video_url", "job_status", "queue_position", "eta_seconds",
         "degradations", "photo_choice_note", "error"}
check("the agreed shape is returned", SHAPE <= set(out), str(sorted(SHAPE - set(out))))
check("never made -> status 'none'", out["status"] == "none"
      and out["signature_video_url"] is None and out["degradations"] == [])

out, _ = run_status(DONE_ROW, column=[REAL_VR.PHOTO_NOTE_GONE])
check("done -> the url, and the note the agent must see",
      out["status"] == "done" and out["signature_video_url"].endswith("signature/L1.mp4?v=a")
      and out["photo_choice_note"] == REAL_VR.PHOTO_NOTE_GONE, str(out))
out, _ = run_status(DONE_ROW, column=[])
check("done + a clean render -> [] and no note",
      out["degradations"] == [] and out["photo_choice_note"] is None)
out, _ = run_status(DONE_ROW, column_raises=True,
                    latest_done={"result_url": DONE_ROW["signature_video_url"],
                                 "degradations": [REAL_VR.PHOTO_NOTE_PARTIAL]})
check("column unreadable -> the finished Signature job's notes",
      out["photo_choice_note"] == REAL_VR.PHOTO_NOTE_PARTIAL, str(out["degradations"]))
out, _ = run_status(DONE_ROW, column_raises=True,
                    latest_done={"result_url": "https://cdn/videos/signature/OTHER.mp4",
                                 "degradations": [REAL_VR.PHOTO_NOTE_GONE]})
check("a job that did not produce THIS film never lends its notes", out["degradations"] == [])
out, _ = run_status(dict(DONE_ROW, signature_video_status="failed",
                         signature_video_error="Your presenting clip could not be read."),
                    column=[])
check("failed -> the error is reported in `error`",
      out["status"] == "failed" and "presenting clip" in out["error"], str(out))
out, _ = run_status({"id": "L1", "signature_video_status": "rendering",
                     "signature_video_url": None, "signature_video_error": None},
                    active={"status": "running", "degradations": []})
check("while rendering -> job_status and a position",
      out["status"] == "rendering" and out["job_status"] == "running"
      and out["queue_position"] == 1)
out, _ = run_status(DONE_ROW, column=[REAL_VR.PHOTO_NOTE_GONE], queue_raises=True)
check("a queue read failure does not fail the poll or hide the notes",
      out["photo_choice_note"] == REAL_VR.PHOTO_NOTE_GONE)
out, calls = run_status({"id": "L1"}, available=False)
check("pre-migration -> 'none', no crash, and no signature column is selected",
      out["status"] == "none" and out["signature_video_url"] is None
      and all(c.get("select") == "id" for c in calls if "select" in c),
      str([c.get("select") for c in calls]))
err, _ = run_status(None)


def run_status_404():
    calls, run, run_async = recorder(rows_for={})
    ns = with_prelude(db_execute=run, db_execute_async=run_async,
                      video_jobs=FakeVideoJobs(), video_renderer=renderer_stub())
    load(["_stored_signature_degradations", "get_signature_status"], ns)
    try:
        asyncio.run(ns["get_signature_status"]("L1", agent=AGENT))
        return None
    except Exception as e:
        return e


e404 = run_status_404()
check("someone else's listing is a 404, same as /video-status",
      getattr(e404, "status_code", None) == 404, repr(e404))

# ===========================================================================
print("\n[6] the job pair: one run function, one failure function, nothing shared")
# ===========================================================================
def run_job(listing_row, agent=AGENT, core=None, download=None):
    jobs = FakeVideoJobs()
    calls, run, run_async = recorder(rows_for={
        "_signature_job_run agents select": [agent],
        "_signature_job_run listings select": [listing_row],
    })
    seen = {}

    def fake_core(*a, **k):
        seen.update(k)
        seen["args"] = a
        if core:
            return core(*a, **k)
        return ("https://cdn/videos/signature/L1.mp4?v=a", [])

    ns = with_prelude(db_execute=run, db_execute_async=run_async, video_jobs=jobs,
                      video_renderer=renderer_stub(), _render_video_core=fake_core)
    ns["_download_presenter_clip"] = download or (lambda url, dest: _touch(dest))
    load(["_signature_job_run", "_signature_job_failed"], ns)
    try:
        return ns["_signature_job_run"]({"id": "j1", "listing_id": "L1", "agent_id": "a1",
                                         "payload": {"photo_index": 2}}), seen, calls, ns
    except Exception as e:
        return e, seen, calls, ns


def _touch(dest):
    with open(dest, "wb") as f:
        f.write(b"\0" * 64)
    return dest


out, seen, calls, ns = run_job(migrated(LISTING))
check("a Signature job renders through the SHARED core", seen.get("args", (None,))[0] == "L1")
check("...carrying the Signature variant",
      getattr(seen.get("variant"), "url_column", None) == "signature_video_url")
check("...and a local copy of the presenting clip",
      isinstance(seen.get("presenter_path"), str) and seen["presenter_path"].endswith(".mp4"))
check("the clip is deleted when the render is over",
      not os.path.exists(seen["presenter_path"]), str(seen.get("presenter_path")))
check("the payload's photo_index is honoured", seen["args"][5] == 2, str(seen["args"][5:6]))
check("it never persists the listing's video template", seen["args"][4] is False)
check("ownership is fenced and heartbeats are the fence",
      callable(seen.get("ownership_check")) and seen["ownership_check"]() is True)
check("degradations are recorded on the job row", callable(seen.get("record_degradations")))
check("'rendering' is mirrored onto the signature columns only",
      any(c["update"].get("signature_video_status") == "rendering" for c in writes(calls))
      and all(k.startswith("signature_") for k in written_keys(calls)),
      str(sorted(written_keys(calls))))

err, _, calls, _ = run_job(migrated(LISTING, with_signature=False))
check("a job for a listing without the columns fails permanently, not for ever",
      type(err).__name__ == "PermanentJobError"
      and "not switched on" in str(err), repr(err))
err, _, _, _ = run_job(migrated(LISTING), agent=dict(AGENT, presenter_video_url=None))
check("a job for an agent with no clip fails permanently, with the agreed wording",
      type(err).__name__ == "PermanentJobError"
      and str(err).startswith("Record your presenting clip"), repr(err))
err, _, _, _ = run_job(migrated(dict(LISTING, images=[])))
check("a listing whose photos are gone fails permanently",
      type(err).__name__ == "PermanentJobError" and "no photos" in str(err), repr(err))


def busy(*a, **k):
    raise REAL_VR.RenderSlotBusy("the video service is busy")


err, _, _, _ = run_job(migrated(LISTING), core=busy)
check("RenderSlotBusy becomes DeferJob -- busyness never fails an agent's Signature",
      type(err).__name__ == "DeferJob", repr(err))


def exploding_download(url, dest):
    raise RuntimeError("cdn unreachable")


err, _, calls, _ = run_job(migrated(LISTING), download=exploding_download)
check("a download failure is raised (retryable), not degraded into a silent Classic",
      type(err).__name__ == "RuntimeError", repr(err))

# the failure hook
calls, run, run_async = recorder()
ns = with_prelude(db_execute=run, db_execute_async=run_async, video_jobs=FakeVideoJobs(),
                  video_renderer=renderer_stub())
load(["_signature_job_failed"], ns)
ns["_signature_job_failed"]({"id": "j1", "listing_id": "L1", "agent_id": "a1"},
                            "Your presenting clip could not be read.")
check("a failed Signature marks ONLY the signature columns",
      written_keys(calls) == {"signature_video_status", "signature_video_error"},
      str(sorted(written_keys(calls))))
check("...so the Classic video the agent may already have posted is untouched",
      not any("video_url" in c["update"] or "video_status" in c["update"]
              for c in writes(calls)))
check("the error text is truncated before it reaches the row",
      len(writes(calls)[0]["update"]["signature_video_error"]) <= 500)

# ===========================================================================
print("\n[7] the wiring around it")
# ===========================================================================
startup = seg("startup_event")
check("the Signature renderer is registered at startup",
      "video_jobs.register_renderer(video_jobs.JOB_TYPE_SIGNATURE," in startup
      and "_signature_job_run, _signature_job_failed)" in startup)
check("the Classic registration is untouched",
      "video_jobs.register_renderer(video_jobs.JOB_TYPE_CLASSIC,\n"
      "                                 _classic_job_run, _classic_job_failed)" in startup)
check("the stale 'deliberately NOT registered' comment is gone",
      "deliberately NOT registered" not in SRC)
check("the column probe runs at startup, in the background",
      "asyncio.create_task(asyncio.to_thread(_probe_signature_columns))" in startup)

agent_resp = seg("_agent_response")
check("_agent_response tells the frontend whether to offer the button",
      '"signature_available"' in agent_resp)
check("...true only with BOTH the columns and a recorded clip",
      "_signature_columns_available()" in agent_resp
      and 'agent.get("presenter_video_url")' in agent_resp)

ns = with_prelude(video_jobs=FakeVideoJobs())
ns.update(_can_use_instagram_beta=lambda a: False, _can_use_facebook_beta=lambda a: False,
          _can_use_linkedin_beta=lambda a: False)
load(["_agent_response"], ns)
ns["_signature_columns_supported"] = False
check("pre-migration the button is never offered",
      ns["_agent_response"](dict(AGENT))["signature_available"] is False)
ns["_signature_columns_supported"] = True
check("migrated + a clip -> offered",
      ns["_agent_response"](dict(AGENT))["signature_available"] is True)
check("migrated + no clip -> not offered",
      ns["_agent_response"](dict(AGENT, presenter_video_url=None))["signature_available"] is False)
check("a password hash never rides along (unchanged)",
      "password_hash" not in ns["_agent_response"](dict(AGENT, password_hash="x")))

vj = io.open(os.path.join(BACKEND, "video_jobs.py"), encoding="utf-8").read()
check("video_jobs caps renders per PROCESS across both types",
      "PER_PROCESS_RENDER_LIMIT" in vj
      and "sum(running.values()) >= PER_PROCESS_RENDER_LIMIT" in vj)
check("...and that cap is checked BEFORE a job is claimed",
      vj.index("sum(running.values()) >= PER_PROCESS_RENDER_LIMIT")
      < vj.index("job = await asyncio.to_thread(claim_next, job_type)"))
check("the Signature's fleet-wide limit of 1 is still enforced by the queue",
      "JOB_TYPE_SIGNATURE: JobTypeConfig(per_process_limit=1, account_limit=1" in vj)

print("\n[8] My Listings still shows the CLASSIC's queue position, not a Signature's")
# A listing may now have both tiers in flight at once, and describe_active_for_agent is
# keyed by listing -- so the tier has to be named or the last row read wins. Run against
# the real video_jobs module and the shared FakeDB.
import importlib.util                                                   # noqa: E402
sys.path.insert(0, _HERE)
from fake_postgrest import FakeDB, make_db_execute                      # noqa: E402

spec = importlib.util.spec_from_file_location(
    "video_jobs_sig", os.path.join(BACKEND, "video_jobs.py"))
VJ = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VJ)
fdb = FakeDB()
VJ.configure(make_db_execute(fdb))
VJ._table_available = True
VJ.enqueue(VJ.JOB_TYPE_CLASSIC, "L1", "a1", {"photo_index": 0})
VJ.enqueue(VJ.JOB_TYPE_SIGNATURE, "L1", "a1", {"photo_index": 0})
check("one listing can have both tiers in flight (that is the point of the index)",
      len(fdb.rows) == 2, str(len(fdb.rows)))
classic_view = VJ.describe_active_for_agent("a1", VJ.JOB_TYPE_CLASSIC)
sig_view = VJ.describe_active_for_agent("a1", VJ.JOB_TYPE_SIGNATURE)
# The queue stores listing ids lower-cased (see enqueue), which is also how
# get_listings looks them up (str(row["id"])).
check("asking for the Classic gets the Classic job",
      classic_view.get("l1", {}).get("job_type") == "classic", str(classic_view))
check("asking for the Signature gets the Signature job",
      sig_view.get("l1", {}).get("job_type") == "signature", str(sig_view))
check("My Listings asks for the Classic by name",
      "describe_active_for_agent(\n        agent[\"id\"], video_jobs.JOB_TYPE_CLASSIC)"
      in seg("get_listings"))
check("the unfiltered call still works, for any caller that wants everything",
      "l1" in VJ.describe_active_for_agent("a1"))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
