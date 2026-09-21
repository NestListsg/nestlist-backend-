"""Executable cover on the auth paths: login rate limiting, password verification,
the bcrypt length ceiling, and the ordering inside password reset.

Nothing in backend/tests touched these lines before, which is how a reset-lockout
regression got as far as a commit. main.py cannot be imported here (supabase,
anthropic, fitz and bcrypt are all absent locally), so each function under test is
lifted out of the real source with ast and executed against stubs. That means these
tests run against the code that actually ships, not a copy.

On bcrypt: the real library is used when it is installed (Railway, CI). Locally it
is stubbed, and the stub reproduces the one behaviour these tests turn on --
bcrypt 5.x RAISES ValueError on input longer than 72 bytes rather than truncating
it, confirmed against 5.0.0 during the 2026-09-21 audit. requirements.txt pins
bcrypt>=5.0.0,<6.0.0.
"""
import ast
import hashlib
import io
import logging
import os
import sys
import threading
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(os.path.dirname(_HERE), "main.py")
SOURCE = io.open(MAIN, encoding="utf-8").read()
TREE = ast.parse(SOURCE)

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


# ----------------------------------------------------------------------------
# bcrypt: the real thing if present, otherwise a stub with the same 72-byte rule
# ----------------------------------------------------------------------------
try:
    import bcrypt
    BCRYPT_IS_REAL = True
except ImportError:
    import types

    _b = types.ModuleType("bcrypt")

    def _gensalt(rounds=12):
        return ("$2b$%02d$" % rounds).encode() + hashlib.sha256(os.urandom(16)).hexdigest()[:22].encode()

    def _hashpw(password, salt):
        if len(password) > 72:
            raise ValueError("password cannot be longer than 72 bytes, truncate manually if necessary")
        return salt[:29] + hashlib.sha256(salt[:29] + password).hexdigest().encode()

    def _checkpw(password, hashed):
        if len(password) > 72:
            raise ValueError("password cannot be longer than 72 bytes, truncate manually if necessary")
        if not hashed.startswith(b"$2"):
            raise ValueError("Invalid salt")
        return _hashpw(password, hashed[:29]) == hashed

    _b.gensalt, _b.hashpw, _b.checkpw = _gensalt, _hashpw, _checkpw
    sys.modules["bcrypt"] = _b
    import bcrypt
    BCRYPT_IS_REAL = False


# ----------------------------------------------------------------------------
# Stubs, and the real functions lifted out of main.py
# ----------------------------------------------------------------------------
class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FakeRequest:
    def __init__(self, headers=None, peer="10.0.0.1"):
        self.headers = headers or {}
        self.client = type("C", (), {"host": peer})() if peer else None


class Result:
    def __init__(self, data):
        self.data = data


NS = {
    "datetime": datetime,
    "timedelta": timedelta,
    "hashlib": hashlib,
    "bcrypt": bcrypt,
    "threading": threading,
    "logger": logging.getLogger("nestlist-test"),
    "HTTPException": HTTPException,
    "_rate_limit_last_sweep": {},
    "_rate_limit_lock": threading.Lock(),
    "_login_hits": {},
    "_login_failure_hits": {},
}
NS["logger"].addHandler(logging.NullHandler())
NS["logger"].propagate = False

_WANTED_FUNCS = (
    "password_too_long", "hash_password", "verify_password",
    "_client_ip", "_rate_limited",
    "login", "confirm_password_reset", "register",
)
_WANTED_CONSTS = (
    "MAX_PASSWORD_BYTES", "PASSWORD_TOO_LONG_MESSAGE",
    "_RATE_LIMIT_MAX_WINDOW_SECONDS", "_RATE_LIMIT_SWEEP_SECONDS",
)

_found = set()
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _WANTED_FUNCS:
        node.decorator_list = []  # drop @app.post -- there is no app here
        # Signature annotations (LoginRequest, Request, ...) are evaluated at def
        # time and name types that don't exist outside the app. Strip them; they
        # are typing only, so the body under test is untouched.
        node.returns = None
        for arg in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
            arg.annotation = None
        exec(compile(ast.Module([node], []), MAIN, "exec"), NS)
        _found.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in _WANTED_CONSTS:
                exec(compile(ast.Module([node], []), MAIN, "exec"), NS)
                _found.add(t.id)

