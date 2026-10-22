"""Agent-chosen video photos (#13): selection/validation logic, the fail-safe fallbacks,
and the critical no-regression guarantee that the AUTO path is unchanged.

Runs without ffmpeg/supabase. The no-regression check loads the PREVIOUS committed
video_renderer.py (git HEAD of the commit before this feature, see BASELINE_REF) next to
the new one, stubs every ffmpeg / network / model call identically in both, and asserts
the two produce identical bytes, degradations and captions for every automatic request
across many listing shapes. main.py wiring is read with ast (it cannot be imported here).
"""
import ast, importlib.util, io, itertools, logging, os, random, subprocess, sys, tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, BACKEND)
logging.disable(logging.CRITICAL)
import video_renderer as new  # noqa: E402

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))

# The last commit before this feature. Override with BASELINE_REF=<sha> once it has moved.
BASELINE_REF = os.environ.get("BASELINE_REF", "7c6169f")
old = None
try:
    base_src = subprocess.check_output(
        ["git", "show", "%s:backend/video_renderer.py" % BASELINE_REF], cwd=BACKEND,
        stderr=subprocess.DEVNULL).decode("utf-8")
    tmp = os.path.join(tempfile.mkdtemp(), "video_renderer_baseline.py")
    open(tmp, "w", encoding="utf-8").write(base_src)
    spec = importlib.util.spec_from_file_location("video_renderer_baseline", tmp)
    old = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(old)
except Exception as e:
    print("  NOTE  baseline renderer unavailable (%s); skipping old-vs-new comparison" % e)

# ---------------------------------------------------------------- pure helpers
print("\n[helpers] auto detection / normalising / resolving")
for v in (None, "", " ", "auto", " AUTO ", [], [""], ["auto"], ["Auto", ""], (), 5):
    check("auto value %r" % (v,), new.is_auto_photo_choice(v))
for v in ("a.jpg", ["a.jpg"], ["auto", "a.jpg"], ["0"], '["a"]'):
    check("real choice %r" % (v,), not new.is_auto_photo_choice(v))

imgs = ["https://x/a.jpg?v=1", "https://x/b.jpg", "https://x/c.jpg", "https://x/d.jpg"]
check("urls keep the agent's order",
      new.resolve_photo_choice([imgs[2], imgs[0]], imgs) == ([imgs[2], imgs[0]], []))
check("indices resolve to the listing's own urls",
      new.resolve_photo_choice(["3", "1"], imgs) == ([imgs[3], imgs[1]], []))
check("urls and indices can be mixed", new.resolve_photo_choice([imgs[1], "0"], imgs)[0] == [imgs[1], imgs[0]])
check("unknown url and out-of-range index are ignored and reported",
      new.resolve_photo_choice([imgs[0], "https://evil/other.jpg", "9"], imgs)
      == ([imgs[0]], ["https://evil/other.jpg", "9"]))
check("a photo from another listing can never come back",
      new.resolve_photo_choice(["https://other-agent/z.jpg"], imgs)[0] == [])
check("duplicates collapse to the first occurrence",
      new.resolve_photo_choice([imgs[1], imgs[0], imgs[1], "1"], imgs)[0] == [imgs[1], imgs[0]])
check("url match is exact (a different ?v= is a different photo)",
      new.resolve_photo_choice(["https://x/a.jpg?v=2"], imgs)[0] == [])
check("single JSON-array string is accepted",
      new.resolve_photo_choice('["https://x/b.jpg","0"]', imgs)[0] == [imgs[1], imgs[0]])
check("'auto' mixed into a list is dropped, not matched",
      new.resolve_photo_choice(["auto", imgs[1]], imgs)[0] == [imgs[1]])
check("non-string / hostile entries do not crash",
      new.resolve_photo_choice([None, 7, {"a": 1}, "-1", "  ", "１"], [imgs[0], None, 3])[0] == [])
check("no images at all -> nothing resolves", new.resolve_photo_choice(["0"], [])[0] == [])

