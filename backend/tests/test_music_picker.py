"""Music picker: track list, choice resolution, and that the automatic pick is unchanged.

Runs without ffmpeg/supabase: exercises the selection helpers against a temp audio dir
and reads main.py with ast for the endpoint wiring.
"""
import ast, io, json, logging, os, sys, tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
logging.disable(logging.CRITICAL)
import video_renderer as vr  # noqa: E402

PASS, FAIL = [], []
def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))

d = tempfile.mkdtemp()
for n in ("a.mp3", "b.mp3", "c.mp3", "legacy.mp3"):
    open(os.path.join(d, n), "wb").write(b"x")
vr.AUDIO_DIR = d
vr.TRACKS_MANIFEST = os.path.join(d, "tracks.json")

def manifest(obj):
    # Always write the TEMP manifest, and point the renderer at it. This used to write
    # wherever vr.TRACKS_MANIFEST happened to point -- and because the checks further
    # down deliberately repoint it at the REAL audio/tracks.json, a manifest() call
    # after that overwrote the shipped track list with two fake entries. Belt and
    # braces: assert it, so the mistake cannot come back quietly.
    vr.TRACKS_MANIFEST = os.path.join(d, "tracks.json")
    assert vr.TRACKS_MANIFEST.startswith(d), "refusing to write outside the temp dir"
    json.dump(obj, open(vr.TRACKS_MANIFEST, "w"))

manifest({"_comment": "x", "a.mp3": {"mood": "Warm", "feel": "soft"},
          "b.mp3": {"mood": "Upbeat", "auto": False}, "c.mp3": {"mood": "Ghost"},
          "legacy.mp3": "grand", "missing.mp3": {"mood": "Upbeat"}})
ids = [t["id"] for t in vr.pickable_tracks()]
check("only tracks with a mood AND a file are pickable", ids == ["a.mp3", "b.mp3", "c.mp3"], str(ids))
check("legacy string entries are not pickable", "legacy.mp3" not in ids)
check("manifest line without a file is not pickable", "missing.mp3" not in ids)
check("choice resolves by exact id", vr.resolve_pickable_track("b.mp3") == os.path.join(d, "b.mp3"))
check("unlisted / traversal ids do not resolve",
      vr.resolve_pickable_track("legacy.mp3") is None and vr.resolve_pickable_track("../a.mp3") is None)
picks = {vr._select_music_track("s%d" % i, "")[0] for i in range(300)}
check("auto:false track never auto-picked", os.path.join(d, "b.mp3") not in picks)
check("auto still uses the other tracks", len(picks) >= 2)
check("auto values", all(vr.is_auto_music_choice(x) for x in (None, "", " Auto ")))

open(vr.TRACKS_MANIFEST, "w").write("{not json")
check("broken manifest -> nothing pickable, auto still works",
      vr.pickable_tracks() == [] and vr._select_music_track("s", "")[0] is not None)
os.remove(vr.TRACKS_MANIFEST)
check("missing manifest -> nothing pickable, auto still works",
      vr.pickable_tracks() == [] and vr._select_music_track("s", "")[0] is not None)

# --- raw (unprocessed) tracks: opt-in only, existing tracks untouched ---
manifest({"r.mp3": {"mood": "Upbeat", "auto": False, "raw": True}, "a.mp3": {"mood": "Warm"},
          "legacy.mp3": "grand", "t.mp3": {"mood": "Warm", "raw": "yes"}})
check("raw:true track is flagged raw", vr._is_raw_track(os.path.join(d, "r.mp3")))
check("track without raw flag is not raw", not vr._is_raw_track(os.path.join(d, "a.mp3")))
check("legacy string entry is not raw", not vr._is_raw_track(os.path.join(d, "legacy.mp3")))
check("non-boolean raw value is not raw (strict)", not vr._is_raw_track(os.path.join(d, "t.mp3")))
check("unlisted file is not raw", not vr._is_raw_track(os.path.join(d, "zzz.mp3")))
open(vr.TRACKS_MANIFEST, "w").write("{not json")
check("broken manifest -> nothing is raw", not vr._is_raw_track(os.path.join(d, "r.mp3")))
os.remove(vr.TRACKS_MANIFEST)

real = io.open(os.path.join(os.path.dirname(_HERE), "audio", "tracks.json"), encoding="utf-8").read()
vr.TRACKS_MANIFEST = os.path.join(os.path.dirname(_HERE), "audio", "tracks.json")
m = vr._load_manifest()
check("real manifest: Upbeat entry is raw, picker-only, mood Upbeat",
      m["acoustic_upbeat.mp3"]["raw"] is True and m["acoustic_upbeat.mp3"]["auto"] is False
      and m["acoustic_upbeat.mp3"]["mood"] == "Upbeat")
