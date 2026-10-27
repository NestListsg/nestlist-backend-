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
import io, os, re, subprocess, sys, types
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
         "_generate_room_captions", "_smart_auto_selection", "_survey_photos",
         "_probe_duration", "_has_ffmpeg_filter")


def capture(module, captions=None, ffmpeg=None, src_photo=None, smart=None,
            labels=None, **kwargs):
    """Run a render with every external dependency stubbed out.
    Returns (ffmpeg commands it would have run, render result).

    src_photo swaps the stand-in listing photo, which is how the matrix below reaches
    the portrait branch: a source taller than 9:16 composites differently and is the
    one shape that must NOT drift sideways.

    `smart` replaces _smart_auto_selection outright -- "a survey that returned this
    pick". `labels` goes one level deeper and replaces only the VISION CALL, with a
    space label per photo, so the real _smart_auto_selection and _varied_order run and
    the golden pins the order they actually choose. That is the only way the opening-
    shot rule reaches the golden at all, since `smart` hands the answer over ready-made."""
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
    if labels:
        # Leave _smart_auto_selection REAL and fake only the vision call it makes, so
        # the golden records the photo order _varied_order really produces.
        module._survey_photos = lambda photos, copy_guard=None: (
            [(labels[i], "the %s ... where the day begins" % labels[i])
             for i in range(len(photos))], None)
    else:
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
# a successful smart selection, and (added 2026-10-27) two runs of the REAL selection
# with only the vision call faked, which is what pins the opening-shot rule. Those are
# the shapes where the clip assembly branches, and a change that moves any of them
# shows up here as an exact command diff.
#
# REGENERATED 2026-10-27 for the opening-shot rule. The change was PURELY ADDITIVE:
# 12 configs/187 commands -> 14 configs/255 commands, and not one byte of an existing
# row moved. That is not luck, it is what the rule's blast radius is -- every older
# row either stubs _smart_auto_selection out entirely (so it renders plain upload
# order, which the rule does not touch) or hands the pick over ready-made (so it never
# reaches _varied_order). The two new rows are the only ones the rule can move.
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


# THE OPENING-SHOT RULE, through the real selection code. These are space labels, one
# per photo of MANY_ARGS, fed to the real _varied_order via the `labels` hook -- so
# the golden pins the photo ORDER the rule produces, not a pick handed over ready-made.
#
# The first is the shape Jane complained about: the hero photo (My Listings' star) is
# a STAIRWELL, which is how a real film came to open on one. The rule must move the
# living room to the front, keep the hero in the film, and push the stairwell and the
# entrance lobby to the end. The second has no living room, so it pins the fallback.
STAIRWELL_HERO_LABELS = ["stairs", "living", "dining", "kitchen", "bedroom",
                         "bathroom", "pool", "garden", "entrance", "view",
                         "carpark", "terrace"]
NO_LIVING_LABELS = ["stairs", "exterior", "dining", "kitchen", "bedroom",
                    "bathroom", "pool", "garden", "entrance", "view",
                    "carpark", "terrace"]


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
    # THE SHAPE PRODUCTION ACTUALLY HAS. Every photo on the live listing is 800x600
    # (measured 2026-10-27, and none of them has an upscaled twin), which is the size
    # that decides the per-shot zoom ceiling -- see _motion_zoom_ceiling. Without this
    # row the whole matrix renders 1600px sources and the golden never pins the ceiling
    # the agents' own photos are given.
    yield "classic-production-800x600", dict(CLASSIC_ARGS)
    # A survey that SUCCEEDED: it chose four of the twelve photos and wrote captions
    # for three of them. Pins that the renderer uses the chosen photos in the chosen
    # order, burns in the captions it was handed WITHOUT a second model call, and
    # leaves the fourth photo bare rather than shifting a caption onto it.
    yield "classic-smart-selected", dict(MANY_ARGS, smart=smart_survey)
    # The real selection running end to end, twice: a listing whose star photo is a
    # stairwell, and a listing with no living room at all. Pins which photos go in,
    # in which order, and therefore which camera move lands on the opening shot.
    yield "classic-opens-on-living-room", dict(MANY_ARGS, labels=STAIRWELL_HERO_LABELS)
    yield "classic-opens-on-facade", dict(MANY_ARGS, labels=NO_LIVING_LABELS)


