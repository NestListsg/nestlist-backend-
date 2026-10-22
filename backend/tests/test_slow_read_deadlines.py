"""Deadlines that actually end things, proved against a server that really dribbles.

THE BUG THIS GUARDS. A read timeout in requests and httpx is PER READ. A server that
sends one byte just inside the timeout resets the clock forever, so a download runs
without limit while every individual read looks healthy. A deadline checked between
chunks is no help at all: the check cannot run while a read is blocked on the chunk
that is filling too slowly.

That matters here more than it looks. The photo survey runs with the job CLAIMED, and
video_jobs admits one Classic per worker, so a stuck fetch does not slow a worker down,
it stops it -- while the heartbeat thread keeps reporting healthy, so sweep_stale never
reclaims the job. An unbounded read is therefore a worker lost until the process is
restarted.

Everything below uses a real socket and real wall-clock time, because the failure being
guarded against is specifically that the obvious implementations LOOK right and block
anyway. A mock would prove nothing.
"""
import os, socket, sys, threading, time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
import logging  # noqa: E402
logging.disable(logging.CRITICAL)
import slow_read  # noqa: E402
import video_renderer as vr  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


class DribblingServer:
    """Sends headers, then one byte every `gap` seconds, forever.

    Never finishes, never errors, and every individual read completes well inside any
    sane read timeout. This is the shape that defeats timeout=(5, 25).
    """

    def __init__(self, gap=0.2, declared=None):
        self.gap = gap
        self.declared = declared
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/slow.jpg" % self.port

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.recv(65536)
            length = ("Content-Length: %d\r\n" % self.declared) if self.declared else \
                     "Transfer-Encoding: chunked\r\n"
            conn.sendall(("HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\n"
                          + length + "\r\n").encode())
            while not self.stop.is_set():
                if self.declared:
                    conn.sendall(b"\xff")
                else:
                    conn.sendall(b"1\r\n\xff\r\n")
                time.sleep(self.gap)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


print("\n[the premise] the dribbling server really does defeat a read timeout")
server = DribblingServer(gap=0.2)
try:
    import requests
    started = time.monotonic()
    blocked = {}

    def naive():
        try:
            with requests.get(server.url, timeout=(5, 25), stream=True) as r:
                for _chunk in r.iter_content(chunk_size=64 * 1024):
                    if time.monotonic() - started > 25:
                        blocked["ran_past"] = time.monotonic() - started
                        return
        except Exception as e:
            blocked["error"] = repr(e)

    t = threading.Thread(target=naive, daemon=True)
    t.start()
    t.join(12)
    check("a plain read-timeout download is STILL RUNNING after 12s, which is the "
          "bug the deadlines below exist to fix",
          t.is_alive() or "ran_past" in blocked,
          "it ended on its own, so this server is not dribbling: %s" % blocked)

    print("\n[_fetch_image] an enforced deadline ends a dribbling download")
    started = time.monotonic()
    try:
        vr._fetch_image(server.url, attempts=1,
                        deadline=time.monotonic() + 3)
        took = time.monotonic() - started
        check("raises rather than returning a half-downloaded photo", False,
              "returned after %.1fs" % took)
    except Exception as e:
        took = time.monotonic() - started
        check("gives up within a second or so of its deadline (took %.1fs)" % took,
              2.5 <= took <= 8.0, "%.1fs, %s" % (took, e))
        check("...and says it was the deadline, not a mystery transport error",
              "deadline" in str(e).lower() or "timeout" in str(e).lower(), str(e))

    print("\n[_fetch_image] the deadline covers BOTH attempts, not each one")
    started = time.monotonic()
    try:
        vr._fetch_image(server.url, attempts=2, deadline=time.monotonic() + 4)
    except Exception:
        pass
    took = time.monotonic() - started
    check("two attempts still finish inside the shared deadline (took %.1fs)" % took,
          took <= 9.0, "%.1fs" % took)

    print("\n[survey] fifteen dribbling candidates cannot outlast the fetch budget")
    saved_budget = vr.SURVEY_FETCH_BUDGET_SECONDS
    saved_min = vr.MIN_VIDEO_PHOTOS
    vr.SURVEY_FETCH_BUDGET_SECONDS = 4
    try:
        started = time.monotonic()
        out = vr._smart_auto_selection([server.url + "?i=%d" % i for i in range(15)], 0)
        took = time.monotonic() - started
        check("falls back to upload order rather than hanging (took %.1fs)" % took,
              out == (None, None, None), str(out))
        check("...inside the fetch budget, not fifteen times it",
              took <= 12.0, "%.1fs for 15 candidates" % took)
    finally:
        vr.SURVEY_FETCH_BUDGET_SECONDS = saved_budget
        vr.MIN_VIDEO_PHOTOS = saved_min
