"""Upload-time photo super-resolution -- the source-side half of the sharpness work.

WHY THIS EXISTS
Agents are *given* their photos by their agency, already portal-resized: the real-world
input is around 800x600 (an "rs_" prefix is the giveaway). There are no higher-res
originals to ask for. Every downstream artefact then has to STRETCH that:

    Classic video   800x600  ->  a 2808x3840 working frame   (_prepare_frame)
    Poster          800x600  ->  1200x1500                   (poster_renderer._fit)

Stretching invents pixels; downsampling preserves detail. If the stored photo is 3200x2400
instead, both renderers DOWNSAMPLE into their working size and the result is measurably
sharper -- roughly double, on a greyscale-minus-blur sharpness proxy, across four test
shots. That gain comes from the source, not from sharpening the output.

WHY AT UPLOAD, NOT AT RENDER
One 800x600 photo takes ~26s on Replicate. A listing holds up to 15. Doing it at render
time would add ~7 minutes to EVERY render and repeat it on every regeneration. Done once
at upload, every later artefact inherits it for free -- every Classic video, every
regeneration, and every poster.

RELIABILITY MODEL -- this is an upload WRITE path, so it is strictly additive
  * The upload stores the original and returns success BEFORE any of this runs. A listing
    is fully usable the instant the upload completes.
  * Upscaling happens on a background worker thread, one photo at a time.
  * The enhanced file is written ALONGSIDE the original, never over it:
        original   listings-images/{listing_id}/{name}.jpg
        enhanced   listings-images/{listing_id}/hires/{name}.jpg
    Nothing in the database changes. `listings.images` still points at the originals, so
    reorder, delete and rollback all keep working untouched, and "turn this off" is one
    env var -- the renderers simply stop looking in hires/.
  * Renderers ASK for the hires file and fall back to the original on any miss. A photo
    that was never upscaled, or whose upscale failed, is indistinguishable from today.
  * Every failure mode (not configured, timeout, HTTP error, quota, budget, fidelity
    rejection) degrades silently to the original and is logged with WHICH one it was.

CONCURRENCY
Replicate allows ONE prediction at a time on Jane's account, and Railway runs four uvicorn
workers. Rather than invent a distributed lock, each worker processes its own queue
strictly one photo at a time and treats HTTP 429 as "another worker has the slot" --
backing off with jitter and retrying. That is self-correcting, needs no shared state, and
survives a worker restart (an interrupted photo is simply never upscaled, which the
renderers already tolerate).

COMPLIANCE -- read this before changing anything here
Real-ESRGAN is a GAN. It RECONSTRUCTS texture, which sits close to Jane's standing
no-fabrication guardrail. Two things in this module exist solely because of that:
  * face_enhance is pinned OFF. That flag runs GFPGAN, which rebuilds facial features
    from a prior -- i.e. it can change what a person looks like. Never enable it.
  * Every result goes through _fidelity_report() before it is stored: the upscale is
    downsampled back to the source's own size and compared with the source tile by tile.
    A faithful upscale round-trips almost exactly; invented detail diverges. The measured
    figure is logged for EVERY photo so the threshold can be calibrated against real
    listings rather than guessed at.
The original is always retained and is always the record of what the property looks like.
"""
import io
import logging
import os
import queue
import random
import threading
import time
from datetime import datetime, timezone

import httpx
from PIL import Image, ImageChops, ImageStat

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration -- every one of these is an env var, and the default is OFF.
# ---------------------------------------------------------------------------
# Master switch. Deliberately opt-in: merging this module must not change production
# behaviour until Jane turns it on, and turning it off is the rollback.
ENABLED_ENV = "PHOTO_UPSCALE_ENABLED"
TOKEN_ENV = "REPLICATE_API_TOKEN"

REPLICATE_BASE = "https://api.replicate.com/v1"
REPLICATE_MODEL = os.environ.get("REPLICATE_UPSCALE_MODEL", "nightmareai/real-esrgan")
# Optional pinned version hash. Unset means "whatever Replicate currently calls latest".
# Worth pinning once the fidelity evidence (see module docstring) has been gathered:
# the compliance claim is a claim about a SPECIFIC model version, and an unannounced
# upstream retrain would invalidate it silently.
REPLICATE_VERSION = os.environ.get("REPLICATE_UPSCALE_VERSION", "").strip()

