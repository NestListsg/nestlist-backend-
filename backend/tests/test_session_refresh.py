"""Executable cover on the sliding-session refresh added to the auth layer.

Like test_auth_guards.py, main.py cannot be imported here (supabase, anthropic,
fitz, bcrypt, pyjwt are absent locally), so the functions under test -- create_token
and get_current_agent -- are lifted out of the real source with ast and executed
against stubs. That means these tests run against the code that actually ships.

jwt is stubbed with a faithful-enough fake: encode serialises the payload (turning
a datetime `exp` into UTC epoch seconds, exactly as PyJWT does), decode reverses it
and raises ExpiredSignatureError when `exp` is in the past and InvalidTokenError on
garbage. Signature verification is not modelled -- that behaviour is PyJWT's and is
unchanged by this feature.

What is pinned here:
  [1] a fresh login token embeds the absolute-cap claim and a 30-day exp
  [2] a near-expiry valid token yields a fresh X-Refresh-Token; a far-from-expiry
      one does not; the refreshed token keeps the SAME absolute cap and never
      exceeds it
  [3] a token already past its absolute cap still authenticates but is NOT refreshed
  [4] an OLD token with no abs_exp claim still authenticates (grandfathered), and a
      near-expiry old token is refreshed with a reconstructed cap
  [5] an expired token still raises 401 "session expired"; garbage still raises 401
  [6] refresh is best-effort: it never sets a header on a response it did not touch
"""
import ast
import asyncio
import base64
import io
import json as _json
import logging
import os
import sys
import types
from datetime import datetime, timedelta

_HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(os.path.dirname(_HERE), "main.py")
SOURCE = io.open(MAIN, encoding="utf-8").read()
TREE = ast.parse(SOURCE)

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


_EPOCH_DT = datetime(1970, 1, 1)


def _epoch(dt):
    return int((dt - _EPOCH_DT).total_seconds())


# ----------------------------------------------------------------------------
# Stub jwt (absent locally). Models the two behaviours the code relies on:
# datetime `exp` -> UTC epoch seconds on encode, and ExpiredSignatureError on a
# past `exp` / InvalidTokenError on an undecodable token.
# ----------------------------------------------------------------------------
class ExpiredSignatureError(Exception):
    pass


class InvalidTokenError(Exception):
    pass


def _jwt_encode(payload, secret, algorithm="HS256"):
    p = dict(payload)
    exp = p.get("exp")
    if hasattr(exp, "utctimetuple"):          # a datetime, as PyJWT special-cases
        p["exp"] = _epoch(exp)
    return base64.urlsafe_b64encode(_json.dumps(p).encode()).decode()


def _jwt_decode(token, secret, algorithms=None):
    try:
        p = _json.loads(base64.urlsafe_b64decode(token.encode()).decode())
    except Exception:
        raise InvalidTokenError("could not decode token")
    exp = p.get("exp")
    if exp is not None and exp < _epoch(datetime.utcnow()):
        raise ExpiredSignatureError("signature has expired")
    return p


_jwt = types.ModuleType("jwt")
_jwt.encode = _jwt_encode
_jwt.decode = _jwt_decode
_jwt.ExpiredSignatureError = ExpiredSignatureError
_jwt.InvalidTokenError = InvalidTokenError
sys.modules["jwt"] = _jwt
import jwt  # noqa: E402


