"""NestList property video renderer -- the production "Classic tier" listing video.

Turns a listing's photos into a 1080x1920 (9:16) cinematic slideshow: alternating Ken
Burns motion, one expressive AI-written room caption per photo, a soft piano bed, and a
closing contact card built from the agent's own profile.

Vertical is the only output format, because these videos are posted to Facebook,
Instagram and TikTok. Since listing photos are overwhelmingly landscape, the frame is
always filled by a cover-crop and the motion drifts horizontally across whatever width
the photo has to spare -- a tall frame travelling over a wide room is the shot that
reads best on Reels, and it recovers the sides a static 9:16 crop would simply lose.

This is a port of the recipe validated over three weeks of prototyping (see
prototypes/classic_build.py, caption_signature.py, contact_card.py, assemble_v28.py).
The numbers below -- zoom increment, zoom ceiling, caption size and position, music
volume and fade lengths, card timings -- are the validated values; treat them as tuned
constants rather than knobs.

RELIABILITY MODEL
Every optional stage degrades instead of failing. The ladder, worst-case last:
    music fails      -> silent video
    captions fail    -> caption-free slideshow
    card fails       -> slideshow with no closing card
    a photo fails    -> that photo is skipped
Only "no usable photos at all" raises. Which degradations fired is returned to the
caller (and logged) so a quietly-degraded render is visible in Railway's logs rather
than looking like a normal success.

Requires the `ffmpeg` binary (with `ffprobe`) on PATH -- see railpack.json.
"""
import base64
import io
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Validated prototype constants
# ---------------------------------------------------------------------------
VW, VH = 1080, 1920  # 9:16 -- these videos are posted to Reels, Stories and TikTok
FPS = 24
SECONDS_PER_PHOTO = 5.0
FRAMES_PER_PHOTO = int(SECONDS_PER_PHOTO * FPS)  # 120

# Motion works on an upscaled copy of the photo: at native size the per-frame crop
# rectangle lands on whole pixels and slow motion visibly stair-steps. Upscaling first
# gives the crop sub-pixel resolution. 2x is the balance point for a 1080x1920 frame --
# the prototype's 4x was tuned for a much smaller 1440x1080 canvas, and at 9:16 the
# same factor would mean a 33MP filter frame (~100MB) per concurrent render.
UPSCALE = 2

# --- horizontal pan (the usual case) ---
# Listing photos are overwhelmingly landscape, so a 9:16 frame can only ever show a
# slice of one. Rather than throw the rest away, the frame keeps a working strip 1.3x
# its own width and drifts across it: a vertical frame travelling over a wide room is
# the motion that reads best on Reels. Slack is 0.30 x 1080 = 324px over 5s (~65px/s),
# a slow drift rather than a whip pan.
PAN_WIDTH_RATIO = 1.30
# A photo needs this much width (relative to its height) to have anything spare to pan
# across. Anything squarer than ~3:4 qualifies; taller portrait shots fall back to zoom.
PAN_MIN_ASPECT = (VW / VH) * PAN_WIDTH_RATIO

# --- zoom fallback (portrait/tall sources with no width to spare) ---
ZOOM_STEP = 0.0009
ZOOM_MAX = 1.13

# ---------------------------------------------------------------------------
# STYLE B -- "dissolve": contain-over-blur with soft crossfades.
# A bake-off variant, NOT the default. Style A fills the frame by cropping and
# drifts across the photo; style B shows each photo whole, centred over a blurred
# copy of itself, and dissolves between them. Jane picks the house style.
# ---------------------------------------------------------------------------
STYLE_B_ID = "dissolve"
# The drift-cut look (cover-crop + horizontal pan + hard cuts) that shipped first.
# Jane picked the dissolve style after the bake-off, so this is now reachable only by
# naming it explicitly -- it is kept because it costs nothing to keep, it is the
# fallback if the dissolve ever misbehaves on a listing, and the pan code is the only
# thing that handles a photo whose width we actively want to travel across.
STYLE_A_ID = "driftcut"
# 1.25s = exactly 30 frames at 24fps. The round frame count matters: the dissolve is
# assembled by cutting clips on frame boundaries (see _dissolve_segments), and a
# transition length that isn't a whole number of frames cannot be cut cleanly.
XFADE_SECONDS = 1.25
XFADE_FRAMES = int(XFADE_SECONDS * FPS)  # 30
# The zoom here is deliberately far gentler than style A's 1.13. It magnifies the
# WHOLE composite, so every extra percent crops a little off the edges of a photo
# whose entire point is being shown whole. At 1.06 the photo is complete at one end
# of every clip and loses 3% a side at the other -- motion without visibly eating
# the picture.
CONTAIN_ZOOM_MAX = 1.06
CONTAIN_ZOOM_STEP = round((CONTAIN_ZOOM_MAX - 1.0) / FRAMES_PER_PHOTO, 6)
# Backdrop tuning. The first cut used radius 28 at quarter scale (~112px at full
# resolution) plus 42% black, which turned every bright interior into the same flat
# grey -- the backdrop stopped reading as the photo at all. A moderate blur with only
# slight darkening keeps the room's actual colour and shape recognisable behind the
# sharp photo, which is the whole point of the technique.
CONTAIN_BLUR_RADIUS = 11    # at quarter scale, so ~44px at full resolution
CONTAIN_BG_DARKEN = 0.18    # just enough that the sharp photo still separates

OPEN_FADE_SECONDS = 0.75  # first photo fades up from black; never a cold first frame

# Closing card, assembled by the still-frame method (see _build_card_clip).
CARD_SECONDS = 5.5
CARD_XFADE_SECONDS = 1.2
CARD_HOLD_SECONDS = 0.3   # frozen last frame held before the dissolve starts
CARD_TAIL_SECONDS = 1.6   # length of the frozen-frame input feeding the dissolve

AUDIO_VOLUME = 0.22
AUDIO_FADE_IN_SECONDS = 1.5
AUDIO_FADE_OUT_SECONDS = 4.5

# Each photo is one sequential ffmpeg encode. The previous renderer also folded clips
# together with N-1 progressive xfade passes, each one re-encoding the whole
# accumulated video -- that cascade, not the per-photo encode, is what forced the cap
# down to 5. This pipeline concatenates with a stream copy instead (no re-encode at
# all), so the same CPU budget comfortably covers the 6 shots the prototype was
# validated at. Still deliberately well below the listing's 15-photo upload cap
# (MAX_LISTING_PHOTOS in main.py), which has no per-photo rendering cost.
MAX_VIDEO_PHOTOS = 6