password_too_long = NS["password_too_long"]
hash_password = NS["hash_password"]
verify_password = NS["verify_password"]
_client_ip = NS["_client_ip"]
_rate_limited = NS["_rate_limited"]

print("[0] Every function under test was found in the shipped main.py")
check("all expected functions and constants extracted",
      _found == set(_WANTED_FUNCS) | set(_WANTED_CONSTS),
      "missing=%s" % sorted((set(_WANTED_FUNCS) | set(_WANTED_CONSTS)) - _found))
print("      bcrypt: %s" % ("REAL library" if BCRYPT_IS_REAL else "stubbed (models 5.x >72-byte ValueError)"))


# ----------------------------------------------------------------------------
print("\n[1] verify_password: bcrypt or nothing")
# ----------------------------------------------------------------------------
real_hash = hash_password("s3cret-passphrase")
check("a real bcrypt hash still verifies", verify_password("s3cret-passphrase", real_hash) is True)
check("a wrong password is rejected", verify_password("nope", real_hash) is False)
check("a PLAIN-TEXT stored value is not a credential",
      verify_password("hunter2", "hunter2") is False,
      "this is the fallback that made a readable column a working login")
check("an empty stored value is rejected", verify_password("", "") is False)
check("a null stored value does not raise", verify_password("x", None) is False)
check("a $2y$ hash is refused rather than silently accepted",
      verify_password("x", "$2y$12$" + "a" * 53) is False,
      "gensalt() cannot emit $2y$, so this is unreachable for live rows")


# ----------------------------------------------------------------------------
print("\n[2] The bcrypt 72-byte ceiling is caught before bcrypt sees it")
# ----------------------------------------------------------------------------
check("MAX_PASSWORD_BYTES matches bcrypt's actual limit", NS["MAX_PASSWORD_BYTES"] == 72)
check("72 bytes is allowed", password_too_long("a" * 72) is False)
check("73 bytes is rejected", password_too_long("a" * 73) is True)
# The reason the guard counts bytes: 30 emoji is 30 characters and 120 bytes.
emoji_pw = "\U0001f3e0" * 30
check("an emoji passphrase under 72 CHARACTERS is still caught",
      len(emoji_pw) < 72 and password_too_long(emoji_pw) is True,
      "chars=%d bytes=%d" % (len(emoji_pw), len(emoji_pw.encode("utf-8"))))
check("a None password does not raise", password_too_long(None) is False)

_raised = None
try:
    hash_password("a" * 200)
except Exception as e:
    _raised = e
check("hash_password genuinely raises past the limit, so the guard is load-bearing",
      isinstance(_raised, ValueError), repr(_raised))


# ----------------------------------------------------------------------------
print("\n[3] Password reset: nothing irreversible happens before it can fail")
# ----------------------------------------------------------------------------
class ResetReq:
    def __init__(self, token, new_password):
        self.token = token
        self.new_password = new_password


def run_reset(new_password, fail_on=None, used_at=None, expires_in=3600):
    """Run the shipped confirm_password_reset against a recording fake DB."""
    calls = []
    row = {
        "id": "reset-1",
        "agent_id": "agent-1",
        "used_at": used_at,
        "expires_at": (datetime.utcnow() + timedelta(seconds=expires_in)).isoformat(),
    }

    def fake_db_execute(build, attempts=3, what="query", idempotent=True):
        calls.append(what)
        if fail_on and fail_on in what:
            raise ConnectionError("connection pool is dead")
        if "password_resets select" in what:
            return Result([row])
        return Result([])

    NS["db_execute"] = fake_db_execute
    token = "t" * 40
    row["token_hash"] = hashlib.sha256(token.encode()).hexdigest()
    try:
        out = NS["confirm_password_reset"](ResetReq(token, new_password))
        return out, calls, None
    except HTTPException as e:
        return None, calls, e


def burned(calls):
    return any("password_resets update" in c for c in calls)


def changed(calls):
    return any("agents update" in c for c in calls)


out, calls, err = run_reset("a-perfectly-normal-passphrase")
check("the happy path succeeds", out == {"success": True}, str(err))
check("the token is burned BEFORE the password is changed",
      burned(calls) and changed(calls)
      and calls.index([c for c in calls if "password_resets update" in c][0])
      < calls.index([c for c in calls if "agents update" in c][0]),
      " -> ".join(calls))

