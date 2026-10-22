"""fal.ai image generation -- one bounded, interruptible call that always degrades.

WHAT THIS IS FOR
The "From Old to New" video needs a second photograph of each room: the room redesigned,
from the SAME camera position. We generate it from the agent's own photo rather than
asking them to upload one (see docs/old-to-new-video-spec.md, "Why the angle match
matters") -- generating the after FROM the before is what makes the two frames share a
camera position by construction. `fal-ai/nano-banana/edit` does that for about four
cents an image.

THE RELIABILITY MODEL, which is the part not to lose
This runs INSIDE an agent's render. A refused or slow generation must cost that room its
transformation, never the agent's video and never the worker. So:

  * ONE ABSOLUTE DEADLINE for the whole call, queue time included, and it actually
    interrupts -- AND IT FREES THE SOCKET AND THE THREAD, not only the caller. requests'
    timeout=(connect, read) is PER READ: an endpoint dribbling a byte just inside it
    resets the clock forever, and a deadline checked between polls does not run while a
    read is blocked. slow_read has the measurements. Every HTTP call here therefore
    streams its reply and reads it under slow_read's watchdog, which shuts the socket
    down at the deadline. See _request for what the weaker version of this cost.
  * BOUNDED ATTEMPTS, SPLIT BY WHAT THEY COST. At most two SUBMITS -- the only billable
    call -- and only when we can tell that the first one never reached fal. Everything
    after an accepted submit is collection, is free, and gets several more tries,
    because throwing away an image fal has already charged us for is the expensive
    outcome, not the slow one.
  * A PERMANENT / RETRYABLE SPLIT, because the two must not be treated alike. A content
    refusal or a malformed prompt will be refused identically every time; retrying it
    spends the agent's wait and our money to arrive at the same answer.
  * A RATE LIMIT IS NEITHER. fal starts a new account at two concurrent requests, so a
    429 is an expected outcome, not a rarity. Retrying it inside this call spends the
    agent's wait on a queue that is full; FalRateLimited exists so the caller can put the
    JOB back instead and let it run when there is room.
  * NOTHING HERE RAISES INTO A RENDER. Callers catch FalError and fall back; see
    old_to_new.py, where a refusal becomes a plain dissolve.

THE KEY
FAL_KEY comes from the process environment and from nowhere else. It is never read from
a file, never written to one, never logged, and never put in an error message -- errors
from this module are shown to agents and copied into Railway's logs. _auth_headers()
builds the header fresh per call for exactly that reason.

AND WE DO NOT QUOTE FAL'S BODIES AT ALL. The first version of this module quoted 4xx
bodies so a refusal was diagnosable, and relied on a redactor that replaced the key
where it appeared BYTE FOR BYTE. An audit on 2026-10-22 walked straight past it with
`Key%20<id>%3A<secret>`, with either half of the key on its own, and with the case
changed -- every one of which would have reached Railway's logs. A body we do not quote
cannot leak anything, so _classify now reports a status and a category from OUR OWN
vocabulary and nothing fal wrote. _redact survives, now matching by PATTERN rather than
by equality, for the one place prose from fal is still worth having (the
completed-without-an-image explanation) and for anything that is quoted in future.

COST
EVERY ACCEPTED SUBMIT IS BILLED, not every picture we receive. That distinction is the
whole of the second audit's most expensive finding: five measured shapes (every collect
attempt 502s, a non-JSON 200, a 200 shape we do not understand, COMPLETED with no
picture, 429 throughout collection) all leave fal paid and us empty-handed. So every
error this module raises carries `images_billed` -- how many pictures fal may already
have charged for -- and billed_images() reads it. old_to_new's tally adds that number,
not the number of files it got.

generate() still does NOT decide whether we can afford to generate -- that belongs to the
caller, up front, before the first call (old_to_new.reserve_budget). This module only
reports what one image costs so there is one number rather than two that can drift.
"""
import io
import json
import logging
import os
import re
import time

import requests

import slow_read

# Bound at import, from the real requests, because tests and audit probes replace
# `requests` in this module's namespace with a stub that has no .exceptions. A failure
# we cannot classify is treated as "fal may already have our job", which is the safe
# side of the only question that costs money.
_UNSENT_TRANSPORT_ERRORS = (
    requests.exceptions.ConnectTimeout,
    requests.exceptions.SSLError,
    requests.exceptions.ProxyError,
)

logger = logging.getLogger(__name__)

KEY_ENV = "FAL_KEY"