print("\n[choose_video_photos] render-time fail-safe")
check("auto -> (None, None)", new.choose_video_photos(None, imgs) == (None, None))
check("valid choice honoured silently", new.choose_video_photos([imgs[2], imgs[0]], imgs) == ([imgs[2], imgs[0]], None))
u, n = new.choose_video_photos([imgs[0], "https://gone/x.jpg"], imgs)
check("two chosen, one since deleted -> auto (fewer than two remain) with a note", u is None and n)
u, n = new.choose_video_photos([imgs[0], imgs[1], "https://gone/x.jpg"], imgs)
check("three chosen, one deleted -> the two that remain, with a note", u == [imgs[0], imgs[1]] and n)
u, n = new.choose_video_photos([imgs[1]], imgs)
check("a deliberate single-photo choice is honoured", u == [imgs[1]] and n is None)
u, n = new.choose_video_photos(["https://gone/x.jpg"], imgs)
check("everything chosen deleted -> auto with a note", u is None and n)
many = ["https://x/%d.jpg" % i for i in range(12)]
u, _ = new.choose_video_photos(many, many)
check("over the cap is truncated at render time (never fails)", u == many[:new.MAX_VIDEO_PHOTOS])

# ----------------------------------------------- full-render harness (stubbed)
def install_stubs(mod, unreadable=()):
    calls = []
    def fetch(url):
        if url in unreadable:
            raise IOError("boom")
        return "P:" + url
    def write(path, text):
        open(path, "w").write(text)
    mod._fetch_listing_photo = fetch
    mod._fetch_image = lambda url, attempts=2, deadline=None: "AGENT"
    mod._generate_room_captions = lambda photos, copy_guard=None: (["cap(%s)" % p for p in photos], None)
    mod._caption_font_size = lambda captions: 1
    def contain(photo, clip_path, workdir, i, **kw):
        calls.append((photo, i, kw.get("caption"), kw.get("zoom_in"), kw.get("first"), kw.get("last")))
        write(clip_path, "[%s|%s|%s|%s]" % (photo, i, kw.get("first"), kw.get("last")))
    mod._contain_clip = contain
    def join(paths, out):
        write(out, "".join(open(p).read() for p in paths))
    mod._dissolve_segments = lambda paths, out, wd, t: (join(paths, out), 5.0)[1]
    mod._concat = lambda paths, out, wd, name, t: join(paths, out)
    mod._probe_duration = lambda path, est: est
    mod._render_contact_card = lambda *a, **k: "card"
    mod._build_card_clip = lambda body, card, out, wd: write(out, "<card>")
    mod._select_music_track = lambda seed, pt: (None, None)
    mod._run_ffmpeg = lambda cmd, t: write(cmd[-1], "FINAL:" + open(cmd[cmd.index("-i") + 1]).read())
    return calls

def render(mod, images, photo_index=0, unreadable=(), **kw):
    calls = install_stubs(mod, unreadable)
    try:
        out = mod.render_property_video(images, photo_index=photo_index, agent_name="A",
                                        agent_contact_line="c", agent_photo_url="http://p", **kw)
        return ("ok", out[0], list(out[1]), dict(out[2]), calls)
    except Exception as e:
        return ("err", type(e).__name__ + ":" + str(e), None, None, calls)

