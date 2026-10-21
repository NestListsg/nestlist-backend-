"""THE CLASSIC MUST NOT MOVE: every ffmpeg command it issues, pinned.

Every agent video goes through render_property_video, so any change anywhere near it is
only safe if a Classic render issues the identical ffmpeg commands it issued before.
Test [1] compares a matrix of Classic configurations against a COMMITTED GOLDEN FILE,
command by command, rather than trusting that the diff looks harmless. [1b] then pins
WHY -- the motion the Classic is supposed to have -- so that a careless
--update-golden cannot quietly undo the thing the golden was written for.

(This file was tests/test_signature_render.py until the Signature tier was withdrawn on
2026-10-21. The Signature-only tests went with the tier; the golden guard and the
Classic checks are what is left, which is what the file was really for.)
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
STUBS = ("_run_ffmpeg", "_fetch_listing_photo", "_fetch_image",
         "_generate_room_captions", "_smart_auto_selection",
         "_probe_duration", "_has_ffmpeg_filter")


def capture(module, captions=None, ffmpeg=None, src_photo=None, smart=None, **kwargs):
    """Run a render with every external dependency stubbed out.
    Returns (ffmpeg commands it would have run, render result).

    src_photo swaps the stand-in listing photo, which is how the matrix below reaches
    the portrait branch: a source taller than 9:16 composites differently and is the
    one shape that must NOT drift sideways."""
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
    module._fetch_listing_photo = lambda url: (src_photo or photo())
    # The agent's portrait. Stubbed so the golden never depends on a live network.
    module._fetch_image = lambda url, attempts=2, deadline=None: (src_photo or photo())
    # Smart photo selection is stubbed OUT by default, i.e. "the survey was not
    # available, use upload order". Without this the golden would depend on whether
    # the machine running the tests happens to have an ANTHROPIC_API_KEY set, which
    # would make it pass on one laptop and fail on another. `smart` opts a config in.
    module._smart_auto_selection = (
        smart if callable(smart) else (lambda urls, idx, copy_guard=None: (None, None, None)))
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


def vf_of(cmd):
    return cmd[cmd.index("-vf") + 1] if "-vf" in cmd else ""


# Temp directory names differ between runs, so compare with the per-run workdir
# factored out of absolute paths.
def norm(cmd):
    # The delivery filename carries a random suffix by design, so mask that too --
    # otherwise two runs of the SAME code never compare equal.
    out = []
    for x in cmd:
        x = os.path.basename(x) if os.path.isabs(x) else x
        # Filter strings EMBED absolute paths (drawtext textfile=/tmp/.../cap_00.txt,
        # fontfile=/.../Gelasio-Variable.ttf) inside a single argv element, so the
        # basename() above never sees them and the golden would change every run.
        # Matched on the OPTION NAME rather than on "looks like a path": a general
        # path-shaped regex also eats the slashes in zoompan expressions, quietly
        # turning x='iw/2-(iw/zoom/2)' into x='iw/2-(iw2)' and pinning a corrupted
        # version of the very motion this file exists to protect.
        x = re.sub(r"((?:textfile|fontfile)=)((?:\\:|[^:,\]])*)",
                   lambda m: m.group(1) + os.path.basename(m.group(2)), x)
        out.append(re.sub(r"final_[0-9a-f]{8}\.mp4", "final_XXXXXXXX.mp4", x))
    return out


# ---------------------------------------------------------------------------
# [1] THE CLASSIC GUARD
#
# This used to diff a Classic render against `git show HEAD:...`. That worked while the
# Classic was frozen, but it can only ever catch UNCOMMITTED drift: the moment a change
# is committed, HEAD becomes the new behaviour and the guard agrees with whatever it is
# handed. On 2026-10-17 the Classic's output deliberately changed (real motion, up to
# ten photos, CRF 24), so the guard has been re-pointed at a COMMITTED GOLDEN FILE
# instead. The golden is the intended output, written down, and it survives commits.
#
# It is deliberately a matrix rather than one render: one photo and ten, portrait and
# landscape sources, captions and none, an agent photo and none, an explicit photo pick,
# and a successful smart selection. Those are the shapes where the clip assembly
# branches, and a change that moves any of them shows up here as an exact command diff.
#
# WHEN THIS FAILS, READ THE DIFF BEFORE REGENERATING. It is telling you a Classic now
# renders differently, which is either the change you meant or a change you did not.
# If you meant it:  python3 tests/test_classic_render.py --update-golden
# and commit the golden alongside the code, so the review sees both.
# ---------------------------------------------------------------------------
GOLDEN = os.path.join(HERE, "golden", "classic_commands.txt")

PORTRAIT_ARGS = dict(CLASSIC_ARGS, image_urls=["https://example.test/p.jpg"] * 3)
MANY_ARGS = dict(CLASSIC_ARGS,
                 image_urls=["https://example.test/%d.jpg" % i for i in range(12)])
CAPS = ["the living hall ... where laughter lingers", None,
        "the dining hall ... where every meal becomes a gathering"]

SMART_PICK = ["https://example.test/%d.jpg" % i for i in (0, 4, 7, 9)]
SMART_CAPTIONS = {
    SMART_PICK[0]: "the living hall ... where laughter lingers",
    SMART_PICK[1]: "the kitchen ... where the day begins",
    SMART_PICK[3]: "the terrace ... where golden hours drift by",
}


def smart_survey(urls, photo_index, copy_guard=None):
    """Stands in for a vision survey that worked: a varied four-photo pick with
    captions keyed BY URL, which is the contract _smart_auto_selection promises."""
    return list(SMART_PICK), dict(SMART_CAPTIONS), None


def matrix():
    """(name, kwargs for capture()) for every shape worth pinning."""
    yield "classic-3-photos", dict(CLASSIC_ARGS)
    yield "classic-3-photos-captioned", dict(CLASSIC_ARGS, captions=CAPS)
    yield "classic-1-photo", dict(CLASSIC_ARGS, image_urls=["https://example.test/a.jpg"])
    yield "classic-2-photos", dict(CLASSIC_ARGS,
                                   image_urls=["https://example.test/a.jpg",
                                               "https://example.test/b.jpg"])
    yield "classic-12-photos-caps-at-10", dict(MANY_ARGS)
    yield "classic-12-photos-hero-is-7", dict(MANY_ARGS, photo_index=7)
    yield "classic-agent-photo", dict(CLASSIC_ARGS,
                                      agent_photo_url="https://example.test/agent.jpg")
    yield "classic-no-agent-name", dict(CLASSIC_ARGS, agent_name="", agent_contact_line="")
    yield "classic-explicit-pick", dict(
        CLASSIC_ARGS, video_photos=["https://example.test/c.jpg",
                                    "https://example.test/a.jpg"])
    yield "classic-portrait-sources", dict(PORTRAIT_ARGS)
    # A survey that SUCCEEDED: it chose four of the twelve photos and wrote captions
    # for three of them. Pins that the renderer uses the chosen photos in the chosen
    # order, burns in the captions it was handed WITHOUT a second model call, and
    # leaves the fourth photo bare rather than shifting a caption onto it.
    yield "classic-smart-selected", dict(MANY_ARGS, smart=smart_survey)


def render_matrix():
    """{name: [normalised ffmpeg commands]} for the whole matrix."""
    out = {}
    tall = photo(1000, 2400)          # taller than 9:16 -> the no-drift branch
    for name, kwargs in matrix():
        if name == "classic-portrait-sources":
            kwargs = dict(kwargs, src_photo=tall)
        cmds, _ = capture(vr, **kwargs)
        out[name] = [norm(c) for c in cmds]
    return out


def dump(rendered):
    lines = []
    for name in sorted(rendered):
        for i, cmd in enumerate(rendered[name]):
            lines.append("%s\t%d\t%s" % (name, i, "\x1f".join(cmd)))
    return "\n".join(lines) + "\n"


rendered = render_matrix()
produced = dump(rendered)

if "--update-golden" in sys.argv:
    os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
    with open(GOLDEN, "w", encoding="utf-8") as f:
        f.write(produced)
    print("  WROTE %s (%d configs, %d commands)"
          % (GOLDEN, len(rendered), sum(len(v) for v in rendered.values())))
    sys.exit(0)

print("\n[1] a Classic render issues exactly the commands the golden pins")
if not os.path.exists(GOLDEN):
    check("golden file exists", False,
          "missing %s -- regenerate with --update-golden and review the diff" % GOLDEN)
else:
    expected = open(GOLDEN, encoding="utf-8").read()
    check("%d configs, %d ffmpeg commands match the golden exactly"
          % (len(rendered), sum(len(v) for v in rendered.values())),
          produced == expected)
    if produced != expected:
        exp_rows = dict((l.split("\t", 2)[0] + "\t" + l.split("\t", 2)[1],
                         l.split("\t", 2)[2]) for l in expected.splitlines() if "\t" in l)
        got_rows = dict((l.split("\t", 2)[0] + "\t" + l.split("\t", 2)[1],
                         l.split("\t", 2)[2]) for l in produced.splitlines() if "\t" in l)
        shown = 0
        for key in sorted(set(exp_rows) | set(got_rows)):
            if exp_rows.get(key) != got_rows.get(key) and shown < 3:
                shown += 1
                print("       %s\n         golden: %s\n         now:    %s"
                      % (key,
                         (exp_rows.get(key) or "(absent)").replace("\x1f", " ")[:300],
                         (got_rows.get(key) or "(absent)").replace("\x1f", " ")[:300]))

# The golden pins WHAT is rendered. These pin WHY, so that a careless
# --update-golden cannot quietly undo the thing the golden was written for.
print("\n[1b] the motion the Classic is supposed to have")
photo_vfs = [vf_of(c) for c in rendered["classic-12-photos-caps-at-10"]
             if "zoompan" in vf_of(c)]
check("ten photos render ten moving clips (cap raised 6 -> 10)",
      len(photo_vfs) == vr.MAX_VIDEO_PHOTOS == 10, "found %d" % len(photo_vfs))
# The bug this replaced: z='min(zoom+step,max)' reads a previous-frame feedback
# value that does not exist at d=1, so the shot never moved. If that form ever
# comes back, the Classic is static again and nothing else here would notice.
check("no clip uses the feedback zoom form that silently does nothing",
      not any("min(zoom+" in v or "max(zoom-" in v for v in photo_vfs),
      next((v for v in photo_vfs if "min(zoom+" in v), ""))
check("every clip's zoom is driven by the frame number",
      all(re.search(r"zoompan=z='[^']*\bon/\d+", v) for v in photo_vfs))
check("every clip drifts sideways across the photo",
      all("(iw-iw/zoom)" in v for v in photo_vfs))
check("the drift is bounded by the zoom's spare width, so it cannot leave the photo",
      all(re.search(r"x='[0-9.]+\*(\(1-)?on/\d+\)?\*\(iw-iw/zoom\)", v)
          for v in photo_vfs))
zoom_in = [("1+%s*on" % round(vr.CONTAIN_ZOOM_MAX - 1.0, 6)) in v for v in photo_vfs]
drift_right = [("'%s*on/" % vr.CONTAIN_DRIFT_SPAN) in v for v in photo_vfs]
check("the zoom alternates in and out", zoom_in[:4] == [True, False, True, False],
      str(zoom_in))
check("the drift alternates left and right on a different period",
      drift_right[:4] == [True, True, False, False], str(drift_right))
check("so ten shots cycle through four moves, not two",
      len(set(zip(zoom_in, drift_right))) == 4,
      str(sorted(set(zip(zoom_in, drift_right)))))
# A source taller than 9:16 does not reach the sides of its own composite, so
# drifting it would slide the sharp photo around inside blurred margins.
tall_vfs = [vf_of(c) for c in rendered["classic-portrait-sources"]
            if "zoompan" in vf_of(c)]
check("a photo taller than the frame keeps the centred zoom, no drift",
      tall_vfs and all("iw/2-(iw/zoom/2)" in v for v in tall_vfs), str(tall_vfs[:1]))
check("and still zooms", all("zoompan=z='1+" in v or "zoompan=z='%s-" % vr.CONTAIN_ZOOM_MAX
                             in v for v in tall_vfs))
check("delivery CRF is the measured 24", vr.DELIVERY_CRF == "24")

after, _ = capture(vr, **CLASSIC_ARGS)

print("\n[2] the Classic soundtrack is one music chain and nothing else")
# There has only ever been one audio layer in a Classic -- the music bed. This pins
# that the soundtrack pass stays that: one chain from input 1, ending at [aout], with
# no mixer, no second audio input and no per-frame volume ramp.
mux = [c for c in after if "[1:a]" in graph_of(c)]
check("exactly one audio mux pass", len(mux) == 1, "found %d" % len(mux))
g = graph_of(mux[0]) if mux else ""
check("one chain, ending at [aout]", g.endswith("[aout]") and g.count("[1:a]") == 1)
check("no amix", "amix" not in g)
check("no per-frame volume ramp", "eval=frame" not in g)
check("no second audio input", "[2:a]" not in g)
check("the picture is not re-encoded by the soundtrack pass",
      mux and "-c:v" in mux[0] and mux[0][mux[0].index("-c:v") + 1] == "copy")

print("\n[3] a photo that will not encode is skipped, and no photos at all raises")
saved_clip = vr._contain_clip
def exploding(*a, **k): raise RuntimeError("clip encode failed")
vr._contain_clip = exploding
try:
    capture(vr, **CLASSIC_ARGS)
    check("raises when nothing could be rendered", False, "it returned a video")
except RuntimeError as e:
    check("raises 'No photo could be rendered'", "No photo could be rendered" in str(e), str(e))
finally:
    vr._contain_clip = saved_clip

print("\n[4] no music on disk: the silent fallback is reached without a doomed call")
# The ladder's last rung must not be an attempt that cannot succeed: with no track
# there is nothing to put in -filter_complex, so the command would be an empty
# filtergraph with -map [aout]. That spends an ffmpeg call on the way to the silent
# fallback that was always going to handle this.
saved_select = vr._select_music_track
vr._select_music_track = lambda seed, property_type=None: (None, "no music")
try:
    cmds, result = capture(vr, **CLASSIC_ARGS)
    empty = [c for c in cmds
             if "-filter_complex" in c and c[c.index("-filter_complex") + 1] == ""]
    soundtrack = [c for c in cmds if "[aout]" in c]
    check("no ffmpeg call is built with an empty filtergraph", not empty, str(empty))
    check("no soundtrack attempt was made at all", len(soundtrack) == 0,
          "%d attempts" % len(soundtrack))
    data, degradations, _ = result
    check("the film still ships", bool(data))
    check("and the agent is told the music was missing",
          any("music" in d for d in degradations), str(degradations))
finally:
    vr._select_music_track = saved_select


print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