# THE REGRESSION. An over-long passphrase used to reach bcrypt only after the
# token had been burned: 500, password unchanged, link dead, and a password
# manager refilling the same value would burn every fresh link the same way.
out, calls, err = run_reset("a" * 100)
check("an over-long password is a clean 400, not a 500",
      err is not None and err.status_code == 400, repr(err))
check("the message tells the agent what to do about it",
      err is not None and "too long" in err.detail.lower() and "shorter" in err.detail.lower(),
      err.detail if err else "")
check("the reset token is NOT burned -- the link still works",
      not burned(calls), " -> ".join(calls) or "(no writes)")
check("the password was not changed either", not changed(calls))

out, calls, err = run_reset("\U0001f3e0" * 30)
check("an emoji passphrase over 72 bytes is caught the same way, link intact",
      err is not None and err.status_code == 400 and not burned(calls))

# The deliberate trade from the reordering: if the password write fails, the
# token IS spent, and the agent must be told plainly rather than left guessing.
out, calls, err = run_reset("another-fine-passphrase", fail_on="agents update")
check("a failed password write returns 503, not a bare 500",
      err is not None and err.status_code == 503, repr(err))
check("it says the password was NOT changed",
      err is not None and "not changed" in err.detail.lower(), err.detail if err else "")
check("it tells them to request a new link",
      err is not None and "new one" in err.detail.lower(), err.detail if err else "")

out, calls, err = run_reset("fine-passphrase", used_at=datetime.utcnow().isoformat())
check("an already-used link is still refused, and writes nothing",
      err is not None and err.status_code == 400 and not burned(calls) and not changed(calls))

out, calls, err = run_reset("fine-passphrase", expires_in=-60)
check("an expired link is still refused, and writes nothing",
      err is not None and err.status_code == 400 and not burned(calls) and not changed(calls))

out, calls, err = run_reset("short")
check("the minimum-length check still fires first", err is not None and err.status_code == 400)


# ----------------------------------------------------------------------------
print("\n[4] _client_ip keys on an address the caller cannot choose")
# ----------------------------------------------------------------------------
REAL = "203.0.113.9"
SPOOF = "198.51.100.7"
check("a single-entry header is used as-is (platform stripped the inbound one)",
      _client_ip(FakeRequest({"x-forwarded-for": REAL})) == REAL)
check("with an appended chain, the RIGHTMOST entry wins",
      _client_ip(FakeRequest({"x-forwarded-for": "%s, %s" % (SPOOF, REAL)})) == REAL,
      "leftmost would have returned the spoofed value")