def render_matrix():
    """{name: [normalised ffmpeg commands]} for the whole matrix."""
    out = {}
    tall = photo(1000, 2400)          # taller than 9:16 -> tilts instead of drifting
    small = photo(800, 600)           # the size the live listing's photos really are
    for name, kwargs in matrix():
        if name == "classic-portrait-sources":
            kwargs = dict(kwargs, src_photo=tall)
        if name == "classic-production-800x600":
            kwargs = dict(kwargs, src_photo=small)
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
#
# REWRITTEN 2026-10-27, when the golden was regenerated DELIBERATELY. Up to then every
# shot did one move -- a straight ramp to CONTAIN_ZOOM_MAX, full-width drift, 5.0s --
# and these checks pinned the four ways its two booleans could be arranged. Identical
# movement on every frame is what read as a slideshow, so the booleans are gone and
# what is pinned now is VARIETY: that the shots differ from each other, that one of them
# holds nearly still, that one of them overshoots and comes back, and that none of them
# is allowed to go soft. The zoom numbers themselves are the move table's business.
print("\n[1b] the motion the Classic is supposed to have")


def part_of(vf, key):
    """The z / x / y expression out of one clip's zoompan, quotes stripped."""
    return re.search(r"[:=]%s='([^']*)'" % key, vf).group(1)


def evaluate(expr, frame):
    """What ffmpeg will compute for this expression at this frame.

    An INDEPENDENT reader of the string the renderer actually emits, rather than a
    second call into the renderer's own knot arithmetic -- otherwise these checks would
    only prove the module agrees with itself. The grammar is narrow: nested
    if(lt(on,N),THEN,ELSE) over leaves of the form a+b*(on-c)/d, or a bare number.
    """
    expr = expr.strip()
    while expr.startswith("(") and expr.endswith(")"):
        depth = 0
        for i, ch in enumerate(expr):
            depth += (ch == "(") - (ch == ")")
            if depth == 0 and i < len(expr) - 1:
                break
        else:
            expr = expr[1:-1].strip()
            continue
        break
    if expr.startswith("if(lt(on,"):
        rest = expr[len("if(lt(on,"):]
        limit, rest = rest.split(")", 1)
        rest = rest.lstrip(",")
        depth, cut = 0, None
        for i, ch in enumerate(rest):
            depth += (ch == "(") - (ch == ")")
            if ch == "," and depth == 0:
                cut = i
                break
        then, other = rest[:cut], rest[cut + 1:]
        if other.endswith(")"):
            other = other[:-1]
        return evaluate(then if frame < float(limit) else other, frame)
    m = re.fullmatch(r"(-?[\d.]+)\+(-?[\d.]+)\*\(on-(\d+)\)/(\d+)", expr)
    if m:
        a, b, c, d = (float(g) for g in m.groups())
        return a + b * (frame - c) / d
    return float(expr)


def shot(vf):
    """(zooms, x fractions, y fractions) over every frame of one clip."""
    z, x, y = (part_of(vf, "z"),
               part_of(vf, "x").replace("*(iw-iw/zoom)", ""),
               part_of(vf, "y").replace("*(ih-ih/zoom)", ""))
    frames = range(vr.FRAMES_PER_PHOTO)
    return ([evaluate(z, f) for f in frames],
            [evaluate(x, f) for f in frames],
            [evaluate(y, f) for f in frames])


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
      all(re.search(r"zoompan=z='[^']*\bon\b", v) for v in photo_vfs))
check("the pan is a fraction of the zoom's OWN spare on both axes, so no move can "
      "leave the photo at any zoom and a gentle shot cannot drift far",
      all(part_of(v, "x").endswith("*(iw-iw/zoom)")
          and part_of(v, "y").endswith("*(ih-ih/zoom)") for v in photo_vfs))