# The queue API rather than the synchronous one, deliberately. A synchronous call holds
# one HTTP request open for the whole generation, so a slow generation is indistinguish-
# able from a hung connection and there is nothing to poll. Submit-then-poll means every
# individual request is short, which is what makes a hard deadline meaningful.
QUEUE_BASE = "https://queue.fal.run"

# The image editor. Named here rather than at the call sites so there is one place to
# change when fal renames or versions it.
EDIT_MODEL = "fal-ai/nano-banana/edit"

# What one generated image costs, in US dollars. Measured from fal's billing on the
# session that produced docs/old-to-new-video-spec.md: about four cents an image. This
# is the ONLY copy of the figure; old_to_new's ceilings are derived from it.
COST_PER_IMAGE_USD = 0.04

# Per-call deadline, queue time included. A nano-banana edit lands in 10-25s when fal is
# healthy, so 90s is generous; the number only has to be small enough that a stuck
# generation cannot hold a render slot. The whole-listing budget in old_to_new is the
# ceiling that actually protects the agent's wait -- this one protects the worker.
CALL_DEADLINE_SECONDS = 90

# How long one HTTP request may take. Short on purpose: these are small JSON bodies, and
# the deadline above is what bounds the generation.
HTTP_CONNECT_SECONDS = 5
HTTP_READ_SECONDS = 20

# Poll cadence while the request sits in fal's queue. Backs off so a long queue does not
# turn into a poll flood, capped so we never sit idle for most of our own deadline.
POLL_FIRST_SECONDS = 1.5
POLL_MAX_SECONDS = 6.0

# Bounded attempts, and only retryable failures consume one. TWO COUNTERS, because the
# two kinds of attempt cost completely different things.
#
# MAX_SUBMITS is the number of times we may ask fal to GENERATE. Every one of those is
# billed the moment fal queues it, whether or not we ever collect the picture. The second
# one is spent only when we can tell the first never arrived (see _request and
# `may_be_queued`): a failure that might have left a job in fal's queue is never answered
# by submitting another.
#
# MAX_COLLECT_ATTEMPTS is the number of times we may go back for an image a submit has
# ALREADY BOUGHT -- polling, fetching the result, downloading the file. None of those can
# be billed, and an audit on 2026-10-22 showed every realistic failure in this phase
# (a 429 on a poll, a 502 on the result fetch after the job had COMPLETED, a failed
# download, a transport error mid-poll) made the old code SUBMIT AGAIN and pay twice for
# one picture. So this phase is generous where the one above is strict.
MAX_SUBMITS = 2
MAX_COLLECT_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 2.0

# A status or result reply is a few hundred bytes of JSON. Anything claiming to be
# megabytes is not one, and reading it would be a way to spend the whole deadline.
MAX_JSON_BYTES = 2 * 1024 * 1024

# The generated image comes back as a URL we then download. Same decompression-bomb and
# runaway-body guards the video renderer uses on listing photos.
MAX_IMAGE_BYTES = 20 * 1024 * 1024

# Words fal's safety layer refuses, INCLUDING when they appear as exclusions ("no
# explosion"). Two generations were refused before this was understood; see the
# prompt-safety note in docs/old-to-new-video-spec.md. Screened here, at the boundary,
# rather than only where prompts are written, because part of every room prompt is
# written by a model reading a photo and could contain anything.
#
# A hit is a PERMANENT refusal raised BEFORE the request is sent: it costs nothing, it
# cannot succeed on a retry, and it degrades the room to a plain dissolve. That is much
# better than paying for a 422.
REFUSED_WORDS = (
    "explosion", "explode", "exploding", "blast", "blow up", "fire", "flames",
    "burning", "debris", "rubble", "demolish", "demolition", "wreck", "destroy",
    "destruction", "smash", "shatter", "bomb", "collapse",
)


class FalError(Exception):
    """Anything that stopped a generation. Callers catch THIS and degrade.

    `may_be_queued` answers the only question in this module that costs money: could
    fal have taken a job we are about to be billed for, before this went wrong? False
    means we know it did not (it answered us with an error, or we never reached it), so
    submitting again is safe. True means we cannot tell, so submitting again would risk
    paying twice for one picture and generate() will not do it. Defaults to the safe
    answer and is set to True only on the genuinely ambiguous paths.

    `images_billed` answers the same question in money rather than in policy: HOW MANY
    pictures fal may already have charged for when this was raised. generate() sets it on
    every error it raises, and billed_images() below is how callers read it. It exists
    because the two had been conflated -- may_be_queued decided whether to resubmit, and
    NOTHING decided what to count, so an audit on 2026-10-22 measured five everyday
    failure shapes in which fal was paid and the per-agent tally stayed at zero."""

    may_be_queued = False
    images_billed = 0


