"""HDR presenting clips: the teal contact card, and the colour label that caused it.

WHAT BROKE. Modern iPhones record HDR (HLG) by default. The upload transcode brought
Janel's clip down to 8-bit but neither tonemapped nor retagged it, so the stored clip
kept HLG-curve samples AND bt2020/HLG labels. That clip is slide zero of a Signature,
the assembly is a stream-copy concat, and a stream copy takes its colour description
from the FIRST input -- so the whole 41s film published as HDR, players applied an
HDR->SDR transform to it, and the closing contact card came out teal instead of the
warm off-white it is designed to be.

This file runs REAL ffmpeg against REAL generated video, like test_presenter_clip does,
because the thing being protected is what ffmpeg actually writes into the file. The
colour tags are read out of ffmpeg's own banner -- there is no ffprobe here, and the
codebase has been bitten twice by assuming there is.

Four things are pinned:

1. An HDR upload comes out TRUE SDR bt709 -- converted, not merely relabelled. The
   relabel-only shortcut fixes the card but leaves the agent flat, and the agent is the
   entire point of a Signature.
2. An SDR upload is NOT TOUCHED. Same ffmpeg arguments as before the fix, so an
   ordinary recording cannot be collateral damage.
3. The fallback ladder really falls back. zscale/tonemap are build-time options and
   Railway installs Debian's ffmpeg, so each rung is forced and checked rather than
   assumed -- and EVERY rung, including the bottom one, still ends up tagged bt709,
   because that is what keeps the card right.
4. No presenter clip can relabel the film any more -- and a clip uploaded BEFORE this
   fix, still sitting in storage as HLG, is REPAIRED on the way into the film rather
   than leaving the agent to go and upload it again.
5. The Classic does not move. The golden file in test_signature_render is the real
   guard, command for command; the narrow statement here is that the colour tag goes on
   the presenter slide and nowhere else, so a Classic cannot be touched by it.
"""
import os, re, shutil, subprocess, sys, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_renderer as vr

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))


# The renderer calls plain "ffmpeg" from PATH, which Railway has and this laptop does
# not. Borrow imageio-ffmpeg's bundled binary, as the other video tests do.
if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
    try:
        import imageio_ffmpeg
        shim = tempfile.mkdtemp()
        shutil.copy(imageio_ffmpeg.get_ffmpeg_exe(), os.path.join(shim, "ffmpeg"))
        os.environ["PATH"] = shim + os.pathsep + os.environ["PATH"]
    except Exception as e:
        print("no ffmpeg available (%s) - skipping" % e); sys.exit(0)

TMP = tempfile.mkdtemp()


def colour_of(path):
    """The colour description ffmpeg prints for `path`, e.g. "tv, bt709" -- read from
    the banner, same as the production code."""
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", path],
                         capture_output=True).stderr.decode("utf-8", "replace")
    line = next((l for l in err.splitlines() if "Video:" in l), "")
    m = re.search(r"\((tv|pc)(?:, ([\w/-]+))?[,)]", line)
    return "%s, %s" % (m.group(1), m.group(2) or "-") if m else "untagged"


def luma_md5(path):
    """md5 of the decoded frames, so "did the picture change" is answerable."""
    import hashlib
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "rawvideo",
                          "-pix_fmt", "yuv420p", "-"], capture_output=True).stdout
    return hashlib.md5(raw).hexdigest()


def colourfulness(path):
    """Mean saturation of a decoded frame. The one number that says whether the agent
    kept her colour or came out washed out."""
    png = os.path.join(TMP, "cf.png")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", path, "-frames:v", "1",
                    "-update", "1", "-vf", "scale=320:-2", png], capture_output=True)
    from PIL import Image
    import numpy as np
    a = np.asarray(Image.open(png).convert("RGB")).astype(float)
    mx, mn = a.max(2), a.min(2)
    return float(np.where(mx > 0, (mx - mn) / np.maximum(mx, 1), 0).mean())