finally:
    server.close()

print("\n[declared length] a cut-short download is never mistaken for a complete one")
server2 = DribblingServer(gap=0.2, declared=5_000_000)
try:
    started = time.monotonic()
    try:
        vr._fetch_image(server2.url, attempts=1, deadline=time.monotonic() + 3)
        check("a truncated body is rejected", False, "it returned an image")
    except Exception as e:
        check("a truncated body is rejected rather than decoded", True, type(e).__name__)
        check("...and it took about the deadline, not the full body",
              time.monotonic() - started <= 8.0)
finally:
    server2.close()

print("\n[call_with_deadline] work with no reachable socket is abandoned on time")
started = time.monotonic()
try:
    slow_read.call_with_deadline(lambda: time.sleep(30), time.monotonic() + 1.5,
                                 name="stuck call")
    check("raises TimeoutError", False, "it returned")
except TimeoutError:
    took = time.monotonic() - started
    check("the CALLER is released on time (took %.1fs)" % took, took <= 3.0, "%.1fs" % took)
check("a healthy call still returns its value",
      slow_read.call_with_deadline(lambda: 42, time.monotonic() + 5) == 42)
try:
    slow_read.call_with_deadline(lambda: (_ for _ in ()).throw(ValueError("inner")),
                                 time.monotonic() + 5)
    check("an exception inside propagates", False, "it did not raise")
except ValueError as e:
    check("an exception inside propagates to the caller", str(e) == "inner")

print("\n[_vision_call] a hung SDK call cannot outlast the budget")
saved = vr._anthropic_keys
vr._anthropic_keys = lambda: ["key-one-placeholder", "key-two-placeholder"]
try:
    import slow_read as _sr
    real = _sr.call_with_deadline
    def stuck(fn, deadline, name="call"):
        return real(lambda: time.sleep(60), deadline, name)
    _sr.call_with_deadline = stuck
    try:
        started = time.monotonic()
        raw, err = vr._vision_call([{"type": "text", "text": "x"}], timeout=75, budget=3)
        took = time.monotonic() - started
        check("returns (None, error) instead of hanging", raw is None and err is not None,
              "%r %r" % (raw, err))
        check("...inside the budget even with two keys to try (took %.1fs)" % took,
              took <= 7.0, "%.1fs" % took)
    finally:
        _sr.call_with_deadline = real
finally:
    vr._anthropic_keys = saved

print("\n[fal client] a dribbling fal leaves no thread and no socket behind")
# THE MEASUREMENT THIS GUARDS, taken by an audit on 2026-10-22 against the shipped code:
# five calls to a one-byte-every-three-seconds endpoint returned on time and left FIVE
# 'fal request' threads -- and their sockets -- alive 105 seconds later, for good. The
# caller's deadline was honoured and nothing else was. A cold Dramatic render makes tens
# of these calls, so a degraded fal could fill a Railway worker with stuck threads.
#
# The fix is the one slow_read's own docstring now insists on: stream the reply and read
# the body inside deadline_watchdog, so the deadline shuts the socket down and the
# blocked read raises. This asserts the THREADS GO, not merely that the caller returns.
os.environ["FAL_KEY"] = "test-only-placeholder-not-a-key"
import fal_images  # noqa: E402

def _fal_threads():
    return [t for t in threading.enumerate() if t.name == "fal request" and t.is_alive()]

server3 = DribblingServer(gap=0.2)
try:
    before = len(_fal_threads())
    took = []
    for _ in range(5):
        started = time.monotonic()
        try:
            fal_images._request("GET", server3.url, time.monotonic() + 1.2)
            check("a dribbling endpoint does not return a result", False)
        except fal_images.FalError:
            pass
        took.append(time.monotonic() - started)
    check("every call returned at its own deadline (worst %.1fs)" % max(took),
          max(took) <= 4.0, "%.1fs" % max(took))
    # The socket shutdown and the thread's exit are not instantaneous; a short grace
    # here is about scheduling, not about waiting for a timeout to expire. The leak
    # being guarded against survived 105 seconds, so a second is a fair test.
    deadline = time.monotonic() + 3.0
    while len(_fal_threads()) > before and time.monotonic() < deadline:
        time.sleep(0.1)
    leaked = len(_fal_threads()) - before
    check("and NO thread was left blocked on the dribble (leaked %d of 5)" % leaked,
          leaked == 0, str([t.name for t in _fal_threads()]))
finally:
    server3.close()
    os.environ.pop("FAL_KEY", None)


print("\n[fal client, header phase] what REALLY bounds a server that never finishes "
      "its headers")
