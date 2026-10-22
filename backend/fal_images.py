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
    interrupts. requests' timeout=(connect, read) is PER READ: an endpoint dribbling a
    byte just inside it resets the clock forever, and a deadline checked between polls
    does not run while a read is blocked. slow_read has the measurements. Every HTTP
    call here is therefore made under slow_read, not merely watched by a timer.
  * BOUNDED ATTEMPTS. Two, and only for errors that could plausibly succeed next time.
  * A PERMANENT / RETRYABLE SPLIT, because the two must not be treated alike. A content
    refusal or a malformed prompt will be refused identically every time; retrying it
    spends the agent's wait and our money to arrive at the same answer. A 429 or a 502
    is worth exactly one more try.
  * NOTHING HERE RAISES INTO A RENDER. Callers catch FalError and fall back; see
    old_to_new.py, where a refusal becomes a plain dissolve.

THE KEY
FAL_KEY comes from the process environment and from nowhere else. It is never read from
a file, never written to one, never logged, and never put in an error message -- errors
from this module are shown to agents and copied into Railway's logs. _auth_headers()
builds the header fresh per call for exactly that reason.

COST
Every successful generation is billed. generate() therefore does NOT decide whether we
can afford to generate -- that belongs to the caller, up front, before the first call
(old_to_new.reserve_budget). This module only reports what one image costs so there is
one number rather than two that can drift.
"""
import io
import logging
import os
import time

import requests

import slow_read

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

# Bounded attempts, and only retryable failures consume one.
MAX_ATTEMPTS = 2
RETRY_BACKOFF_SECONDS = 2.0

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
    """Anything that stopped a generation. Callers catch THIS and degrade."""


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
    """Worth one more try: rate limiting, a 5xx, a transport error, a deadline."""


def is_enabled():
    """True when a key is present. No second switch: a deploy either has the key or it
    does not, and a half-configured deploy behaves exactly like an unconfigured one."""
    return bool(os.environ.get(KEY_ENV, "").strip())


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


def _redact(text):
    """Any text we are about to quote, with the key taken out of it.

    THE REASON THIS EXISTS, found by a test rather than by reading the code: fal's 4xx
    bodies echo request detail, and we quote them so a refusal is diagnosable. That text
    reaches Railway's logs and, through a degradation note, the agent. An echoed
    Authorization header would therefore have put the key in both. The key is never
    DELIBERATELY formatted anywhere in this module -- this guards the path where
    somebody else puts it in our mouth.
    """
    text = str(text or "")
    key = os.environ.get(KEY_ENV, "").strip()
    if key and key in text:
        text = text.replace(key, "[redacted]")
    return text


def _classify(status_code, body_text):
    """One HTTP failure -> the exception that says what to do about it."""
    body_text = _redact(body_text)
    if status_code in (401, 403):
        # Never include the body: fal echoes request details, and this one is about the
        # key. "Check FAL_KEY in Railway" is the whole actionable content.
        return FalPermanent("fal rejected our credentials -- check FAL_KEY in Railway")
    if status_code == 429:
        return FalTemporary("fal is rate limiting this account")
    if status_code in (400, 422):
        # fal's validation and safety refusals both land here. Distinguish on the body
        # so a refusal reads as a refusal in the logs, and truncate hard -- this text
        # reaches Railway's logs and, via a degradation note, the agent.
        low = (body_text or "").lower()
        if any(w in low for w in ("safety", "policy", "blocked", "prohibited",
                                  "refus", "content filter", "not allowed")):
            return FalRefused(f"fal would not generate this image: {body_text[:200]}")
        return FalPermanent(f"fal rejected the request: {body_text[:200]}")
    if status_code == 404:
        return FalPermanent(f"fal does not know the model {EDIT_MODEL!r}")
    if status_code >= 500:
        return FalTemporary(f"fal returned {status_code}")
    return FalTemporary(f"fal returned {status_code}: {(body_text or '')[:200]}")


def _request(method, url, deadline, json_body=None):
    """One short HTTP call to fal, under the CALLER'S absolute deadline.

    The deadline is enforced, not merely checked. requests' read timeout cannot bound
    this (see the module docstring), and these bodies are small JSON with no socket
    worth reaching into mid-read, so the call runs in slow_read's abandoned-thread form:
    we stop waiting at the deadline and the daemon thread dies when requests eventually
    gives up. It holds no lock a render needs and writes nothing.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise FalTemporary("ran out of time before calling fal")

    def attempt():
        return requests.request(
            method, url,
            headers=_auth_headers(),
            json=json_body,
            timeout=(HTTP_CONNECT_SECONDS, HTTP_READ_SECONDS),
        )

    try:
        response = slow_read.call_with_deadline(attempt, deadline, name="fal request")
    except FalNotConfigured:
        raise
    except TimeoutError as e:
        raise FalTemporary("fal did not answer before the deadline") from e
    except Exception as e:
        # Transport: DNS, connection reset, TLS. Worth one more try.
        raise FalTemporary(f"could not reach fal ({type(e).__name__})") from e

    if response.status_code >= 400:
        try:
            body = response.text
        except Exception:
            body = ""
        raise _classify(response.status_code, body)
    try:
        return response.json()
    except Exception as e:
        raise FalTemporary("fal returned something that is not JSON") from e


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


