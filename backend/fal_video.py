"""fal.ai VIDEO generation (Kling) -- the Elegant film's clips, bought one at a time.

WHAT THIS IS FOR
The Elegant ("From Old to New", phase two) video is not built out of still photographs
the way the Dramatic is. Each of its shots is a short generated CLIP: two photographs we
already own become the first and last frame of a Kling generation, and Kling invents the
motion between them. That is what makes the old house come down and the new one rise in
one continuous move, with no cut -- the effect Jane asked for after watching a competitor
do it. See docs/old-to-new-video-spec.md for the recipe and the prompts.

THIS IS THE EXPENSIVE MODULE IN THE WHOLE PRODUCT. One Elegant listing is about fifty
seconds of generated video, which is roughly US$3.50 -- about fifteen times what the
Dramatic's six images cost. Everything below is shaped by that.

IT IS A SIBLING OF fal_images, NOT A FORK OF IT
fal_images' HTTP discipline (one absolute deadline that frees the socket and the thread,
streamed bodies under slow_read's watchdog, a permanent/retryable/rate-limited split, a
body we never quote, a key that is never logged) was paid for by two adversarial audits
and must not exist twice. So this module IMPORTS it: _request, _download, _classify,
_redact, screen_prompt and the whole FalError hierarchy are fal_images'. What is here is
only what genuinely differs:

  * THE UNIT OF MONEY IS A SECOND, not a picture. Kling bills per second of generated
    video, so every error carries `seconds_billed` where an image error carries
    `images_billed`, and the per-agent ceiling in old_to_new counts seconds.
  * THE TIME SCALE IS MINUTES, not seconds. A nano-banana edit lands in 10-25s; a Kling
    clip takes two to six minutes. So the deadline, the poll cadence and the byte cap
    are all much larger, and a job that waits on this must NOT hold a render slot while
    it does -- see video_jobs.JobTypeConfig.counts_toward_render_limit.
  * THE MODEL'S FIELD NAMES ARE DATA, not code. fal renamed Kling's keyframe fields
    between versions (v1.6 took image_url/tail_image_url; v2.6 takes
    start_image_url/end_image_url) and deprecated the version the spec was written
    against. MODELS below is one table with the endpoint, the two field names and the
    measured price together, so switching version is editing one row and nothing else.

THE KEY
FAL_KEY comes from the process environment, through fal_images._auth_headers, and from
nowhere else. It is never read from a file, never written to one, never logged, and never
put in an error message. We quote NONE of fal's response bodies, for the reason
fal_images documents at length: its 4xx bodies echo our own request back, including the
Authorization header, and an audit walked straight past a byte-for-byte redactor with a
url-encoded key. What leaves this module is a status code and a word from our own
vocabulary.

COST, AND THE ONE RULE THAT MATTERS
EVERY ACCEPTED SUBMIT IS BILLED FOR ITS WHOLE DURATION, whether or not we ever collect
the file. A 10-second clip fal queued and then failed to hand over costs US$0.70 exactly
as one we received does. So:

  * at most MAX_SUBMITS asks per clip, and the second one only when we can tell the first
    never reached fal (fal_images' `may_be_queued`, same meaning, much more money);
  * everything after an accepted submit is free collection and gets several more tries,
    because throwing away a clip fal has charged us for is the expensive outcome;
  * every error carries seconds_billed, and billed_seconds() is how callers read it.

generate_clip() does NOT decide whether we can afford to generate. That belongs to the
caller, up front, before the first call -- old_to_new.reserve_video_budget.
"""
import logging
import os
import time

import fal_images
from fal_images import (  # noqa: F401  (re-exported on purpose; see below)
    FalError, FalNotConfigured, FalPermanent, FalRefused, FalTemporary,
    FalRateLimited, KEY_ENV, screen_prompt,
)

logger = logging.getLogger(__name__)

# The exception hierarchy is fal_images', re-exported above so a caller that handles
# both images and clips catches ONE set of classes. There is deliberately no parallel
# hierarchy: "fal refused this" means the same thing whichever endpoint said it, and two
# hierarchies would mean every caller needing two except clauses.