def make(path, transfer=None, primaries=None, matrix=None, seconds=3, size="640x360"):
    """A test recording. `transfer` set to arib-std-b67 or smpte2084 makes a clip that
    DECLARES itself HDR, which is what the production code keys off -- a phone's tags
    are the only thing we can read without ffprobe, and they are what leaked.

    The picture is a colour gradient plus moving bars, so a tonemap has something to
    actually change and colourfulness means something.

    Deliberately small and short. These clips are transcoded a dozen times over by the
    checks below, and the tonemap works in floating point, so a full-size 12s source
    would make this file slower than the rest of the suite put together. 3s still clears
    PRESENTER_MIN_SECONDS, and 640x360 is under PRESENTER_LONG_EDGE so the scale step is
    exercised without any upscaling."""
    cmd = ["ffmpeg", "-y", "-v", "error",
           "-f", "lavfi", "-i", "testsrc=size=%s:rate=30:duration=%s" % (size, seconds),
           "-f", "lavfi", "-i", "sine=frequency=440:duration=%s" % seconds,
           "-c:a", "aac", "-c:v", "libx264", "-t", str(seconds)]
    if transfer:
        cmd += ["-pix_fmt", "yuv420p10le", "-x264-params",
                "colorprim=%s:transfer=%s:colormatrix=%s"
                % (primaries or "bt2020", transfer, matrix or "bt2020nc")]
    else:
        cmd += ["-pix_fmt", "yuv420p"]
    subprocess.run(cmd + [path], check=True, capture_output=True)
    return path


HLG = make(os.path.join(TMP, "hlg.mov"), transfer="arib-std-b67")
PQ = make(os.path.join(TMP, "pq.mov"), transfer="smpte2084")
SDR = make(os.path.join(TMP, "sdr.mp4"))


def prepare(src, filters=None):
    """Runs the real prepare_presenter_clip and returns (stored path, seconds, note,
    the ffmpeg argv it used). `filters` stubs which ffmpeg filters appear to exist,
    which is how each rung of the ladder is forced."""
    wd = tempfile.mkdtemp(dir=TMP)
    cmds = []
    real_run, real_has = vr._run_ffmpeg, vr._has_ffmpeg_filter
    def spy(cmd, timeout):
        cmds.append(list(cmd)); return real_run(cmd, timeout)
    vr._run_ffmpeg = spy
    if filters is not None:
        vr._has_ffmpeg_filter = lambda name: name in filters
    vr._hdr_rung_cache = None
    try:
        data, seconds, note = vr.prepare_presenter_clip(open(src, "rb").read(), wd)
    finally:
        vr._run_ffmpeg, vr._has_ffmpeg_filter = real_run, real_has
        vr._hdr_rung_cache = None
    out = os.path.join(wd, "stored.mp4")
    with open(out, "wb") as f:
        f.write(data)
    return out, seconds, note, cmds[-1]


# ---------------------------------------------------------------------------
print("\n[1] reading a clip's colour tags off ffmpeg's banner (no ffprobe)")
# ---------------------------------------------------------------------------
hlg_c = vr._source_colour(HLG)
check("an HLG recording reports its transfer as arib-std-b67",
      hlg_c["transfer"] == "arib-std-b67", hlg_c)
check("...and bt2020 primaries with the bt2020nc matrix",
      (hlg_c["primaries"], hlg_c["matrix"]) == ("bt2020", "bt2020nc"), hlg_c)
pq_c = vr._source_colour(PQ)
check("a PQ recording reports smpte2084", pq_c["transfer"] == "smpte2084", pq_c)
sdr_c = vr._source_colour(SDR)
check("an ordinary SDR recording reports no HDR transfer",
      sdr_c["transfer"] not in vr.HDR_TRANSFERS, sdr_c)
check("a file with no colour tags at all answers None rather than raising",
      vr._source_colour(SDR) is not None)
check("an unreadable path answers None instead of raising",
      vr._source_colour(os.path.join(TMP, "does-not-exist.mp4")) is None)

# The single-value banner shape (ffmpeg prints "bt709" once when all three agree) has
# to parse as all three, not as a matrix with two Nones -- getting this wrong would
# make a healthy bt709 clip look like it was missing its transfer.
one_value = make(os.path.join(TMP, "t709.mp4"), transfer="bt709",
                 primaries="bt709", matrix="bt709")
c = vr._source_colour(one_value)
check("the one-name banner shape parses as matrix AND primaries AND transfer",
      c["matrix"] == c["primaries"] == c["transfer"] == "bt709", c)

# ---------------------------------------------------------------------------
print("\n[2] an HDR upload is CONVERTED, not just relabelled")
# ---------------------------------------------------------------------------
stored, secs, note, cmd = prepare(HLG)
check("the stored clip no longer claims to be HLG",
      "arib-std-b67" not in colour_of(stored), colour_of(stored))
check("the stored clip states bt709 outright",
      colour_of(stored) == "tv, bt709", colour_of(stored))