shots = [shot(v) for v in photo_vfs]
check("no shot ever zooms below 1.0, which is what would show a black edge",
      all(min(zs) >= 1.0 - 1e-9 for zs, _x, _y in shots),
      "%.4f" % min(min(zs) for zs, _x, _y in shots))
check("every pan fraction stays inside 0..1 on both axes",
      all(all(0.0 - 1e-9 <= p <= 1.0 + 1e-9 for p in xs + ys)
          for _z, xs, ys in shots))
# THE POINT OF THE WHOLE CHANGE. Ten shots used to be four arrangements of one move.
peaks = [round(max(zs), 3) for zs, _x, _y in shots]
check("ten shots do NOT all do the same thing (%d different zoompan expressions)"
      % len(set(photo_vfs)), len(set(photo_vfs)) >= 6, str(peaks))
check("and no two NEIGHBOURING shots move alike",
      all(photo_vfs[i] != photo_vfs[i + 1] for i in range(len(photo_vfs) - 1)))
# Judged against the ceiling rather than against a fixed zoom, because the ceiling is
# the photograph's and the matrix renders several sizes.
room = vr._motion_zoom_ceiling((1600, 1067)) - 1.0
check("at least one shot is near-static -- the contrast that makes the others read "
      "as movement (quietest peak %.3f)" % min(peaks),
      min(peaks) <= 1.0 + 0.15 * room, str(peaks))
check("at least one shot pushes all the way to the photograph's ceiling "
      "(hardest peak %.3f of %.3f)" % (max(peaks), 1.0 + room),
      max(peaks) >= 1.0 + 0.95 * room, str(peaks))
check("at least one shot OVERSHOOTS and settles back -- the move the old straight "
      "z0->z1 ramp could not make at all",
      any(max(zs) > zs[-1] + 0.02 and max(zs) > zs[0] + 0.02
          for zs, _x, _y in shots))
check("at least one shot PULLS OUT, starting tight and widening",
      any(zs[0] > zs[-1] + 0.2 for zs, _x, _y in shots))
check("the pan travels in both senses across a film, not always one way",
      len({xs[-1] > xs[0] for _z, xs, _y in shots}) == 2)
drift = [abs(xs[-1] - xs[0]) * (max(zs) - 1.0) for zs, xs, _y in shots]
check("no shot slides the picture further than %.0f%% of the frame's width -- the "
      "drift speed Jane approved (worst %.1f%%)"
      % (vr.CAMERA_DRIFT_MAX_FRAME_FRACTION * 100, max(drift) * 100),
      max(drift) <= vr.CAMERA_DRIFT_MAX_FRAME_FRACTION + 1e-6, str(drift))

# A source taller than 9:16 does not reach the SIDES of its own composite, so drifting
# it sideways would slide the sharp photo around inside blurred margins. It does reach
# the top and bottom, so that is the axis it is allowed to move on -- and it is new:
# these shots used to be pinned dead centre on both axes.
tall_vfs = [vf_of(c) for c in rendered["classic-portrait-sources"]
            if "zoompan" in vf_of(c)]
tall = [shot(v) for v in tall_vfs]
check("a photo taller than the frame does not drift sideways",
      tall and all(len(set(round(p, 9) for p in xs)) == 1 and abs(xs[0] - 0.5) < 1e-9
                   for _z, xs, _y in tall), str(tall_vfs[:1]))
check("it TILTS vertically instead, which is the axis it has room on",
      any(len(set(round(p, 6) for p in ys)) > 1 for _z, _x, ys in tall))
check("and it still zooms", all(max(zs) > min(zs) + 0.005 for zs, _x, _y in tall))

# The softness guard. The live listing's photos are 800x600 and have no upscaled twin,
# so this is the ceiling the agents' own photos are really given.
prod_vfs = [vf_of(c) for c in rendered["classic-production-800x600"]
            if "zoompan" in vf_of(c)]