class FalNotConfigured(FalError):
    """No FAL_KEY in the environment. The feature is off, not broken."""


class FalPermanent(FalError):
    """This will be refused again. Do not retry, do not pay twice.

    A content refusal, a prompt we screened ourselves, a malformed request, a model id
    fal does not know, a key fal rejects."""


class FalRefused(FalPermanent):
    """fal's safety layer would not make this image.

    Separate from FalPermanent because it is the one permanent failure that is NOT a bug
    on our side and must read as a normal, expected outcome in the logs -- the room
    degrades to a plain dissolve and the agent still gets their video."""


class FalTemporary(FalError):
    """Worth one more try: a 5xx, a transport error, a deadline."""


class FalRateLimited(FalTemporary):
    """fal is at this account's concurrency limit. Not a fault, and not ours to retry.

    SEPARATE FROM FalTemporary BECAUSE THE RIGHT ANSWER IS DIFFERENT. fal starts a new
    account at TWO concurrent requests, rising with credits spent, and a third party on
    the same key (Jane's own ~/.nestlist_fal_key experiments share it) is enough to make
    this an everyday answer rather than a rare one. The Dramatic queue was dropped to ONE
    render at a time for the rollout partly for that reason -- see
    video_jobs.JOB_TYPES.

    Retrying inside this call spends the agent's wait on a queue that is full, and when
    it runs out the room silently loses its transformation. Handing it back instead lets
    video_jobs.DeferJob put the whole job back for a few seconds -- which rewinds its
    attempt counter, so waiting for capacity can never fail a render. That is what
    DeferJob was built for, and this is the external-service 429 its docstring names.

    The one exception is a rate limit hit while collecting an image we have ALREADY paid
    for: there, waiting the few seconds inside this call is much cheaper than giving the
    picture up, so generate() retries that case rather than handing it back."""


def is_enabled():
    """True when a key is present. No second switch: a deploy either has the key or it
    does not, and a half-configured deploy behaves exactly like an unconfigured one."""
    return bool(os.environ.get(KEY_ENV, "").strip())


def billed_images(exc):
    """How many pictures fal may already have charged for before `exc` stopped the call.

    THE TALLY MUST COUNT THIS, NOT THE PICTURES THAT ARRIVED. A submit fal accepted is
    billed whether or not we ever collect the file, so "we received nothing" and "we were
    charged nothing" are different statements, and the ceiling has to be built on the
    second one. Measured costs of getting that wrong, on 2026-10-22:

      * fal incident, every collect attempt 502s: five pictures billed per press of
        Generate, US$0.20, and the old tally read US$0.00.
      * fal rate-limiting collection: the same picture re-submitted and re-paid for once
        per deferral cycle -- 17 cycles inside the 600s patience window, US$0.68 for one
        press, tally US$0.00. (That loop is closed in old_to_new, by not deferring a pass
        that already lost money; this function is how the money becomes visible at all.)

    Tolerant on purpose: `exc` is often an exception from an INJECTED generator (tests,
    video_renderer) that has never heard of this attribute, and "I do not know" has to
    mean zero rather than a crash inside a degradation handler.
    """
    try:
        return max(0, int(getattr(exc, "images_billed", 0) or 0))
    except (TypeError, ValueError):
        return 0


def _auth_headers():
    """Built fresh per call and NEVER logged, echoed or put in an exception."""
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        raise FalNotConfigured(
            "image generation is not configured on this deploy (FAL_KEY is not set)")
    return {"Authorization": f"Key {key}", "Content-Type": "application/json"}


def screen_prompt(prompt):
    """The refused word in `prompt`, or None when it is safe to send.

    Public because the prompt writers want to screen their own text before they build a
    plan around it -- knowing a room cannot be generated BEFORE the agent waits is worth
    more than discovering it afterwards.
    """
    low = (prompt or "").lower()
    for word in REFUSED_WORDS:
        if word in low:
            return word
    return None