check("it was tonemapped, not merely tagged (a zscale/tonemap chain ran)",
      "tonemap" in " ".join(cmd) and "zscale" in " ".join(cmd))
check("the agent is not told anything, because nothing was degraded", note is None, note)
check("the clip is still a usable length", secs > 1.5, secs)

relabel_only, _, _, _ = prepare(HLG, filters=())
check("relabelling alone would leave LESS colour than tonemapping does",
      colourfulness(stored) > colourfulness(relabel_only),
      (colourfulness(stored), colourfulness(relabel_only)))
check("...and the tonemapped and relabelled clips are genuinely different pixels",
      luma_md5(stored) != luma_md5(relabel_only))

pq_stored, _, pq_note, pq_cmd = prepare(PQ)
check("a PQ clip is converted too", colour_of(pq_stored) == "tv, bt709", colour_of(pq_stored))
check("...through the same tonemap chain", "tonemap" in " ".join(pq_cmd))

# ---------------------------------------------------------------------------
print("\n[3] an SDR upload is left completely alone")
# ---------------------------------------------------------------------------
sdr_stored, sdr_secs, sdr_note, sdr_cmd = prepare(SDR)
joined = " ".join(sdr_cmd)
for unwanted in ("zscale", "tonemap", "colorspace=", "x264-params"):
    check("an SDR clip's transcode does not reach for %s" % unwanted,
          unwanted not in joined, joined[:200])
check("an SDR clip's filter chain is exactly the scale and format it always was",
      sdr_cmd[sdr_cmd.index("-vf") + 1].endswith("format=yuv420p")
      and "zscale" not in sdr_cmd[sdr_cmd.index("-vf") + 1],
      sdr_cmd[sdr_cmd.index("-vf") + 1])
check("an SDR clip carries no note", sdr_note is None, sdr_note)
check("an SDR clip still transcodes to something usable", sdr_secs > 1.5, sdr_secs)

# ---------------------------------------------------------------------------
print("\n[4] the fallback ladder, each rung forced")
# ---------------------------------------------------------------------------
RUNGS = [
    ("rung 1: zscale + tonemap", ("zscale", "tonemap", "colorspace"), False),
    ("rung 2: colorspace only",  ("colorspace",),                     True),
    ("rung 3: relabel only",     (),                                  True),
]
tagged, noted, pixels = {}, {}, {}
for label, filters, expect_note in RUNGS:
    p, _, n, c = prepare(HLG, filters=filters)
    tagged[label], noted[label], pixels[label] = colour_of(p), n, luma_md5(p)
    check("%s produces a clip tagged bt709" % label,
          tagged[label] == "tv, bt709", tagged[label])
    check("%s %s the agent" % (label, "warns" if expect_note else "does not warn"),
          bool(n) == expect_note, n)

check("every rung, even the bottom one, stops the film claiming to be HDR",
      all(v == "tv, bt709" for v in tagged.values()), tagged)
check("rung 1 and rung 2 really do different work to the picture",
      pixels["rung 1: zscale + tonemap"] != pixels["rung 2: colorspace only"])
check("rung 3 changes no pixels at all -- it is purely a relabel",
      pixels["rung 3: relabel only"] != pixels["rung 1: zscale + tonemap"])
check("the bottom rung's warning is in plain words an agent can act on",
      "flat" in (noted["rung 3: relabel only"] or "").lower())

# PQ cannot use rung 2: the gamut-only filter reads the curve as gamma, which comes out
# very dark, so a PQ clip must drop straight to the relabel.
pq_p, _, pq_n, pq_c2 = prepare(PQ, filters=("colorspace",))
check("a PQ clip SKIPS the HLG-only rung rather than being darkened by it",
      "colorspace=" not in " ".join(pq_c2), " ".join(pq_c2)[:200])
check("...and is still tagged bt709", colour_of(pq_p) == "tv, bt709", colour_of(pq_p))
check("...and still warns the agent", bool(pq_n))

# ---------------------------------------------------------------------------
print("\n[5] a conversion that fails must not cost a good recording")
# ---------------------------------------------------------------------------
wd = tempfile.mkdtemp(dir=TMP)
real = vr._hdr_to_sdr_filter
vr._hdr_to_sdr_filter = lambda colour: ("zscale=t=this-is-not-a-transfer", None)
try:
    data, secs, note = vr.prepare_presenter_clip(open(HLG, "rb").read(), wd)
    broke = None