# ---------------------------------------------------------------------------
# The model, its field names, and its price -- in ONE row.
#
# WHY THIS IS A TABLE AND NOT THREE CONSTANTS. docs/old-to-new-video-spec.md names
# `fal-ai/kling-video/v1.6/pro/image-to-video`, which fal has since marked deprecated
# ("no longer supported"), and the replacement does not take the same field names: v1.6
# took `image_url` / `tail_image_url`, v2.6 takes `start_image_url` / `end_image_url`.
# Pinning a dead endpoint would ship a feature that 404s; hardcoding field names would
# mean the next rename is a bug hunt rather than an edit.
#
# THE PRICE TRAVELS WITH THE MODEL, because it is the model's price and not ours. Jane's
# own fal bill measured US$0.07 a second, and fal's v2.6/pro page states exactly that for
# audio OFF -- so the measured figure and the chosen endpoint agree, which is the only
# reason this is the chosen endpoint. With native audio the same model is US$0.14 a
# second and with voice control US$0.168: DOUBLE and more, for audio this film does not
# want and must not have (the Classic's own music track plays over it, and generated
# speech is against a standing rule). generate_audio=false is therefore pinned in the
# payload below and pinned again in the tests.
class VideoModel:
    def __init__(self, endpoint, start_field, end_field, cost_per_second,
                 extra_payload=None):
        self.endpoint = endpoint
        self.start_field = start_field
        self.end_field = end_field
        self.cost_per_second = cost_per_second
        self.extra_payload = dict(extra_payload or {})


MODELS = {
    "kling-2.6-pro": VideoModel(
        endpoint="fal-ai/kling-video/v2.6/pro/image-to-video",
        start_field="start_image_url",
        end_field="end_image_url",
        cost_per_second=0.07,
        # NOT NEGOTIABLE. generate_audio defaults to TRUE on this endpoint, which would
        # both double the bill and put model-generated speech into an agent's listing
        # video. Both are refused.
        extra_payload={"generate_audio": False},
    ),
    # The version the spec was written against, kept so the measurement it records has
    # something to point at. fal marks it deprecated; it is not the default and should
    # not be made the default without re-measuring the price.
    "kling-1.6-pro": VideoModel(
        endpoint="fal-ai/kling-video/v1.6/pro/image-to-video",
        start_field="image_url",
        end_field="tail_image_url",
        cost_per_second=0.07,
    ),
}

MODEL_ID = "kling-2.6-pro"


def model():
    """The VideoModel in use. A function rather than a module constant so a test can
    point this at a stub endpoint without reaching into module state."""
    return MODELS[MODEL_ID]


# What one second of generated video costs, in US dollars. THE ONLY COPY OF THE FIGURE:
# old_to_new's ceilings and every log line that mentions money are derived from it, for
# the same reason fal_images keeps one COST_PER_IMAGE_USD.
COST_PER_SECOND_USD = MODELS[MODEL_ID].cost_per_second

# The durations Kling accepts, as the strings it wants them as. There is no 7-second
# clip: asking for one is a 422, so the caller is snapped to one of these.
CLIP_SECONDS_SHORT = 5
CLIP_SECONDS_LONG = 10
ALLOWED_CLIP_SECONDS = (CLIP_SECONDS_SHORT, CLIP_SECONDS_LONG)

QUEUE_BASE = fal_images.QUEUE_BASE

# Per-clip deadline, queue time included.
#
# EIGHT MINUTES, against the image client's ninety SECONDS, and the difference is the
# whole reason this module exists separately. A Kling clip is two to six minutes of real
# generation when fal is healthy. The number only has to be large enough that a healthy
# generation is never thrown away (throwing one away costs US$0.35-0.70 and buys
# nothing) and small enough that one stuck clip cannot eat the whole pass -- which is
# what TOTAL_CLIP_BUDGET_SECONDS in old_to_new is for.
CLIP_DEADLINE_SECONDS = 480