if old is not None:
    print("\n[no-regression] auto path identical to the previous renderer (bytes, degradations, captions, per-clip calls)")
    random.seed(13)
    cases = 0; same = 0
    auto_forms = [{}, {"video_photos": None}, {"video_photos": ""}, {"video_photos": "auto"},
                  {"video_photos": []}, {"video_photos": ["auto"]}, {"video_photos": [""]}]
    # Up to the OLD cap only. Past it the auto path deliberately diverges: the cap rose
    # from 6 to 10 on 2026-10-17, so an 8-photo listing now renders 8 shots where the
    # baseline rendered 6. Below the old cap nothing about Auto has moved, and that is
    # still worth proving, because it is where this feature could have broken it.
    # The new behaviour above the old cap is pinned separately, just below.
    for n in range(0, old.MAX_VIDEO_PHOTOS + 1):
        urls = ["https://x/%d.jpg" % i for i in range(n)]
        for pi in (-1, 0, 1, n // 2, n - 1, n, n + 3):
            for bad in ((), tuple(random.sample(urls, min(len(urls), 1 + n // 4))) if urls else ()):
                ref = render(old, urls, pi, bad)
                for form in auto_forms:
                    got = render(new, urls, pi, bad, **form)
                    cases += 1
                    if got == ref:
                        same += 1
                    else:
                        print("   MISMATCH n=%d pi=%d bad=%s form=%s\n    old=%r\n    new=%r" % (n, pi, bad, form, ref[:4], got[:4]))
    check("%d auto-path renders identical to baseline (up to the old cap)" % cases,
          same == cases, "%d/%d" % (same, cases))
    check("harness is not vacuous (a 7-photo render really caps at 6 with hero first)",
          render(old, ["u%d" % i for i in range(7)], 3)[1].count(b"[P:") == 6)

def order(r):
    """The photo urls that actually reached a clip, in clip order."""
    return [c[0][2:] for c in r[4]]

print("\n[auto path, new] the raised cap, and smart selection falling back safely")
# The vision call cannot run here: there is no API key and the stubbed photos are
# strings, not images. That is the point -- this is the degraded path, and it must come
# out as plain upload order, silently, with no degradation shown to the agent.
for n, expected in ((7, 7), (10, 10), (12, 10), (15, 10)):
    r = render(new, ["https://x/%d.jpg" % i for i in range(n)], 0)
    check("%d photos -> %d shots (cap is now %d)" % (n, expected, new.MAX_VIDEO_PHOTOS),
          r[0] == "ok" and r[1].count(b"[P:") == expected,
          "got %d" % r[1].count(b"[P:"))
r = render(new, ["https://x/%d.jpg" % i for i in range(12)], 3)
check("the hero still opens a 12-photo listing", order(r)[0] == "https://x/3.jpg", str(order(r)[:2]))
check("a failed survey is never reported to the agent as a degradation",
      not any("photo" in d and "select" in d for d in r[2]), str(r[2]))
r6 = render(new, ["https://x/%d.jpg" % i for i in range(6)], 0)
check("a listing at or under MIN_VIDEO_PHOTOS does not even try to select",
      r6[0] == "ok" and r6[1].count(b"[P:") == 6)

print("\n[chosen path] full stubbed render")
urls = ["https://x/%d.jpg" % i for i in range(8)]
r = render(new, urls, 0, video_photos=[urls[5], urls[2], urls[7]])
check("exactly the chosen photos, in the chosen order", r[0] == "ok" and order(r) == [urls[5], urls[2], urls[7]], str(order(r)))
check("no degradation on a clean choice", r[2] == [] or r[2] == ["the background music file is missing from the deploy"], str(r[2]))
check("first chosen opens, last chosen closes (fade flags)", r[4][0][4] is True and r[4][-1][5] is True)
r = render(new, urls, 4, video_photos=[urls[1], urls[0]])
check("photo_index is ignored when photos are chosen", order(r) == [urls[1], urls[0]])
check("captions come back for exactly the chosen photos", set(r[3]) == {urls[1], urls[0]})
r = render(new, urls, 0, video_photos=[urls[3]])
check("single chosen photo renders a one-photo video", r[0] == "ok" and order(r) == [urls[3]])
over = ["https://x/o%d.jpg" % i for i in range(new.MAX_VIDEO_PHOTOS + 3)]
r = render(new, over, 0, video_photos=list(reversed(over)))
check("an oversized choice is capped at MAX_VIDEO_PHOTOS in order",
      order(r) == list(reversed(over))[:new.MAX_VIDEO_PHOTOS], str(len(order(r))))

auto = render(new, urls, 0)
r = render(new, urls, 0, video_photos=["https://deleted/1.jpg", "https://deleted/2.jpg"])
check("all chosen photos gone -> same video as Auto, not a failure",
      r[0] == "ok" and r[1] == auto[1] and order(r) == order(auto))
check("...and the agent is told", any("automatic photo selection" in d for d in r[2]), str(r[2]))
r = render(new, urls, 0, unreadable=(urls[2], urls[3]), video_photos=[urls[2], urls[3]])
check("chosen photos all unreadable -> falls back to Auto", r[0] == "ok" and len(order(r)) > 0 and urls[2] not in order(r))
r = render(new, urls, 0, unreadable=(urls[2],), video_photos=[urls[2], urls[3]])
check("two chosen, one unreadable -> Auto rather than a surprise one-photo video",
      r[0] == "ok" and len(order(r)) > 1 and any("could not be read" in d for d in r[2]))
r = render(new, urls, 0, unreadable=(urls[2],), video_photos=[urls[2], urls[3], urls[4]])
check("three chosen, one unreadable -> the two readable ones, in order", order(r) == [urls[3], urls[4]])
r = render(new, urls, 0, unreadable=tuple(urls), video_photos=[urls[2]])
check("nothing readable at all still fails exactly as before", r[0] == "err" and "None of this listing's photos" in r[1])

# ------------------------------------------------------------ main.py wiring
print("\n[main.py wiring]")
src = io.open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
tree = ast.parse(src)
fns = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
seg = lambda n: ast.get_source_segment(src, fns[n])
args = [a.arg for a in fns["generate_video"].args.args]
check("generate_video accepts video_photos", "video_photos" in args)
check("param is an optional repeated query list", "video_photos: Optional[List[str]] = Query(None)" in seg("generate_video"))
gv = seg("generate_video")
check("auto adds nothing to the queue payload (conditional spread)",
      '**({"video_photos": chosen_video_photos} if chosen_video_photos else {})' in gv)
check("legacy fallback path carries the choice", "chosen_music_track,\n        chosen_video_photos or None,\n    ))" in gv)
check("unknown-photo and too-many are 400s", gv.count("status_code=400") >= 4 and "Unknown photo" in gv)
check("queue run passes payload video_photos to the core", 'video_photos=payload.get("video_photos")' in seg("_classic_job_run"))
check("core forwards to the renderer", "video_photos=video_photos" in seg("_render_video_core"))
check("legacy job forwards to the core", "video_photos=video_photos" in seg("_render_video_job"))
check("in-flight job's photos are what the response reports",
      'in_flight_video_photos = list((job.get("payload") or {}).get("video_photos") or [])' in gv
      and '"video_photos": in_flight_video_photos' in gv)

# ------------------------------------------- the REAL generate_video, stubbed
print("\n[endpoint] the shipped generate_video source, executed against stubs")
import asyncio, datetime as _dt, types
from typing import Optional, List
from fastapi import HTTPException, Depends, Query

def _run_endpoint(listing, **params):
    captured = {}
    class FakeJobs:
        JOB_TYPE_CLASSIC = "classic"
        JOB_TYPE_DRAMATIC = "dramatic"
        @staticmethod
        def available(): return True
        @staticmethod
        def enqueue(job_type, listing_id, agent_id, payload):
            captured["payload"] = payload
            return {"id": "j1", "payload": captured.get("existing_payload", payload)}, "existing_payload" not in captured
        @staticmethod
        def describe(job): return {"job_status": "queued", "queue_position": 1}
    async def db_exec(fn, what=""):
        return types.SimpleNamespace(data=[listing])
    async def noop(*a, **k): return None
    g = dict(asyncio=asyncio, datetime=_dt.datetime, timezone=_dt.timezone, timedelta=_dt.timedelta,
             HTTPException=HTTPException, Depends=Depends, Query=Query, Optional=Optional, List=List,
             video_renderer=new, video_jobs=FakeJobs, db_execute_async=db_exec,
             send_telegram_alert_throttled=noop, get_current_agent=lambda: None,
             DEFAULT_VIDEO_TEMPLATE_ID="classic",
             _resolve_video_template_id=lambda t: t if t in ("classic",) else None,
             logger=logging.getLogger("t"), _video_render_tasks=set(),
             _render_video_job=lambda *a: captured.setdefault("legacy_args", a),
             # Added when the Dramatic template arrived (2026-10-22). Stubbed so this
             # file keeps asking its own question -- that the AUTO Classic path is
             # unchanged -- rather than becoming a test of template routing, which
             # test_dramatic_render.py owns.
             UNRELEASED_VIDEO_TEMPLATE_IDS=set(),
             _job_type_for_template=lambda t: "classic",
             _find_active_video_job=lambda listing_id: None,
             fal_images=types.SimpleNamespace(is_enabled=lambda: True),
             _render_dramatic_job=lambda *a: captured.setdefault("legacy_args", a))
    node = fns["generate_video"]
    exec(compile("\n".join(src.splitlines()[node.lineno - 1:node.end_lineno]), "generate_video", "exec"), g)
    kw = dict(video_template_id=None, photo_index=0, music_track=None, video_photos=None, agent={"id": "a1"})
    kw.update(params)
    if "existing" in kw:
        captured["existing_payload"] = kw.pop("existing")
    try:
        resp = asyncio.run(g["generate_video"]("L1", **kw))
    except HTTPException as e:
        resp = ("HTTP", e.status_code, e.detail)
    return resp, captured.get("payload")

L = {"id": "L1", "images": urls[:5], "video_status": None}
base_resp, base_payload = _run_endpoint(L)
check("auto: payload is exactly today's (no video_photos key at all)",
      base_payload == {"video_template_id": "classic", "persist_video_template_id": False, "photo_index": 0}, str(base_payload))
for form in ("auto", "", ["auto"], [""], []):
    r, p = _run_endpoint(L, video_photos=form)
    check("auto form %r -> identical payload, auto response" % (form,),
          p == base_payload and r["video_photos"] == [] and r["video_photos_mode"] == "auto", str((p, r.get("video_photos_mode"))))
r, p = _run_endpoint(L, video_photos=[urls[3], urls[1], "2"])
check("custom: payload carries the validated urls in order (index resolved)",
      p["video_photos"] == [urls[3], urls[1], urls[2]], str(p))
check("custom: response reflects the choice",
      r["video_photos"] == [urls[3], urls[1], urls[2]] and r["video_photos_mode"] == "custom"
      and r["video_photos_ignored"] == [] and r["status"] == "rendering"
      and r["max_video_photos"] == new.MAX_VIDEO_PHOTOS)
r, p = _run_endpoint(L, video_photos=[urls[0], "https://not-mine/z.jpg"])
check("partial unknown: ignored, reported, the rest used",
      p["video_photos"] == [urls[0]] and r["video_photos_ignored"] == ["https://not-mine/z.jpg"])
r, p = _run_endpoint(L, video_photos=["https://not-mine/z.jpg", "99"])
check("nothing valid -> 400 Unknown photo, no job queued", r[0] == "HTTP" and r[1] == 400 and "Unknown photo" in r[2] and p is None)
# One photo past whatever the cap is, so this keeps testing the cap and not the number 6.
over_cap = new.MAX_VIDEO_PHOTOS + 1
big = {"id": "L1", "images": ["https://x/b%d.jpg" % i for i in range(over_cap + 2)]}
r, p = _run_endpoint(big, video_photos=big["images"][:over_cap])
check("more than the cap -> 400, no job queued", r[0] == "HTTP" and r[1] == 400 and p is None)
r, p = _run_endpoint(big, video_photos=big["images"][:new.MAX_VIDEO_PHOTOS])
check("exactly the cap is accepted", p is not None and len(p["video_photos"]) == new.MAX_VIDEO_PHOTOS)
r, p = _run_endpoint(L, video_photos=[urls[2]], existing={"video_template_id": "classic", "video_photos": [urls[4]]})
check("second press while one is in flight reports the render actually running",
      r["video_photos"] == [urls[4]] and r["requested_video_photos"] == [urls[2]] and r["already_in_flight"] is True)
r, p = _run_endpoint(L, existing={"video_template_id": "classic"})
check("an in-flight AUTO job reports auto", r["video_photos"] == [] and r["video_photos_mode"] == "auto")

# ------------------------------------------- audit fix 2: "[]" is an Auto spelling
print("\n[auto spellings] JSON-array strings")
for v in ("[]", ["[]"], ' [ ] ', '["auto"]', ['["auto", ""]'], "[ ]", [" [] ", "auto"]):
    check("JSON-ish auto %r" % (v,), new.is_auto_photo_choice(v) and new.normalize_photo_choice(v) == [])
check("a real JSON array is still a choice", not new.is_auto_photo_choice('["https://x/b.jpg"]'))
check("malformed JSON-looking string is a choice (not silently auto)", not new.is_auto_photo_choice("[oops"))
r, p = _run_endpoint(L, video_photos=["[]"])
check("endpoint: [\"[]\"] -> Auto, no 400, payload unchanged", p == base_payload and r["video_photos_mode"] == "auto")
r, p = _run_endpoint(L, video_photos=['["%s","1"]' % urls[3]])
check("endpoint: JSON-array string choice still works", p["video_photos"] == [urls[3], urls[1]])

# ---------------------------- audit fix 1: the agent can SEE that a pick was not honoured
print("\n[degradations reach the agent] real get_video_status / _stored_video_degradations")
NOTE = new.PHOTO_NOTE_GONE
def _run_status(listing_row, column=None, column_missing=False, jobs_available=True,
                active=None, latest_done=None, queue_raises=False):
    class FJ:
        JOB_TYPE_CLASSIC = "classic"
        @staticmethod
        def available(): return jobs_available
        @staticmethod
        def find_active(t, lid):
            if queue_raises: raise RuntimeError("queue down")
            return active
        @staticmethod
        def describe(job): return {"job_status": job["status"], "degradations": job.get("degradations") or []}
        @staticmethod
        def find_latest_done(t, lid): return latest_done
    async def db_exec(fn, what=""):
        if what == "get_video_status degradations select":
            if column_missing: raise RuntimeError("column video_degradations does not exist")
            return types.SimpleNamespace(data=[{"video_degradations": column}])
        return types.SimpleNamespace(data=[listing_row])
    g = dict(asyncio=asyncio, HTTPException=HTTPException, Depends=Depends, video_jobs=FJ,
             video_renderer=new, db_execute_async=db_exec, logger=logging.getLogger("t"),
             get_current_agent=lambda: None,
             # Since 2026-10-22 there are two render types, so the status path looks for
             # a job of EITHER. The two lookups are loaded from main.py below rather
             # than stubbed, so this still tests the shipped code.
             VIDEO_JOB_TYPES=("classic", "dramatic"))
    for nm in ("_find_active_video_job", "_find_done_video_job",
               "get_video_status", "_stored_video_degradations"):
        n = fns[nm]
        exec(compile("\n".join(src.splitlines()[n.lineno - 1:n.end_lineno]), nm, "exec"), g)
    return asyncio.run(g["get_video_status"]("L1", agent={"id": "a1"}))

done_row = {"video_status": "done", "video_url": "https://v/1.mp4?v=a", "video_error": None, "video_template_id": "classic"}
r = _run_status(done_row, column=[NOTE])
check("done + note on the listing -> degradations and photo_choice_note returned",
      r["degradations"] == [NOTE] and r["photo_choice_note"] == NOTE)
r = _run_status(done_row, column=[])
check("done + clean render -> [] and null", r["degradations"] == [] and r["photo_choice_note"] is None)
r = _run_status(done_row, column_missing=True,
                latest_done={"result_url": done_row["video_url"], "degradations": [NOTE, "x"]})
check("column not migrated -> falls back to the finished job's notes (queued path)",
      r["degradations"] == [NOTE, "x"] and r["photo_choice_note"] == NOTE)
r = _run_status(done_row, column=None,
                latest_done={"result_url": done_row["video_url"], "degradations": [new.PHOTO_NOTE_PARTIAL]})
check("column null -> job fallback too", r["photo_choice_note"] == new.PHOTO_NOTE_PARTIAL)
r = _run_status(done_row, column_missing=True,
                latest_done={"result_url": "https://v/OTHER.mp4", "degradations": [NOTE]})
check("a job that did not produce this video never lends its notes", r["degradations"] == [])
r = _run_status(done_row, column=[NOTE], jobs_available=False)
check("legacy path (no queue): notes come from the listing column", r["photo_choice_note"] == NOTE)
r = _run_status(done_row, column_missing=True, jobs_available=False)
check("legacy path before the migration: [] (cannot know), no crash", r["degradations"] == [] and r["video_status"] == "done")
r = _run_status(done_row, column=[NOTE], queue_raises=True)
check("queue lookup failing does not hide the notes or fail the request", r["photo_choice_note"] == NOTE)
r = _run_status(done_row, column=[], active={"status": "running", "degradations": [NOTE]})
check("finishing window: notes recorded on the still-active job are returned", r["photo_choice_note"] == NOTE)
r = _run_status(dict(done_row, video_status="rendering"), column=[NOTE], active={"status": "running", "degradations": []})
check("while rendering, an OLD video's notes are not shown", r["degradations"] == [] and r["photo_choice_note"] is None)
r = _run_status(dict(done_row, video_status="failed"), column=[NOTE])
check("failed -> no notes", r["degradations"] == [])
check("photo_choice_note ignores unrelated degradations",
      new.photo_choice_note(["the background music could not be added"]) is None)

core = seg("_render_video_core")
# These column names were briefly behind a `variant` object, while the Signature tier
# shared this core; that tier was withdrawn on 2026-10-21 and they are written out
# literally again. The GUARANTEES are the same ones as before: the notes are published
# before the listing is flipped to done, and the column is only written when the row
# has it. tests/test_classic_core_wiring.py executes this function and asserts which
# columns it actually writes.
check("core publishes notes (job row + listing column) BEFORE flipping the listing to done",
      core.index("record_degradations(list(degradations))") < core.index('update_payload = {"video_url"')
      and core.index('{"video_degradations": list(degradations)}') < core.index('update_payload = {"video_url"'))
check("core only writes the column when the row has it (no failing write pre-migration)",
      'if "video_degradations" in listing:' in core)
check("both note writes are best-effort (cannot cost the agent their video)",
      core.count("except Exception as e:\n            logger.warning(\"video %s: could not") >= 2)
check("queued run wires record_degradations to the fenced job write",
      "record_degradations=lambda degs: video_jobs.record_degradations(job, degs)" in seg("_classic_job_run"))
check("legacy job path reaches the same core (so the same column write)",
      "_render_video_core(" in seg("_render_video_job"))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