# Ceiling on renders running at once *in this worker process*. Railway runs 4 uvicorn
# workers, so the fleet ceiling is 4x this. Renders are background jobs, so waiting is
# cheap and far better than 50 agents each holding an ffmpeg process and taking the
# instance down. Past the wait window the job fails with a retryable message rather
# than queueing behind the listing's 10-minute stale-render lock.
#
# Set to 1 after measuring one clip render at ~800MB peak RSS (both styles, macOS).
# At 2 the fleet worst case was 8 concurrent x ~800MB = ~6.6GB, uncomfortably close to
# the instance limit; at 1 it is 4 x ~800MB = ~3.3GB.
#
# The other candidate lever -- lowering UPSCALE -- was measured and rejected: peak RSS
# is essentially flat across upscale factors (986MB at 1x, 766MB at 2x, 876MB at 3x),
# so the cost is dominated by the x264 encoder and frame buffering at 1080x1920, not
# by the source frame. Dropping to 1x would have coarsened the slow zoom for no memory
# saving at all.
MAX_CONCURRENT_RENDERS = 1
RENDER_SLOT_WAIT_SECONDS = 240
_RENDER_SLOTS = threading.Semaphore(MAX_CONCURRENT_RENDERS)

# Whole-job wall-clock budget. Past this we stop adding photos and finish with what is
# already rendered -- a shorter video beats a listing stuck in 'rendering'.
RENDER_BUDGET_SECONDS = 420

# Per-step ffmpeg timeouts (seconds).
TIMEOUT_CLIP = 150
TIMEOUT_CONCAT = 90
TIMEOUT_XFADE_CHAIN = 300  # style B re-encodes the whole body in this one pass
TIMEOUT_CARD = 120
TIMEOUT_MUSIC = 120
TIMEOUT_PROBE = 30

_HERE = os.path.dirname(os.path.abspath(__file__))

# "Piano Soft Gentle Morning Keys" by Alex Morgan, from Pixabay, whose Content License
# permits commercial use (including social video) without attribution -- see
# audio/LICENSE-music.txt. Byte-identical to the track validated in the prototypes.
AUDIO_PATH = os.path.join(_HERE, "audio", "soft_piano.mp3")

FONT_DIR = os.path.join(_HERE, "fonts")
# Gelasio is metric-compatible with Georgia, which the prototypes used (a macOS system
# font we cannot ship). Same string at the same size measures identically in both, so
# the prototype's fontsize/position numbers carry over exactly -- and Gelasio is OFL,
# so it can live in the repo. See fonts/OFL-Gelasio.txt.
SERIF_ITALIC = os.path.join(FONT_DIR, "Gelasio-Italic-Variable.ttf")
SERIF_REGULAR = os.path.join(FONT_DIR, "Gelasio-Variable.ttf")

# Contact card palette (prototypes/contact_card.py).
CARD_BG = (250, 248, 245)     # warm off-white
CARD_INK = (43, 43, 43)       # soft charcoal
CARD_GOLD = (176, 141, 87)    # quiet gold accent

# Captions sit in the lower third but well clear of the bottom ~350px, where Reels and
# TikTok stack their own UI (caption text, handle, action rail, progress bar). Anything
# drawn down there is covered by the platform on at least one network. The block is two
# lines split at the " ... ", with its LAST line topped at CAPTION_LAST_LINE_Y, so the
# block grows upward and the clearance below never changes.
CAPTION_FONT_SIZE = 54
CAPTION_MIN_FONT_SIZE = 38
CAPTION_LAST_LINE_Y = 1460          # 1920 - 1460 = 460px of clearance below the text
CAPTION_LINE_SPACING = 1.32         # multiple of font size
CAPTION_SIDE_MARGIN = 60            # so max text width is 1080 - 120 = 960

# Identical encoder settings on every clip, so the final concat can be a stream copy
# (no re-encode) without mismatched stream parameters.
_ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "fast", "-crf", "18",
    "-pix_fmt", "yuv420p", "-r", str(FPS), "-vsync", "cfr", "-an",
    # A keyframe every 30 frames (1.25s), with scene detection off so the spacing is
    # exact. Style B's dissolve assembly stream-copies clip segments starting at frame
    # 30, and a stream copy can only start on a keyframe -- without this the cut would
    # silently snap back to frame 0. Harmless for style A, and it makes both styles
    # more responsive to scrub on social players.
    "-g", str(XFADE_FRAMES), "-sc_threshold", "0",
]

CAPTION_MODEL = "claude-sonnet-4-5"  # same model main.py uses for listing copy
CAPTION_TIMEOUT_SECONDS = 60
CAPTION_MAX_CHARS = 72
CAPTION_SEPARATOR = " ... "

# Superlatives the listing-copy rules already ban as unsubstantiated. A caption that
# reaches for one is dropped rather than rewritten -- one photo silently loses its
# caption, which is invisible to the viewer, whereas a hedge-word rewrite would not be.
_BANNED_CAPTION_WORDS = {
    "luxurious", "luxury", "prestigious", "coveted", "exclusive", "best", "finest",
    "stunning", "unrivalled", "unrivaled", "world-class", "premier", "ultimate",
    "epitome", "unparalleled", "iconic", "sought-after", "magnificent", "spectacular",
    "opulent", "lavish", "breathtaking", "sprawling", "boasts", "nestled",
}
_PRICE_TOKENS = ("$", "sgd", "psf", "price", "priced", "psm", "£", "€", "usd")


# ---------------------------------------------------------------------------
# ffmpeg helpers
# ---------------------------------------------------------------------------
def _run_ffmpeg(cmd, timeout):
    """subprocess.run's default CalledProcessError str() is just the command and exit
    code -- useless for diagnosing an actual ffmpeg failure. This surfaces the real
    stderr (truncated) so both Railway's logs and the API error response say what
    actually went wrong."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else "(no stderr captured)"
        raise RuntimeError(f"ffmpeg exited {e.returncode}: {stderr[-1500:]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s") from e


def _probe_duration(path, fallback):
    """Real duration of a rendered file. Concat and xfade both shift duration slightly
    off the arithmetic estimate, and the music fade-out has to start 4.5s before the
    *actual* end or it fades over the wrong thing. Falls back to the estimate if
    ffprobe is missing or unparseable -- a slightly mistimed fade is not worth failing
    an otherwise-finished video over."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, timeout=TIMEOUT_PROBE, check=True,
        )
        value = float(out.stdout.decode().strip())
        if value > 0:
            return value
    except Exception as e:
        logger.warning("ffprobe failed on %s (%s); using estimated duration %.2fs", path, e, fallback)
    return fallback