prod_peak = max(max(zs) for zs, _x, _y in (shot(v) for v in prod_vfs))
check("on the 800x600 photos production really has, no shot pushes past %.2f "
      "(measured: 0.63 of the acutance of the 1.20 frame Jane approved; 2.20 would "
      "be 0.17) -- hardest shot %.3f" % (vr._motion_zoom_ceiling((800, 600)),
                                         prod_peak),
      prod_peak <= vr._motion_zoom_ceiling((800, 600)) + 1e-6)
check("that ceiling is never BELOW the look Jane already approved",
      vr._motion_zoom_ceiling((800, 600)) >= vr.CONTAIN_ZOOM_MAX)
check("delivery CRF is the measured 24", vr.DELIVERY_CRF == "24")


# ---------------------------------------------------------------------------
print("\n[1c] a regenerate is not a lottery, and no move can leave the photograph")
# ---------------------------------------------------------------------------
# DETERMINISM. An agent who regenerates a video must get the same film back. The camera
# plan is seeded from the listing id (main.py passes it as music_seed), so this has to
# hold across two separate renders in the same process -- which is also where a stray
# random.seed() or a dict-ordering dependency would show up.
again, _ = capture(vr, **MANY_ARGS)
check("TWO RENDERS OF ONE LISTING ARE THE SAME FILM, camera and all",
      [norm(c) for c in again] == rendered["classic-12-photos-caps-at-10"])
check("and the camera plan itself is stable",
      [m["name"] for m in vr._camera_plan(10, "listing-42")]
      == [m["name"] for m in vr._camera_plan(10, "listing-42")])
other = capture(vr, **dict(MANY_ARGS, music_seed="listing-77"))[0]
check("a DIFFERENT listing does not get the same camera, so two agents' films do not "
      "open with the same shot",
      [vf_of(c) for c in other if "zoompan" in vf_of(c)] != photo_vfs)
_SRC = io.open(os.path.join(BACKEND, "video_renderer.py"), encoding="utf-8").read()
check("nothing here reaches for randomness -- no random module, no unseeded draw",
      not re.search(r"^\s*(import random|from random import)", _SRC, re.M)
      and "random.random" not in _SRC and "random.shuffle" not in _SRC
      and "random.seed" not in _SRC)
check("the camera plan is seeded by hashing, which has no global state a later render "
      "in the same worker could inherit",
      "hashlib.sha256" in _SRC[_SRC.index("def _camera_plan"):
                               _SRC.index("def _contain_clip")])

# THE PANNABLE AREA, proved rather than asserted. The window zoompan crops must stay
# inside the SHARP photograph -- not merely inside the composite, because the composite
# has blurred margins on one axis and sliding the photo around inside those reads as a
# layout fault. Swept over every move and every source shape that composites
# differently, evaluating the real expression at every frame.
print("  (sweeping %d moves x 6 source shapes x %d frames)"
      % (len(vr.CAMERA_MOVES), vr.FRAMES_PER_PHOTO))
worst = []
for size in [(800, 600), (1600, 1067), (1920, 1280), (1080, 1920), (1000, 2400),
             (1200, 1200)]:
    fg_w, fg_h = vr._contain_foreground_box(size)
    full_w, full_h = vr.VW * vr.UPSCALE, vr.VH * vr.UPSCALE
    for move in vr.CAMERA_MOVES:
        z, x, y, _k = vr._camera_expressions(move, size, vr.FRAMES_PER_PHOTO)
        xs = x.replace("*(iw-iw/zoom)", "")
        ys = y.replace("*(ih-ih/zoom)", "")
        for frame in range(vr.FRAMES_PER_PHOTO):
            zoom = evaluate(z, frame)
            for full, fg, frac in ((full_w, fg_w, evaluate(xs, frame)),
                                   (full_h, fg_h, evaluate(ys, frame))):
                window = full / zoom
                near = frac * (full - window)
                # The sharp photo's own band inside the composite.
                lo, hi = (full - fg) / 2.0, (full + fg) / 2.0
                if window >= fg:
                    # The window is wider than the photo on this axis, so it MUST show
                    # blurred margin -- the requirement is that the margin stays EVEN,
                    # which is the one position that does not read as a layout fault.
                    # Checked on the realised pixel position, not on the fraction: at
                    # zoom 1.0 there is no spare at all and every fraction lands here.
                    ok = abs(near - (full - window) / 2.0) < 1e-6
                else:
                    ok = near >= lo - 1e-6 and near + window <= hi + 1e-6
                if not ok:
                    worst.append((size, move["name"], frame, round(zoom, 4),
                                  round(frac, 4)))