# Poll cadence while the clip is generating. Starts slower and climbs higher than the
# image client's: a clip submitted ten seconds ago is certainly not done, and polling a
# five-minute job every 1.5 seconds is two hundred pointless requests.
POLL_FIRST_SECONDS = 6.0
POLL_MAX_SECONDS = 20.0

# Bounded attempts, split by what they cost -- the same two counters fal_images uses, and
# for the same reasons, except that here one wasted submit is US$0.35-0.70 rather than
# four cents.
MAX_SUBMITS = 2
MAX_COLLECT_ATTEMPTS = 4
RETRY_BACKOFF_SECONDS = 4.0

# A 10-second 1080p Kling mp4 measures around 10-20MB. 120MB is generous enough that a
# higher-bitrate model does not trip it and small enough that a runaway body cannot eat
# the pass. Checked while streaming, so an oversized file is cut off rather than buffered.
MAX_CLIP_BYTES = 120 * 1024 * 1024

# What the clip must at least look like to be worth putting in a film. A few hundred
# bytes of HTML error page served with a 200 is not a video, and finding that out in
# ffmpeg means the whole shot fails instead of degrading.
MIN_CLIP_BYTES = 64 * 1024

# The negative prompt, which is where the NO-FABRICATION rule is enforced on the model's
# own terms.
#
# WHY A NEGATIVE PROMPT AT ALL, when the spec's hard-won lesson is "describe what should
# happen, never what should not". That lesson is about ONE SPECIFIC TRAP: fal's safety
# layer refuses a prompt containing words like explosion, blast, fire or debris EVEN AS
# EXCLUSIONS, so those words cannot appear anywhere, in any polarity. It is not a rule
# against exclusions in general -- and Kling has a dedicated negative_prompt field whose
# whole job is exclusions, whose own default is the string "blur, distort, and low
# quality".
#
# WHAT IS EXCLUDED, AND WHY EXACTLY THESE THINGS. The competitor film Jane was shown had
# a red supercar and a yellow one parked in the driveway. They are not the property. We
# never invent a property's features -- it is the hardest standing rule there is, it is
# what the whole brand's claim to be trustworthy rests on, and a generated car is a
# fabricated fact about a driveway just as surely as an invented bedroom would be. So
# vehicles, people, animals, signage and planting that is not in the photograph are
# excluded here, on top of the positive pinning every prompt in old_to_new carries ("the
# driveway stays empty", "the garden keeps exactly the planting in the photograph").
# BOTH, deliberately: the positive pin is what the model actually follows, and this is
# the backstop for when it does not.
#
# Screened through screen_prompt like every other string we send, so a word fal refuses
# can never reach it even from here.
NO_FABRICATION_NEGATIVE = (
    "blur, distort, low quality, "
    "cars, vehicles, parked car, supercar, motorbike, bicycle, "
    "people, person, human figures, children, pets, animals, "
    "added furniture, extra rooms, new windows, new doors, "
    "added trees, added plants, new landscaping, new paving, "
    "signage, text, watermarks, logos, "
    "swimming pool, fountain, balcony that is not in the photograph"
)


class FalVideoNotConfigured(FalNotConfigured):
    """No FAL_KEY in the environment. The feature is off, not broken."""


def is_enabled():
    """True when a key is present.

    SEPARATE FROM THE FEATURE SWITCH ON PURPOSE. A key being present is what makes a
    clip POSSIBLE; it is not what makes the Elegant style REACHABLE. The key is already
    set in production for the Dramatic, so if this were the only gate, deploying this
    module would expose a half-finished paid feature to every agent. main.py's
    _elegant_available() is the switch, and it requires this AND an explicit opt-in AND
    the seconds tally columns.
    """
    return bool(os.environ.get(KEY_ENV, "").strip())


def billed_seconds(exc):
    """How many SECONDS of video fal may already have charged for before `exc` stopped
    the call.

    THE TALLY MUST COUNT THIS, NOT THE CLIPS THAT ARRIVED -- the same lesson fal_images'
    billed_images() records, in a unit fifteen times more expensive. A submit fal
    accepted is billed for its whole duration whether or not we ever collect the file, so
    "we received nothing" and "we were charged nothing" are different statements and the
    ceiling has to be built on the second one.

    Tolerant on purpose: `exc` is often an exception from an INJECTED generator (tests,
    a stub during development) that has never heard of this attribute, and "I do not
    know" has to mean zero rather than a crash inside a degradation handler.
    """
    try:
        return max(0, int(getattr(exc, "seconds_billed", 0) or 0))
    except (TypeError, ValueError):
        return 0