def _submit_and_wait(prompt, image_urls, deadline):
    """One attempt: submit, poll to completion, return the output payload."""
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
    if not status_url or not response_url:
        # Includes the shape where fal answered synchronously; treat it as usable.
        if submitted.get("images"):
            return submitted
        raise FalTemporary("fal accepted the request but gave us nothing to poll")

    wait = POLL_FIRST_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise FalTemporary("fal was still generating at the deadline")
        # Sleep BEFORE the first poll: a request submitted a moment ago is never done.
        time.sleep(min(wait, max(0.0, deadline - time.monotonic())))
        wait = min(wait * 1.6, POLL_MAX_SECONDS)
        state = _request("GET", status_url, deadline)
        status = str(state.get("status") or "").upper()
        if status == "COMPLETED":
            break
        if status in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
            # fal's own terminal failure. Its error text is the only clue to whether
            # this is a refusal or a fault, so classify on it rather than guessing.
            detail = str(state.get("error") or state.get("detail") or status)
            raise _classify(422, detail)   # _classify redacts
    return _request("GET", response_url, deadline)


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
    last = None
    for attempt in range(MAX_ATTEMPTS):
        if time.monotonic() >= overall:
            raise last or FalTemporary("ran out of time before generating an image")
        started = time.monotonic()
        try:
            payload = _submit_and_wait(prompt, image_urls, overall)
            images = payload.get("images") or []
            url = (images[0] or {}).get("url") if images else None
            if not url:
                # COMPLETED with no image is how nano-banana declines something its
                # safety layer did not reject outright -- `description` says why. It is
                # permanent: the same prompt gets the same answer.
                raise FalRefused(
                    "fal completed without producing an image"
                    + (f": {_redact(payload.get('description'))[:200]}"
                       if payload.get("description") else ""))
            data = _download(url, overall)
            logger.info("fal generated an image in %.1fs (%d KB, %s)",
                        time.monotonic() - started, len(data) // 1024, EDIT_MODEL)
            return data
        except FalPermanent:
            # Includes FalRefused. No retry: the answer will not change and the second
            # attempt would be billed.
            raise
        except FalTemporary as e:
            last = e
            if attempt + 1 < MAX_ATTEMPTS and time.monotonic() + RETRY_BACKOFF_SECONDS < overall:
                logger.warning("fal generation attempt %d failed (%s); retrying once",
                               attempt + 1, e)
                time.sleep(RETRY_BACKOFF_SECONDS)
                continue
            raise
    raise last or FalTemporary("fal generation failed")
