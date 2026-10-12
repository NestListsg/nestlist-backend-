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

rsrc = io.open(os.path.join(os.path.dirname(_HERE), "video_renderer.py"), encoding="utf-8").read()
check("non-raw filter text is the original string (no loudnorm unless normalize)",
      'f"[1:a]atrim=0:{silent_duration:.3f},asetpts=PTS-STARTPTS,"\n                        f"{norm}"' in rsrc
      and "attempts = [True, False] if is_raw else [False]" in rsrc)

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
