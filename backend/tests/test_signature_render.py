"""The Signature tier: the agent's own footage as the opening slide of the film.

Two things are being protected here.

1. THE CLASSIC PATH MUST NOT MOVE. Every agent video goes through
   render_property_video, so the Signature work is only safe if a Classic render issues
   the identical ffmpeg commands it issued before. Test [1] compares them against the
   previous COMMITTED version of the module, command by command, rather than trusting
   that the diff looks harmless.

2. The Signature's own assembly has to be right where it differs: the presenter clip is
   slide zero, so the first photo's caption has to wait out the dissolve; a Signature
   with no usable photo must fail rather than ship the agent's clip on its own; the
   agent's stored upload must never be written over or moved; and the voice layer must
   degrade to music rather than cost the video.
"""
import os, re, subprocess, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image
import video_renderer as vr

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
REPO = os.path.dirname(BACKEND)

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))


def photo(w=1600, h=1067, shade=160):
    img = Image.new("RGB", (w, h), (shade, shade - 20, shade - 40))
    for x in range(0, w, 97):          # structure, so blur and clarity have work to do
        for y in range(0, h, 61):
            img.putpixel((x, y), (20, 30, 40))
    return img


CLASSIC_ARGS = dict(
    image_urls=["https://example.test/a.jpg", "https://example.test/b.jpg",
                "https://example.test/c.jpg"],
    property_type="Semi-Detached",
    agent_name="Janel",
    agent_contact_line="9123 4567",
    music_seed="listing-42",
)
STUBS = ("_run_ffmpeg", "_fetch_listing_photo", "_generate_room_captions",
         "_probe_duration", "_has_ffmpeg_filter")


def capture(module, captions=None, ffmpeg=None, **kwargs):
    """Run a render with every external dependency stubbed out.
    Returns (ffmpeg commands it would have run, render result)."""
    commands = []

    def fake_run_ffmpeg(cmd, timeout):
        commands.append(list(cmd))
        if ffmpeg:
            ffmpeg(cmd)                      # a hook that can fail a chosen command
        out = cmd[-1]                        # every call here writes its last argument
        if not out.startswith("-"):
            with open(out, "wb") as f:
                f.write(b"\0" * 2048)

    saved = {name: getattr(module, name) for name in STUBS}
    module._run_ffmpeg = fake_run_ffmpeg
    module._fetch_listing_photo = lambda url: photo()
    def fake_captions(photos, copy_guard=None):
        return (list(captions) if captions else [None] * len(photos)), None
    module._generate_room_captions = fake_captions
    module._probe_duration = lambda path, fallback: fallback
    module._has_ffmpeg_filter = lambda name: name == "cas"
    module._clarity_filter_cache = None
    try:
        return commands, module.render_property_video(**kwargs)
    finally:
        for name, fn in saved.items():
            setattr(module, name, fn)
        module._clarity_filter_cache = None


def previous_module():
    """The committed version of video_renderer, loaded alongside the working copy."""
    blob = subprocess.run(["git", "-C", REPO, "show", "HEAD:backend/video_renderer.py"],
                          capture_output=True, check=True, timeout=30).stdout.decode("utf-8")
    mod = types.ModuleType("video_renderer_previous")
    # It reads __file__ to find audio/ and fonts/, so it must believe it lives where the
    # real module does.
    mod.__file__ = os.path.join(BACKEND, "video_renderer.py")
    exec(compile(blob, mod.__file__, "exec"), mod.__dict__)
    return mod


def graph_of(cmd):
    return cmd[cmd.index("-filter_complex") + 1] if "-filter_complex" in cmd else ""


print("\n[1] a Classic render issues exactly the commands it issued before")
before, _ = capture(previous_module(), **CLASSIC_ARGS)
after, _ = capture(vr, **CLASSIC_ARGS)
check("same number of ffmpeg calls", len(before) == len(after),
      "was %d, now %d" % (len(before), len(after)))
# Temp directory names differ between runs, so compare with the per-run workdir
# factored out of absolute paths.
def norm(cmd):
    # The delivery filename carries a random suffix by design, so mask that too --
    # otherwise two runs of the SAME code never compare equal.
    return [re.sub(r"final_[0-9a-f]{8}\.mp4", "final_XXXXXXXX.mp4",
                   os.path.basename(x) if os.path.isabs(x) else x) for x in cmd]
mismatch = next((i for i, (b, a) in enumerate(zip(before, after))
                 if norm(b) != norm(a)), None)
check("every command byte-identical", mismatch is None,
      "" if mismatch is None else
      "command %d:\n       was %s\n       now %s"
      % (mismatch, norm(before[mismatch]), norm(after[mismatch])))

print("\n[2] the Classic soundtrack carries no voice machinery")
mux = [c for c in after if "[1:a]" in graph_of(c)]
check("exactly one audio mux pass", len(mux) == 1, "found %d" % len(mux))
g = graph_of(mux[0]) if mux else ""
check("one chain, ending at [aout]", g.endswith("[aout]") and g.count("[1:a]") == 1)
check("no amix", "amix" not in g)
check("no duck stage", "eval=frame" not in g)
check("no second audio input", "[2:a]" not in g)

print("\n[3] the presenter clip is built once, into the render's own workdir")
upload = os.path.join(BACKEND, "tests", "_fake_presenter.mp4")
with open(upload, "wb") as f:
    f.write(b"\0" * 4096)
