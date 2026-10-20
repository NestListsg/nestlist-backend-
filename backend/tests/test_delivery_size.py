"""Never hand Supabase a file it will reject for size.

WHY THIS IS WORTH GUARDING. Supabase's limit is 50MB. Going over it is not a tidy
failure: _store_rendered_video uploads the whole file and retries four times,
re-uploading all of it each time, then fails the job -- and video_jobs then retries the
JOB, so the agent waits through several complete re-renders and ends up with an error
they can do nothing about. The cost is paid in full, several times, for nothing.

A real Prestige listing renders at ~24MB, comfortably clear. A texture-heavy one --
landed property with mature planting, which is most of them -- reached 56MB in testing.
So the ceiling is reachable and the failure is expensive, which is the combination that
earns a guard.

THE THING MOST AT RISK HERE IS THE NORMAL PATH. This must be completely invisible to
the 24MB film that is the ordinary case: no extra ffmpeg call, no changed bytes, no
degradation line. Most of the checks below are about that.

Runs without ffmpeg: the encoder is stubbed and the sizes are made on disk.
"""
import logging, os, sys, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
logging.disable(logging.CRITICAL)
import video_renderer as vr  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def write(path, nbytes, fill=b"\0"):
    with open(path, "wb") as f:
        f.write(fill * nbytes)
    return path


class Encoder:
    """Stands in for _run_ffmpeg. Writes `produces` bytes to the output path."""

    def __init__(self, produces=None, fail=False):
        self.produces = produces
        self.fail = fail
        self.calls = []

    def __call__(self, cmd, timeout):
        self.calls.append((list(cmd), timeout))
        if self.fail:
            raise RuntimeError("ffmpeg exited 1: no space left on device")
        write(cmd[-1], self.produces if self.produces is not None else 1024)


def run_fit(size, produces=None, fail=False):
    """(_fit_delivery_size result, the encoder that stood in for ffmpeg, source path).

    A raised exception is CAUGHT and returned as the result, so that "this must never
    raise" reads as a failing check rather than a traceback that stops the file -- a
    crash here would take every check below it with it.
    """
    wd = tempfile.mkdtemp()
    src = write(os.path.join(wd, "final_abc.mp4"), size)
    enc = Encoder(produces=produces, fail=fail)
    saved = vr._run_ffmpeg
    vr._run_ffmpeg = enc
    try:
        return vr._fit_delivery_size(src, wd), enc, src
    except Exception as e:
        return ("RAISED: %r" % (e,), None), enc, src
    finally:
        vr._run_ffmpeg = saved


MB = 1024 * 1024
CEILING = vr.DELIVERY_MAX_BYTES

print("\n[the normal path] an ordinary film is not touched at all")
for mb in (1, 12, 24, 39):
    (path, note), enc, src = run_fit(mb * MB)
    check("a %dMB film: no re-encode, no note, same file" % mb,
          enc.calls == [] and note is None and path == src,
          "%d ffmpeg calls, note=%r" % (len(enc.calls), note))
(path, note), enc, src = run_fit(CEILING)
check("a film exactly ON the ceiling is left alone",
      enc.calls == [] and note is None and path == src)

print("\n[oversize] one pass, and the smaller file is what ships")
(path, note), enc, src = run_fit(56 * MB, produces=31 * MB)
check("exactly one re-encode", len(enc.calls) == 1, "%d calls" % len(enc.calls))
check("the smaller file is returned", path != src and os.path.getsize(path) == 31 * MB)
check("the agent is told, in plain words", note == vr.VIDEO_NOTE_SHRUNK, str(note))
cmd = enc.calls[0][0]
check("it re-encodes the FINISHED file, not the clips", src in cmd)
check("the soundtrack is copied, not re-encoded",
      "-c:a" in cmd and cmd[cmd.index("-c:a") + 1] == "copy")
check("frames are passed through, never retimed (-vsync 0)",
      "-vsync" in cmd and cmd[cmd.index("-vsync") + 1] == "0", " ".join(cmd))
check("...and no frame-rate forcing is smuggled in alongside it", "-r" not in cmd)
check("faststart is kept, so it still streams on social players",
      "+faststart" in cmd)
check("it is bounded by a timeout", enc.calls[0][1] == vr.TIMEOUT_SHRINK)

print("\n[the step] softened as little as the overshoot needs")


def crf_for(mb):
    (_p, _n), enc, _s = run_fit(int(mb * MB), produces=1 * MB)
    cmd = enc.calls[0][0]
    return cmd[cmd.index("-crf") + 1]


steps = [(41, "25"), (43.91, "25"), (50, "25"), (56, "26"), (80, "28")]
for mb, want in steps:
    got = crf_for(mb)
    check("%.2fMB -> CRF %s" % (mb, want), got == want, "got %s" % got)
check("the mildest step is tried first for a file barely over",
      crf_for(CEILING / MB + 0.5) == vr._SHRINK_STEPS[0][1])
check("an absurd file still only gets one pass, at the strongest step",
      crf_for(400) == vr._SHRINK_STEPS[-1][1])

print("\n[failure] a shrink that goes wrong never costs the video")
(path, note), enc, src = run_fit(56 * MB, fail=True)
check("a failed re-encode NEVER raises -- the upload must still be attempted",
      not str(path).startswith("RAISED"), str(path))
check("a failed re-encode falls back to the original file", path == src)
check("...with no note, because nothing about the video changed", note is None)
check("...and it was attempted exactly once", len(enc.calls) == 1)
(path, note), enc, src = run_fit(56 * MB, produces=70 * MB)
check("a re-encode that came out BIGGER is discarded", path == src and note is None)
(path, note), enc, src = run_fit(56 * MB, produces=56 * MB)
check("...as is one that saved nothing", path == src and note is None)