# THE RESIDUAL, PINNED WITH A NUMBER BECAUSE A NOTE ABOUT IT HAS NOW BEEN WRONG TWICE.
#
# The section above fixed the BODY phase: the reply is streamed, the body is read under
# the watchdog, the socket is shut down at the deadline. The HEADER phase cannot have that
# -- there is no response object yet, so there is no socket to reach -- and _request's
# docstring used to claim "requests' own connect and read timeouts do bound that case".
# They do not bound it in SECONDS: header lines keep arriving, so every individual read is
# healthy and the read timeout never fires. It is the per-read trap this whole file exists
# to encode, moved one phase earlier.
#
# What bounds it is http.client._MAXHEADERS: 100 header lines, after which the thread ends
# however long they took. These two cases are the measurement the corrected docstring
# quotes, so the claim and the test cannot drift apart.
import http.client  # noqa: E402

os.environ["FAL_KEY"] = "test-only-placeholder-not-a-key"


class HeaderTrickler:
    """Answers with a status line, then one header line every `gap` seconds, for ever.

    Never sends the blank line that ends the headers, so requests never returns a
    response object at all.
    """

    def __init__(self, gap):
        self.gap = gap
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = threading.Event()
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def url(self):
        return "http://127.0.0.1:%d/status" % self.port

    def _serve(self):
        while not self.stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn):
        try:
            conn.recv(65536)
            conn.sendall(b"HTTP/1.1 200 OK\r\n")
            while not self.stop.is_set():
                conn.sendall(b"X-Pad: y\r\n")
                time.sleep(self.gap)
        except Exception:
            pass
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def close(self):
        self.stop.set()
        try:
            self.sock.close()
        except Exception:
            pass


def _header_release(gap, read_timeout, give_up=30.0):
    """(seconds the caller waited, seconds until the worker thread exited)."""
    saved = fal_images.HTTP_READ_SECONDS
    fal_images.HTTP_READ_SECONDS = read_timeout
    server = HeaderTrickler(gap)
    try:
        before = len(_fal_threads())
        started = time.monotonic()
        try:
            fal_images._request("GET", server.url, time.monotonic() + 0.5)
        except Exception:
            pass
        returned = time.monotonic() - started
        released = None
        while time.monotonic() - started < give_up:
            if len(_fal_threads()) <= before:
                released = time.monotonic() - started
                break
            time.sleep(0.05)
        return returned, released
    finally:
        server.close()
        fal_images.HTTP_READ_SECONDS = saved


check("http.client._MAXHEADERS is the constant the bound is made of",
      http.client._MAXHEADERS == 100, str(http.client._MAXHEADERS))

# Case 1: a gap BELOW the read timeout. Every read is healthy, so the timeout never
# fires and the thread lives for 100 header lines -- here 100 x 0.05s = 5s, NOT the 1s
# read timeout the old docstring credited.
returned, released = _header_release(gap=0.05, read_timeout=1.0)
check("the CALLER is still released at its own deadline (%.1fs)" % returned,
      returned < 2.0, "%.1fs" % returned)
check("but the thread is NOT bounded by the read timeout: it lived %s against a 1.0s "
      "read timeout" % (("%.1fs" % released) if released else ">30s"),
      released is not None and released > 2.0,
      "released=%r" % released)
check("it is bounded by 100 header lines instead (~%.1fs at a 0.05s gap)"
      % (http.client._MAXHEADERS * 0.05),
      released is not None
      and abs(released - http.client._MAXHEADERS * 0.05) < 3.0,
      "released=%r expected=%.1f" % (released, http.client._MAXHEADERS * 0.05))

# Case 2: a gap ABOVE the read timeout. NOW the timeout does fire, which is what makes
# the worst case 100 x HTTP_READ_SECONDS rather than open-ended -- and is why the audit's
# "a header every 30 seconds holds a thread for 50 minutes" overstates it: a 30s gap
# against a 20s read timeout is answered in 20s.
returned, released = _header_release(gap=2.0, read_timeout=0.6)
check("a gap ABOVE the read timeout DOES fire it, so the bound is 100 x "
      "HTTP_READ_SECONDS and not endless",
      released is not None and released < 2.0,
      "released=%r" % released)

# And the docstring has to say so, in the terms that were measured. This is the part the
# audit actually asked for: the written claim was the risk.
_fal_src = open(os.path.join(BACKEND, "fal_images.py"), encoding="utf-8").read()
_request_doc = fal_images._request.__doc__ or ""
check("_request's docstring names _MAXHEADERS as the real bound",
      "_MAXHEADERS" in _request_doc, _request_doc[-200:])
check("and no longer claims the connect and read timeouts bound the header phase",
      "connect and read timeouts do bound that case" not in _fal_src)
os.environ.pop("FAL_KEY", None)

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