except Exception as e:
    data = None; broke = e
finally:
    vr._hdr_to_sdr_filter = real
check("a broken tonemap chain does NOT reject the agent's clip", data is not None, broke)
if data:
    p = os.path.join(wd, "fallback.mp4")
    with open(p, "wb") as f:
        f.write(data)
    check("...it falls back and still stores a usable clip", secs > 1.5, secs)
    check("...still tagged bt709, so the card is still right",
          colour_of(p) == "tv, bt709", colour_of(p))
    check("...and the agent is told they may look flat", bool(note), note)

# A file that genuinely is not video must still be refused, clearly.
try:
    vr.prepare_presenter_clip(b"this is not a video at all, not even close",
                              tempfile.mkdtemp(dir=TMP))
    refused = False
except ValueError as e:
    refused = "video" in str(e).lower()
check("a file that is not video is still refused with wording for the agent", refused)

# ---------------------------------------------------------------------------
print("\n[6] no presenter clip can relabel the film -- not even a pre-fix one")
# ---------------------------------------------------------------------------
# Janel's clip is sitting in storage RIGHT NOW tagged HLG. The film must stop
# inheriting its label, or every video she makes keeps the teal card -- and she should
# not have to re-upload to get her own colour back either, so it is repaired on the way
# into the film.
legacy = make(os.path.join(TMP, "legacy_stored.mp4"), transfer="arib-std-b67")
check("the stand-in for her stored clip really is tagged HLG",
      "arib-std-b67" in colour_of(legacy), colour_of(legacy))

slide = os.path.join(TMP, "clip_presenter.mp4")
legacy_note = vr._presenter_opening_clip(legacy, slide)
check("slide zero states bt709 even when the stored clip says HLG",
      colour_of(slide) == "tv, bt709", colour_of(slide))
check("...and nothing is reported, because the clip was REPAIRED, not just relabelled",
      legacy_note is None, legacy_note)

# A healthy stored clip must take the identical path it always did.
healthy = make(os.path.join(TMP, "healthy_stored.mp4"), transfer="bt709",
               primaries="bt709", matrix="bt709")
healthy_slide = os.path.join(TMP, "clip_presenter_ok.mp4")
cmds = []
real_run = vr._run_ffmpeg
def spy(cmd, timeout):
    cmds.append(list(cmd)); return real_run(cmd, timeout)
vr._run_ffmpeg = spy
try:
    healthy_note = vr._presenter_opening_clip(healthy, healthy_slide)
finally:
    vr._run_ffmpeg = real_run
graph = cmds[-1][cmds[-1].index("-filter_complex") + 1]
check("a healthy stored clip's filtergraph has no repair spliced into it",
      "zscale" not in graph and "tonemap" not in graph and "colorspace=" not in graph,
      graph[:160])
check("...and it starts straight into fps, exactly as before",
      graph.startswith("[0:v]fps="), graph[:40])
check("...and reports nothing", healthy_note is None, healthy_note)

# When the server CANNOT repair it, the agent has to be told -- that is the one case
# where re-uploading is the answer.
real_has = vr._has_ffmpeg_filter
vr._has_ffmpeg_filter = lambda name: name == "cas"
vr._hdr_rung_cache = None
try:
    stuck_note = vr._presenter_opening_clip(legacy, os.path.join(TMP, "stuck.mp4"))
finally:
    vr._has_ffmpeg_filter = real_has
    vr._hdr_rung_cache = None
check("an ffmpeg that cannot repair the stored clip DOES warn the agent",
      bool(stuck_note), stuck_note)

# ...and that is what the stream-copy concat publishes.
other = os.path.join(TMP, "clip_00.mp4")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "testsrc=size=1080x1920:rate=24:duration=2"]
               + vr._ENCODE_ARGS + [other], check=True, capture_output=True)
film = os.path.join(TMP, "film.mp4")
vr._concat([slide, other], film, TMP, "hdr_list", 120)
check("the concatenated film is published bt709, not HDR",
      colour_of(film) == "tv, bt709", colour_of(film))
check("...and carries no trace of the HLG transfer",
      "arib-std-b67" not in colour_of(film), colour_of(film))