# What a credential looks like once something has had its way with it.
#
# REDACTING BY EQUALITY WAS NOT ENOUGH, and that is the whole lesson here. The first
# version of _redact replaced os.environ["FAL_KEY"] where it appeared byte for byte, and
# its test echoed the key back byte for byte -- so the test passed against a redactor
# that any transformation defeats. An audit walked past it on 2026-10-22 with
# `Key%20<id>%3A<secret>` (url-encoded), with the secret half on its own, with the id
# half on its own, and with the case changed. All four would have reached Railway's logs.
#
# A key is not a string we can look for, it is a SHAPE: a long run of key-ish characters,
# usually just after a word like "Key". So that is what these match. They over-match
# happily -- a long url path segment in an error message gets starred out too -- which is
# the correct trade for a line in a log.
_KEY_PATTERNS = (
    # The header shape fal echoes back, however it has been escaped or cased:
    # "Key <token>", "Bearer <token>", "authorization": "<token>", "Key%20<token>".
    re.compile(r"""(?i)\b(key|bearer|token|authorization)\b([\s:=,"'\\]|%20|%22|%3A)+"""
               r"""([^\s"',}\\]+)"""),
    # Anything long enough and dense enough to BE a credential: hex, base64, a uuid, or
    # a uuid and a secret joined by a colon -- url-encoded or not. 24 is comfortably
    # below either half of a real fal key, so half a key is caught as readily as all of
    # one, and so is a truncated echo.
    re.compile(r"[A-Za-z0-9+=_%:-]{24,}"),
)


def _redact(text):
    """Any text we are about to quote, with anything credential-shaped taken out of it.

    THE REASON THIS EXISTS: text that came from fal reaches Railway's logs and, through a
    degradation note, the agent. The key is never DELIBERATELY formatted anywhere in this
    module -- this guards the path where somebody else puts it in our mouth.

    It is now a BELT rather than the braces. _classify quotes no fal body at all, so the
    only external prose still passed through here is fal's own explanation of a
    completed-without-an-image decline. Anything that starts quoting fal again must come
    through this function, and must not go back to matching by equality.
    """
    text = str(text or "")
    for pattern in _KEY_PATTERNS:
        text = pattern.sub(
            lambda m: (m.group(1) + " [redacted]") if m.lastindex else "[redacted]",
            text)
    # Last belt: the key itself, and the two cases a sloppy echo arrives in. Everything
    # above should already have caught these; this costs nothing and does not rely on a
    # pattern being right.
    key = os.environ.get(KEY_ENV, "").strip()
    for variant in {key, key.lower(), key.upper()} if key else ():
        if variant and variant in text:
            text = text.replace(variant, "[redacted]")
    return text


# The words in a fal error that mean "we would not make this picture", as opposed to
# "your request was wrong". OURS, not fal's: what we log is one of these words, never the
# body it was found in, so the categorising cannot itself leak anything.
_REFUSAL_MARKERS = ("safety", "policy", "blocked", "prohibited", "refus",
                    "content filter", "not allowed")


def _refusal_marker(body_text):
    """Which refusal word fal's reply contains, or None. Scanned in memory and dropped."""
    low = str(body_text or "").lower()
    for word in _REFUSAL_MARKERS:
        if word in low:
            return word
    return None


def _body_shape(body_text):
    """What a reply we will NOT quote looks like: how big it is, and if it is JSON, the
    NAMES of its top-level fields. No values, in any branch.

    THE MIDDLE PATH, and it was pointed out as one we had skipped. Quoting fal's 4xx
    bodies put the key one bad redaction away from Railway's logs, so we stopped; but
    "HTTP 422; its reply is not quoted" cannot tell a renamed request field from a safety
    refusal, and those two want completely different answers from whoever reads the log.
    The SHAPE separates them: a 422 whose body carries `detail` is a schema complaint, one
    that is not JSON at all is a proxy in the way, one with fields we have never seen is
    fal changing its API.

    A field name is not a credential -- but the names go through _redact anyway, and are
    capped in number and length, because "cannot carry a key" is exactly how the leak
    that started all this was argued.
    """
    text = str(body_text or "")
    if not text.strip():
        return "its reply is empty"
    size = "%d bytes" % len(text)
    try:
        parsed = json.loads(text)
    except Exception:
        return f"{size}, not JSON"
    if isinstance(parsed, dict):
        names = [_redact(str(name))[:40] for name in list(parsed.keys())[:8]]
        if not names:
            return f"{size}, JSON with no fields"
        return f"{size}, JSON fields: {', '.join(names)}"
    return f"{size}, JSON {type(parsed).__name__}"