def clip_cost_usd(seconds):
    """What `seconds` of generated video costs. One place, so no number can drift."""
    return max(0, int(seconds or 0)) * COST_PER_SECOND_USD


def snap_seconds(seconds):
    """The nearest duration Kling will actually accept.

    Kling takes 5 or 10 and nothing else, so a caller asking for 7 would get a 422 --
    paid for nothing, and after the agent had already waited. Snapped here, at the
    boundary, rather than trusted to every call site.
    """
    try:
        wanted = int(seconds)
    except (TypeError, ValueError):
        return CLIP_SECONDS_SHORT
    if wanted <= CLIP_SECONDS_SHORT:
        return CLIP_SECONDS_SHORT
    if wanted >= CLIP_SECONDS_LONG:
        return CLIP_SECONDS_LONG
    # Between the two: round to the nearer, and on a tie take the CHEAPER one.
    midpoint = (CLIP_SECONDS_SHORT + CLIP_SECONDS_LONG) / 2.0
    return CLIP_SECONDS_SHORT if wanted <= midpoint else CLIP_SECONDS_LONG


def _submit(prompt, start_url, end_url, seconds, negative_prompt, deadline):
    """Ask Kling to generate. THIS IS THE ONLY BILLABLE CALL IN THIS MODULE.

    Returns a small dict describing what fal accepted: where to poll it and where to
    collect it, or -- when fal answered synchronously -- the finished payload itself.
    Everything in _collect works from that dict and cannot be billed, which is the whole
    reason the two are separate functions rather than one.
    """
    spec = model()
    # NO aspect_ratio FIELD, DELIBERATELY. v2.6/pro's schema does not have one -- an
    # image-to-video generation takes its shape from the keyframes -- and sending a field
    # an endpoint does not know is a 422 we would have waited for and, worse, a shape
    # change we would have to notice. The clip therefore arrives in the listing
    # photograph's own aspect, which is what we want anyway: the stills around it are
    # composed from the same photographs through the same contain-over-blur, so the shot
    # sits at exactly the same scale and the cut does not read as a jump (see geometry
    # mistake 1 in video_renderer).
    body = {
        "prompt": prompt,
        spec.start_field: start_url,
        "duration": str(int(seconds)),
        "negative_prompt": negative_prompt or NO_FABRICATION_NEGATIVE,
    }
    if end_url:
        # THE END FRAME IS THE WHOLE EFFECT. "Pick the end frame to be the state you want
        # reached" is the spec's own hardest-won line: anchored on the finished house the
        # model washes one picture into the other, and anchored on a cleared plot it has
        # no way there except by bringing the house down.
        body[spec.end_field] = end_url
    body.update(spec.extra_payload)

    submitted = fal_images._request(
        "POST", f"{QUEUE_BASE}/{spec.endpoint}", deadline, json_body=body,
        model=spec.endpoint)
    status_url = submitted.get("status_url")
    response_url = submitted.get("response_url")
    if status_url and response_url:
        return {"status_url": status_url, "response_url": response_url, "payload": None}
    if (submitted.get("video") or {}).get("url"):
        # fal answered synchronously. Nothing to poll, and the clip is already paid for.
        return {"status_url": None, "response_url": None, "payload": submitted}
    # fal took our request, said 200, and gave us no way to collect what we are being
    # charged for. PERMANENT on purpose, exactly as in fal_images: this is the one
    # failure where trying again means paying a second time for a response shape we do
    # not understand, and a second payment would not teach us anything the first did not.
    # Marked ambiguous as well, which costs nothing in retry policy and makes the BILLING
    # rule in generate_clip a single question: fal answered 200, so fal has the job, so we
    # were charged.
    raise fal_images._ambiguous(
        FalPermanent("fal accepted the clip but gave us nothing to poll"))