saved = vr._run_ffmpeg
vr._run_ffmpeg = lambda cmd, timeout: None      # writes nothing: output missing
try:
    wd = tempfile.mkdtemp()
    src = write(os.path.join(wd, "f.mp4"), 56 * MB)
    path, note = vr._fit_delivery_size(src, wd)
    check("an encoder that silently produces nothing falls back too",
          path == src and note is None)
finally:
    vr._run_ffmpeg = saved

path, note = vr._fit_delivery_size(os.path.join(tempfile.mkdtemp(), "gone.mp4"), "/tmp")
check("a missing file does not raise", note is None)

print("\n[both tiers] the Signature goes through the same gate")
# A Signature is the same film with one more slide, so it is MORE likely to hit the
# ceiling, not less. Rather than assert on a second render path that does not exist,
# this pins that the call sits on the shared path: one call site, before the bytes are
# read, inside render_property_video.
src_text = open(os.path.join(BACKEND, "video_renderer.py"), encoding="utf-8").read()
body = src_text[src_text.index("def render_property_video("):]
check("there is exactly one place the delivery size is enforced",
      body.count("_fit_delivery_size(") == 1, str(body.count("_fit_delivery_size(")))
check("...and it runs before the bytes are read for upload",
      body.index("_fit_delivery_size(") < body.index('with open(final_path, "rb")'))
check("...on the shared path, so a Signature cannot bypass it",
      "if presenter_path" not in
      body[body.index("_fit_delivery_size(") - 400:body.index("_fit_delivery_size(")])
check("the ceiling lives with the rest of the sizing reasoning",
      src_text.index("DELIVERY_MAX_BYTES =") - src_text.index("DELIVERY_CRF = ") < 3000)

print("\n[end to end] the shrunk bytes are what the caller gets")
# The checks above pin _fit_delivery_size and where it is called. This drives the whole
# render so the degradation and the BYTES are the ones that would reach the upload.
from PIL import Image  # noqa: E402


def photo(w=1600, h=1067):
    return Image.new("RGB", (w, h), (150, 140, 130))


def render(final_mb, shrunk_mb=None, fail_shrink=False, **kw):
    """A full stubbed render whose FINAL mux lands at final_mb."""
    seen = {"shrinks": 0}
    saved = {n: getattr(vr, n) for n in
             ("_run_ffmpeg", "_fetch_listing_photo", "_fetch_image",
              "_generate_room_captions", "_smart_auto_selection", "_probe_duration",
              "_has_ffmpeg_filter")}

    def fake_ffmpeg(cmd, timeout):
        out = cmd[-1]
        if out.startswith("-"):
            return
        if os.path.basename(out) == "delivery_shrunk.mp4":
            seen["shrinks"] += 1
            if fail_shrink:
                raise RuntimeError("ffmpeg exited 1")
            write(out, int((shrunk_mb or 1) * MB), b"S")
            return
        # The final mux is the one that carries the delivery size.
        size = int(final_mb * MB) if os.path.basename(out).startswith("final_") else 2048
        write(out, size, b"F")

    vr._run_ffmpeg = fake_ffmpeg
    vr._fetch_listing_photo = lambda url: photo()
    vr._fetch_image = lambda url, attempts=2, deadline=None: photo()
    vr._generate_room_captions = lambda photos, copy_guard=None: ([None] * len(photos), None)
    vr._smart_auto_selection = lambda urls, i, copy_guard=None: (None, None, None)
    vr._probe_duration = lambda path, fallback: fallback
    vr._has_ffmpeg_filter = lambda name: name == "cas"
    vr._clarity_filter_cache = None
    try:
        data, degs, _caps = vr.render_property_video(
            image_urls=["https://e.test/a.jpg", "https://e.test/b.jpg"],
            agent_name="Janel", agent_contact_line="9123 4567", music_seed="L1", **kw)
        return data, list(degs), seen
    finally:
        for n, f in saved.items():
            setattr(vr, n, f)
        vr._clarity_filter_cache = None


data, degs, seen = render(final_mb=24)
check("a 24MB render: no shrink attempted", seen["shrinks"] == 0)
check("...no size note reaches the agent", vr.VIDEO_NOTE_SHRUNK not in degs, str(degs))
check("...and the bytes are the mux's own", data[:1] == b"F" and len(data) == 24 * MB)

data, degs, seen = render(final_mb=56, shrunk_mb=31)
check("a 56MB render is shrunk once", seen["shrinks"] == 1)
check("...the CALLER receives the shrunk bytes, not the big ones",
      data[:1] == b"S" and len(data) == 31 * MB, "%d bytes" % len(data))
check("...and is told why", vr.VIDEO_NOTE_SHRUNK in degs, str(degs))
check("...under Supabase's 50MB limit", len(data) < 50 * MB)

data, degs, seen = render(final_mb=56, fail_shrink=True)
check("a failed shrink still ships the video", data and len(data) == 56 * MB)
check("...reporting no size note, since the video is unchanged",
      vr.VIDEO_NOTE_SHRUNK not in degs, str(degs))

data, degs, seen = render(final_mb=56, shrunk_mb=31, presenter_path=None,
                          caption_overrides={"https://e.test/a.jpg":
                                             "the living hall ... where mornings begin"})
check("a render with agent captions shrinks the same way",
      seen["shrinks"] == 1 and len(data) == 31 * MB)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