check("no move, on any source shape, at any frame, puts the frame outside the sharp "
      "photograph", not worst, str(worst[:3]))
check("and every move really does move something (so the sweep is not vacuous)",
      all(max(vr._zoom_at(vr._camera_expressions(m, (800, 600),
                                                 vr.FRAMES_PER_PHOTO)[3], f)
              for f in range(vr.FRAMES_PER_PHOTO))
          > 1.0 + 1e-6 for m in vr.CAMERA_MOVES))

# The table's own invariants, checked over the table rather than over one film.
check("every move names itself, and the names are unique",
      len({m["name"] for m in vr.CAMERA_MOVES}) == len(vr.CAMERA_MOVES))
check("every move's zoom amounts are fractions in 0..1, so no move can ask for a zoom "
      "below 1.0 or above the photograph's own ceiling",
      all(0.0 <= a <= 1.0 for m in vr.CAMERA_MOVES for _t, a in m["zoom"]))
check("every move's waypoints run from the start of the shot to its end",
      all(m["zoom"][0][0] == 0.0 and m["zoom"][-1][0] == 1.0
          for m in vr.CAMERA_MOVES))
check("every move's drift is in -1..+1",
      all(-1.0 <= d <= 1.0 for m in vr.CAMERA_MOVES
          for key in ("drift", "cross") for _t, d in m.get(key) or ()))
check("the table covers the six shapes the brief asked for",
      {m["name"] for m in vr.CAMERA_MOVES} >= {
          vr.MOVE_SLOW_PUSH, vr.MOVE_HARD_PUSH, vr.MOVE_PUSH_SETTLE,
          vr.MOVE_PULL_OUT, vr.MOVE_HOLD, vr.MOVE_DIAGONAL})
check("a film of three or more always contains the near-static shot",
      all(any(m["name"] == vr.MOVE_HOLD for m in vr._camera_plan(n, "listing-%d" % n))
          for n in range(3, 13)))
# These go out as Reels and Stories. Opening on the shot that deliberately holds still
# wastes the one second that decides whether anybody watches -- which is exactly what
# the demo listing's seed did on the first real render.
check("but it NEVER opens the film",
      all(vr._camera_plan(n, "seed-%d" % s)[0]["name"] != vr.MOVE_HOLD
          for n in range(1, 13) for s in range(40)))
check("and no film ever repeats a move back to back",
      all(all(p[i]["name"] != p[i + 1]["name"] for i in range(len(p) - 1))
          for p in (vr._camera_plan(n, "seed-%d" % s)
                    for n in range(1, 13) for s in range(8))))
# _contain_foreground_box is a third statement of arithmetic two other functions do
# inline, on purpose (the Classic composite is pinned command-for-command and must not
# be refactored underneath the golden). The copies must not drift.
box_ok = []
for size in [(800, 600), (1600, 1067), (1000, 2400), (1200, 1200), (1080, 1920)]:
    src = photo(*size)
    composite, fills = vr._contain_over_blur(src)
    _bd, dramatic_box, _off = vr._dramatic_backdrop(src)
    mine = vr._contain_foreground_box(size)
    box_ok.append((mine == dramatic_box, (mine[0] >= composite.size[0]) == fills))
check("the camera's idea of where the photo sits agrees with _dramatic_backdrop's",
      all(a for a, _b in box_ok), str(box_ok))