try:
    sig, _ = capture(vr, presenter_path=upload, **CLASSIC_ARGS)
    # The soundtrack pass also takes the upload (as the voice) and also uses
    # -stream_loop (for the music), so match on what it WRITES instead.
    builds = [c for c in sig if upload in c and c[-1].endswith("clip_presenter.mp4")]
    check("built exactly once from the upload", len(builds) == 1, "found %d" % len(builds))
    if builds:
        check("written into the workdir, not over the upload",
              builds[0][-1].endswith("clip_presenter.mp4") and builds[0][-1] != upload,
              builds[0][-1])
        check("contained, never cropped to fill",
              "force_original_aspect_ratio=decrease" in graph_of(builds[0]))
        check("backdrop cover-scale cannot ask for a crop bigger than the frame",
              "force_original_aspect_ratio=increase" in graph_of(builds[0]))
    check("one more slide than the Classic", len(sig) > len(after))
    check("the agent's upload is still there", os.path.exists(upload))

    print("\n[4] the first photo's caption waits out the dissolve from the presenter")
    def caption_vf(commands):
        for c in commands:
            if "-vf" in c and "b_comp_00" in " ".join(c) and "drawtext" in c[c.index("-vf") + 1]:
                return c[c.index("-vf") + 1]
        return ""
    caps = ["the living hall ... where laughter lingers", None, None]
    classic_vf = caption_vf(capture(vr, captions=caps, **CLASSIC_ARGS)[0])
    sig_vf = caption_vf(capture(vr, captions=caps, presenter_path=upload, **CLASSIC_ARGS)[0])
    check("Classic: first photo's caption may appear at t=0",
          "between(t,0.00," in classic_vf, classic_vf[:120])
    check("Signature: it waits XFADE_SECONDS for the dissolve to finish",
          "between(t,%.2f," % vr.XFADE_SECONDS in sig_vf, sig_vf[:120])

    print("\n[5] the soundtrack ducks the music under the agent's voice")
    mux = [c for c in sig if "amix" in graph_of(c)]
    check("exactly one pass mixes the soundtrack", len(mux) == 1, "found %d" % len(mux))
    if mux:
        cmd, g = mux[0], graph_of(mux[0])
        check("the voice comes from the agent's upload, not the silent slide", upload in cmd)
        check("input 0 video, 1 music, 2 voice", "[2:a]" in g)
        check("the duck is a per-frame ramp", "eval=frame" in g)
        check("ducked to PRESENTER_MUSIC_DUCK", str(vr.PRESENTER_MUSIC_DUCK) in g)
        check("the picture is not re-encoded",
              "-c:v" in cmd and cmd[cmd.index("-c:v") + 1] == "copy")

    print("\n[6] a Signature with no usable photo fails instead of shipping the clip alone")
    saved_clip = vr._contain_clip
    def exploding(*a, **k): raise RuntimeError("clip encode failed")
    vr._contain_clip = exploding
    try:
        capture(vr, presenter_path=upload, **CLASSIC_ARGS)
        check("raises", False, "it returned a video built from the presenter clip alone")
    except RuntimeError as e:
        check("raises 'No photo could be rendered'", "No photo could be rendered" in str(e), str(e))
    finally:
        vr._contain_clip = saved_clip
    check("the agent's stored upload survived the failure", os.path.exists(upload))

    print("\n[7] a voice-layer failure costs the voice, never the video")
    def fail_amix(cmd):
        if "amix" in graph_of(cmd):
            raise RuntimeError("ffmpeg exited 1: no audio stream in the presenter clip")
    _, result = capture(vr, ffmpeg=fail_amix, presenter_path=upload, **CLASSIC_ARGS)
    data, degradations, _ = result
    check("the video still ships", bool(data))
    voice_notes = [d for d in degradations if "voice" in d]
    check("the agent is told once, not once per attempt", len(voice_notes) == 1,
          repr(degradations))
    check("and told the right thing", voice_notes and "music only" in voice_notes[0])
    check("the music is not also reported as lost",
          "the background music could not be added" not in degradations, repr(degradations))
finally:
    if os.path.exists(upload):
        os.remove(upload)

print("\n[8] no music on disk: the silent fallback is reached without a doomed call")
# With the music file missing and the voice layer failing, the ladder's last rung used
# to be "no music, no voice" -- which builds -filter_complex "" with -map [aout] and
# cannot succeed. It cost one ffmpeg call on the way to the silent fallback that was
# always going to handle this.
upload2 = os.path.join(BACKEND, "tests", "_fake_presenter2.mp4")
with open(upload2, "wb") as f:
    f.write(b"\0" * 4096)
saved_select = vr._select_music_track
vr._select_music_track = lambda seed, property_type=None: (None, "no music")
try:
    def fail_mix(cmd):
        if "[aout]" in cmd:          # the soundtrack pass, not the slide builds
            raise RuntimeError("ffmpeg exited 1: cannot mix")
    cmds, result = capture(vr, ffmpeg=fail_mix, presenter_path=upload2, **CLASSIC_ARGS)
    empty = [c for c in cmds
             if "-filter_complex" in c and c[c.index("-filter_complex") + 1] == ""]
    soundtrack = [c for c in cmds if "[aout]" in c]
    check("no ffmpeg call is built with an empty filtergraph", not empty, str(empty))
    check("exactly one soundtrack attempt was made (the voice)", len(soundtrack) == 1,
          "%d attempts" % len(soundtrack))
    data, degradations, _ = result
    check("the film still ships", bool(data))
    check("and the agent is told the music was missing",
          any("music" in d for d in degradations), str(degradations))
finally:
    vr._select_music_track = saved_select
    if os.path.exists(upload2):
        os.remove(upload2)

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