# The model card puts the maximum recommended INPUT at 1440p. 800x600 -- the real-world
# case -- is comfortably inside that at scale 4. Larger sources get a smaller factor, and
# anything already big enough is skipped: it has nothing to gain and is the most likely
# input to blow up the model's memory.
UPSCALE_SKIP_MIN_EDGE = 1600   # long edge at/above which we don't bother
UPSCALE_TARGET_EDGE = 3200     # what we are aiming the long edge at
UPSCALE_MAX_SCALE = 4

# Replicate returns a prediction that we then wait on. `Prefer: wait` holds the request
# open so the common case is a single round trip; if the account slot is busy the
# prediction queues and we fall back to bounded polling.
PREDICT_WAIT_SECONDS = 60
PREDICT_DEADLINE_SECONDS = 300   # hard ceiling per photo, queue time included
POLL_INTERVAL_SECONDS = 3
HTTP_TIMEOUT_SECONDS = 30

# 429 = someone else holds the account's single prediction slot. Back off with jitter so
# four uvicorn workers don't re-collide in lockstep.
#
# The retry window has to be sized against how long the slot can legitimately be held.
# One 15-photo listing occupies it for ~6.5 minutes, so two agents uploading at the same
# time means a ~13-minute wait -- and giving up sooner than that would silently deny the
# quality gain to whichever agent happened to be second. 8 retries with a 180s ceiling
# is a worst case of roughly 17 minutes (before jitter), which covers that collision.
# Past it the photo simply keeps its original, which is the whole degradation contract.
BUSY_MAX_RETRIES = 8
BUSY_BASE_BACKOFF_SECONDS = 20
BUSY_MAX_BACKOFF_SECONDS = 180

MAX_DOWNLOAD_BYTES = 40 * 1024 * 1024
MAX_SOURCE_PIXELS = 80_000_000   # same decompression-bomb guard the renderers use

# Stored at a high quality on purpose. The whole point of this file is detail we paid a
# cent for; re-encoding it at the originals' quality 80 would throw much of it away.
HIRES_JPEG_QUALITY = 92

# --- fidelity gate (see the compliance note in the docstring) -------------------------
# TILE SIZE IS THE WHOLE BALL GAME -- do not raise it. A changed house number occupies
# maybe 30x44 pixels of an 800x600 photo, so a coarse tile averages the evidence away
# against thousands of untouched pixels. Measured on a synthetic 800x600 room with a
# nameplate, comparing a faithful resample against one where "23A" was redrawn as "28A":
#
#     tile   faithful   hallucinated   margin
#       64     2.49        6.9-9.7       2.8x   <- useless, the first cut used this
#       32     2.93       11.9-20.0      4.1x
#       16     5.41      34.6-48.1       6.4x   <- chosen
#        8     8.77     111.0-114.9     12.6x   <- best margin, ~4x the CPU
#
# 16 keeps a comfortable margin at a quarter of 8's cost. See the report for the method.
FIDELITY_TILE = 16
# Mean absolute difference, 0-255, between the round-tripped upscale and the source.
#
# CALIBRATION STATUS: the tile size above is evidence-based; this threshold is NOT yet.
# The "faithful" baseline above was a pure LANCZOS resample, which round-trips almost
# perfectly. A real Real-ESRGAN output adds legitimate texture everywhere, so its honest
# baseline will sit higher than 5.4 -- how much higher is exactly what the per-photo
# logging below is for. Set loose on purpose, above any plausible honest baseline and
# still well under the ~35 floor of a detected hallucination. Tighten from the logs, not
# from intuition.
FIDELITY_MAX_TILE_MAE = 25.0
FIDELITY_MAX_GLOBAL_MAE = 8.0

# --- cost guards ----------------------------------------------------------------------
# Per listing: a listing can only ever hold MAX_LISTING_PHOTOS photos, so this caps the
# damage from an agent repeatedly re-uploading the same set. Counted per process.
MAX_UPSCALES_PER_LISTING = 30
# Per account, per calendar month, PER WORKER PROCESS. Railway runs four workers, so the
# real fleet ceiling is 4x this -- it is a tripwire, not an accountant. The authoritative
# hard cap belongs on Replicate's own billing spend limit, which cannot be outrun by a
# bug on our side. See the report accompanying this change.
MAX_UPSCALES_PER_MONTH = 3000