def _classify(status_code, body_text):
    """One HTTP failure -> the exception that says what to do about it.

    THE BODY IS NEVER QUOTED, in any branch. fal's 4xx bodies echo request detail back,
    and the detail includes the Authorization header we sent; quoting them put the key
    one bad redaction away from Railway's logs, and the redaction WAS bad (see
    _KEY_PATTERNS). What comes out of here is a status code, and for a refusal one word
    from our own vocabulary. That is most of what the body was ever read for.

    What this gives up: when fal rejects a request for a reason that is neither a refusal
    nor obvious from the status, the log says less than it did. The trade is deliberate --
    a reason we have to guess at costs a room its transformation, a leaked key costs Jane
    her account -- but it gave up MORE than it had to, so the 4xx branch now reports the
    body's SHAPE (see _body_shape): its size and its top-level field names, never a value.
    """
    if status_code in (401, 403):
        return FalPermanent("fal rejected our credentials -- check FAL_KEY in Railway")
    if status_code == 429:
        return FalRateLimited("fal is rate limiting this account")
    if status_code in (400, 422):
        # fal's validation and safety refusals both land here, and they are not the same
        # thing: a refusal is a normal, designed-for outcome and must read as one.
        marker = _refusal_marker(body_text)
        if marker:
            return FalRefused(
                f"fal would not generate this image (its reply mentions {marker!r})")
        return FalPermanent(
            f"fal rejected the request (HTTP {status_code}; {_body_shape(body_text)}; its "
            "reply is not quoted -- it can echo our own request headers)")
    if status_code == 404:
        return FalPermanent(f"fal does not know the model {EDIT_MODEL!r}")
    if status_code >= 500:
        return FalTemporary(f"fal returned {status_code}")
    return FalTemporary(f"fal returned {status_code}")


def _ambiguous(exc):
    """Mark an error as "fal might have taken our job before this went wrong"."""
    exc.may_be_queued = True
    return exc


