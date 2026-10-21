"""Making a deadline that actually ends things, rather than one that is merely checked.

WHY THIS MODULE EXISTS

A read timeout in `requests` and in `httpx` is PER READ, not total. A server that
dribbles one byte just inside the timeout resets the clock forever, so a download or an
API call can run without limit while every individual read looks healthy. A deadline
checked between chunks cannot help: the check does not run while a read is blocked on
the chunk that is filling too slowly.

Worse, the obvious ways to interrupt it do not work, and not merely by being slower.
Measured against a deliberately dribbling server:

  * response.close() hands the connection back tidily, which means waiting out the body
    that is still arriving in a trickle -- 80 seconds past the deadline.
  * closing the buffered reader underneath it BLOCKS, because the stalled read holds
    that object's own lock. The watchdog thread ends up waiting for exactly the read it
    was supposed to interrupt (measured: 81.3 seconds inside close()).

Shutting the SOCKET down is a syscall on the file descriptor. It takes no Python lock,
returned in 0.000s, and the blocked read came back in the same instant.

main.py worked this out first, for a video download, and carried its own copy as
`_abandon_response` for a while. This module is that lesson extracted so the video
renderer's photo fetches get it too, and since 2026-10-21 (when the download main.py
had it for was removed with the Signature tier) it is the ONLY copy. Anything in this
codebase that streams a response on a deadline should come through here rather than
grow a second one.
"""
import logging
import socket
import threading
import time

logger = logging.getLogger(__name__)


def abandon_response(response, warn=False):
    """Make a read that is blocked on this response return, now.

    Reaches into urllib3's internals, so every step is best-effort and the CALLER'S
    DEADLINE STANDS BEHIND IT. An interrupted read may look like a clean end of body
    rather than an error, which is why callers must check the deadline AFTER the read
    loop as well -- a cut-short download must never be mistaken for a complete one.
    """
    sock = None
    for path in (lambda: response.raw._fp.fp.raw._sock,
                 lambda: response.raw._original_response.fp.raw._sock,
                 lambda: response.raw._connection.sock):
        try:
            found = path()
        except Exception:
            continue
        if isinstance(found, socket.socket):
            sock = found
            break
    if sock is None:
        # A shape of urllib3 we do not know, or -- the common case -- a finished
        # download whose connection has already been released. Only the watchdog cares,
        # and only then is it worth a line in the log. Do NOT fall back to
        # response.close() here: from the watchdog thread that is the call that blocks.
        if warn:
            logger.warning("could not reach the socket to abandon a slow download")
        return
    for action in (lambda: sock.shutdown(socket.SHUT_RDWR), sock.close):
        try:
            action()
        except Exception:
            pass


class deadline_watchdog:
    """Abandons `response` if the body is still arriving at `deadline`.

    A healthy download cancels the timer on the way out and never notices it.
    """

    def __init__(self, response, deadline, warn=True):
        self._timer = threading.Timer(
            max(0.1, deadline - time.monotonic()), abandon_response, args=(response, warn))
        self._timer.daemon = True

    def __enter__(self):
        self._timer.start()
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        return False


def call_with_deadline(fn, deadline, name="call"):
    """fn()'s result, or TimeoutError at `deadline` whatever fn is still doing.

    For work that has NO interruptible file descriptor we can reach -- an SDK that owns
    its own httpx client, for instance. The worker thread is a daemon and is simply
    abandoned: we stop waiting, the caller gets its deadline honoured, and the thread
    dies whenever the underlying library finally gives up.

    THE HONEST LIMITATION: this bounds how long the CALLER waits, not how long the work
    runs. It is the right trade here because the thing being protected is a render
    making forward progress, and an abandoned thread holds no lock the render needs. It
    is NOT a substitute for the library having its own total timeout, and it would be
    the wrong tool if the abandoned work held a scarce resource or wrote anything.
    """
    box = {}
    done = threading.Event()

    def run():
        try:
            box["value"] = fn()
        except BaseException as e:          # noqa: BLE001 -- re-raised in the caller
            box["error"] = e
        finally:
            done.set()

    worker = threading.Thread(target=run, daemon=True, name=name)
    worker.start()
    if not done.wait(max(0.0, deadline - time.monotonic())):
        raise TimeoutError(f"{name} did not finish before its deadline")
    if "error" in box:
        raise box["error"]
    return box.get("value")