check("a long forged chain still resolves to the appended real address",
      _client_ip(FakeRequest({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3, %s" % REAL})) == REAL)
check("odd spacing and empty entries are tolerated",
      _client_ip(FakeRequest({"x-forwarded-for": " , %s ,  , %s " % (SPOOF, REAL)})) == REAL)
check("x-real-ip is the fallback when there is no forwarded chain",
      _client_ip(FakeRequest({"x-real-ip": REAL})) == REAL)
check("with no proxy headers at all it falls back to the socket peer",
      _client_ip(FakeRequest({}, peer="10.1.2.3")) == "10.1.2.3")
check("a request with no client does not raise",
      _client_ip(FakeRequest({}, peer=None)) == "unknown")

# The whole point: a rotating forged header must not mint a fresh bucket.
store = {}
NS["_rate_limit_last_sweep"].clear()
rotated = [
    _rate_limited(store, _client_ip(FakeRequest({"x-forwarded-for": "10.0.0.%d, %s" % (i, REAL)})),
                  limit=10, window_seconds=600)
    for i in range(14)
]
check("rotating the forged leftmost entry does NOT evade the limit",
      rotated[10] is True and len(store) == 1,
      "buckets=%d (leftmost keying would have made 14)" % len(store))


# ----------------------------------------------------------------------------
print("\n[5] Login: successes are free, failures are what get rationed")
# ----------------------------------------------------------------------------
GOOD_PW = "correct-horse-battery"
AGENT = {"id": "agent-1", "email": "agent@example.com", "password_hash": hash_password(GOOD_PW)}

NS["create_token"] = lambda agent_id: "token-for-" + agent_id
NS["_agent_response"] = lambda agent: {k: v for k, v in agent.items() if k != "password_hash"}


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._email = None

    def table(self, name):
        return self

    def select(self, cols):
        return self

    def eq(self, col, val):
        self._email = val
        return self

    def execute(self):
        return Result([r for r in self._rows if r["email"] == self._email])


NS["get_db"] = lambda: FakeQuery([AGENT])


class LoginReq:
    def __init__(self, email, password):
        self.email = email
        self.password = password


def attempt(email, password, ip=REAL, forged=None):
    xff = "%s, %s" % (forged, ip) if forged else ip
    try:
        return NS["login"](LoginReq(email, password), FakeRequest({"x-forwarded-for": xff})), None
    except HTTPException as e:
        return None, e


def reset_login_state():
    NS["_login_hits"].clear()
    NS["_login_failure_hits"].clear()
    NS["_rate_limit_last_sweep"].clear()


reset_login_state()
out, err = attempt(AGENT["email"], GOOD_PW)
check("a correct password still logs in", out is not None and out["token"] == "token-for-agent-1", repr(err))
check("the password hash never leaves the building",
      out is not None and "password_hash" not in out["agent"])

reset_login_state()
codes = []
for i in range(13):
    _, e = attempt(AGENT["email"], "wrong-guess-%d" % i)
    codes.append(e.status_code if e else 200)
check("ten wrong guesses are answered 401", codes[:10] == [401] * 10, str(codes))
check("the eleventh is refused with 429", codes[10] == 429, str(codes))
check("and it stays refused", codes[11:] == [429, 429], str(codes))

# Over the failure budget the password must not even be TESTED -- otherwise the
# limiter only tells an attacker "wrong" and never actually stops a guess.
out, err = attempt(AGENT["email"], GOOD_PW)
check("once over budget, even the CORRECT password is refused",
      out is None and err.status_code == 429,
      "a counter consulted only after the password check would have let this in")

# The onboarding-day case: one office NAT address, many agents signing in fine.
reset_login_state()
ok = all(attempt(AGENT["email"], GOOD_PW)[0] is not None for _ in range(40))
check("40 successful sign-ins from one shared office IP all go through", ok)
_, e = attempt(AGENT["email"], "wrong-guess")
check("and the failure budget is still untouched afterwards",
      e is not None and e.status_code == 401,
      "successes must not spend the failure budget")

# The flood cap is the thing that still covers raw hammering.
reset_login_state()
flood = [attempt(AGENT["email"], GOOD_PW)[1] for _ in range(63)]
check("the flood cap catches sheer volume even when every request succeeds",
      flood[60] is not None and flood[60].status_code == 429,
      "first 429 at attempt %s" % next((i + 1 for i, f in enumerate(flood) if f), None))

# Enumeration: an unknown email must be indistinguishable from a wrong password.
reset_login_state()
_, unknown = attempt("nobody@example.com", "whatever")
_, wrong = attempt(AGENT["email"], "whatever")
check("an unknown email and a wrong password give the identical answer",
      unknown.status_code == wrong.status_code == 401 and unknown.detail == wrong.detail,
      "%s / %s" % (unknown.detail, wrong.detail))

reset_login_state()
for i in range(11):
    attempt("nobody@example.com", "guess-%d" % i)
_, e = attempt("nobody@example.com", "guess")
_, e2 = attempt(AGENT["email"], "guess")
check("being rate-limited does not reveal whether an email exists",
      e.status_code == e2.status_code == 429 and e.detail == e2.detail)

# Different offices must not share a budget.
reset_login_state()
for i in range(12):
    attempt(AGENT["email"], "guess-%d" % i)
out, err = attempt(AGENT["email"], GOOD_PW, ip="198.51.100.200")
check("a second address is unaffected by the first one's failures", out is not None, repr(err))


# ----------------------------------------------------------------------------
print("\n[6] _rate_limited under concurrency and over time")
# ----------------------------------------------------------------------------
store = {}
NS["_rate_limit_last_sweep"].clear()
res = [_rate_limited(store, "k", limit=5, window_seconds=600) for _ in range(7)]
check("limit=N allows N then blocks", res == [False] * 5 + [True, True], str(res))

peek_store = {}
NS["_rate_limit_last_sweep"].clear()
for _ in range(4):
    _rate_limited(peek_store, "k", limit=5, window_seconds=600)
before = len(peek_store["k"])
peeked = _rate_limited(peek_store, "k", limit=5, window_seconds=600, record=False)
check("a peek does not consume a hit", len(peek_store["k"]) == before and peeked is False)
_rate_limited(peek_store, "k", limit=5, window_seconds=600)
check("a peek reports True exactly when the next record would exceed",
      _rate_limited(peek_store, "k", limit=5, window_seconds=600, record=False) is True)
check("peeking an unknown key does not create one",
      _rate_limited(peek_store, "never-seen", limit=5, record=False) is False
      and "never-seen" not in peek_store)

# Eviction, and the horizon floor that protects the 3600s callers.
store = {}
NS["_rate_limit_last_sweep"].clear()
_rate_limited(store, "live", limit=10)
stale = datetime.utcnow() - timedelta(seconds=7200)
for i in range(5000):
    store["dead-%d" % i] = [stale]
store["empty"] = []
NS["_rate_limit_last_sweep"][id(store)] = datetime.utcnow() - timedelta(seconds=1200)
_rate_limited(store, "live", limit=10)
check("dead keys are evicted so the store cannot grow forever",
      len(store) == 1 and "live" in store, "5002 keys -> %d" % len(store))

for i in range(100):
    store["dead-%d" % i] = [stale]
_rate_limited(store, "live", limit=10)
check("the sweep is not repeated inside its interval, so it stays cheap",
      len(store) == 101, "keys=%d" % len(store))

store = {}
NS["_rate_limit_last_sweep"].clear()
store["hourly-caller"] = [datetime.utcnow() - timedelta(seconds=1800)]
_rate_limited(store, "other", limit=10, window_seconds=600)
check("a 600s caller's sweep does not evict a 3600s caller's live hits",
      "hourly-caller" in store)

# The fold-in: concurrent access must neither raise nor lose hits.
store = {}
NS["_rate_limit_last_sweep"].clear()
stale = datetime.utcnow() - timedelta(seconds=7200)
for i in range(60000):
    store["dead-%d" % i] = [stale]
errors = []
barrier = threading.Barrier(8)


def hammer(n):
    try:
        barrier.wait()
        for i in range(400):
            _rate_limited(store, "shared-key", limit=10 ** 9, window_seconds=600)
            _rate_limited(store, "thread-%d-%d" % (n, i), limit=10 ** 9, window_seconds=600)
    except Exception as e:
        errors.append("%s: %s" % (type(e).__name__, e))


threads = [threading.Thread(target=hammer, args=(n,)) for n in range(8)]
NS["_rate_limit_last_sweep"][id(store)] = datetime.utcnow() - timedelta(seconds=1200)
for t in threads:
    t.start()
for t in threads:
    t.join()
check("sweeping while other threads insert does not raise",
      not errors, "; ".join(errors[:3]))
check("no hit is lost to a read-modify-write race",
      len(store.get("shared-key", [])) == 8 * 400,
      "counted %d of %d" % (len(store.get("shared-key", [])), 8 * 400))


# ----------------------------------------------------------------------------
print("\n[7] Register applies the same ceiling")
# ----------------------------------------------------------------------------
class RegisterReq:
    def __init__(self, password):
        self.email = "new@example.com"
        self.password = password
        self.name = "New Agent"
        self.agency = ""
        self.specialty = ""
        self.username = ""


reg_calls = []


def reg_db_execute(build, attempts=3, what="query", idempotent=True):
    reg_calls.append(what)
    return Result([])


NS["db_execute"] = reg_db_execute
try:
    NS["register"](RegisterReq("a" * 100))
    reg_err = None
except HTTPException as e:
    reg_err = e
except Exception as e:  # any other raise means the guard did not fire first
    reg_err = e
check("an over-long password at signup is a clean 400",
      isinstance(reg_err, HTTPException) and reg_err.status_code == 400, repr(reg_err))
check("it is rejected before any database work",
      not reg_calls, " -> ".join(reg_calls))


print("\n" + "=" * 70)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name in FAIL:
        print("  - " + name)
print("=" * 70)
sys.exit(1 if FAIL else 0)