check("real manifest: every pre-existing track is NOT raw",
      all(not e["raw"] for n, e in m.items() if n != "acoustic_upbeat.mp3"))
check("real manifest: Upbeat is not listed while its file is not deployed",
      "acoustic_upbeat.mp3" not in [t["id"] for t in vr.pickable_tracks()]
      or os.path.exists(os.path.join(os.path.dirname(_HERE), "audio", "acoustic_upbeat.mp3")))
check("real manifest: Upbeat never in the automatic pick",
      all(os.path.basename(vr._select_music_track("s%d" % i, pt)[0]) != "acoustic_upbeat.mp3"
          for i in range(200) for pt in ("", "Bungalow", "Condo")))

# The raw-track loudness ladder, checked by running a render rather than by matching the
# source text. The text form of this check broke the moment the soundtrack stage was
# refactored for Signature, while the behaviour it cared about was entirely intact --
# and a string match could never have told the difference. The stronger guarantee that
# a Classic render issues the IDENTICAL ffmpeg commands it issued before the refactor
# now lives in tests/test_classic_render.py, which diffs them against a committed golden.
def soundtrack_graphs(track_id):
    """The audio filter strings a render would hand ffmpeg, in attempt order."""
    from PIL import Image
    graphs = []

    def fake_run_ffmpeg(cmd, timeout):
        if "-filter_complex" in cmd:
            g = cmd[cmd.index("-filter_complex") + 1]
            if "[1:a]" in g:
                graphs.append(g)
                raise RuntimeError("ffmpeg exited 1: forced, to see the next attempt")
        out = cmd[-1]
        if not out.startswith("-"):
            open(out, "wb").write(b"\0" * 2048)

    saved = {n: getattr(vr, n) for n in ("_run_ffmpeg", "_fetch_listing_photo",
                                         "_generate_room_captions", "_probe_duration")}
    vr._run_ffmpeg = fake_run_ffmpeg
    vr._fetch_listing_photo = lambda url: Image.new("RGB", (1200, 800), (150, 150, 150))
    vr._generate_room_captions = lambda photos, copy_guard=None: ([None] * len(photos), None)
    vr._probe_duration = lambda path, fallback: fallback
    try:
        vr.render_property_video(image_urls=["https://example.test/a.jpg"],
                                 agent_name="A", music_track=track_id, music_seed="s")
    except Exception:
        pass
    finally:
        for n, fn in saved.items():
            setattr(vr, n, fn)
    return graphs

real_manifest_path = vr.TRACKS_MANIFEST          # the real audio/tracks.json, set above
manifest({"a.mp3": {"mood": "Warm"}, "raw.mp3": {"mood": "Upbeat", "raw": True}})
check("the real track list was not written to",
      vr.TRACKS_MANIFEST != real_manifest_path and vr.TRACKS_MANIFEST.startswith(d))
open(os.path.join(d, "raw.mp3"), "wb").write(b"x")
vr.AUDIO_DIR = d
plain = soundtrack_graphs("a.mp3")
check("a non-raw track gets exactly one attempt", len(plain) == 1, repr(len(plain)))
check("and no loudnorm in it", plain and "loudnorm" not in plain[0])
rawg = soundtrack_graphs("raw.mp3")
check("a raw track tries loudness normalization first", len(rawg) == 2 and "loudnorm" in rawg[0],
      repr([("loudnorm" in g) for g in rawg]))
check("then falls back to the track as-is", len(rawg) == 2 and "loudnorm" not in rawg[1])
check("the fallback is otherwise the same filter as a non-raw track",
      len(rawg) == 2 and plain and rawg[1] == plain[0].replace("a.mp3", "raw.mp3"))
os.remove(os.path.join(d, "raw.mp3"))

src = io.open(os.path.join(os.path.dirname(_HERE), "main.py"), encoding="utf-8").read()
tree = ast.parse(src)
fns = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
args = [a.arg for a in fns["generate_video"].args.args]
check("generate_video accepts music_track", "music_track" in args)
check("music-tracks endpoint exists", "get_music_tracks" in fns and '"/api/music-tracks"' in src)
check("queued payload and legacy path both carry the choice",
      'payload.get("music_track")' in src and "chosen_music_track,\n        chosen_video_photos or None,\n    ))" in src)

check("audio preview endpoint exists and is login-gated",
      "get_music_track_audio" in fns and '"/api/music-tracks/{track_id}/audio"' in src
      and "get_current_agent" in ast.get_source_segment(src, fns["get_music_track_audio"]).split("\n")[0])
check("audio endpoint 404s unless the track resolves as pickable",
      "resolve_pickable_track(track_id)" in ast.get_source_segment(src, fns["get_music_track_audio"])
      and "404" in ast.get_source_segment(src, fns["get_music_track_audio"]))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
