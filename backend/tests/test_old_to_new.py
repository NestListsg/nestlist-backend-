"""The paid half of "From Old to New": the fal client, the prompts, the ceiling, the cache.

Nothing here touches the network or needs a key. The fal client's transport is stubbed,
which is the only way to test the thing that actually matters about it -- that a refusal
is never retried and never billed twice, that a deadline is honoured, and that the key
cannot reach a log or an error message.

Run: python3 tests/test_old_to_new.py
"""
import os, sys, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
import fal_images
import old_to_new

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))


SECRET = "fal-secret-key-nobody-should-ever-see"


class FakeResponse:
    def __init__(self, status=200, payload=None, body="", chunks=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = body
        self._chunks = chunks if chunks is not None else [b"JPEGBYTES"]
        self.raw = None
    def json(self):
        return self._payload
    def iter_content(self, chunk_size=None):
        return iter(self._chunks)


class FakeRequests:
    """Stands in for the requests module. Records every call and replays a script."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
    def _next(self, method, url, headers):
        self.calls.append((method, url, dict(headers or {})))
        if not self.script:
            raise AssertionError("fal client made an unexpected extra call to %s" % url)
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
    def request(self, method, url, headers=None, json=None, timeout=None):
        return self._next(method, url, headers)
    def get(self, url, timeout=None, stream=None):
        return self._next("GET", url, {})


def install(script):
    fake = FakeRequests(script)
    fal_images.requests = fake
    return fake


REAL_REQUESTS = fal_images.requests
SUBMITTED = FakeResponse(payload={"status_url": "https://fal/s", "response_url": "https://fal/r"})
COMPLETED = FakeResponse(payload={"status": "COMPLETED"})
RESULT = FakeResponse(payload={"images": [{"url": "https://fal/out.jpg"}]})
IMAGE = FakeResponse(chunks=[b"JPEG", b"BYTES"])


# ===========================================================================
print("\n[1] The key never leaves the environment")
# ===========================================================================
os.environ[fal_images.KEY_ENV] = SECRET
check("is_enabled reads the environment and nothing else", fal_images.is_enabled())
headers = fal_images._auth_headers()
check("the header is built fresh, as 'Key <token>'",
      headers["Authorization"] == "Key " + SECRET)
source = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "fal_images.py"), encoding="utf-8").read()
check("the module never opens a file, so a key file cannot become the source",
      "open(" not in source)
check("the environment is read in three places and no more -- the switch, the "
      "header, and the redactor that keeps it out of quoted fal errors",
      source.count("os.environ.get(KEY_ENV") == 3,
      str(source.count("os.environ.get(KEY_ENV")))
check("no log line is handed the key",
      not any("key" in line for line in source.splitlines() if "logger." in line))
# 401/403: the body is NOT echoed, because fal repeats request details back and this
# particular failure is about the credential.
err = fal_images._classify(401, "Authorization: Key %s is invalid" % SECRET)
check("a rejected key produces an error with no key in it", SECRET not in str(err))
check("and it is permanent -- retrying a bad key is pointless",
      isinstance(err, fal_images.FalPermanent))
os.environ.pop(fal_images.KEY_ENV)
check("no key -> FalNotConfigured, which reads as 'off', not 'broken'",
      not fal_images.is_enabled())
try:
    fal_images.generate("a calm room", ["https://x/a.jpg"])
    check("generate refuses without a key", False)
except fal_images.FalNotConfigured:
    check("generate refuses without a key", True)
except Exception as e:
    check("generate refuses without a key", False, type(e).__name__)
os.environ[fal_images.KEY_ENV] = SECRET

# A grep only proves what someone remembered to grep for. This drives every failure
# shape through the client with logging captured, and asserts the key is in none of it.
class Capture(logging.Handler):
    def __init__(self):
        logging.Handler.__init__(self)
        self.lines = []
    def emit(self, record):
        try:
            self.lines.append(record.getMessage() % () if not record.args
                              else record.getMessage())
        except Exception:
            self.lines.append(str(record.msg))

logging.disable(logging.NOTSET)
cap = Capture()
fal_logger = logging.getLogger("fal_images")
fal_logger.addHandler(cap)
fal_logger.setLevel(logging.DEBUG)
leaked = []
REAL_REQ = fal_images.requests
for script in ([FakeResponse(status=401, body="bad key %s" % SECRET)],
               [FakeResponse(status=500, body=SECRET), FakeResponse(status=500, body=SECRET)],
               [FakeResponse(status=422, body="safety: %s" % SECRET)],
               [RuntimeError("connection reset while sending Key " + SECRET)] * 2):
    fal_images.requests = FakeRequests(script)
    try:
        fal_images.generate("a calm room", ["https://x/a.jpg"])
    except Exception as e:
        if SECRET in str(e):
            leaked.append("exception: " + type(e).__name__)
fal_images.requests = REAL_REQ
leaked += ["log: " + ln for ln in cap.lines if SECRET in ln]
fal_logger.removeHandler(cap)
logging.disable(logging.CRITICAL)
check("driven through every failure shape, the key reaches no log line and no error",
      not leaked, str(leaked[:2]))
check("(and the failures really did happen)", len(cap.lines) >= 1)


# ===========================================================================
print("\n[2] Permanent and retryable are not treated alike")
# ===========================================================================
for status, kind in ((429, fal_images.FalTemporary), (500, fal_images.FalTemporary),
                     (503, fal_images.FalTemporary), (404, fal_images.FalPermanent),
                     (400, fal_images.FalPermanent), (403, fal_images.FalPermanent)):
    check("HTTP %d -> %s" % (status, kind.__name__),
          isinstance(fal_images._classify(status, "x"), kind))
check("a 422 mentioning safety is a REFUSAL, not a bug on our side",
      isinstance(fal_images._classify(422, "blocked by the safety filter"),
                 fal_images.FalRefused))
check("and a refusal is a kind of permanent, so the retry loop skips it",
      issubclass(fal_images.FalRefused, fal_images.FalPermanent))

fake = install([SUBMITTED, COMPLETED, RESULT, IMAGE])
data = fal_images.generate("a calm cream living room", ["https://x/a.jpg"])
check("the happy path returns the image bytes", data == b"JPEGBYTES")
check("and it took exactly submit, poll, fetch, download", len(fake.calls) == 4,
      str([c[1] for c in fake.calls]))
check("every fal call carries the auth header",
      all(c[2].get("Authorization") == "Key " + SECRET
          for c in fake.calls if c[1].startswith("https://fal/s")
          or c[1].endswith("/edit")))

fake = install([FakeResponse(status=429, body="slow down"),
                SUBMITTED, COMPLETED, RESULT, IMAGE])
data = fal_images.generate("a calm room", ["https://x/a.jpg"])
check("a 429 is retried once and then succeeds", data == b"JPEGBYTES")
check("the retry really was a second submit", len(fake.calls) == 5)

fake = install([FakeResponse(status=422, body="blocked by the safety filter")])
try:
    fal_images.generate("a calm room", ["https://x/a.jpg"])
    check("a refusal is raised, not retried", False)
except fal_images.FalRefused:
    check("a refusal is raised, not retried", True)
check("and it cost exactly ONE request -- no second bill", len(fake.calls) == 1,
      str(len(fake.calls)))

fake = install([FakeResponse(status=500, body="boom"), FakeResponse(status=500, body="boom")])
try:
    fal_images.generate("a calm room", ["https://x/a.jpg"])
    check("attempts are BOUNDED, not endless", False)
except fal_images.FalTemporary:
    check("attempts are BOUNDED, not endless", True)
check("exactly MAX_ATTEMPTS of them", len(fake.calls) == fal_images.MAX_ATTEMPTS,
      str(len(fake.calls)))

fake = install([SUBMITTED, COMPLETED,
                FakeResponse(payload={"images": [], "description": "I can't make that"})])
try:
    fal_images.generate("a calm room", ["https://x/a.jpg"])
    check("COMPLETED with no image is a refusal, not a retry", False)
except fal_images.FalRefused as e:
    check("COMPLETED with no image is a refusal, not a retry", "can't make that" in str(e))


# ===========================================================================
print("\n[3] The deadline is absolute and covers the queue")
# ===========================================================================
# A request that sits IN_QUEUE forever must end at the deadline, not run until fal
# changes its mind. The poll loop is what would otherwise spin.
QUEUED = FakeResponse(payload={"status": "IN_QUEUE"})
fake = install([SUBMITTED] + [QUEUED] * 60)
started = time.monotonic()
try:
    fal_images.generate("a calm room", ["https://x/a.jpg"],
                        deadline=time.monotonic() + 1.5)
    check("a request stuck in fal's queue ends at the deadline", False)
except fal_images.FalTemporary:
    check("a request stuck in fal's queue ends at the deadline", True)
elapsed = time.monotonic() - started
check("and it ends ON TIME rather than whenever fal gives up (%.2fs)" % elapsed,
      elapsed < 4.0, "%.2fs" % elapsed)
check("the deadline bounds the WHOLE call, so an expired one never even submits",
      isinstance(fal_images._classify(500, ""), fal_images.FalTemporary))
fake = install([])
try:
    fal_images.generate("a calm room", ["https://x/a.jpg"],
                        deadline=time.monotonic() - 1)
    check("a deadline already past spends nothing", False)
except fal_images.FalError:
    check("a deadline already past spends nothing", len(fake.calls) == 0)


# ===========================================================================
print("\n[4] Prompt safety is screened BEFORE we pay for a refusal")
# ===========================================================================
for word in ("explosion", "no explosion", "without any debris", "demolish the wall",
             "FIRE", "the blast"):
    check("%r is screened out" % word, fal_images.screen_prompt(word) is not None)
for word in ("a calm cream living room", "slide the sofa out of frame",
             "warm cove lighting switches on"):
    check("%r is fine" % word, fal_images.screen_prompt(word) is None)
fake = install([])
try:
    fal_images.generate("remove the old sofa, no explosion", ["https://x/a.jpg"])
    check("a screened prompt never reaches fal", False)
except fal_images.FalRefused:
    check("a screened prompt never reaches fal", len(fake.calls) == 0)
check("the whole point: the exclusion form is refused too, which cost two "
      "generations to learn", fal_images.screen_prompt("no fire, no debris"))

# Every prompt this product actually sends has to pass its own screen.
check("the anonymise prompt is safe to send",
      fal_images.screen_prompt(old_to_new.anonymise_prompt()) is None)
check("the rebuild prompt is safe to send",
      fal_images.screen_prompt(old_to_new.rebuild_prompt()) is None)
check("the room prompt is safe to send",
      fal_images.screen_prompt(old_to_new.room_prompt()) is None)
check("and so is the description prompt we send to Claude",
      fal_images.screen_prompt(old_to_new._DESCRIBE_PROMPT.replace(
          "Do not use the words explosion, blast, fire,\nflames, burning, debris, "
          "rubble, demolish or destroy, in any form, for any reason.", "")) is None)
check("a model-written description carrying a refused word is dropped, not sent",
      old_to_new._clean_contents("a fireplace with no fire") is None)
check("an over-long description is dropped too",
      old_to_new._clean_contents("sofa " * 200) is None)
check("a normal one survives, whitespace-tidied",
      old_to_new._clean_contents("  a low  cream\nsofa ") == "a low cream sofa")
fal_images.requests = REAL_REQUESTS


# ===========================================================================
print("\n[5] The prompts obey the rules the spec paid for")
# ===========================================================================
anon = old_to_new.anonymise_prompt(["to the left", "immediately to the right",
                                    "behind the roofline"])
check("each neighbouring building is named INDIVIDUALLY (a general instruction "
      "leaves the recognisable one untouched)",
      anon.count("Replace the building") == 3, anon)
check("the subject house is pinned feature by feature -- this frame is a real "
      "photograph and carries the privacy line, not the AI notice",
      all(w in anon for w in ("window", "door", "roofline", "gate")))
check("with no description pass we still name positions rather than generalise",
      old_to_new.anonymise_prompt().count("Replace the building")
      == len(old_to_new.DEFAULT_NEIGHBOUR_POSITIONS))
for name, prompt in (("anonymise", anon), ("rebuild", old_to_new.rebuild_prompt()),
                     ("room", old_to_new.room_prompt())):
    check("the %s prompt pins the camera -- the whole effect depends on it"
          % name, "Same camera position" in prompt or "same camera position" in prompt)
check("the rebuild keeps the street it inherited",
      all(w in old_to_new.rebuild_prompt()
          for w in ("road", "neighbouring buildings", "sky")))
check("the room prompt pins the STRUCTURE, because the mask is the furniture",
      all(w in old_to_new.room_prompt()
          for w in ("walls", "ceiling", "windows", "doorways")))
room = old_to_new.room_prompt("the dark rosewood sofas", "a low cream sofa")
check("a described room uses the model's own words for both halves",
      "the dark rosewood sofas" in room and "a low cream sofa" in room)
check("an undescribed room falls back to the house scheme, not to nothing",
      old_to_new.DEFAULT_NEW_CONTENTS in old_to_new.room_prompt())


# ===========================================================================
print("\n[6] The cost ceiling is checked UP FRONT, and it trims rather than refuses")
# ===========================================================================
check("a full listing is the exterior pair plus the rooms",
      old_to_new.IMAGES_PER_LISTING == 2 + old_to_new.MAX_ROOMS == 6)
cost = old_to_new.IMAGES_PER_LISTING * fal_images.COST_PER_IMAGE_USD
check("which costs about 24 US cents (%.2f)" % cost, abs(cost - 0.24) < 0.001)
check("the per-agent ceiling is derived from the listing ceiling, so they cannot drift",
      old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH
      == old_to_new.MAX_LISTINGS_PER_AGENT_PER_MONTH * old_to_new.IMAGES_PER_LISTING)
monthly = old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH * fal_images.COST_PER_IMAGE_USD
check("about US$9.60 an agent a month (%.2f)" % monthly, abs(monthly - 9.6) < 0.01)
check("plenty of room left -> the whole plan", old_to_new.reserve_budget(6, 0) == 6)
check("three images of allowance left -> a trimmed plan, not an error",
      old_to_new.reserve_budget(6, old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH - 3) == 3)
try:
    old_to_new.reserve_budget(6, old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH)
    check("no allowance at all -> BudgetExhausted", False)
except old_to_new.BudgetExhausted as e:
    check("no allowance at all -> BudgetExhausted", True)
    check("and it says so in words an agent can act on",
          "Classic" in str(e) and "next month" in str(e), str(e))


# ===========================================================================
print("\n[7] build_plan: order, cache, and every way it degrades")
# ===========================================================================
EXT = "https://x/ext.jpg"
ROOMS = [("https://x/r%d.jpg" % i, None) for i in range(4)]


def run(cache=None, used=0, fail=(), describe=None, rooms=None, exterior=EXT):
    """Runs build_plan with a recording generator. `fail` names roles that raise."""
    calls = []
    def gen(prompt, sources, deadline):
        calls.append({"prompt": prompt, "sources": list(sources)})
        for role, exc in fail:
            if role in prompt or (role == "ANON" and "Keep the house" in prompt) \
                    or (role == "NEW" and "Rebuild the house" in prompt) \
                    or (role == "ROOM" and "Redesign this room" in prompt):
                raise exc
        return b"IMAGE"
    stored = []
    def store(role, key, data):
        stored.append((role, key))
        return "https://store/%s.jpg" % key.replace(":", "_")
    plan = old_to_new.build_plan(
        exterior, rooms if rooms is not None else ROOMS, cache,
        generate_image=gen, store_image=store, describe=describe,
        images_used_this_month=used)
    return plan, calls, stored


plan, calls, stored = run()
check("a cold listing generates exactly six images", len(calls) == 6, str(len(calls)))
check("the ANONYMISED frame is generated FIRST", "Keep the house" in calls[0]["prompt"])
check("and from the agent's own photograph", calls[0]["sources"] == [EXT])
check("THE REBUILD IS GENERATED FROM THE ANONYMISED FRAME, not the original -- "
      "otherwise the real neighbours come back on screen",
      "Rebuild the house" in calls[1]["prompt"]
      and calls[1]["sources"] == [plan.opening_url], str(calls[1]["sources"]))
check("then one redesign per room, each from its own photo",
      all("Redesign this room" in c["prompt"] for c in calls[2:])
      and [c["sources"][0] for c in calls[2:]] == [u for u, _ in ROOMS])
check("the plan reports what it paid for", plan.images_paid == 6)
check("every room came back transformable", len(plan.transformable_rooms) == 4)
check("and a clean plan says nothing to the agent", plan.degradations == [],
      str(plan.degradations))

warm = old_to_new.read_cache(old_to_new.write_cache(plan.cache))
check("the cache round-trips through the listing column", warm == plan.cache)
plan2, calls2, _ = run(cache=warm)
check("A REGENERATE PAYS NOTHING", calls2 == [] and plan2.images_paid == 0)
check("and still produces the same film", plan2.opening_url == plan.opening_url
      and [r["after_url"] for r in plan2.rooms] == [r["after_url"] for r in plan.rooms])
describe_calls = []
def describe():
    describe_calls.append(1)
    return None, [(None, None)] * 5, True
run(cache=warm, describe=describe)
check("a full cache hit does not even pay for the DESCRIPTION call -- most of the "
      "wait as well as most of the cost", describe_calls == [])
run(describe=describe)
check("a cold one does", describe_calls == [1])

check("a junk cache column is ignored rather than crashing",
      old_to_new.read_cache("not a dict") == {}
      and old_to_new.read_cache({"images": {"k": 7}}) == {})
check("the key is stable across runs for the same photo",
      old_to_new.cache_key("room_new", EXT) == old_to_new.cache_key("room_new", EXT))
check("and different per photo and per role",
      len({old_to_new.cache_key("room_new", EXT),
           old_to_new.cache_key("exterior_new", EXT),
           old_to_new.cache_key("room_new", "https://x/other.jpg")}) == 3)
check("the key never carries the photo's address",
      EXT not in old_to_new.cache_key("room_new", EXT))

refusal = fal_images.FalRefused("nope")
plan, calls, _ = run(fail=[("ROOM", refusal)])
check("every room refused -> the rooms are still in the film, as real photographs",
      len(plan.rooms) == 4 and not plan.transformable_rooms)
check("and the agent is told", any("shown as they are today" in d
                                   for d in plan.degradations), str(plan.degradations))
check("a refusal is not counted as paid", plan.images_paid == 2)

plan, calls, _ = run(fail=[("ANON", refusal)])
check("THE OPENING IS NEVER REPLACED BY THE REAL PHOTOGRAPH -- that would put the "
      "neighbours Jane wants hidden back on screen",
      plan.opening_url is None and plan.exterior_url is None)
check("the rebuild is not even attempted without a frame to generate it from",
      len(calls) == 5, str(len(calls)))
check("and the agent is told the video starts inside",
      any("starts inside" in d for d in plan.degradations), str(plan.degradations))

plan, calls, _ = run(fail=[("Rebuild", refusal)])
check("a failed rebuild keeps the opening and loses only the new-build shot",
      plan.opening_url and plan.exterior_url is None)
check("and says so", any("opens on the property as it is today" in d
                         for d in plan.degradations))

plan, _, _ = run(used=old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH - 2)
check("two images of allowance -> the exterior PAIR survives, the rooms do not",
      plan.opening_url and plan.exterior_url and not plan.transformable_rooms)
plan, calls, _ = run(used=old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH - 1)
check("ONE image of allowance -> the pair is NOT half-bought (a rebuild with no "
      "anonymised frame to come from is impossible, not cheaper)",
      plan.exterior_url is None and len(calls) <= 1, str(len(calls)))
check("and the near-limit note reaches the agent",
      any("monthly limit" in d for d in plan.degradations), str(plan.degradations))
try:
    run(used=old_to_new.MAX_IMAGES_PER_AGENT_PER_MONTH)
    check("no allowance at all stops before the first generation", False)
except old_to_new.BudgetExhausted:
    check("no allowance at all stops before the first generation", True)

plan, calls, _ = run(describe=lambda: (None, [(None, None)] * 5, False))
check("a first photo that is NOT an exterior skips the opening rather than "
      "rebuilding a living room", plan.opening_url is None and len(calls) == 4)
check("and tells the agent how to get one",
      any("put an exterior photo first" in d for d in plan.degradations),
      str(plan.degradations))

plan, _, _ = run(describe=lambda: (_ for _ in ()).throw(RuntimeError("model down")))
check("a description pass that blows up costs STYLE, never the feature",
      len(plan.transformable_rooms) == 4)

plan, calls, _ = run(exterior=None)
check("a listing with no exterior still gets its rooms",
      plan.opening_url is None and len(calls) == 4)


print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
