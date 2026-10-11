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

src = io.open(os.path.join(os.path.dirname(_HERE), "main.py"), encoding="utf-8").read()
tree = ast.parse(src)
fns = {n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
args = [a.arg for a in fns["generate_video"].args.args]
check("generate_video accepts music_track", "music_track" in args)
check("music-tracks endpoint exists", "get_music_tracks" in fns and '"/api/music-tracks"' in src)
check("queued payload and legacy path both carry the choice",
      'payload.get("music_track")' in src and "chosen_music_track,\n    ))" in src)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
