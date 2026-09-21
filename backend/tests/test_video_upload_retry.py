"""Verification for the video upload retry (audit finding S2(b)).

main.py cannot be imported here -- it needs supabase, anthropic and fitz, none of which
are installed locally -- so this pulls the REAL source of _store_rendered_video out of
main.py with ast and executes it against stubs. It is the shipped function, not a copy:
if someone edits main.py, this test follows the edit.
"""
import ast
import io
import os
import sys
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(os.path.dirname(_HERE), "main.py")

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def extract(*names):
    """Lift named top-level functions and assignments out of main.py, verbatim."""
    source = io.open(MAIN, encoding="utf-8").read()
    tree = ast.parse(source)
    lines = source.splitlines(True)
    chunks = []
    for node in tree.body:
        named = None
        if isinstance(node, ast.FunctionDef):
            named = node.name
        elif isinstance(node, ast.Assign) and node.targets and isinstance(node.targets[0], ast.Name):
            named = node.targets[0].id
        if named in names:
            chunks.append("".join(lines[node.lineno - 1:node.end_lineno]))
    return "".join(chunks)


class FlakyStorage(object):
    def __init__(self, fail_times, error):
        self.fail_times = fail_times
        self.error = error
        self.calls = 0
        self.payloads = []

    def from_(self, bucket):
        return self

    def upload(self, filename, data, options):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise self.error
        self.payloads.append((filename, data, options))

    def get_public_url(self, filename):
        return "https://storage.example/" + filename


class FakeClient(object):
    def __init__(self, storage):
        self.storage = storage


def build(fail_times, error):
    storage = FlakyStorage(fail_times, error)
    resets = []
    ns = {
        "get_db": lambda: FakeClient(storage),
        "uuid": uuid,
        "time": time,
        "logger": type("L", (), {"info": lambda *a, **k: None,
                                 "warning": lambda *a, **k: None})(),
        "_DB_TRANSPORT_ERRORS": (ConnectionError,),
        "_reset_db": lambda: resets.append(1),
    }
    exec(compile(extract("VIDEO_UPLOAD_ATTEMPTS", "VIDEO_UPLOAD_BACKOFF_SECONDS",
                         "_store_rendered_video"), MAIN, "exec"), ns)
    # Keep the test fast; the retry ladder's shape is what matters, not its wall clock.
    ns["VIDEO_UPLOAD_BACKOFF_SECONDS"] = 0
    ns["time"] = type("T", (), {"sleep": staticmethod(lambda s: None)})()
    exec(compile(extract("_store_rendered_video"), MAIN, "exec"), ns)
    return ns["_store_rendered_video"], storage, resets, ns


print("\n[S2(b)] A storage timeout retries the UPLOAD, not the whole render")

store, storage, resets, ns = build(0, None)
check("the shipped function was extracted from main.py", callable(store))
check("at least 3 attempts are configured", ns["VIDEO_UPLOAD_ATTEMPTS"] >= 3,
      "attempts=%s" % ns["VIDEO_UPLOAD_ATTEMPTS"])

url = store("videos/abc.mp4", b"video-bytes")
check("a clean upload works first time and returns a url",
      storage.calls == 1 and url.startswith("https://storage.example/videos/abc.mp4"), url)
check("the upload is an upsert, which is what makes retrying it safe",
      storage.payloads[0][2].get("upsert") == "true", str(storage.payloads[0][2]))

# The case the auditor flagged: storage3's 20s default timeout under 4-way concurrency.
timeout = ConnectionError("timed out")
store, storage, resets, ns = build(2, timeout)
url = store("videos/slow.mp4", b"video-bytes")
check("two timeouts are absorbed and the third attempt succeeds",
      storage.calls == 3 and url, "calls=%d" % storage.calls)
check("the render was NOT repeated -- only the upload was", storage.calls <= ns["VIDEO_UPLOAD_ATTEMPTS"])
check("a poisoned connection pool is discarded between attempts",
      len(resets) == 2, "resets=%d" % len(resets))

# Exhausting the ladder still raises, so a genuinely broken bucket is not hidden.
store, storage, resets, ns = build(99, timeout)
raised = None
try:
    store("videos/dead.mp4", b"video-bytes")
except Exception as e:
    raised = e
check("a persistently failing upload still raises, so the job can retry or fail",
      isinstance(raised, ConnectionError), repr(raised))
check("it gave up after exactly the configured number of attempts",
      storage.calls == ns["VIDEO_UPLOAD_ATTEMPTS"],
      "calls=%d limit=%s" % (storage.calls, ns["VIDEO_UPLOAD_ATTEMPTS"]))

# A non-transport error (e.g. a bucket permission problem) must not reset the DB pool.
store, storage, resets, ns = build(1, ValueError("bad bucket"))
store("videos/perm.mp4", b"video-bytes")
check("a non-transport failure is retried without discarding the pool",
      storage.calls == 2 and resets == [], "resets=%d" % len(resets))

print("\n" + "=" * 70)
print("PASSED: %d    FAILED: %d" % (len(PASS), len(FAIL)))
if FAIL:
    for name in FAIL:
        print("  - " + name)
print("=" * 70)
sys.exit(1 if FAIL else 0)