# ----------------------------------------------------------------------------
# Stubs the lifted functions close over
# ----------------------------------------------------------------------------
class HTTPException(Exception):
    def __init__(self, status_code, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class FakeResponse:
    """Stands in for the FastAPI-injected Response: just a headers dict."""
    def __init__(self):
        self.headers = {}


class FakeCreds:
    def __init__(self, token):
        self.credentials = token


class Result:
    def __init__(self, data):
        self.data = data


# controllable DB + alert stubs
_DB = {"result": Result([{"id": "agent-1", "email": "a@example.com"}]), "raise": False}
_ALERTS = []


async def _db_execute_async(build, what="query", attempts=3, idempotent=True):
    if _DB["raise"]:
        raise ConnectionError("pool dead")
    return _DB["result"]


async def _send_alert(key, message, cooldown_seconds=600):
    _ALERTS.append(key)


logger = logging.getLogger("test_session_refresh")
logger.addHandler(logging.NullHandler())
logger.propagate = False

NS = {
    "__builtins__": __builtins__,
    "jwt": jwt,
    "JWT_SECRET": "test-secret",
    "HTTPException": HTTPException,
    "Depends": lambda x=None: None,
    "security": None,
    "Response": FakeResponse,
    "Optional": None,
    "datetime": datetime,
    "timedelta": timedelta,
    "db_execute_async": _db_execute_async,
    "send_telegram_alert_throttled": _send_alert,
    "logger": logger,
}

_WANTED_FUNCS = ("_to_epoch_seconds", "create_token", "get_current_agent")
_WANTED_CONSTS = (
    "_EPOCH", "TOKEN_TTL_DAYS", "TOKEN_ABSOLUTE_CAP_DAYS",
    "TOKEN_REFRESH_WITHIN_DAYS", "REFRESH_TOKEN_HEADER",
)

_found = set()
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _WANTED_FUNCS:
        node.decorator_list = []
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

create_token = NS["create_token"]
get_current_agent = NS["get_current_agent"]
TTL = NS["TOKEN_TTL_DAYS"]
CAP = NS["TOKEN_ABSOLUTE_CAP_DAYS"]
WITHIN = NS["TOKEN_REFRESH_WITHIN_DAYS"]
HEADER = NS["REFRESH_TOKEN_HEADER"]


def run_auth(token):
    """Drive the real get_current_agent; return (agent_or_None, refresh_token_or_None, err)."""
    _DB["raise"] = False
    resp = FakeResponse()
    try:
        agent = asyncio.run(get_current_agent(resp, FakeCreds(token)))
    except HTTPException as e:
        return None, resp.headers.get(HEADER), e
    return agent, resp.headers.get(HEADER), None


def mint(exp_dt, abs_exp_dt="__fresh__"):
    """Hand-craft a token payload directly, to pin exact exp / abs_exp scenarios."""
    payload = {"agent_id": "agent-1", "exp": exp_dt}
    if abs_exp_dt != "__fresh__":
        if abs_exp_dt is not None:
            payload["abs_exp"] = _epoch(abs_exp_dt)
    return jwt.encode(payload, "test-secret", algorithm="HS256")


print("[0] All functions and constants were extracted from the shipped main.py")
check("found everything", _found == set(_WANTED_FUNCS) | set(_WANTED_CONSTS),
      "missing=%s" % sorted((set(_WANTED_FUNCS) | set(_WANTED_CONSTS)) - _found))
check("defaults are sane (within < ttl <= cap)", WITHIN < TTL <= CAP,
      "within=%d ttl=%d cap=%d" % (WITHIN, TTL, CAP))


# ----------------------------------------------------------------------------
print("\n[1] A fresh login token embeds the absolute cap and a TTL-day exp")
# ----------------------------------------------------------------------------
now = datetime.utcnow()
fresh = create_token("agent-1")
p = jwt.decode(fresh, "test-secret", algorithms=["HS256"])
check("fresh token carries abs_exp claim", "abs_exp" in p)
check("exp is ~TTL days out",
      abs(p["exp"] - _epoch(now + timedelta(days=TTL))) < 120,
      "exp=%d" % p["exp"])
check("abs_exp is ~CAP days out",
      abs(p["abs_exp"] - _epoch(now + timedelta(days=CAP))) < 120,
      "abs_exp=%d" % p["abs_exp"])


# ----------------------------------------------------------------------------
print("\n[2] Sliding refresh fires only near expiry, and never past the cap")
# ----------------------------------------------------------------------------
# Far from expiry: no refresh.
tok = mint(now + timedelta(days=TTL), abs_exp_dt=now + timedelta(days=CAP))
agent, refresh, err = run_auth(tok)
check("a token far from expiry authenticates", agent is not None and err is None)
check("...and is NOT refreshed", refresh is None)

# Near expiry, cap well ahead: refresh fires, keeps the same cap, honours it.
cap_dt = now + timedelta(days=CAP - 10)
tok = mint(now + timedelta(days=WITHIN - 1), abs_exp_dt=cap_dt)
agent, refresh, err = run_auth(tok)
check("a near-expiry token authenticates", agent is not None and err is None)
check("...and yields a fresh refresh token", refresh is not None)
if refresh:
    np = jwt.decode(refresh, "test-secret", algorithms=["HS256"])
    check("refreshed exp moved forward (toward now+TTL)",
          np["exp"] > _epoch(now + timedelta(days=TTL - 1)),
          "exp=%d" % np["exp"])
    check("refreshed token keeps the ORIGINAL absolute cap",
          abs(np["abs_exp"] - _epoch(cap_dt)) < 120,
          "abs_exp=%d want~%d" % (np["abs_exp"], _epoch(cap_dt)))
    check("refreshed exp never exceeds the absolute cap",
          np["exp"] <= np["abs_exp"])

# Near expiry but cap only 3 days away: refresh still fires, but exp is CLAMPED
# to the cap rather than jumping a full TTL past it.
cap_soon = now + timedelta(days=3)
tok = mint(now + timedelta(days=WITHIN - 1), abs_exp_dt=cap_soon)
agent, refresh, err = run_auth(tok)
check("near-expiry, cap-soon token still refreshes while under the cap", refresh is not None)
if refresh:
    np = jwt.decode(refresh, "test-secret", algorithms=["HS256"])
    check("refreshed exp is clamped at the cap, not now+TTL",
          abs(np["exp"] - _epoch(cap_soon)) < 120,
          "exp=%d cap=%d" % (np["exp"], _epoch(cap_soon)))


# ----------------------------------------------------------------------------
print("\n[3] A token past the absolute cap authenticates but is NOT refreshed")
# ----------------------------------------------------------------------------
# exp still in the future (so the token is valid), but abs_exp already in the past.
tok = mint(now + timedelta(days=WITHIN - 1), abs_exp_dt=now - timedelta(days=1))
agent, refresh, err = run_auth(tok)
check("a past-cap but unexpired token still authenticates", agent is not None and err is None)
check("...and is NOT refreshed (session may not be eternal)", refresh is None)


# ----------------------------------------------------------------------------
print("\n[4] Old tokens with no abs_exp are grandfathered")
# ----------------------------------------------------------------------------
# Old token, far from expiry, no abs_exp claim at all.
old_far = mint(now + timedelta(days=20), abs_exp_dt="__fresh__")  # fresh=>no abs_exp injected
# ensure it truly has no abs_exp
check("old token genuinely lacks abs_exp",
      "abs_exp" not in jwt.decode(old_far, "test-secret", algorithms=["HS256"]))
agent, refresh, err = run_auth(old_far)
check("an old token still authenticates", agent is not None and err is None)
check("...and far from expiry is not refreshed", refresh is None)

# Old token NEAR expiry: refreshed, with a reconstructed cap (original login+CAP),
# which is in the future, so refresh is allowed -- carrying the agent past the old
# hard 30-day cliff instead of logging them out.
old_near = mint(now + timedelta(days=WITHIN - 1), abs_exp_dt="__fresh__")
agent, refresh, err = run_auth(old_near)
check("a near-expiry OLD token authenticates", agent is not None and err is None)
check("...and IS refreshed (grandfathered cap reconstructed)", refresh is not None)
if refresh:
    np = jwt.decode(refresh, "test-secret", algorithms=["HS256"])
    # reconstructed cap = old exp - TTL + CAP; old exp ~ now + (WITHIN-1)
    want_cap = _epoch((now + timedelta(days=WITHIN - 1)) - timedelta(days=TTL) + timedelta(days=CAP))
    check("reconstructed cap matches original-login + CAP",
          abs(np["abs_exp"] - want_cap) < 300,
          "abs_exp=%d want~%d" % (np["abs_exp"], want_cap))


# ----------------------------------------------------------------------------
print("\n[5] Expired / invalid tokens still 401 -- unchanged")
# ----------------------------------------------------------------------------
expired = mint(now - timedelta(days=1), abs_exp_dt=now + timedelta(days=CAP))
agent, refresh, err = run_auth(expired)
check("an expired token is 401", err is not None and err.status_code == 401, repr(err))
check("...with the 'session has expired' message",
      err is not None and "expired" in err.detail.lower(), err.detail if err else "")
check("...and emits no refresh header", refresh is None)

_ALERTS.clear()
agent, refresh, err = run_auth("this-is-not-a-valid-token")
check("a garbage token is 401", err is not None and err.status_code == 401, repr(err))
check("...and fires the JWT-secret-rotation alert (hardening intact)",
      "jwt_invalid" in _ALERTS, str(_ALERTS))


# ----------------------------------------------------------------------------
print("\n[6] The DB-unreachable hardening path is untouched")
# ----------------------------------------------------------------------------
_ALERTS.clear()
_DB["raise"] = True
resp = FakeResponse()
_err = None
try:
    asyncio.run(get_current_agent(resp, FakeCreds(mint(now + timedelta(days=10),
                                                       abs_exp_dt=now + timedelta(days=CAP)))))
except HTTPException as e:
    _err = e
check("a DB outage is a 503, not an auth failure", _err is not None and _err.status_code == 503, repr(_err))
check("...and fires the db_unreachable alert", "db_unreachable" in _ALERTS, str(_ALERTS))
check("...and sets no refresh header", resp.headers.get(HEADER) is None)
_DB["raise"] = False


# ----------------------------------------------------------------------------
print("\n[7] Integration: real FastAPI merges the header onto JSON, not onto a "
      "directly-returned FileResponse, and nothing on an anonymous request")
# ----------------------------------------------------------------------------
# This is the part that pins the Sev3 fix: the refresh rides on the injected
# Response (merged onto serialised JSON) and the download byte path is NOT wrapped
# by BaseHTTPMiddleware. We mount the REAL lifted get_current_agent as a dependency
# in a real FastAPI app and drive it with TestClient. Skipped gracefully if fastapi
# is absent (e.g. a minimal CI image); the unit sections above still cover the logic.
try:
    import tempfile
    from fastapi import FastAPI, Depends as RealDepends
    from fastapi.responses import FileResponse, StreamingResponse
    from fastapi.security import HTTPBearer
    from fastapi.testclient import TestClient
    _HAVE_FASTAPI = True
except Exception as _e:  # pragma: no cover
    _HAVE_FASTAPI = False
    print("  SKIP  fastapi/TestClient not importable here (%s) -- unit sections cover the logic" % _e)

if _HAVE_FASTAPI:
    # Re-lift create_token + get_current_agent WITH annotations intact, into a
    # namespace wired to the REAL fastapi types, so FastAPI's own dependency
    # injection and header-merge behaviour is what gets exercised.
    import fastapi as _fastapi
    TREE_INT = ast.parse(SOURCE)
    NS_INT = {
        "__builtins__": __builtins__,
        "jwt": jwt,
        "JWT_SECRET": "test-secret",
        "HTTPException": _fastapi.HTTPException,
        "Depends": _fastapi.Depends,
        "security": HTTPBearer(auto_error=True),
        "Response": _fastapi.Response,
        "Request": _fastapi.Request,
        "HTTPAuthorizationCredentials": __import__("fastapi.security", fromlist=["HTTPAuthorizationCredentials"]).HTTPAuthorizationCredentials,
        "Optional": None,
        "datetime": datetime,
        "timedelta": timedelta,
        "db_execute_async": _db_execute_async,
        "send_telegram_alert_throttled": _send_alert,
        "logger": logger,
    }
    _int_wanted = {"_to_epoch_seconds", "create_token", "get_current_agent"}
    _int_consts = {"_EPOCH", "TOKEN_TTL_DAYS", "TOKEN_ABSOLUTE_CAP_DAYS",
                   "TOKEN_REFRESH_WITHIN_DAYS", "REFRESH_TOKEN_HEADER"}
    for node in TREE_INT.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _int_wanted:
            node.decorator_list = []
            exec(compile(ast.Module([node], []), MAIN, "exec"), NS_INT)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in _int_consts:
                    exec(compile(ast.Module([node], []), MAIN, "exec"), NS_INT)
    real_get_current_agent = NS_INT["get_current_agent"]

    app = FastAPI()

    @app.get("/api/thing")
    async def _thing(agent=RealDepends(real_get_current_agent)):
        return {"ok": True, "agent": agent["id"]}

    _tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
    _tmp.write(b"poster-bytes")
    _tmp.close()

    @app.get("/api/download")
    async def _download(agent=RealDepends(real_get_current_agent)):
        # Directly-returned FileResponse -- the poster/video download byte path.
        return FileResponse(_tmp.name, media_type="application/octet-stream")

    @app.get("/api/stream")
    async def _stream(agent=RealDepends(real_get_current_agent)):
        def _gen():
            yield b"chunk"
        return StreamingResponse(_gen(), media_type="application/octet-stream")

    client = TestClient(app)
    near = mint(now + timedelta(days=WITHIN - 1), abs_exp_dt=now + timedelta(days=CAP - 5))
    far = mint(now + timedelta(days=TTL), abs_exp_dt=now + timedelta(days=CAP))

    r = client.get("/api/thing", headers={"Authorization": "Bearer " + near})
    check("authed JSON response carries X-Refresh-Token", r.status_code == 200 and HEADER in r.headers,
          "status=%d headers=%s" % (r.status_code, dict(r.headers)))
    if HEADER in r.headers:
        np = jwt.decode(r.headers[HEADER], "test-secret", algorithms=["HS256"])
        check("the emitted header is a usable fresh token with the same cap",
              np["agent_id"] == "agent-1" and abs(np["abs_exp"] - _epoch(now + timedelta(days=CAP - 5))) < 300)

    r = client.get("/api/thing", headers={"Authorization": "Bearer " + far})
    check("authed JSON far from expiry carries NO header", r.status_code == 200 and HEADER not in r.headers)

    r = client.get("/api/thing")  # anonymous -- no Authorization
    check("anonymous request is rejected and carries NO refresh header",
          r.status_code in (401, 403) and HEADER not in r.headers,
          "status=%d" % r.status_code)

    # The Sev3 point: download byte path returns a Response directly. FastAPI does
    # not merge the dependency's injected-Response headers onto it -- so no header
    # here -- and crucially it is NOT routed through BaseHTTPMiddleware (there is
    # none). The refresh simply lands on the next JSON call. Accepted behaviour.
    r = client.get("/api/download", headers={"Authorization": "Bearer " + near})
    check("FileResponse download still succeeds (byte path intact)",
          r.status_code == 200 and r.content == b"poster-bytes")
    check("FileResponse download does NOT carry the header (accepted)", HEADER not in r.headers)

    r = client.get("/api/stream", headers={"Authorization": "Bearer " + near})
    check("StreamingResponse download still succeeds", r.status_code == 200 and r.content == b"chunk")
    check("StreamingResponse does NOT carry the header (accepted)", HEADER not in r.headers)

    try:
        os.unlink(_tmp.name)
    except OSError:
        pass


# ----------------------------------------------------------------------------
print("\n" + "=" * 70)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
print("=" * 70)
sys.exit(1 if FAIL else 0)