def _request(method, url, deadline, json_body=None):
    """One short HTTP call to fal, under the CALLER'S absolute deadline.

    THE DEADLINE NOW FREES THE SOCKET AND THE THREAD, not just the caller, and that is
    the change worth reading. This function used to run the whole call -- connect, send,
    and the entire body read -- inside slow_read.call_with_deadline and simply stop
    waiting, on the stated grounds that these are "small JSON with no socket worth
    reaching into" and that the abandoned thread dies when requests gives up.
    Both halves of that were wrong:

      * requests' read timeout is PER READ. A server dribbling one byte every three
        seconds keeps every read healthy, so requests NEVER gives up -- which is the
        exact lesson slow_read exists to encode, applied to the download next door but
        not here. Measured by an audit on 2026-10-22: five calls to such an endpoint left
        five threads AND five sockets alive 105 seconds later, for good.
      * the socket is as reachable here as it is in _download. There was nothing special
        about these replies except that they are smaller.

    A cold Dramatic render makes tens of these calls, so a degraded fal could have filled
    a Railway worker with stuck threads and file descriptors until it was redeployed.
    So: stream=True, which returns as soon as the headers are in, and the body is then
    read under slow_read.deadline_watchdog -- at the deadline the socket is shut down,
    the blocked read raises, and the worker thread ends.

    THE RESIDUAL, AND THIS NOTE USED TO GET IT WRONG. A server that never finishes
    sending HEADERS is still merely abandoned: there is no response object yet, so the
    watchdog has no socket to shut down. The previous version of this paragraph credited
    requests' own connect and read timeouts with bounding that case, on the grounds that
    nothing has arrived to reset them. NOTHING ARRIVING IS NOT THE SHAPE: header lines are
    arriving, every individual read is therefore healthy, and the read timeout never fires
    -- the per-read trap at the top of this module, one phase earlier.

    What really bounds it is http.client._MAXHEADERS -- 100 header lines, after which the
    thread ends however long they took. Measured on 2026-10-22 against a server sending
    one header line every 0.02s / 0.05s / 0.15s: the thread and its socket were released
    after 2.7s / 5.6s / 15.6s, i.e. 100 x the gap each time, not HTTP_READ_SECONDS.

    SO THE WORST CASE IS 100 x HTTP_READ_SECONDS, about 33 minutes, for one thread and one
    socket per call. It is reached by a gap just UNDER the read timeout; a gap above it
    does fire the timeout (measured: a 2.0s gap against a 1.0s read timeout released at
    1.0s), which is what makes 33 minutes a bound rather than an open end. Bounding it
    honestly would need the connection before its headers arrive, which requests does not
    hand out, so this is left measured rather than fixed, and
    tests/test_slow_read_deadlines.py pins the number so the next person to read this gets
    one that was checked.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FalTemporary("ran out of time before calling fal")

    def attempt():
        response = requests.request(
            method, url,
            headers=_auth_headers(),
            json=json_body,
            timeout=(HTTP_CONNECT_SECONDS, HTTP_READ_SECONDS),
            # So the body is read HERE, under the watchdog, instead of inside requests
            # where nothing can interrupt it.
            stream=True,
        )
        try:
            declared = int((getattr(response, "headers", None) or {})
                           .get("content-length") or 0)
        except (TypeError, ValueError):
            declared = 0
        if declared > MAX_JSON_BYTES:
            raise FalTemporary("fal's reply is too big to be a status or a result")
        try:
            with slow_read.deadline_watchdog(response, deadline, warn=False):
                if response.status_code >= 400:
                    return response.status_code, response.text, None
                try:
                    return response.status_code, None, response.json()
                except ValueError:
                    # Not JSON. Reported by the caller as such, from payload=None, so a
                    # 200 we cannot read is never mistaken for a transport failure.
                    return response.status_code, None, None
        finally:
            # Never reused, and the tidy close is the slow one -- same reasoning as
            # _download's finally.
            slow_read.abandon_response(response)

    try:
        status_code, body, payload = slow_read.call_with_deadline(
            attempt, deadline, name="fal request")
    except FalNotConfigured:
        raise
    except FalError:
        # Raised by attempt() itself (the size guard). Already classified.
        raise
    except TimeoutError as e:
        # We stopped waiting at the deadline. If this was a submit, fal may well have the
        # job: the deadline says nothing about whether the request arrived.
        raise _ambiguous(
            FalTemporary("fal did not answer before the deadline")) from e
    except Exception as e:
        if time.monotonic() > deadline:
            # The watchdog shut the socket down and the blocked read surfaced as an
            # ordinary transport error. Past the deadline, that is what it is -- the same
            # reading _download gives it.
            raise _ambiguous(
                FalTemporary("fal did not answer before the deadline")) from e
        error = FalTemporary(f"could not reach fal ({type(e).__name__})")
        if not isinstance(e, _UNSENT_TRANSPORT_ERRORS):
            # A connect timeout or a TLS failure happened before we sent anything. A
            # reset, a short read, a body that was not JSON -- any of those can have
            # happened after fal accepted the job, so they are ambiguous and must not be
            # answered by submitting again.
            _ambiguous(error)
        raise error from e

    if status_code >= 400:
        # fal answered, with an error. It did not take the job, so a resubmit is safe.
        raise _classify(status_code, body)
    if payload is None:
        raise _ambiguous(FalTemporary("fal returned something that is not JSON"))
    return payload


def _download(url, deadline):
    """The finished image's bytes, with a hard byte cap and a deadline that interrupts.

    THIS one has a socket we can reach, so it gets the real watchdog rather than the
    abandoned thread: a body still arriving at the deadline is cut off by shutting the
    socket down. A tidy close would mean waiting out the trickle -- measured at 80s past
    the deadline in slow_read's own notes.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FalTemporary("ran out of time before downloading the generated image")
    try:
        response = requests.get(url, timeout=(HTTP_CONNECT_SECONDS, HTTP_READ_SECONDS),
                                stream=True)
    except Exception as e:
        raise FalTemporary(f"could not download the generated image "
                           f"({type(e).__name__})") from e
    try:
        if response.status_code >= 400:
            raise _classify(response.status_code, "")
        buffer = io.BytesIO()
        try:
            with slow_read.deadline_watchdog(response, deadline):
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    if time.monotonic() > deadline:
                        raise FalTemporary("the generated image passed its deadline")
                    buffer.write(chunk)
                    if buffer.tell() > MAX_IMAGE_BYTES:
                        raise FalPermanent(
                            f"the generated image is over "
                            f"{MAX_IMAGE_BYTES // 1024 // 1024}MB")
        except FalError:
            raise
        except Exception as e:
            # A read the watchdog interrupted surfaces as an ordinary transport error.
            # Past the deadline, that is what it is.
            if time.monotonic() > deadline:
                raise FalTemporary(
                    "the generated image passed its deadline") from e
            raise FalTemporary(
                f"the generated image download failed ({type(e).__name__})") from e
    finally:
        # Always: this connection is never reused, and the tidy close is the slow one.
        slow_read.abandon_response(response)
    if time.monotonic() > deadline:
        # An interrupted read can look like a clean end of body, so a download that ran
        # past its deadline is never trusted to be complete. Half an image in a video is
        # worse than no transformation at all.
        raise FalTemporary("the generated image passed its deadline")
    data = buffer.getvalue()
    if not data:
        raise FalTemporary("fal served an empty image")
    return data