# Bounded so a burst can never grow the queue without limit. Full = we skip, log, and the
# listing renders from originals. Never blocks the request thread.
QUEUE_MAX = 400


# ---------------------------------------------------------------------------
# Public helpers -- these are what main.py and the renderers import
# ---------------------------------------------------------------------------
def is_enabled() -> bool:
    """True only when the switch is on AND a token is present. Both are required, so a
    half-configured deploy behaves exactly like an unconfigured one."""
    if os.environ.get(ENABLED_ENV, "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    return bool(os.environ.get(TOKEN_ENV, "").strip())


def hires_url_for(url: str):
    """Map a listing photo's public URL to where its upscaled twin would live, or None if
    this isn't a URL we own.

        .../listings-images/{listing_id}/3_ab12cd34_9f8e7d6c.jpg
     -> .../listings-images/{listing_id}/hires/3_ab12cd34_9f8e7d6c.jpg

    Derived rather than stored on purpose: there is no second array to keep in step with
    `listings.images`, so deleting or reordering a photo carries its hires twin along for
    free, and there is no way for the two to drift apart.

    Returns None -- meaning "just use the original" -- for anything unexpected. Every
    caller treats None and a failed fetch identically, so being wrong here is cheap.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        base, sep, query = url.partition("?")
        # Only ever rewrite paths inside our own listing-photo bucket.
        if "/listings-images/" not in base:
            return None
        if "/hires/" in base:
            return None  # already a hires URL; never nest hires/hires
        head, slash, name = base.rpartition("/")
        if not slash or not name:
            return None
        return f"{head}/hires/{name}{sep}{query}"
    except Exception:
        # A helper on the render path must never be the thing that fails a render.
        logger.exception("hires_url_for failed on a photo URL; using the original")
        return None


def _hires_storage_path(path: str):
    """Same mapping as hires_url_for, but for a storage object path
    ("{listing_id}/{name}.jpg" -> "{listing_id}/hires/{name}.jpg")."""
    if not path or "/hires/" in path:
        return None
    head, slash, name = path.rpartition("/")
    if not slash or not name:
        return None
    return f"{head}/hires/{name}"


# ---------------------------------------------------------------------------
# Budget accounting (process-local -- see MAX_UPSCALES_PER_MONTH)
# ---------------------------------------------------------------------------
_budget_lock = threading.Lock()
_budget_month = None
_budget_used = 0
_listing_counts = {}


def _budget_take() -> bool:
    """Claim one upscale from this process's monthly allowance. False means the tripwire
    has fired and we stop spending until the month rolls over."""
    global _budget_month, _budget_used
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    with _budget_lock:
        if month != _budget_month:
            _budget_month, _budget_used = month, 0
        if _budget_used >= MAX_UPSCALES_PER_MONTH:
            return False
        _budget_used += 1
        return True


def _listing_take(listing_id: str) -> bool:
    """Per-listing cap, so a stuck client re-uploading in a loop cannot run up a bill on
    one listing. Reset when the process restarts, which is the right trade: the cap
    exists to bound a runaway, not to be an exact ledger."""
    with _budget_lock:
        used = _listing_counts.get(listing_id, 0)
        if used >= MAX_UPSCALES_PER_LISTING:
            return False
        _listing_counts[listing_id] = used + 1
        # Cheap unbounded-growth guard: this dict only ever holds small ints.
        if len(_listing_counts) > 5000:
            _listing_counts.clear()
        return True


# ---------------------------------------------------------------------------
# Fidelity gate
# ---------------------------------------------------------------------------
def _fidelity_report(original: Image.Image, upscaled: Image.Image):
    """Round-trip check: shrink the upscale back to the source's own size and measure how
    far it has drifted from the source, globally and in the worst 64px tile.

    The reasoning: a faithful super-resolution is approximately a right-inverse of
    downsampling, so round-tripping it should land very close to where it started.
    Invented detail -- a stroke added to a character on a nameplate, a digit reshaped on a
    house number -- does not round-trip, and shows up as a local spike.

    Returns (global_mae, worst_tile_mae, worst_pixel). Raising is not an option on this
    path, so any failure returns a sentinel the caller treats as "cannot vouch for this".
    worst_pixel is logged but NOT gated on: it separated even more cleanly than the tile
    figure in testing, but a single hot pixel is far too easy to trip on legitimate GAN
    texture. It is recorded as a second calibration signal, nothing more.

    HONEST LIMIT, stated so nobody over-trusts this: it measures divergence, not
    plausibility. It caught a house number being redrawn as a different one only once the
    tile was small enough to stop averaging the evidence away -- and a subtler edit
    (a "3" gaining a serif rather than becoming an "8") will diverge less. It is a net
    for gross failures. It is not a proof of truthfulness, and the feature's real
    safeguard remains that the untouched original is always retained.
    """
    try:
        a = original.convert("L")
        b = upscaled.convert("L").resize(a.size, Image.LANCZOS)
        diff = ImageChops.difference(a, b)
        global_mae = ImageStat.Stat(diff).mean[0]
        worst_pixel = diff.getextrema()[1]

        worst = 0.0
        w, h = diff.size
        for top in range(0, h, FIDELITY_TILE):
            for left in range(0, w, FIDELITY_TILE):
                tile = diff.crop((left, top,
                                  min(left + FIDELITY_TILE, w),
                                  min(top + FIDELITY_TILE, h)))
                worst = max(worst, ImageStat.Stat(tile).mean[0])
        return global_mae, worst, worst_pixel
    except Exception:
        logger.exception("fidelity check failed; treating this upscale as unverifiable")
        return float("inf"), float("inf"), 255


# ---------------------------------------------------------------------------
# Replicate
# ---------------------------------------------------------------------------
class _Busy(Exception):
    """Replicate's single prediction slot is taken (429)."""


def _scale_for(width: int, height: int):
    """Pick the upscale factor, or None to skip this photo entirely."""
    long_edge = max(width, height)
    if long_edge <= 0:
        return None
    if long_edge >= UPSCALE_SKIP_MIN_EDGE:
        # Already has enough pixels for both renderers to downsample into; paying a cent
        # and 26 seconds to make it bigger buys nothing and risks a model OOM.
        return None
    scale = min(UPSCALE_MAX_SCALE, max(2, int(UPSCALE_TARGET_EDGE // long_edge)))
    return scale


def _auth_headers():
    """Built fresh per call and never logged. The token lives only in the process
    environment -- it is never written to a file, echoed, or included in an error."""
    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError("REPLICATE_API_TOKEN is not set")
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _predict(client: httpx.Client, image_url: str, scale: int) -> str:
    """Run one prediction and return the output image URL.

    `image` is handed to Replicate as the photo's own public Supabase URL rather than as
    inlined bytes: the bucket is already public (both renderers fetch from it unauthed),
    so this avoids pushing several MB through our own process for no benefit.
    """
    payload = {
        "input": {
            "image": image_url,
            "scale": scale,
            # NEVER turn this on. It runs GFPGAN, which reconstructs facial features from
            # a learned prior -- that is not sharpening a face, it is redrawing one, and
            # it is exactly what Jane's no-fabrication guardrail exists to prevent.
            "face_enhance": False,
        }
    }
    if REPLICATE_VERSION:
        url = f"{REPLICATE_BASE}/predictions"
        payload["version"] = REPLICATE_VERSION
    else:
        url = f"{REPLICATE_BASE}/models/{REPLICATE_MODEL}/predictions"

    started = time.monotonic()
    response = client.post(
        url, json=payload,
        headers={**_auth_headers(), "Prefer": f"wait={PREDICT_WAIT_SECONDS}"},
        # MUST outlast the Prefer: wait window. The client's default timeout is shorter
        # than PREDICT_WAIT_SECONDS, so without this override httpx would abort the
        # request at 30s -- every single prediction would look like a timeout while
        # Replicate was still happily working on it (and still billing for it).
        timeout=PREDICT_WAIT_SECONDS + HTTP_TIMEOUT_SECONDS,
    )
    if response.status_code == 429:
        raise _Busy("Replicate reported its prediction slot busy")
    response.raise_for_status()
    prediction = response.json()

    # `Prefer: wait` usually returns a finished prediction. If the account slot was busy
    # the prediction is queued instead, so poll -- bounded hard by the deadline, because
    # a stuck prediction must never pin this worker thread.
    while prediction.get("status") not in ("succeeded", "failed", "canceled"):
        if time.monotonic() - started > PREDICT_DEADLINE_SECONDS:
            raise RuntimeError(f"prediction still {prediction.get('status')} after "
                               f"{PREDICT_DEADLINE_SECONDS}s")
        time.sleep(POLL_INTERVAL_SECONDS)
        get_url = (prediction.get("urls") or {}).get("get")
        if not get_url:
            raise RuntimeError("prediction response carried no polling URL")
        poll = client.get(get_url, headers=_auth_headers())
        if poll.status_code == 429:
            raise _Busy("Replicate rate-limited the status poll")
        poll.raise_for_status()
        prediction = poll.json()

    if prediction.get("status") != "succeeded":
        raise RuntimeError(f"prediction {prediction.get('status')}: {prediction.get('error')}")

    output = prediction.get("output")
    if isinstance(output, list):
        output = output[0] if output else None
    if not output or not isinstance(output, str):
        raise RuntimeError("prediction succeeded but returned no image URL")
    return output


def _download(client: httpx.Client, url: str) -> bytes:
    """Streamed with a hard byte cap, same guard the video renderer uses on photos."""
    buffer = io.BytesIO()
    with client.stream("GET", url, timeout=HTTP_TIMEOUT_SECONDS) as response:
        response.raise_for_status()
        for chunk in response.iter_bytes(64 * 1024):
            buffer.write(chunk)
            if buffer.tell() > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"upscaled image exceeded "
                                 f"{MAX_DOWNLOAD_BYTES // 1024 // 1024}MB")
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------
_queue = queue.Queue(maxsize=QUEUE_MAX)
_worker = None
_worker_lock = threading.Lock()


def _hires_exists(storage, hires_path: str) -> bool:
    """Idempotence, and the cheapest cost guard there is: never pay twice for a photo we
    already upscaled. A re-upload, a retried batch or a restarted worker all land here."""
    try:
        head, _, name = hires_path.rpartition("/")
        listed = storage.list(head)
        return any(entry.get("name") == name for entry in (listed or []))
    except Exception as e:
        # Unknown means "go ahead": a duplicate upscale costs a cent, whereas wrongly
        # skipping means the photo silently never gets the quality gain.
        logger.warning("could not check for an existing hires file (%s); proceeding", e)
        return False


def _process_one(storage, listing_id: str, source_path: str, source_url: str):
    """One photo, start to finish. Returns a short reason string for the log."""
    hires_path = _hires_storage_path(source_path)
    if not hires_path:
        return "skipped: unrecognised storage path"
    if _hires_exists(storage, hires_path):
        return "skipped: already upscaled"

    with httpx.Client(timeout=HTTP_TIMEOUT_SECONDS, follow_redirects=True) as client:
        original_bytes = _download(client, source_url)
        original = Image.open(io.BytesIO(original_bytes))
        if original.width * original.height > MAX_SOURCE_PIXELS:
            return f"skipped: source is {original.width}x{original.height}"
        original.load()
        original = original.convert("RGB")

        scale = _scale_for(original.width, original.height)
        if scale is None:
            return f"skipped: {original.width}x{original.height} is already large enough"

        if not _budget_take():
            return "skipped: monthly upscale budget reached"
        if not _listing_take(listing_id):
            return "skipped: per-listing upscale cap reached"

        # 429 is "another worker has the account's one slot", not a failure.
        backoff = BUSY_BASE_BACKOFF_SECONDS
        output_url = None
        for attempt in range(BUSY_MAX_RETRIES):
            try:
                output_url = _predict(client, source_url, scale)
                break
            except _Busy:
                if attempt + 1 >= BUSY_MAX_RETRIES:
                    return "gave up: Replicate stayed busy"
                # Jitter matters: without it four workers retry in lockstep forever.
                time.sleep(backoff * (0.5 + random.random()))
                backoff = min(backoff * 2, BUSY_MAX_BACKOFF_SECONDS)
        if not output_url:
            return "gave up: Replicate stayed busy"

        upscaled_bytes = _download(client, output_url)

    upscaled = Image.open(io.BytesIO(upscaled_bytes))
    if upscaled.width * upscaled.height > MAX_SOURCE_PIXELS:
        return f"rejected: upscale is {upscaled.width}x{upscaled.height}"
    upscaled.load()
    upscaled = upscaled.convert("RGB")

    global_mae, tile_mae, worst_pixel = _fidelity_report(original, upscaled)
    # Logged for EVERY photo, pass or fail -- this is the corpus the threshold gets
    # calibrated from. Grep "upscale fidelity" in Railway to gather it.
    logger.info(
        "upscale fidelity: listing=%s photo=%s %dx%d->%dx%d scale=%s "
        "global_mae=%.2f worst_tile_mae=%.2f worst_pixel=%d",
        listing_id, source_path.rpartition("/")[2], original.width, original.height,
        upscaled.width, upscaled.height, scale, global_mae, tile_mae, worst_pixel,
    )
    if global_mae > FIDELITY_MAX_GLOBAL_MAE or tile_mae > FIDELITY_MAX_TILE_MAE:
        logger.warning(
            "upscale REJECTED on fidelity for listing %s photo %s "
            "(global_mae=%.2f limit=%.2f, worst_tile_mae=%.2f limit=%.2f) -- "
            "keeping the original",
            listing_id, source_path.rpartition("/")[2],
            global_mae, FIDELITY_MAX_GLOBAL_MAE, tile_mae, FIDELITY_MAX_TILE_MAE,
        )
        return "rejected: failed the fidelity check"

    buffer = io.BytesIO()
    upscaled.save(buffer, format="JPEG", quality=HIRES_JPEG_QUALITY, optimize=True)
    # Written to hires/, never over the original. upsert is safe here precisely because
    # the path is derived from the original's already-unique filename.
    storage.upload(hires_path, buffer.getvalue(),
                   {"content-type": "image/jpeg", "upsert": "true"})
    return f"stored {upscaled.width}x{upscaled.height}"


def _run():
    """The worker loop. One photo at a time, forever, and it must never die: an unhandled
    exception here would silently stop every future upscale in this process."""
    while True:
        item = _queue.get()
        try:
            listing_id, source_path, source_url = item
            started = time.monotonic()
            outcome = _process_one(_storage(), listing_id, source_path, source_url)
            logger.info("upscale %s for listing %s in %.0fs (queue depth %d)",
                        outcome, listing_id, time.monotonic() - started, _queue.qsize())
        except Exception as e:
            # Deliberately broad. Every failure here means one photo keeps its original,
            # which is exactly what the renderers already handle.
            logger.warning("upscale failed, keeping the original photo (%s: %s)",
                           type(e).__name__, e)
        finally:
            _queue.task_done()


_storage_factory = None


def _storage():
    if _storage_factory is None:
        raise RuntimeError("photo_upscale was never given a storage factory")
    return _storage_factory()


def configure(storage_factory):
    """main.py hands us a callable returning a Supabase storage client for the
    listings-images bucket. Injected rather than imported so this module has no import
    cycle with main.py and stays trivially testable."""
    global _storage_factory
    _storage_factory = storage_factory


def _ensure_worker():
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_run, name="photo-upscale", daemon=True)
            _worker.start()


def enqueue(listing_id: str, photos):
    """Queue photos for background upscaling. `photos` is [(storage_path, public_url)].

    Call this AFTER the upload has been committed. It never blocks, never raises, and
    never touches the caller's result -- the upload has already succeeded by the time we
    get here and nothing this function does is allowed to change that.

    Photos are queued in listing order, which is also roughly the order that matters:
    the Classic video uses the hero plus the first few, and the poster uses one. If the
    queue drains slowly, the photos most likely to be rendered are done first.
    """
    if not photos:
        return
    if not is_enabled():
        return
    try:
        _ensure_worker()
        queued = dropped = 0
        for source_path, source_url in photos:
            try:
                _queue.put_nowait((listing_id, source_path, source_url))
                queued += 1
            except queue.Full:
                dropped += 1
        if dropped:
            logger.warning(
                "upscale queue full: %d photo(s) for listing %s keep their originals",
                dropped, listing_id,
            )
        if queued:
            logger.info("queued %d photo(s) for upscaling (listing %s, queue depth %d)",
                        queued, listing_id, _queue.qsize())
    except Exception:
        logger.exception("could not queue photos for upscaling; originals are unaffected")