# End to end through a real render: a pre-fix clip must now come through quietly, with
# nothing for the agent to do, while an ffmpeg that cannot repair it says so.
def render_degradations(presenter, can_repair=True):
    """A render with everything external stubbed, returning its degradation list.

    `can_repair` says whether this pretend ffmpeg has the tonemap filters.

    REAL ffmpeg runs for the presenter opening clip and nothing else. That is the
    command under test -- it is the one that reads the stored clip's colour and decides
    whether to repair it -- and faking the other dozen keeps this a couple of seconds
    rather than three full renders."""
    from PIL import Image
    saved = {n: getattr(vr, n) for n in
             ("_run_ffmpeg", "_fetch_listing_photo", "_fetch_image",
              "_generate_room_captions", "_smart_auto_selection", "_probe_duration",
              "_has_ffmpeg_filter")}
    real_run = vr._run_ffmpeg

    def part_real(cmd, timeout):
        out = cmd[-1]
        if out.endswith("clip_presenter.mp4"):
            return real_run(cmd, timeout)        # the command under test
        if not out.startswith("-"):
            with open(out, "wb") as f:
                f.write(b"\0" * 2048)

    vr._run_ffmpeg = part_real
    img = Image.new("RGB", (1600, 1067), (150, 140, 130))
    vr._fetch_listing_photo = lambda url: img
    vr._fetch_image = lambda url, attempts=2, deadline=None: img
    vr._generate_room_captions = lambda photos, copy_guard=None: ([None] * len(photos), None)
    vr._smart_auto_selection = lambda urls, idx, copy_guard=None: (None, None, None)
    vr._probe_duration = lambda path, fallback: fallback
    vr._has_ffmpeg_filter = (
        (lambda name: name in ("cas", "zscale", "tonemap", "colorspace")) if can_repair
        else (lambda name: name == "cas"))
    vr._clarity_filter_cache = None
    vr._hdr_rung_cache = None
    try:
        _data, degradations, _captions = vr.render_property_video(
            image_urls=["https://example.test/%d.jpg" % i for i in range(3)],
            property_type="Semi-Detached", agent_name="Janel",
            agent_contact_line="9123 4567", music_seed="listing-42",
            presenter_path=presenter)
        return list(degradations or [])
    finally:
        for n, fn in saved.items():
            setattr(vr, n, fn)
        vr._clarity_filter_cache = None
        vr._hdr_rung_cache = None


def hdr_notes(degradations):
    return [d for d in degradations if "hdr" in d.lower() or "flat" in d.lower()]


try:
    repaired = render_degradations(legacy, can_repair=True)
    stuck = render_degradations(legacy, can_repair=False)
    fixed_stored, _, _, _ = prepare(HLG)
    healthy_degs = render_degradations(fixed_stored, can_repair=True)
except Exception as e:
    repaired = stuck = healthy_degs = None
    check("the end-to-end render ran at all", False, repr(e)[:400])

if repaired is not None:
    check("a render off Janel's pre-fix clip now needs NOTHING from her",
          not hdr_notes(repaired), repaired)
    check("...and the film is not otherwise degraded either",
          repaired == [], repaired)
    check("but an ffmpeg that cannot repair it does tell her she may look flat",
          bool(hdr_notes(stuck)), stuck)
    check("a render off a properly converted clip says nothing about HDR",
          not hdr_notes(healthy_degs), healthy_degs)

# ---------------------------------------------------------------------------
print("\n[7] the Classic does not move")
# ---------------------------------------------------------------------------
# The golden file (test_signature_render) is the real guard, command for command. This
# is the narrower statement that matters for THIS change: the colour tag is applied to
# the presenter slide and nowhere else, so a Classic -- which has no presenter slide --
# cannot be touched by it.
check("the colour tag is NOT in the shared clip encoder settings",
      not any("x264-params" in a for a in vr._ENCODE_ARGS), vr._ENCODE_ARGS)
check("the shared clip encoder settings mention no colour options at all",
      not any(a.startswith(("-colorspace", "-color_primaries", "-color_trc",
                            "-color_range")) for a in vr._ENCODE_ARGS),
      vr._ENCODE_ARGS)

plain = os.path.join(TMP, "plain_clip.mp4")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "testsrc=size=1080x1920:rate=24:duration=2"]
               + vr._ENCODE_ARGS + [plain], check=True, capture_output=True)
check("a clip built with those settings is tagged exactly as it was before: not at all",
      colour_of(plain) == "untagged", colour_of(plain))

print("\n" + "=" * 60)
print("PASSED: %-5d FAILED: %d" % (P, F))
print("=" * 60)
shutil.rmtree(TMP, ignore_errors=True)
sys.exit(1 if F else 0)