def _submit(prompt, image_urls, deadline):
    """Ask fal to generate. THIS IS THE ONLY BILLABLE CALL IN THIS MODULE.

    Returns a small dict describing what fal accepted: where to poll it and where to
    collect it, or -- when fal answered synchronously -- the finished payload itself.
    Everything in _collect below works from that dict and cannot be billed, which is the
    whole reason the two are separate functions rather than one.
    """
    submitted = _request(
        "POST", f"{QUEUE_BASE}/{EDIT_MODEL}", deadline,
        json_body={
            "prompt": prompt,
            "image_urls": list(image_urls),
            "num_images": 1,
            "output_format": "jpeg",
        },
    )
    status_url = submitted.get("status_url")
    response_url = submitted.get("response_url")
    if status_url and response_url:
        return {"status_url": status_url, "response_url": response_url, "payload": None}
    if submitted.get("images"):
        # fal answered synchronously. Nothing to poll, and the image is already paid for.
        return {"status_url": None, "response_url": None, "payload": submitted}
    # fal took our request, said 200, and gave us no way to collect what we are being
    # charged for. PERMANENT on purpose: this is the one failure where trying again means
    # paying a second time for a response shape we do not understand, and a second
    # payment would not teach us anything the first one did not.
    #
    # Marked ambiguous as well, which costs nothing in retry policy (a FalPermanent is
    # never retried either way) and makes the BILLING rule in generate() a single
    # question: fal answered 200, so fal has the job, so we were charged.
    raise _ambiguous(
        FalPermanent("fal accepted the request but gave us nothing to poll"))


def _collect(accepted, deadline):
    """Poll an ALREADY SUBMITTED job to completion and fetch its result payload.

    FREE. Every call in here is a GET against a job fal has already queued and already
    charged for, so a failure in this phase is worth going back for rather than paying
    for again -- see MAX_COLLECT_ATTEMPTS.
    """
    if accepted.get("payload") is not None:
        return accepted["payload"]

    wait = POLL_FIRST_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise FalTemporary("fal was still generating at the deadline")
        # Sleep BEFORE the first poll: a request submitted a moment ago is never done.
        time.sleep(min(wait, max(0.0, deadline - time.monotonic())))
        wait = min(wait * 1.6, POLL_MAX_SECONDS)
        state = _request("GET", accepted["status_url"], deadline)
        status = str(state.get("status") or "").upper()
        if status == "COMPLETED":
            break
        if status in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
            # fal's own terminal failure. Its error text is the only clue to whether
            # this is a refusal or a fault, so classify on it rather than guessing --
            # _classify reads the text and quotes none of it.
            detail = str(state.get("error") or state.get("detail") or status)
            raise _classify(422, detail)
    return _request("GET", accepted["response_url"], deadline)