def _escape_filter_path(path):
    """Paths land inside an ffmpeg filter argument, where ':' separates options and
    '\\' escapes. Our own paths never contain either, but the font/text files are
    joined onto whatever temp root the OS hands us, so escape rather than assume."""
    return path.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


# ---------------------------------------------------------------------------
# Photo fetching and normalization
# ---------------------------------------------------------------------------
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
MAX_SOURCE_PIXELS = 80_000_000  # decompression-bomb / phone-panorama guard


def _fetch_image(url, attempts=2):
    """Downloads one photo. Streams with a hard byte cap so a mis-uploaded 200MB file
    can't exhaust memory, and retries once because a single flaky CDN read shouldn't
    cost the agent a whole video."""
    last_error = None
    for attempt in range(attempts):
        try:
            with requests.get(url, timeout=(5, 25), stream=True) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"photo is {int(declared) // 1024 // 1024}MB, over the {MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit")
                buffer = io.BytesIO()
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    buffer.write(chunk)
                    if buffer.tell() > MAX_DOWNLOAD_BYTES:
                        raise ValueError(f"photo exceeded the {MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit while downloading")
            buffer.seek(0)
            img = Image.open(buffer)
            if img.width * img.height > MAX_SOURCE_PIXELS:
                raise ValueError(f"photo is {img.width}x{img.height}, too large to render")
            img.load()
            # Phone photos carry their rotation in EXIF; without this a portrait shot
            # renders on its side.
            return ImageOps.exif_transpose(img)
        except Exception as e:
            last_error = e
            if attempt + 1 < attempts:
                time.sleep(1.0)
    raise RuntimeError(f"could not fetch photo: {last_error}")


def _prepare_frame(img, centering=(0.5, 0.42)):
    """Cover-crops a photo to the working size for its motion, returning
    (image, can_pan).

    The frame is always filled edge to edge -- no letterbox, no bars. A photo with
    width to spare is cropped to a strip PAN_WIDTH_RATIO times wider than the frame so
    the pan has somewhere to travel; a tall photo with nothing spare is cropped square
    to the frame and gets the zoom instead.

    Vertical centering sits slightly above middle: in a 9:16 crop of a room shot, the
    ceiling is the least interesting band and dead-centring tends to cut the floor.
    """
    img = img.convert("RGB")
    src_w, src_h = img.size
    can_pan = (src_w / src_h) >= PAN_MIN_ASPECT

    out_w = round(VW * PAN_WIDTH_RATIO) if can_pan else VW
    target = (out_w * UPSCALE, VH * UPSCALE)
    # ImageOps.fit scales to cover the target and crops the overflow -- never pads.
    return ImageOps.fit(img, target, method=Image.LANCZOS, centering=centering), can_pan


