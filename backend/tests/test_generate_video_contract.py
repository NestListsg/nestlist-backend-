"""Structural guards on main.py for the audit fixes that cannot be executed locally.

generate_video is an async FastAPI handler with a dependency chain that needs supabase,
anthropic and fitz, none of which are installed here. These checks read the real source
with ast instead, so the specific mistakes the audit found cannot come back unnoticed.
"""
import ast
import io
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(os.path.dirname(_HERE), "main.py")
SOURCE = io.open(MAIN, encoding="utf-8").read()
TREE = ast.parse(SOURCE)

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def func(name):
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def src(node):
    return ast.get_source_segment(SOURCE, node) or ""


print("\n[S2(a)] The template reported back is the one that will be rendered")

gv = func("generate_video")
check("generate_video was found", gv is not None)
body = src(gv)

# Find the dict literal returned on the queued path and read what it maps
# "video_template_id" to.
mapped = None
for node in ast.walk(gv):
    if isinstance(node, ast.Dict):
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == "video_template_id":
                if any(isinstance(k, ast.Constant) and k.value == "already_in_flight"
                       for k in node.keys):
                    mapped = value
check("the queued response's video_template_id is bound to the in-flight job's template",
      isinstance(mapped, ast.Name) and mapped.id == "in_flight_template",
      ast.dump(mapped)[:80] if mapped is not None else "not found")
check("it is NOT the newly requested template",
      not (isinstance(mapped, ast.Name) and mapped.id == "chosen_video_template_id"))
check("in_flight_template is read from the existing job's stored payload",
      'job.get("payload") or {}).get("video_template_id")' in body)
check("what the agent asked for is still reported, separately",
      '"requested_video_template_id": chosen_video_template_id' in body)
check("the response says whether this press started a render or joined one",
      '"already_in_flight": not created' in body)

print("\n[S2(c)] Ownership is re-checked before anything the agent can see")

core = func("_render_video_core")
check("_render_video_core was found", core is not None)
check("it takes an ownership_check",
      any(a.arg == "ownership_check" for a in core.args.args),
      str([a.arg for a in core.args.args]))
core_src = src(core)
guards = core_src.count("ownership_check is not None and not ownership_check()")
check("there are exactly two ownership guards", guards == 2, "found=%d" % guards)
check("the first guard sits before the upload",
      core_src.index("ownership_check is not None") < core_src.index("_store_rendered_video("))
check("the second guard sits before the listing is updated",
      core_src.rindex("ownership_check is not None") < core_src.index("update_payload = {"))
check("a superseded render raises rather than writing",
      core_src.count("video_jobs.SupersededError") == 2)

runner = func("_classic_job_run")
check("the queued path supplies the ownership check",
      "ownership_check=lambda: video_jobs.heartbeat(job)" in src(runner))
legacy = func("_render_video_job")
check("the legacy in-process path deliberately supplies none",
      "ownership_check" not in src(legacy))

print("\n[uuid spelling] The queue is keyed on the listing row's own id")
check("enqueue is given the listing row's id, not the URL's spelling",
      'listing.get("id") or listing_id' in body)

print("\n[S2(d)] Queue-off conditions reach Jane, not just the logs")
starter = func("_start_video_queue")
check("a failed probe alerts", "send_telegram_alert_throttled" in src(starter))
check("a fallback enqueue alerts", "video_queue_enqueue_failed" in body)

print("\n" + "=" * 70)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name in FAIL:
        print("  - " + name)
print("=" * 70)
sys.exit(1 if FAIL else 0)
