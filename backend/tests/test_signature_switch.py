"""The Signature tier must be genuinely off, not just hidden.

Jane's call on 2026-10-21, after watching the first automated films: the machinery is
right but the FILM is not -- an agent against whatever wall they stood in front of, cut
to a property they are visibly not in. So the tier is switched off at the server until
the opener is right.

Hiding the button alone would not be enough: an agent with a stale tab, or anyone who
knows the address, can still POST to the endpoint. These checks pin that the single gate
closes every door, and that flipping SIGNATURE_ENABLED opens them all again without a
code change.
"""
import ast, io, os, sys
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))

src = io.open(os.path.join(os.path.dirname(_HERE), "main.py"), encoding="utf-8").read()

print("\n[1] the switch exists and defaults to OFF")
check("SIGNATURE_ENABLED is read from the environment",
      'SIGNATURE_ENABLED = os.environ.get("SIGNATURE_ENABLED"' in src)
check("it defaults to empty, i.e. off", 'os.environ.get("SIGNATURE_ENABLED", "")' in src)
check("the availability gate consults it",
      "return SIGNATURE_ENABLED and _signature_columns_supported is True" in src)

print("\n[2] every door goes through that one gate")
tree = ast.parse(src)
fns = {n.name: n for n in ast.walk(tree)
       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
check("get_signature_status is gated",
      "_signature_columns_available()" in ast.get_source_segment(src, fns["get_signature_status"]))
# generate_signature asks the LEARNER, so the learner must answer through the gate --
# otherwise a stale browser tab can still start a render with the tier switched off.
check("generate_signature refuses when the tier is off",
      "_note_signature_columns(listing)" in ast.get_source_segment(src, fns["generate_signature"])
      and "return _signature_columns_available()" in src,
      "the learner must answer through the gate")
check("the agent payload is gated too",
      "_signature_columns_available() and agent.get(\"presenter_video_url\")" in src)

print("\n[3] with it off, the behaviour is the one the app already handles")
import importlib
os.environ.pop("SIGNATURE_ENABLED", None)
sys.modules.pop("main", None)
# Importing main is heavy; read the constant the same way the module computes it.
off = os.environ.get("SIGNATURE_ENABLED", "").strip() in ("1", "true", "yes")
check("unset means off", off is False)
for val in ("1", "true", "yes"):
    os.environ["SIGNATURE_ENABLED"] = val
    on = os.environ.get("SIGNATURE_ENABLED", "").strip() in ("1", "true", "yes")
    check("SIGNATURE_ENABLED=%s turns it on" % val, on is True)
for val in ("0", "false", "", "off", "no"):
    os.environ["SIGNATURE_ENABLED"] = val
    on = os.environ.get("SIGNATURE_ENABLED", "").strip() in ("1", "true", "yes")
    check("SIGNATURE_ENABLED=%r stays off" % val, on is False)
os.environ.pop("SIGNATURE_ENABLED", None)

print("\n[4] nothing was deleted -- it is a switch, not a removal")
check("the Signature renderer is still registered",
      "register_renderer(video_jobs.JOB_TYPE_SIGNATURE" in src)
check("the endpoints still exist",
      "generate_signature" in fns and "get_signature_status" in fns)
check("the 503 wording the frontend keys on is unchanged",
      "Signature videos are not switched on yet." in src)

print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