def _contain_over_blur(img):
    """STYLE B frame: the whole photo, centred over a blurred, darkened copy of
    itself. Nothing is cropped out of the foreground -- the sides a 9:16 cover-crop
    would lose are all still there, and the backdrop fills the frame so there are no
    bars.

    The blur is done at thumbnail size and then scaled up, not applied at full
    resolution. A 28px Gaussian over a 2160x3840 canvas is enormously expensive and
    the result is indistinguishable -- blurring is destroying detail, so doing it to
    a small copy and enlarging that produces the same image for a fraction of the
    cost.
    """
    img = img.convert("RGB")
    w, h = VW * UPSCALE, VH * UPSCALE

    small = ImageOps.fit(img, (VW // 4, VH // 4), method=Image.LANCZOS, centering=(0.5, 0.5))
    small = small.filter(ImageFilter.GaussianBlur(CONTAIN_BLUR_RADIUS))
    background = small.resize((w, h), Image.LANCZOS)
    background = Image.blend(background, Image.new("RGB", (w, h), (0, 0, 0)),
                             CONTAIN_BG_DARKEN)

    # Contain: full width, natural height, centred. A photo taller than the frame
    # (rare, but a phone panorama shot vertically would be) is bounded by height
    # instead so it still fits whole.
    src_w, src_h = img.size
    fg_w = w
    fg_h = max(1, round(w * src_h / src_w))
    if fg_h > h:
        fg_h = h
        fg_w = max(1, round(h * src_w / src_h))
    foreground = img.resize((fg_w, fg_h), Image.LANCZOS)
    background.paste(foreground, ((w - fg_w) // 2, (h - fg_h) // 2))
    return background


# ---------------------------------------------------------------------------
# Expressive room captions (one batched Claude vision call per video)
# ---------------------------------------------------------------------------
_CAPTION_PROMPT = """You are writing the on-screen captions for a Singapore property
listing video. You have been shown {n} photos of one property, in order.

For each photo, identify the room or space, then write ONE caption in exactly this
style:

    the living hall ... where laughter lingers and memories are made
    the dining hall ... where every meal becomes a gathering
    the master suite ... where your private sanctuary awaits
    quiet mornings ... where rest comes easy
    the lap pool ... where every day feels like a getaway
    the terrace ... where golden hours drift by

Rules, no exceptions:
- All lowercase. No capital letters at all, no full stop at the end.
- Exactly one " ... " separator: a short name for the space, then an evocative phrase
  about how it feels to live there.
- Under 65 characters in total.
- Plain English letters only. No emoji, no accents, no quotation marks, no colons.
- NEVER mention a price, a figure, a house number, a unit number, or any digit.
- NEVER use unsubstantiated superlatives: luxurious, prestigious, coveted, exclusive,
  stunning, best, finest, world-class, iconic, sought-after, breathtaking, or similar.
  Describe what is actually visible and how it feels, not status.
- If a photo shows an exterior, a view, or a detail rather than a room, name that
  instead ("the terrace", "quiet mornings", "the garden path").

Reply with JSON only, no other text, in exactly this shape:
{{"captions": [{{"n": 1, "caption": "..."}}, {{"n": 2, "caption": "..."}}]}}
with one entry per photo, in order, for all {n} photos."""


def _photo_to_block(img, max_side=768, quality=72):
    """Downscales a copy of the photo for the vision call. Full-size listing photos are
    2-4MB each; at 6 photos that is a slow, expensive request for no benefit -- 768px
    is ample for "which room is this"."""
    small = img.convert("RGB").copy()
    small.thumbnail((max_side, max_side), Image.LANCZOS)
    buffer = io.BytesIO()
    small.save(buffer, format="JPEG", quality=quality, optimize=True)
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": "image/jpeg",
            "data": base64.standard_b64encode(buffer.getvalue()).decode("ascii"),
        },
    }


def _caption_is_safe(caption):
    """Second layer over the prompt rules. The prompt asks; this enforces. A caption
    that fails any check is dropped (that photo renders bare) rather than patched --
    the guarantee agents need is that nothing unvetted is burned into a video, and a
    missing caption on one photo is invisible to a viewer."""
    if not caption:
        return False, "empty"
    if len(caption) > CAPTION_MAX_CHARS:
        return False, "too long"
    if CAPTION_SEPARATOR not in caption:
        return False, "wrong format"
    if not caption.isascii() or not caption.isprintable():
        return False, "non-ascii"
    if re.search(r"\d", caption):
        # Covers house numbers, unit numbers, prices, floor areas -- all at once.
        return False, "contains a digit"
    lowered = caption.lower()
    if any(token in lowered for token in _PRICE_TOKENS):
        return False, "price-adjacent wording"
    words = set(re.findall(r"[a-z-]+", lowered))
    hit = words & _BANNED_CAPTION_WORDS
    if hit:
        return False, f"superlative ({', '.join(sorted(hit))})"
    if any(c in caption for c in "'\"\\:%"):
        # Nothing in the validated style needs these, and they are exactly the
        # characters that make drawtext arguments ambiguous.
        return False, "unsupported punctuation"
    return True, ""


def _generate_room_captions(photos, copy_guard=None):
    """One batched vision call for the whole video: all photos in, one caption each
    out. Returns a list the same length as `photos`, with None wherever no safe caption
    could be produced. Never raises -- a caption-free video is a valid outcome.

    Batching matters for more than cost: it is one network dependency and one timeout
    for the whole render, instead of one per photo, and it lets the model see the
    property as a set so it does not caption three different rooms "the living hall"."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    backup_key = os.environ.get("ANTHROPIC_API_KEY_BACKUP", "")
    if not api_key and not backup_key:
        logger.warning("captions skipped: no Anthropic API key configured")
        return [None] * len(photos), "no API key"

    content = []
    for i, photo in enumerate(photos):
        content.append({"type": "text", "text": f"Photo {i + 1}:"})
        content.append(_photo_to_block(photo))
    content.append({"type": "text", "text": _CAPTION_PROMPT.format(n=len(photos))})

    raw = None
    last_error = None
    for key in [k for k in (api_key, backup_key) if k]:
        try:
            import anthropic
            # max_retries=1: the render already has a wall-clock budget, and the SDK's
            # default of 2 retries on top of a 60s timeout can burn three minutes on a
            # degraded API before we fall back to no captions.
            client = anthropic.Anthropic(
                api_key=key, timeout=CAPTION_TIMEOUT_SECONDS, max_retries=1
            )
            response = client.messages.create(
                model=CAPTION_MODEL,
                max_tokens=1024,
                messages=[{"role": "user", "content": content}],
            )
            raw = "".join(b.text for b in response.content if getattr(b, "type", "") == "text")
            break
        except Exception as e:
            last_error = e
            continue

    if raw is None:
        logger.warning("captions skipped: Claude call failed (%s)", last_error)
        return [None] * len(photos), f"AI call failed ({type(last_error).__name__})"

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(match.group(0) if match else raw)
        entries = parsed["captions"]
    except Exception as e:
        logger.warning("captions skipped: could not parse model reply (%s)", e)
        return [None] * len(photos), "AI reply unparseable"

    captions = [None] * len(photos)
    dropped = 0
    for entry in entries:
        try:
            index = int(entry["n"]) - 1
            text = str(entry["caption"]).strip().lower()
        except Exception:
            dropped += 1
            continue
        if not 0 <= index < len(photos):
            dropped += 1
            continue
        text = re.sub(r"\s*\.\.\.\s*", CAPTION_SEPARATOR, text)
        text = re.sub(r"\s+", " ", text).strip(" .")
        if copy_guard:
            # The same guard every other copy surface runs through (house/unit numbers,
            # price leaks). It never raises and never returns empty.
            try:
                text = (copy_guard(text, context="video-caption") or "").strip().lower()
            except Exception as e:
                logger.warning("caption copy guard failed (%s); dropping caption", e)
                dropped += 1
                continue
        ok, reason = _caption_is_safe(text)
        if not ok:
            logger.warning("caption %d rejected (%s): %r", index + 1, reason, text)
            dropped += 1
            continue
        captions[index] = text

    note = None
    if dropped:
        note = f"{dropped} caption(s) rejected by the copy guards"
    if all(c is None for c in captions):
        note = "every caption was rejected"
    return captions, note


# ---------------------------------------------------------------------------
# Clip rendering
# ---------------------------------------------------------------------------
def _caption_lines(caption):
    """Splits a caption at its " ... " into two centred lines. At 1080 wide a 54px
    serif runs out of room around 40 characters, so the validated one-line treatment
    from the 1440-wide prototype would overflow the frame; breaking at the separator is
    where the phrase already wants to breathe anyway."""
    head, _, tail = caption.partition(CAPTION_SEPARATOR)
    lines = [f"{head.strip()} ...", tail.strip()] if tail else [caption]
    return [ln for ln in lines if ln]


def _caption_font_size(captions):
    """ONE font size for every caption in a video -- the largest that fits the longest
    line of any of them. Sizing each caption independently would fit more text on the
    wordy ones, but the type would visibly change size from photo to photo, which reads
    as a mistake. Consistent typography is worth a few points on the long ones."""
    lines = [ln for c in captions if c for ln in _caption_lines(c)]
    if not lines:
        return CAPTION_FONT_SIZE
    max_width = VW - 2 * CAPTION_SIDE_MARGIN
    size = CAPTION_FONT_SIZE
    while size > CAPTION_MIN_FONT_SIZE:
        font = ImageFont.truetype(SERIF_ITALIC, size)
        if max(font.getlength(ln) for ln in lines) <= max_width:
            break
        size -= 2
    return size


def _caption_filters(caption, caption_size, workdir, index, alpha_expr=None,
                     enable_expr=None):
    """The drawtext filters for one caption, shared by both styles so the caption
    treatment can never drift between them during the bake-off.

    alpha_expr ramps the text in and out. Style A doesn't need it (the whole frame
    fades up from black, caption included); style B uses it to keep each caption
    inside its own clip's non-overlap window so it never double-strikes through a
    dissolve.
    """
    if not caption:
        return []
    alpha = f":alpha='{alpha_expr}'" if alpha_expr else ""
    enable = f":enable='{enable_expr}'" if enable_expr else ""
    # One drawtext per line, each independently centred. drawtext's own multi-line
    # handling left-aligns lines within the block unless the `text_align` option is
    # present, which only exists in ffmpeg 7.0+ -- and Railway installs whatever
    # ffmpeg its base image ships. Two filters centre correctly on any version.
    lines = _caption_lines(caption)
    size = caption_size or CAPTION_FONT_SIZE
    line_height = round(size * CAPTION_LINE_SPACING)
    first_line_y = CAPTION_LAST_LINE_Y - (len(lines) - 1) * line_height
    out = []
    for line_no, line in enumerate(lines):
        text_path = os.path.join(workdir, f"cap_{index:02d}_{line_no}.txt")
        with open(text_path, "w", encoding="utf-8") as f:
            f.write(line)
        out.append(
            f"drawtext=fontfile={_escape_filter_path(SERIF_ITALIC)}"
            f":textfile={_escape_filter_path(text_path)}:expansion=none"
            f":fontcolor=white:fontsize={size}"
            f":x=(w-text_w)/2:y={first_line_y + line_no * line_height}"
            f":shadowcolor=black@0.6:shadowx=2:shadowy=2{alpha}{enable}"
        )
    return out


def _contain_clip(photo, out_path, workdir, index, caption=None, caption_size=None,
                  zoom_in=True, first=True, last=True):
    """STYLE B: one photo -> one 5s contain-over-blur clip with a gentle zoom.

    Every clip, the first included, opens fully composed -- sharp photo already in
    place over its backdrop. Jane rejected the earlier fade-up-over-backdrop opening
    ("i don't want the video to open with a blur screen"): the video starts cold on
    slide one.
    """
    composite = _contain_over_blur(photo)
    comp_path = os.path.join(workdir, f"b_comp_{index:02d}.bmp")
    composite.save(comp_path, format="BMP")

    if zoom_in:
        z = f"min(zoom+{CONTAIN_ZOOM_STEP},{CONTAIN_ZOOM_MAX})"
    else:
        z = f"if(lte(zoom,1.0),{CONTAIN_ZOOM_MAX},max(zoom-{CONTAIN_ZOOM_STEP},1.0))"
    # d=1 (one output frame per input frame): the looped still arrives as a frame
    # stream, so the zoom accumulates per frame rather than inside one d=120 hold.
    zoompan = (f"zoompan=z='{z}':d=1:s={VW}x{VH}:fps={FPS}"
               f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'")
    # The caption lives only inside the part of the clip that is NOT shared with a
    # neighbouring clip. Consecutive clips overlap for the length of the dissolve, so a
    # caption drawn across that region cross-fades through the next one and renders as
    # unreadable double-struck text (confirmed on the first style B cut). Keeping it
    # inside the clip's own window means only ever one caption on screen.
    start = 0.0 if first else XFADE_SECONDS
    end = SECONDS_PER_PHOTO if last else SECONDS_PER_PHOTO - XFADE_SECONDS
    caption_alpha = (
        f"max(0,min(1,min((t-{start:.2f})/0.35,({end:.2f}-t)/0.35)))"
    )
    tail = ",".join(
        [zoompan, "setsar=1"]
        + _caption_filters(caption, caption_size, workdir, index,
                           alpha_expr=caption_alpha,
                           enable_expr=f"between(t,{start:.2f},{end:.2f})")
        + ["format=yuv420p"])

    inputs = ["-loop", "1", "-framerate", str(FPS), "-t", str(SECONDS_PER_PHOTO),
              "-i", comp_path]
    cmd = (["ffmpeg", "-y"] + inputs + ["-vf", tail,
                                        "-frames:v", str(FRAMES_PER_PHOTO)]
           + _ENCODE_ARGS + [out_path])
    _run_ffmpeg(cmd, TIMEOUT_CLIP)


def _dissolve_segments(clip_paths, out_path, workdir, timeout):
    """Assembles the clips into one dissolved timeline with BOUNDED memory.

    The obvious implementation -- one filtergraph with every clip as an input and the
    xfades chained together -- was measured and rejected. Chained xfades buffer their
    upstream frames until each fade's offset arrives, so memory grows with the photo
    count rather than staying flat: 2.4GB peak at 6 photos and 4.5GB at 15, on a
    Railway instance that has nowhere near that. It also repeats the mistake the old
    renderer's docstring already warned about.

    Instead the timeline is cut into pieces, and only the joins are re-encoded:

        [clip0 frames 0-90] [dissolve] [clip1 frames 30-90] [dissolve] ... [clipN 30-120]

    A dissolve is its own tiny ffmpeg call over just the 1.25s tail of one clip and the
    1.25s head of the next -- two short inputs, no matter how many photos there are.
    Every other second of video is stream-copied, never re-encoded. Peak memory is
    therefore flat in the photo count, and total work is linear rather than the
    quadratic re-encode cascade the old renderer used.

    Stream-copying a segment that starts mid-clip is only exact because the clips are
    encoded with a keyframe every XFADE_FRAMES (see _ENCODE_ARGS).
    """
    body_start = XFADE_SECONDS                      # 1.25s -> frame 30 (a keyframe)
    body_end = SECONDS_PER_PHOTO - XFADE_SECONDS    # 3.75s -> frame 90

    segments = []
    for i, clip in enumerate(clip_paths):
        first, last = (i == 0), (i == len(clip_paths) - 1)
        body = os.path.join(workdir, f"seg_body_{i:02d}.mp4")
        cut = ["ffmpeg", "-y"]
        if not first:
            cut += ["-ss", f"{body_start:.3f}"]
        cut += ["-i", clip]
        if not last:
            # -t counts from the seek point, so the middle clips keep 2.5s and the
            # first keeps 3.75s.
            cut += ["-t", f"{(body_end - (0.0 if first else body_start)):.3f}"]
        cut += ["-c", "copy", body]
        _run_ffmpeg(cut, timeout)
        segments.append(body)

        if not last:
            join = os.path.join(workdir, f"seg_join_{i:02d}.mp4")
            _run_ffmpeg(
                ["ffmpeg", "-y",
                 "-ss", f"{body_end:.3f}", "-t", f"{XFADE_SECONDS:.3f}", "-i", clip,
                 "-t", f"{XFADE_SECONDS:.3f}", "-i", clip_paths[i + 1],
                 "-filter_complex",
                 f"[0:v]fps={FPS},format=yuv420p,setsar=1[a];"
                 f"[1:v]fps={FPS},format=yuv420p,setsar=1[b];"
                 f"[a][b]xfade=transition=fade:duration={XFADE_SECONDS}:offset=0[v]",
                 "-map", "[v]"] + _ENCODE_ARGS + [join],
                timeout,
            )
            segments.append(join)

    _concat(segments, out_path, workdir, "dissolve_list", timeout)
    n = len(clip_paths)
    return n * SECONDS_PER_PHOTO - (n - 1) * XFADE_SECONDS


def _kenburns_clip(frame_img, out_path, workdir, index, caption=None, caption_size=None,
                   can_pan=True, left_to_right=True, zoom_in=True, fade_in=False):
    """One photo -> one 5s motion clip, with its caption burned in during the same
    pass. Doing the caption here rather than over the finished video means a per-photo
    timing window falls out for free, one photo's caption can be omitted without
    touching the others, and the video is only ever encoded once."""
    # BMP, deliberately. A prepared pan frame is 2808x3840, and PNG spends real time
    # compressing something we delete seconds later. JPEG is faster still but hands
    # ffmpeg a full-range (yuvj420p) source, so the whole video inherits the JPEG range
    # flag and plays with lifted blacks on players that honour it. BMP is uncompressed
    # RGB: fast to write, and RGB converts to limited-range yuv420p the same way the
    # PNG path did.
    src_path = os.path.join(workdir, f"src_{index:02d}.bmp")
    frame_img.save(src_path, format="BMP")

    if can_pan:
        # Fixed-size window drifting horizontally. zoompan cannot do this: its crop
        # window always carries the INPUT's aspect ratio, so panning across a strip
        # wider than 9:16 and forcing the result to 1080x1920 would squeeze the image.
        # crop takes an explicit 9:16 window and only moves it, so nothing distorts.
        # x steps by frame number rather than time so the travel is frame-exact.
        travel = f"(iw-ow)*n/{FRAMES_PER_PHOTO - 1}"
        if not left_to_right:
            travel = f"(iw-ow)*(1-n/{FRAMES_PER_PHOTO - 1})"
        filters = [
            f"crop=w={VW * UPSCALE}:h={VH * UPSCALE}:x='{travel}':y=0",
            f"scale={VW}:{VH}",
        ]
    else:
        if zoom_in:
            z = f"min(zoom+{ZOOM_STEP},{ZOOM_MAX})"
        else:
            z = f"if(eq(on,1),{ZOOM_MAX},max(zoom-{ZOOM_STEP},1.0))"
        filters = [
            (f"zoompan=z='{z}':d={FRAMES_PER_PHOTO}:s={VW}x{VH}:fps={FPS}"
             f":x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"),
        ]
    filters.append("setsar=1")

    # drawtext reads the caption from a file rather than an inline `text=` value: the
    # caption is model-written, and inline text has to be escaped against three nested
    # parsers (shell-free argv, filtergraph, drawtext). textfile= plus expansion=none
    # removes that whole class of bug -- and with it any chance of a caption smuggling
    # filter syntax into the command.
    filters += _caption_filters(caption, caption_size, workdir, index)

    if fade_in:
        # After drawtext, so the caption rises out of black with the photo rather than
        # sitting fully lit over a dark frame.
        filters.append(f"fade=t=in:st=0:d={OPEN_FADE_SECONDS}")

    filters.append("format=yuv420p")

    # -framerate on the INPUT so the looped still is generated at 24fps: the pan's x
    # expression counts input frames, and without this ffmpeg loops at its 25fps
    # default, so the pan would finish its travel slightly before the clip ends.
    cmd = (["ffmpeg", "-y", "-loop", "1", "-framerate", str(FPS),
            "-t", str(SECONDS_PER_PHOTO), "-i", src_path,
            "-vf", ",".join(filters), "-frames:v", str(FRAMES_PER_PHOTO)]
           + _ENCODE_ARGS + [out_path])
    try:
        _run_ffmpeg(cmd, TIMEOUT_CLIP)
    finally:
        try:
            os.unlink(src_path)
        except OSError:
            pass


def _concat(clip_paths, out_path, workdir, name, timeout):
    """Joins clips with a stream copy. Every clip comes out of the same encoder
    settings (_ENCODE_ARGS), so no re-encode is needed -- which is both far cheaper
    than the old pairwise xfade cascade and lossless."""
    list_path = os.path.join(workdir, f"{name}.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for path in clip_paths:
            f.write(f"file '{os.path.abspath(path)}'\n")
    _run_ffmpeg(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
                 "-c", "copy", out_path], timeout)


# ---------------------------------------------------------------------------
# Contact card outro
# ---------------------------------------------------------------------------
def _fit_text(draw, text, font_path, size, max_width):
    """Shrinks a line until it fits. A 74px name is right for "Janel Chee" and 300px
    too wide for a long double-barrelled name plus agency suffix -- without this the
    name simply runs off both edges of the card."""
    while size > 24:
        font = ImageFont.truetype(font_path, size)
        if draw.textlength(text, font=font) <= max_width:
            return font
        size -= 2
    return ImageFont.truetype(font_path, 24)


def _render_contact_card(agent_name, agent_contact_line, agent_photo=None):
    """The closing still: warm off-white card, gold-ringed portrait, name, divider,
    invitation, contact in gold. Layout and colours are the prototype's."""
    card = Image.new("RGB", (VW, VH), CARD_BG)
    draw = ImageDraw.Draw(card)
    cx = VW // 2

    if agent_photo:
        diameter = 620
        photo = agent_photo.convert("RGB")
        side = min(photo.size)
        # centering=(0.5, 0.0) takes the TOP square of the portrait. Centre-cropping a
        # head-and-shoulders shot slices the crown of the head off inside the circle;
        # anchoring to the top keeps whatever headroom the agent's photo has above the
        # hair, which is the difference between a portrait and a mugshot.
        photo = ImageOps.fit(photo, (side, side), centering=(0.5, 0.0))
        photo = photo.resize((diameter, diameter), Image.LANCZOS)
        mask = Image.new("L", (diameter, diameter), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, diameter, diameter], fill=255)
        # The whole block (circle through contact line) is centred on the 1920-tall
        # frame rather than pinned near the top: on a phone the card is seen full
        # height, and a top-weighted layout leaves an obvious dead zone underneath.
        top = 430
        card.paste(photo, (cx - diameter // 2, top), mask)
        draw.ellipse(
            [cx - diameter // 2 - 5, top - 5, cx + diameter // 2 + 5, top + diameter + 5],
            outline=CARD_GOLD, width=5,
        )
        name_y, divider_y, invite_y, contact_y = 1150, 1290, 1330, 1420
    else:
        # No profile photo: no empty circle, no gap where one would have been -- the
        # same text block simply re-centres on the card.
        name_y, divider_y, invite_y, contact_y = 793, 933, 973, 1063

    max_width = VW - 160
    name_font = _fit_text(draw, agent_name or "", SERIF_REGULAR, 86, max_width)
    invite_font = _fit_text(draw, "Contact me to arrange for a viewing",
                            SERIF_ITALIC, 52, max_width)
    contact_font = _fit_text(draw, agent_contact_line or "", SERIF_REGULAR, 64, max_width)

    def centred(text, y, font, fill):
        if not text:
            return
        draw.text((cx - draw.textlength(text, font=font) / 2, y), text, font=font, fill=fill)

    centred(agent_name or "", name_y, name_font, CARD_INK)
    draw.line([cx - 105, divider_y, cx + 105, divider_y], fill=CARD_GOLD, width=3)
    centred("Contact me to arrange for a viewing", invite_y, invite_font, CARD_INK)
    centred(agent_contact_line or "", contact_y, contact_font, CARD_GOLD)
    return card


def _build_card_clip(body_path, card_img, out_path, workdir):
    """Dissolves the finished slideshow into the contact card.

    The dissolve is built from two STILLS -- the body's frozen last frame and the card
    -- and the result is concatenated onto the body. It is never an xfade applied to
    the body stream itself: xfade over a concat-produced stream silently truncates the
    output (it reads the first segment's duration, drops the rest, and exits 0, so
    nothing surfaces as an error) -- the bug documented in prototypes/assemble_v16.py
    that cost several prototype rounds. Keeping the dissolve on two one-frame inputs
    means the body is only ever stream-copied and can never be truncated.
    """
    last_frame = os.path.join(workdir, "last_frame.jpg")
    _run_ffmpeg(["ffmpeg", "-y", "-sseof", "-0.2", "-i", body_path,
                 "-frames:v", "1", "-update", "1", "-q:v", "2", last_frame], TIMEOUT_CARD)

    card_path = os.path.join(workdir, "contact_card.jpg")
    card_img.save(card_path, format="JPEG", quality=95)

    cmd = (["ffmpeg", "-y",
            "-loop", "1", "-t", str(CARD_TAIL_SECONDS), "-i", last_frame,
            "-loop", "1", "-t", str(CARD_SECONDS), "-i", card_path,
            "-filter_complex",
            f"[0:v]scale={VW}:{VH},setsar=1,fps={FPS},format=yuv420p[a];"
            f"[1:v]scale={VW}:{VH},setsar=1,fps={FPS},format=yuv420p[b];"
            f"[a][b]xfade=transition=fade:duration={CARD_XFADE_SECONDS}"
            f":offset={CARD_HOLD_SECONDS}[v]",
            "-map", "[v]"]
           + _ENCODE_ARGS + [out_path])
    _run_ffmpeg(cmd, TIMEOUT_CARD)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def render_property_video(image_urls, property_type=None, district=None, price_text=None,
                          stats=None, agent_name="", agent_contact_line="",
                          style="classic", photo_index=0, agent_photo_url=None,
                          copy_guard=None):
    """Renders the Classic-tier listing video and returns (mp4_bytes, degradations).

    `degradations` is a list of plain-language strings naming anything that quietly
    fell back (no captions, no music, no card). Empty means a full-quality render.

    style: the house style is the dissolve -- each photo shown whole over a blurred
    copy of itself, soft crossfades between them. Every agent-facing id renders it,
    including the "classic" id the picker sends and the retired "card" id still stored
    on old rows, so regenerating an old listing gives today's look rather than an
    error. Passing "driftcut" explicitly opts into the earlier cover-crop/pan/hard-cut
    look; nothing in the product does, and it is absent from VIDEO_TEMPLATES.
    photo_index: which listing photo is the "hero" -- it opens the video. Same selector
    the agent already uses for the poster (My Listings' star picker).
    agent_photo_url: optional. Without it the closing card renders without the portrait
    circle rather than leaving an empty ring.
    copy_guard: optional callable(text, context=...) -> text. main.py passes
    apply_listing_copy_guards so captions go through the same house-number/price
    stripping as every other copy surface. Passed in rather than imported because
    main.py imports this module.

    property_type / district / price_text / stats are accepted for signature
    compatibility and are no longer drawn: the Classic tier's on-screen text is the
    room captions, and a stats block in the same lower third would collide with them.
    """
    if not image_urls:
        raise ValueError("At least one photo is required to generate a video")
    if photo_index < 0 or photo_index >= len(image_urls):
        photo_index = 0

    # Hero photo first, then the rest in their existing order, then truncate. Slicing
    # before honouring photo_index would silently drop the hero whenever the listing
    # has more photos than MAX_VIDEO_PHOTOS.
    hero_url = image_urls[photo_index]
    rest_urls = [u for i, u in enumerate(image_urls) if i != photo_index]
    selected_urls = ([hero_url] + rest_urls)[:MAX_VIDEO_PHOTOS]

    # The dissolve is the house style. Every agent-facing id ("classic", the retired
    # "card", or nothing at all) renders it; only an explicit internal "driftcut"
    # opts out. Defaulting this way round means an old listing row carrying a retired
    # template id gets today's look rather than an error or a stale one.
    dissolve = (style != STYLE_A_ID)

    degradations = []
    started = time.monotonic()

    if not _RENDER_SLOTS.acquire(timeout=RENDER_SLOT_WAIT_SECONDS):
        raise RuntimeError(
            "The video service is busy with other renders right now. Please try again "
            "in a few minutes."
        )
    try:
        photos = []
        for url in selected_urls:
            try:
                photos.append(_fetch_image(url))
            except Exception as e:
                logger.warning("skipping unreadable photo %s: %s", url, e)
                degradations.append("a photo could not be read and was skipped")
        if not photos:
            raise RuntimeError("None of this listing's photos could be downloaded")

        agent_photo = None
        if agent_photo_url:
            try:
                agent_photo = _fetch_image(agent_photo_url)
            except Exception as e:
                logger.warning("agent profile photo unavailable (%s)", e)
                degradations.append("the agent's profile photo could not be loaded, so the closing card has no portrait")

        captions, caption_note = _generate_room_captions(photos, copy_guard=copy_guard)
        if caption_note:
            degradations.append(f"captions: {caption_note}")
        caption_size = _caption_font_size(captions)

        with tempfile.TemporaryDirectory() as workdir:
            clip_paths = []
            for i, photo in enumerate(photos):
                if i > 0 and time.monotonic() - started > RENDER_BUDGET_SECONDS:
                    logger.warning("render budget reached after %d photos; finishing early", i)
                    degradations.append(f"took too long, so only the first {i} photos were used")
                    break
                clip_path = os.path.join(workdir, f"clip_{i:02d}.mp4")
                try:
                    if dissolve:
                        _contain_clip(
                            photo, clip_path, workdir, i,
                            caption=captions[i] if i < len(captions) else None,
                            caption_size=caption_size,
                            zoom_in=(i % 2 == 0),
                            first=(i == 0),
                            last=(i == len(photos) - 1),
                        )
                    else:
                        frame, can_pan = _prepare_frame(photo)
                        _kenburns_clip(
                            frame, clip_path, workdir, i,
                            caption=captions[i] if i < len(captions) else None,
                            caption_size=caption_size,
                            can_pan=can_pan,
                            # Alternating direction is what gives the sequence its
                            # rhythm -- every clip drifting the same way reads like a
                            # conveyor belt.
                            left_to_right=(i % 2 == 0),
                            zoom_in=(i % 2 == 0),
                            fade_in=(i == 0),
                        )
                except Exception as e:
                    logger.warning("clip %d failed (%s); skipping that photo", i, e)
                    degradations.append("a photo failed to render and was skipped")
                    continue
                clip_paths.append(clip_path)

            if not clip_paths:
                raise RuntimeError("No photo could be rendered into the video")

            body_path = os.path.join(workdir, "body.mp4")
            estimated = len(clip_paths) * SECONDS_PER_PHOTO
            if len(clip_paths) == 1:
                os.replace(clip_paths[0], body_path)
            elif dissolve:
                # One filtergraph pass, all clips dissolving into each other.
                estimated = _dissolve_segments(clip_paths, body_path, workdir,
                                               TIMEOUT_XFADE_CHAIN)
            else:
                _concat(clip_paths, body_path, workdir, "body_list", TIMEOUT_CONCAT)
            body_duration = _probe_duration(body_path, estimated)

            # --- closing contact card (degrades to no card) ---
            silent_path = body_path
            silent_duration = body_duration
            try:
                card_img = _render_contact_card(agent_name, agent_contact_line, agent_photo)
                card_clip = os.path.join(workdir, "card.mp4")
                _build_card_clip(body_path, card_img, card_clip, workdir)
                with_card = os.path.join(workdir, "with_card.mp4")
                _concat([body_path, card_clip], with_card, workdir, "card_list", TIMEOUT_CONCAT)
                silent_path = with_card
                silent_duration = _probe_duration(
                    with_card, body_duration + CARD_HOLD_SECONDS + CARD_SECONDS)
            except Exception as e:
                logger.warning("contact card failed (%s); shipping the slideshow without it", e)
                degradations.append("the closing contact card could not be built")

            # --- music bed (degrades to silence) ---
            final_path = os.path.join(workdir, f"final_{uuid.uuid4().hex[:8]}.mp4")
            wrote_final = False
            if os.path.exists(AUDIO_PATH):
                fade_out_start = max(0.0, silent_duration - AUDIO_FADE_OUT_SECONDS)
                audio_filter = (
                    f"[1:a]atrim=0:{silent_duration:.3f},asetpts=PTS-STARTPTS,"
                    f"volume={AUDIO_VOLUME},"
                    f"afade=t=in:st=0:d={AUDIO_FADE_IN_SECONDS},"
                    f"afade=t=out:st={fade_out_start:.3f}:d={AUDIO_FADE_OUT_SECONDS}[aout]"
                )
                try:
                    _run_ffmpeg([
                        "ffmpeg", "-y", "-i", silent_path,
                        # -stream_loop so a track shorter than the video still covers it.
                        "-stream_loop", "-1", "-i", AUDIO_PATH,
                        "-filter_complex", audio_filter,
                        "-map", "0:v", "-map", "[aout]",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", final_path,
                    ], TIMEOUT_MUSIC)
                    wrote_final = True
                except Exception as e:
                    logger.warning("music bed failed (%s); shipping a silent video", e)
                    degradations.append("the background music could not be added")
            else:
                logger.warning("music track missing at %s", AUDIO_PATH)
                degradations.append("the background music file is missing from the deploy")

            if not wrote_final:
                _run_ffmpeg(["ffmpeg", "-y", "-i", silent_path, "-c", "copy",
                             "-movflags", "+faststart", final_path], TIMEOUT_MUSIC)

            with open(final_path, "rb") as f:
                data = f.read()

        if degradations:
            logger.warning("video rendered with degradations: %s", "; ".join(degradations))
        logger.info(
            "video rendered: %d photo(s), %.1fs, %.1fMB, %.0fs wall clock",
            len(clip_paths), silent_duration, len(data) / 1024 / 1024,
            time.monotonic() - started,
        )
        return data, degradations
    finally:
        _RENDER_SLOTS.release()