check("and with _contain_over_blur's own fills_width answer",
      all(b for _a, b in box_ok), str(box_ok))

# ---------------------------------------------------------------------------
print("\n[1d] the opening shot is chosen, and it still gets a move with life in it")
# ---------------------------------------------------------------------------
# Jane: "i don't want the video to start with the stairs. start it with the living
# room", and "the stairs is not an important feature in the house". The rule itself is
# proved in test_smart_photo_selection.py; what matters HERE is the join between the
# rule and the camera -- the opening slot used to be whatever the photo ordering
# produced, and _camera_plan's "never open on the hold" fix was written when that slot
# was an accident. Now that it is a deliberate choice, the two have to hold together.
def selection_with(labels, urls=None, photo_index=0):
    """The real _smart_auto_selection, with only the vision call faked."""
    urls = urls or MANY_ARGS["image_urls"]
    saved = (vr._survey_photos, vr._fetch_image)
    vr._survey_photos = lambda photos, copy_guard=None: (
        [(labels[i], "the %s ... where the day begins" % labels[i])
         for i in range(len(photos))], None)
    vr._fetch_image = lambda u, attempts=2, deadline=None: photo()
    try:
        return vr._smart_auto_selection(urls, photo_index)
    finally:
        vr._survey_photos, vr._fetch_image = saved


def spaces_of(labels, **kw):
    picked, _caps, _note = selection_with(labels, **kw)
    by_url = dict(zip(MANY_ARGS["image_urls"], labels))
    return [by_url[u] for u in (picked or [])]


got = spaces_of(STAIRWELL_HERO_LABELS)
check("a listing whose STAR PHOTO is a stairwell opens on the living room",
      got and got[0] == "living", str(got))
check("...the stairwell is still in the film, at the end, not in slot one or two",
      "stairs" in got and got.index("stairs") >= len(got) - 2, str(got))
check("...and the lowest-ranked spaces lost their slots to real rooms",
      "bathroom" not in got and "carpark" not in got, str(got))
check("a listing with no living room opens on the exterior/facade",
      spaces_of(NO_LIVING_LABELS)[:1] == ["exterior"], str(spaces_of(NO_LIVING_LABELS)))
all_stairs = spaces_of(["stairs"] * 12)
check("a listing of nothing but staircases still ships a film (and opens on one, "
      "because there is nothing better)",
      len(all_stairs) >= vr.MIN_VIDEO_PHOTOS and set(all_stairs) == {"stairs"},
      str(all_stairs))

# THE JOIN. The opening shot is now the living room on purpose, so it must not be the
# shot that deliberately holds still -- that would spend the chosen second of the film
# on the one move with no life in it. Checked on the realised zoom rather than on the
# move's NAME, so renaming or re-tuning the table cannot quietly make the opener dull.
room_for = vr._motion_zoom_ceiling((1600, 1067)) - 1.0
dull = []
for n in range(1, 13):
    for s in range(40):
        opener = vr._camera_plan(n, "seed-%d" % s)[0]
        z, _x, _y, knots = vr._camera_expressions(opener, (1600, 1067),
                                                  vr.FRAMES_PER_PHOTO)
        peak = max(vr._zoom_at(knots, f) for f in range(vr.FRAMES_PER_PHOTO))
        if peak <= 1.0 + 0.15 * room_for:
            dull.append((n, s, opener["name"], round(peak, 4)))
check("over 480 films, the opening shot is never the near-static one -- it always has "
      "movement a viewer can see", not dull, str(dull[:3]))
opening_vfs = [vf_of(c) for c in rendered["classic-opens-on-living-room"]
               if "zoompan" in vf_of(c)]
first_peak = max(shot(opening_vfs[0])[0]) if opening_vfs else 0
check("and on the living-room opening this golden pins, the first shot moves "
      "(peak %.3f of %.3f)" % (first_peak, 1.0 + room_for),
      first_peak > 1.0 + 0.15 * room_for, "%.4f" % first_peak)

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