def generate(prompt, image_urls, deadline=None):
    """One edited image's JPEG/PNG bytes. Raises a FalError; never returns None.

    `image_urls` are PUBLIC urls fal fetches itself -- the listing bucket is already
    public (both renderers read it unauthed), so this avoids pushing several megabytes
    through our own process on the way out.

    `deadline` is an absolute time.monotonic() value and bounds the WHOLE call, attempts
    and queue time included. Omitted, the call bounds itself at CALL_DEADLINE_SECONDS.
    A caller generating several images for one listing passes ONE shared deadline, so a
    slow first image eats into the plan rather than extending it.
    """
    if not image_urls:
        raise FalPermanent("an image edit needs at least one source photo")
    refused = screen_prompt(prompt)
    if refused:
        # Screened before the request, so it costs nothing. See REFUSED_WORDS.
        raise FalRefused(f"the prompt contains {refused!r}, which fal refuses")
    if not is_enabled():
        raise FalNotConfigured(
            "image generation is not configured on this deploy (FAL_KEY is not set)")

    overall = (deadline if deadline is not None
               else time.monotonic() + CALL_DEADLINE_SECONDS)
    started = time.monotonic()

    # THE STATE THAT MAKES A RETRY FREE. Each of these, once set, is carried across every
    # later attempt, so a failure is answered by going back for what we already bought
    # rather than buying it again:
    #   accepted  -- fal has queued (and billed) a job; where to poll and collect it.
    #   payload   -- that job finished and this is its result.
    #   image_url -- the address of the finished picture.
    # The audit that produced this shape found all four realistic failures AFTER an
    # accepted submit -- a 429 on a poll, a 502 on the result fetch once the job had
    # COMPLETED, a failed download, a transport error mid-poll -- each making a SECOND
    # submit. In the worst of them the picture was generated, charged for, thrown away
    # and generated again.
    accepted = None
    payload = None
    image_url = None

    submits = 0
    collects = 0
    last = None
    # WHAT FAL HAS CHARGED US FOR SO FAR IN THIS CALL. Counted at the submit, which is
    # where the charge happens, and not at the download, which is where the old count
    # happened and where five measured failure shapes never arrive. A list rather than an
    # int so the handler at the bottom can read whatever the loop got to.
    billed = [0]
    try:
        while True:
            if time.monotonic() >= overall:
                raise last or FalTemporary("ran out of time before generating an image")
            try:
                if accepted is None and payload is None:
                    submits += 1
                    try:
                        accepted = _submit(prompt, image_urls, overall)
                    except FalError as e:
                        # A submit fal ANSWERED with an error cost nothing -- it did not
                        # take the job. A submit that might have reached fal's queue (a
                        # deadline, a reset, a 200 we could not read, a 200 with nothing
                        # to poll) is counted as charged, because an uncertain bill has to
                        # read as a bill: the other way round is how the ceiling came to
                        # be exact on the healthy path and zero on a bad day.
                        if e.may_be_queued:
                            billed[0] += 1
                        raise
                    billed[0] += 1
                if payload is None:
                    payload = _collect(accepted, overall)
                if image_url is None:
                    images = payload.get("images") or []
                    image_url = (images[0] or {}).get("url") if images else None
                    if not image_url:
                        # COMPLETED with no image is how nano-banana declines something
                        # its safety layer did not reject outright -- `description` says
                        # why. It is permanent: the same prompt gets the same answer. The
                        # description is fal's own prose rather than an echo of our
                        # request, so it is worth keeping -- through _redact, and capped.
                        # IT IS ALSO BILLED: the submit was accepted, and this decline is
                        # the quietest of the five shapes that used to cost nothing on
                        # paper.
                        raise FalRefused(
                            "fal completed without producing an image"
                            + (f": {_redact(payload.get('description'))[:200]}"
                               if payload.get("description") else ""))
                data = _download(image_url, overall)
                logger.info("fal generated an image in %.1fs (%d KB, %s, %d submit(s))",
                            time.monotonic() - started, len(data) // 1024, EDIT_MODEL,
                            submits)
                return data
            except FalPermanent:
                # Includes FalRefused. No retry: the answer will not change and a second
                # attempt would be billed for the same answer.
                raise
            except FalTemporary as e:
                last = e
                if accepted is None and payload is None:
                    # NOTHING COLLECTABLE YET. A rate limit here means fal has no room for
                    # us; hand it back so the whole job can wait for capacity (see
                    # FalRateLimited) instead of burning the agent's wait on a full queue.
                    if isinstance(e, FalRateLimited):
                        raise
                    # Only resubmit when we can tell the first submit never arrived.
                    # Otherwise fal may be generating a picture we are already being
                    # charged for, and a second submit would charge us again for the same
                    # room.
                    if e.may_be_queued:
                        logger.warning("fal submit failed in a way that may already have "
                                       "queued a job (%s); not submitting again", e)
                        raise
                    if submits >= MAX_SUBMITS:
                        raise
                else:
                    # ALREADY PAID FOR. Going back for it is free; giving it up is not.
                    collects += 1
                    if collects >= MAX_COLLECT_ATTEMPTS:
                        raise
                if time.monotonic() + RETRY_BACKOFF_SECONDS >= overall:
                    raise
                logger.warning("fal generation failed (%s); %s", e,
                               "collecting an image we have already paid for again"
                               if accepted is not None or payload is not None
                               else "submitting again")
                time.sleep(RETRY_BACKOFF_SECONDS)
    except FalError as e:
        # EVERY ERROR THIS FUNCTION RAISES LEAVES CARRYING WHAT IT COST, so no caller has
        # to reconstruct it from the exception type -- which is the mistake the second
        # audit found: `may_be_queued` and `accepted` already knew the answer and only the
        # retry logic was asking. old_to_new reads it with billed_images().
        e.images_billed = billed[0]
        if billed[0]:
            logger.warning("fal was charged for %d image(s) we did not get (%s)",
                           billed[0], e)
        raise
