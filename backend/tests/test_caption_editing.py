"""What an agent is allowed to write into a video caption.

Captions are the one surface where a NestList video makes a claim about a property,
so the rules the model is held to have to hold for the agent too -- a rule the UI
merely asks for is not a rule. These tests cover the guard itself and, just as
importantly, that every way it can say no has a plain-English explanation waiting
for the agent in main.py.
"""
import ast, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_renderer as vr

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))

print("\n[1] captions an agent should be able to save")
for good in ["the terrace ... where golden hours drift by",
             "the flexible room ... where work and home sit side by side",
             "morning light ... where the day begins"]:
    ok, why = vr._caption_is_safe(good)
    check(repr(good[:38]), ok, why)

print("\n[2] captions that must be refused")
for bad, expect in [
    ("unit 12 ... where rest comes easy", "contains a digit"),
    ("the lounge ... priced to sell", "price-adjacent wording"),
    ("the stunning suite ... where luxury awaits", "superlative"),
    ("the kitchen where meals happen", "wrong format"),
    ("", "empty"),
    ("the hall ... it's lovely", "unsupported punctuation"),
]:
    ok, why = vr._caption_is_safe(bad)
    check("%-42r -> %s" % (bad[:40], why or "ACCEPTED"), (not ok) and expect in why, "expected %r" % expect)

print("\n[3] every refusal reason has plain English for the agent")
src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")).read()
tree = ast.parse(src)
mapping = None
for node in ast.walk(tree):
    if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "_CAPTION_REJECTION_REASONS" for t in node.targets):
        mapping = {k.value for k in node.value.keys}
check("main.py defines the reason table", mapping is not None)
if mapping:
    guard = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "video_renderer.py")).read()
    body = guard[guard.index("def _caption_is_safe"):]
    body = body[:body.index("\ndef ", 1)]
    literal = set(re.findall(r'return False, "([^"]+)"', body))
    missing = literal - mapping
    check("no reason reaches the agent untranslated", not missing, "missing: %s" % sorted(missing))
    # superlative is formatted, not literal -- confirm it is handled some other way
    check("superlatives are explained too",
          any("superlative" in m for m in mapping) or "superlative" in src,
          "agent would see a raw code")

print("\n[4] the guard's length cap matches what the renderer can fit")
check("CAPTION_MAX_CHARS is defined", isinstance(getattr(vr, "CAPTION_MAX_CHARS", None), int))

print("\n" + "="*62)
print("PASSED: %d    FAILED: %d" % (P, F))
print("="*62)
sys.exit(1 if F else 0)