def _collect(accepted, deadline):
    """Poll an ALREADY SUBMITTED clip to completion and fetch its result payload.

    FREE. Every call in here is a GET against a job fal has already queued and already
    charged for, so a failure in this phase is worth going back for rather than paying
    for again -- see MAX_COLLECT_ATTEMPTS.
    """
    if accepted.get("payload") is not None:
        return accepted["payload"]

    endpoint = model().endpoint
    wait = POLL_FIRST_SECONDS
    while True:
        if time.monotonic() >= deadline:
            raise FalTemporary("fal was still generating the clip at the deadline")
        # Sleep BEFORE the first poll: a clip submitted a moment ago is certainly not
        # done -- these take minutes.
        time.sleep(min(wait, max(0.0, deadline - time.monotonic())))
        wait = min(wait * 1.4, POLL_MAX_SECONDS)
        state = fal_images._request("GET", accepted["status_url"], deadline,
                                    model=endpoint)
        status = str(state.get("status") or "").upper()
        if status == "COMPLETED":
            break
        if status in ("FAILED", "ERROR", "CANCELLED", "CANCELED"):
            # fal's own terminal failure. Its error text is the only clue to whether this
            # is a refusal or a fault, so classify on it rather than guessing --
            # _classify reads the text and quotes none of it.
            detail = str(state.get("error") or state.get("detail") or status)
            raise fal_images._classify(422, detail, endpoint)
    return fal_images._request("GET", accepted["response_url"], deadline, model=endpoint)


def generate_clip(prompt, start_url, end_url=None, seconds=CLIP_SECONDS_SHORT,
                  negative_prompt=None, deadline=None):
    """One generated clip's mp4 bytes. Raises a FalError; never returns None.

    `start_url` and `end_url` are PUBLIC urls fal fetches itself -- the listing bucket is
    already public -- so several megabytes of keyframe never pass through our process on
    the way out. `end_url` is optional; without it Kling invents where the shot ends,
    which is exactly the "washes one picture into the other" failure the spec warns
    about, so every real caller passes one.

    `seconds` is snapped to a duration Kling accepts. It is also WHAT WE ARE BILLED FOR:
    a 10-second clip is US$0.70 at the measured price, so the caller chooses it
    deliberately and the ceiling counts it.

    `deadline` is an absolute time.monotonic() value and bounds the WHOLE call, attempts
    and queue time included. Omitted, the call bounds itself at CLIP_DEADLINE_SECONDS. A
    caller generating several clips for one listing passes ONE SHARED deadline, so a slow
    first clip eats into the plan rather than extending the agent's wait six times over.
    """
    if not start_url:
        raise FalPermanent("a clip needs a first frame")
    seconds = snap_seconds(seconds)

    # Screened BEFORE the request, so a refusal costs nothing. Both strings: the positive
    # prompt is part model-written (old_to_new interpolates a description of the room
    # read off a photograph) and the negative prompt is ours, and either could carry a
    # word fal refuses. A hit here cannot succeed on a retry and degrades the shot.
    for label, text in (("prompt", prompt),
                        ("negative prompt", negative_prompt or NO_FABRICATION_NEGATIVE)):
        refused = screen_prompt(text)
        if refused:
            raise FalRefused(
                f"the {label} contains {refused!r}, which fal refuses")

    if not is_enabled():
        raise FalVideoNotConfigured(
            "video generation is not configured on this deploy (FAL_KEY is not set)")

    overall = (deadline if deadline is not None
               else time.monotonic() + CLIP_DEADLINE_SECONDS)
    started = time.monotonic()

    # THE STATE THAT MAKES A RETRY FREE -- fal_images' shape, and here it is worth
    # fifteen times as much. Each of these, once set, is carried across every later
    # attempt, so a failure is answered by going back for what we already bought rather
    # than buying it again.
    accepted = None
    payload = None
    clip_url = None

    submits = 0
    collects = 0
    last = None
    # WHAT FAL HAS CHARGED US FOR SO FAR IN THIS CALL, in seconds. Counted at the SUBMIT,
    # which is where the charge happens, and not at the download, which is where five
    # measured failure shapes never arrive (fal_images.billed_images lists them). A list
    # rather than an int so the handler at the bottom can read whatever the loop got to.
    billed = [0]
    try:
        while True:
            if time.monotonic() >= overall:
                raise last or FalTemporary("ran out of time before generating a clip")
            try:
                if accepted is None and payload is None:
                    submits += 1
                    try:
                        accepted = _submit(prompt, start_url, end_url, seconds,
                                           negative_prompt, overall)
                    except FalError as e:
                        # A submit fal ANSWERED with an error cost nothing -- it did not
                        # take the job. A submit that might have reached fal's queue (a
                        # deadline, a reset, a 200 we could not read, a 200 with nothing
                        # to poll) is counted as charged, because an uncertain bill has
                        # to read as a bill.
                        if e.may_be_queued:
                            billed[0] += seconds
                        raise
                    billed[0] += seconds
                if payload is None:
                    payload = _collect(accepted, overall)
                if clip_url is None:
                    clip_url = (payload.get("video") or {}).get("url")
                    if not clip_url:
                        # COMPLETED with no video. Permanent: the same prompt and the
                        # same keyframes get the same answer. IT IS ALSO BILLED -- the
                        # submit was accepted -- which is the quietest of the shapes that
                        # used to cost nothing on paper.
                        detail = payload.get("detail") or payload.get("description")
                        raise FalRefused(
                            "fal completed without producing a clip"
                            + (f": {fal_images._redact(detail)[:200]}" if detail else ""))
                data = fal_images._download(clip_url, overall,
                                            max_bytes=MAX_CLIP_BYTES, what="clip")
                if len(data) < MIN_CLIP_BYTES:
                    # Not a video. Treated as TEMPORARY rather than permanent: the clip
                    # is paid for, the fault is almost certainly a proxy or a truncated
                    # body rather than fal's answer, and going back for it is free.
                    raise FalTemporary(
                        "fal served %d bytes, which is too small to be a clip"
                        % len(data))
                logger.info(
                    "fal generated a %ds clip in %.0fs (%d KB, %s, %d submit(s), "
                    "about US$%.2f)",
                    seconds, time.monotonic() - started, len(data) // 1024,
                    model().endpoint, submits, clip_cost_usd(seconds))
                return data
            except FalPermanent:
                # Includes FalRefused. No retry: the answer will not change and a second
                # attempt would be billed for the same answer.
                raise
            except FalTemporary as e:
                last = e
                if accepted is None and payload is None:
                    # NOTHING COLLECTABLE YET. A rate limit here means fal has no room
                    # for us; hand it back so the whole JOB can wait for capacity (see
                    # fal_images.FalRateLimited) instead of burning the agent's wait on a
                    # full queue.
                    if isinstance(e, FalRateLimited):
                        raise
                    # Only resubmit when we can tell the first submit never arrived.
                    # Otherwise fal may be generating a clip we are already being charged
                    # for, and a second submit would charge us again for the same shot --
                    # which at this price is the most expensive mistake in the codebase.
                    if e.may_be_queued:
                        logger.warning(
                            "fal clip submit failed in a way that may already have "
                            "queued a %ds generation (%s); not submitting again",
                            seconds, e)
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
                logger.warning("fal clip generation failed (%s); %s", e,
                               "collecting a clip we have already paid for again"
                               if accepted is not None or payload is not None
                               else "submitting again")
                time.sleep(RETRY_BACKOFF_SECONDS)
    except FalError as e:
        # EVERY ERROR THIS FUNCTION RAISES LEAVES CARRYING WHAT IT COST, so no caller has
        # to reconstruct it from the exception type. old_to_new reads it with
        # billed_seconds().
        e.seconds_billed = billed[0]
        if billed[0]:
            logger.warning(
                "fal was charged for %ds of video we did not get (about US$%.2f) (%s)",
                billed[0], clip_cost_usd(billed[0]), e)
        raise
