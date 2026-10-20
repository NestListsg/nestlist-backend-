"""NestList property video renderer -- the production "Classic tier" listing video.

Turns a listing's photos into a 1080x1920 (9:16) cinematic slideshow: alternating Ken
Burns motion, one expressive AI-written room caption per photo, a music bed picked from
the audio/ library, and a closing contact card built from the agent's own profile.

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
    HDR cannot be    -> the film is still published bt709 (so the closing card is
    tonemapped          right); only the agent looks a little flat. See HDR_TRANSFERS
Only "no usable photos at all" raises. Which degradations fired is returned to the
caller (and logged) so a quietly-degraded render is visible in Railway's logs rather
than looking like a normal success.

Requires the `ffmpeg` binary on PATH -- see railpack.json. ffprobe is used where it
helps but is NEVER required: it is a separate binary, a worker can have a working
ffmpeg without it, and assuming otherwise has caused two bugs here already. Anything
that must be read from a file is read from ffmpeg's own banner instead
(_duration_via_ffmpeg, _source_colour).
"""
import base64
import hashlib
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

import photo_upscale
import slow_read

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
# --- the house move: a zoom combined with a horizontal drift ---
# This replaces the 1.06 centred zoom the Classic shipped with until 2026-10-17. Jane
# watched competitors' property videos and ours read as entry-level next to them; the
# cause was specific, a 6% push from a fixed centre is barely motion at all. She
# approved the bolder look on a prototype and asked for the horizontal drift over the
# prototype's vertical tilts ("make the whole video like that").
#
# THE RULE THIS OBEYS: the camera moves across a real photograph. Nothing is
# generated, interpolated or invented -- every frame is a crop of pixels the agent
# actually uploaded, scaled. That is why this is allowed where an AI camera move is
# not (closed September 2026).
#
# Geometry. zoompan's window is iw/zoom wide, so the spare width it can slide across
# is iw*(1-1/zoom) -- zero at zoom 1.0 and growing as the shot pushes in. The drift is
# expressed as a FRACTION of that spare (0 = hard left, 1 = hard right), which means:
#   * nothing can ever drift past the edge of the composite, at any zoom, by
#     construction rather than by a clamp that has to be got right;
#   * the end of the clip that sits at zoom 1.0 still shows the photo WHOLE, which is
#     the property the 1.06 comment prized and the reason the contain style exists;
#   * the move accelerates out of the dissolve instead of starting at full speed.
#
# 1.20 is the value Jane approved on the prototype, and it is kept deliberately rather
# than re-tuned. MEASURED, because the obvious question is whether more would be
# better, and the answer is that the dial is nearly free on size but not free on the
# picture:
#
#     zoom   pan of frame centre   % of width   crop window -> 1080 wide   10-photo film
#     1.06        61px                2.83%         1.89x downsample        (the old look)
#     1.20       180px                8.33%         1.67x downsample          26.2MB
#     1.22       195px                9.02%         1.64x downsample          26.4MB
#     1.26       223px               10.32%         1.59x downsample          26.6MB
#
# So raising it buys drift for almost no bits -- but at the far end of the move the
# window sits against one edge, and at 1.20 that already leaves 17% of the photo's
# width off-frame on the other side (20% at 1.26). That, not file size and not
# sharpness, is the thing being spent, and it is spent on a style whose whole premise
# is showing the photo whole. Jane signed off 1.20 having watched it; moving it without
# her watching again would be trading her judgement for mine.
#
# SHARPNESS IS NOT THE LIMIT at any of these: at 1.20 the crop window is still
# 1800x3200 feeding a 1080x1920 output, i.e. downsampling 1.67x, so the frame cannot go
# soft from the zoom itself. What a strong zoom DOES expose is a weak SOURCE, which is
# why _fetch_listing_photo prefers the 4x upscaled twin -- that is the headroom this
# setting spends.
CONTAIN_ZOOM_MAX = 1.20
CONTAIN_ZOOM_STEP = round((CONTAIN_ZOOM_MAX - 1.0) / FRAMES_PER_PHOTO, 6)
# How much of the available spare width a shot actually travels. 1.0 means it runs
# from one edge of the spare to the other. Kept as a constant rather than inlined
# because it is the one dial that trades drift against how much of the photo's side
# is off-frame at the far end of the move.
CONTAIN_DRIFT_SPAN = 1.0
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
# Loudness target for tracks flagged "raw": true in audio/tracks.json. This is the level
# the pre-processed library was normalized to (see audio/LICENSE-music.txt, ~-9.8 LUFS),
# so a raw track dropped in untouched sits at the same level once the shared AUDIO_VOLUME
# is applied. Never applied to a track that is not flagged raw.
RAW_TRACK_TARGET_LUFS = -9.8
RAW_TRACK_TRUE_PEAK_DB = -1.5
RAW_TRACK_LRA = 11

# Each photo is one sequential ffmpeg encode. The previous renderer also folded clips
# together with N-1 progressive xfade passes, each one re-encoding the whole
# accumulated video -- that cascade, not the per-photo encode, is what forced the cap
# down to 5. This pipeline concatenates with a stream copy instead (no re-encode at
# all), so the same CPU budget comfortably covers the 6 shots the prototype was
# validated at. Still deliberately well below the listing's 15-photo upload cap
# (MAX_LISTING_PHOTOS in main.py), which has no per-photo rendering cost.
#
# Raised 6 -> 10 on 2026-10-17. The 6 was a leftover from the re-encode cascade above
# and had stopped paying for itself: with the stream-copy concat each extra photo is
# one more sequential clip encode and nothing else. MEASURED here, real ffmpeg, the
# same fifteen 3200x1800 sources, Classic with music and contact card:
#     6 photos  -> 11.1s render, 30.0s of video
#    10 photos  -> 18.0s render, 45.3s of video      (~1.7s per extra photo)
# That is the same ~2s-per-photo slope Jane measured on the demo listing. A ten-photo
# Classic therefore still finishes inside RENDER_BUDGET_SECONDS with the whole budget
# to spare, and the budget check in the clip loop remains the real backstop. File size
# is the constraint that actually moves (see DELIVERY_CRF).
MAX_VIDEO_PHOTOS = 10

# A Classic that drops below this many shots is too short to be worth posting -- six
# was the shipped length, so it is the floor the smart selection is allowed to reach
# before it starts accepting a second photo of a space it has already shown. See
# _smart_auto_selection.
MIN_VIDEO_PHOTOS = 6

# How many of a listing's photos the selection pass will look at. MAX_LISTING_PHOTOS
# in main.py is 15, so this is "all of them" -- stated as its own constant because it
# sets the cost and the latency of the vision call, which is the thing to think about
# if the upload cap ever rises.
MAX_SELECTION_CANDIDATES = 15

# Ceiling on renders running at once *in this worker process*. Railway runs 4 uvicorn
# workers, so the fleet ceiling is 4x this. Renders are background jobs, so waiting is
# cheap and far better than 50 agents each holding an ffmpeg process and taking the
# instance down.
#
# This semaphore used to be the ONLY thing limiting concurrency, which made it the thing
# that failed renders: past the wait window it raised "the video service is busy, try
# again in a few minutes" and the agent was told no. Admission control now lives in
# video_jobs, which hands out at most MAX_CONCURRENT_RENDERS Classic jobs per process,
# so from the queued path this semaphore is never contended and is purely a backstop
# against a future caller forgetting. When it IS contended the caller gets
# RenderSlotBusy, which the queue understands as "put this back and try shortly" --
# busyness never fails a job any more. The legacy in-process path (used only until the
# video_jobs migration is run) still surfaces the old wording, unchanged.
#
# Set to 1 after measuring one clip render at ~800MB peak RSS (both styles, macOS).
#
# CORRECTED 2026-10-19: that 800MB was THIS PROCESS ONLY. ffmpeg runs as a separate
# process and the instance pays for both; measured together, one Classic render peaks
# at ~1.6GB (379MB python + 1234MB ffmpeg at ten photos; an independent audit measured
# ~1.85GB on other hardware). So the fleet worst case at this setting is ~6.4GB, not
# ~3.3GB, and at 2 it would have been ~13GB rather than ~6.6GB. The conclusion is
# unchanged and now has more margin behind it: 1 is the right number. The full table
# lives at JOB_TYPES in video_jobs.py.
#
# Peak memory is nearly FLAT in the photo count (1214MB at six photos, 1234MB at ten),
# because clips are encoded one at a time -- raising MAX_VIDEO_PHOTOS cost CPU and
# bytes, not memory.
#
# The other candidate lever -- lowering UPSCALE -- was measured and rejected: peak RSS
# is essentially flat across upscale factors (986MB at 1x, 766MB at 2x, 876MB at 3x),
# so the cost is dominated by the x264 encoder and frame buffering at 1080x1920, not
# by the source frame. Dropping to 1x would have coarsened the slow zoom for no memory
# saving at all.
MAX_CONCURRENT_RENDERS = 1
RENDER_SLOT_WAIT_SECONDS = 240
_RENDER_SLOTS = threading.Semaphore(MAX_CONCURRENT_RENDERS)


class RenderSlotBusy(RuntimeError):
    """No local render slot was free in time.

    A distinct type so callers can tell "this process is full right now" apart from
    "this render went wrong". The queue treats it as a reason to wait; it is never a
    reason to tell an agent their video failed."""

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

# The music bed is whatever audio files are sitting in audio/ at deploy time -- the
# directory IS the library, so adding a track is a file drop plus a licence note, with
# no code change. Today that directory holds one file ("Piano Soft Gentle Morning Keys"
# by Alex Morgan, from Pixabay, whose Content License permits commercial use including
# social video without attribution -- see audio/LICENSE-music.txt; byte-identical to
# the track validated in the prototypes), so every video sounds exactly as it does now.
AUDIO_DIR = os.path.join(_HERE, "audio")
# Anything ffmpeg can decode and our aac encode can consume. LICENSE-music.txt and any
# stray .DS_Store are excluded by not being on this list.
AUDIO_EXTENSIONS = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus")

# Optional, advisory track metadata: {"soft_piano.mp3": "warm", ...}. It exists so a
# grand house and a terrace don't share a register (content-studio's mood palette --
# see docs/classic-video-music-library-spec.md). It is NOT load-bearing: a missing
# file, malformed JSON, an untagged track or a tier nobody is tagged with all fall
# back to "any track in audio/ will do". Music is the most degradable stage in this
# renderer and this must not be the thing that makes it fragile.
TRACKS_MANIFEST = os.path.join(AUDIO_DIR, "tracks.json")
TIER_GRAND = "grand"   # content-studio's "Grand Prestige": GCB, Detached/Bungalow, Penthouse
TIER_WARM = "warm"     # content-studio's "Warm Home": Semi-Detached, Inter/Corner Terrace
MUSIC_TIERS = (TIER_GRAND, TIER_WARM)
# Substrings that promote a listing to the grand tier, tested against a lowercased
# property_type. "semi" is checked FIRST and separately, because "Semi-Detached"
# contains "detached" -- matching on that alone would score every semi-D as grand.
_GRAND_TYPE_TOKENS = ("gcb", "good class", "bungalow", "detached", "penthouse")

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

# White caption type on a cream-bright interior is close to unreadable -- the 2px
# drop shadow on its own was not enough (Janel, on a bright bedroom around 0:12).
# A soft dark band sits behind the caption rows so the type reads on any photo.
# The type itself is untouched: same Gelasio italic, same size, same positions,
# same shadow. Only what's behind it changes.
CAPTION_SCRIM_ALPHA = 0.46          # peak darkness directly behind the rows
CAPTION_SCRIM_PAD = 12              # full-strength margin around the text block
CAPTION_SCRIM_FEATHER = 180         # px of fade-out above and below that core
CAPTION_SCRIM_STEPS = 45            # strips the feather is built from (4px each)
CAPTION_GLYPH_HEIGHT = 1.30         # Gelasio ascent+descent as a multiple of size

# Output-side clarity pass, applied to each clip after it has been scaled to its final
# 1080x1920 (sharpening the 2x working frame and then downscaling washes it straight back
# out). CAS does the contrast-adaptive lift; the gentle unsharp adds local acutance.
#
# These two values are the survivors of a bake-off -- the alternatives were tested and
# REJECTED, so do not "improve" this string without re-testing:
#   cas=strength=1.0        speckles noise badly in dark areas (a boundary wall and pool
#                           decking both went visibly grainy)
#   a second stacked unsharp haloes around dark window frames, and clips both the black
#                           and white points
#   hqdn3d + sharpen        sharper by the metric but visibly crunchy -- it made a
#                           person's FACE look harshly etched, which is disqualifying
#   Richardson-Lucy deconv  genuinely the best-looking, far too slow to render per photo
CLARITY_FILTER = "cas=strength=0.80,unsharp=9:9:1.10:5:5:0.0"
# cas (Contrast Adaptive Sharpen) only exists in ffmpeg 4.3+. Railway installs Debian's
# ffmpeg, which is new enough -- but an unknown filter name makes ffmpeg exit non-zero,
# and this string goes into EVERY clip encode, so being wrong would fail every render
# rather than degrade one. Probed once per process instead of assumed. unsharp has been
# in ffmpeg essentially forever and is the fallback; if even that is missing, clips
# render exactly as they did before this change.
CLARITY_FALLBACK_FILTER = "unsharp=9:9:1.10:5:5:0.0"
_clarity_filter_cache = None
_clarity_lock = threading.Lock()


def _has_ffmpeg_filter(name):
    """True if this ffmpeg build knows `name`. Any doubt answers False."""
    try:
        probe = subprocess.run(["ffmpeg", "-hide_banner", "-h", f"filter={name}"],
                               capture_output=True, timeout=TIMEOUT_PROBE)
        out = (probe.stdout + probe.stderr).decode("utf-8", errors="replace")
        return probe.returncode == 0 and "Unknown filter" not in out
    except Exception as e:
        logger.warning("could not probe ffmpeg for the %r filter (%s)", name, e)
        return False


def _clarity_filter():
    """The clarity chain this ffmpeg build can actually run, worked out once."""
    global _clarity_filter_cache
    with _clarity_lock:
        if _clarity_filter_cache is None:
            if _has_ffmpeg_filter("cas"):
                _clarity_filter_cache = CLARITY_FILTER
            elif _has_ffmpeg_filter("unsharp"):
                logger.warning("ffmpeg has no 'cas' filter; using unsharp alone -- "
                               "videos will be slightly less sharp than intended")
                _clarity_filter_cache = CLARITY_FALLBACK_FILTER
            else:
                logger.warning("ffmpeg has neither 'cas' nor 'unsharp'; rendering "
                               "without the clarity pass")
                _clarity_filter_cache = ""
        return _clarity_filter_cache

# Delivery quality. SETTLED AT 24 (was 22) -- measured on real style B output, not
# inferred. The banding question below has now been asked three times; the answer is in
# the table, please do not re-litigate it from first principles a fourth time. The 22
# -> 24 move at the bottom of this comment is a SEPARATE measurement on different
# footage, not a reopening of the banding question.
#
# The worry was legitimate: _contain_over_blur puts a large smooth darkened field behind
# every photo, and a big flat dark gradient in every frame is the textbook 8-bit banding
# case, which is exactly what a higher CRF provokes. An earlier 52MB->27MB measurement
# existed but had been taken on the style A path, so it did not transfer.
#
# MEASURED on a production style B render of the demo listing, sampling the blurred
# backdrop above the photo (rows 120-470):
#
#     CRF   distinct luma levels   max step between rows
#      18           136                    0.99
#      20           138                    0.96
#      22           138                    0.94
#      23           138                    0.94
#
# It does not degrade at all -- marginally MORE levels at higher CRF, because 18 is where
# the detail is. Contrast-stretching the backdrop ~100x to expose contouring visually
# shows the same faint contour pattern at EVERY setting including 18: those contours come
# from the Gaussian blur itself, not from the encoder.
#
# METHOD, so this is repeatable if the blur or the backdrop treatment ever changes:
# take a frame, crop the backdrop band above the photo, convert to luma, then (a) count
# distinct luma levels and (b) take the maximum difference between adjacent row means.
# Banding shows up as FEWER distinct levels and a LARGER inter-row step. Separately,
# stretch that crop to full range and look at it -- the arithmetic can miss what the eye
# catches, and vice versa.
#
# Sharpening survives compression almost entirely (same render, sampled at 3s/12s/20s):
# the pre-clarity baseline scored 2.70/2.40/3.83; CRF 18 scored 5.72/3.63/7.99 and CRF 23
# still scored 5.66/3.53/7.68 -- about 96% of the gain, and still roughly DOUBLE the old
# render, so the size saving costs essentially nothing.
#
# Why 22 and not 23: at 18 the clarity pass pushed a real render to 19.5MB against 12.7MB
# before it. Jane's constraints are agent upload time on mobile and Supabase storage
# (free tier; 50 agents x 10 listings), so getting back UNDER the old 12.7MB is the goal,
# and 22 is the mildest setting expected to do it while sitting inside the range where
# banding was measured clean.
#
# STILL TO CONFIRM ON A DIRECT RENDER: the size. The 20/22/23 figures above came from
# RE-ENCODING the 18 output, so they understate a single-pass encode -- the second pass
# never had to carry detail the first had already discarded. The banding and sharpness
# findings are conservative and do transfer; the sizes do not. Expect roughly 11-13MB at
# 22, and check the contact card too, not just the backdrop -- it is a second large flat
# field and it carries fine serif type and a phone number that have to stay legible.
#
# ---------------------------------------------------------------------------
# 2026-10-17: RE-MEASURED ON DIRECT RENDERS, AND MOVED 22 -> 24.
#
# This is not the banding question being reopened. The FOOTAGE changed underneath the
# setting, so the old number no longer describes the same signal: the shots now
# actually move (the zoom had been silently dead for the whole life of style B -- see
# _contain_clip) and a film is now up to 10 photos instead of 6. Real motion means
# almost no two frames are alike, which is exactly the redundancy an inter-frame codec
# was living on before.
#
# Direct single-pass renders, same ten 3200x1800 sources, real ffmpeg, 45.3s of video.
# This also answers the "STILL TO CONFIRM" note above: these are not re-encodes.
# VMAF is measured over a clean 30s window (six shots, no contact card) against a
# CRF 14 reference of the SAME footage, so it reads as "what the encoder threw away"
# rather than as a comparison between two different videos. The last column is Jane's
# own 50 agents x 10 listings:
#
#     CRF    size    VMAF    vs 22    500 videos
#      22   35.5MB   94.82       0%      17.4GB
#      24   26.2MB   93.18     -26%      12.8GB     <-- chosen
#      25   22.7MB   92.23     -36%      11.1GB
#      26   19.7MB   91.18     -45%       9.6GB
#      28   15.0MB   88.64     -58%       7.3GB
#
# For scale, same sources throughout: the film this replaces (6 photos, no working
# zoom) measured 15.6MB at CRF 22, and the same 10 photos WITHOUT the motion fix
# measured 25.2MB. The motion costs the bits, not the extra photos.
#
# Why 24 and not 22: a 26% saving for 1.7 VMAF points, and 93+ is the usual line for
# "a viewer cannot tell on a phone". Why not further: at 2x magnification on a
# high-frequency crop (foliage, a perforated screen), 24 and 25 are hard to separate
# from the CRF 14 reference, but at 26 the foliage visibly flattens and at 28 it is
# plainly mushy. 24 also lands storage at 12.8GB, which is the ~14GB Jane already
# expected this change to cost.
#
# CHECKED, because the comment above asks for it: both large flat fields are fine at
# every setting tested. The blurred backdrop shows no banding at 22, 24, 25, 26 or even
# 28, and the contact card -- fine serif name, italic invitation, gold phone number --
# is indistinguishable from 22 all the way to 28. Both are near-static, so the encoder
# spends almost nothing keeping them clean. Neither is the constraint any more; the
# moving photograph is.
#
# IF STORAGE EVER GETS TIGHT: 25 is the next step and costs about one VMAF point.
#
# A CORRECTION TO THE TABLE ABOVE, 2026-10-20. Those VMAF numbers were taken with
# libvmaf comparing two separate ffmpeg inputs, and that harness turns out to MISALIGN
# on these files: a video compared against ITSELF scored 97, not 100. These films are
# assembled by stream-copy concatenation, so their timestamps are not a clean ladder
# (ffmpeg reports non-monotonic DTS at the joins, at frames 94 and 1084), and the two
# decode paths resample differently.
#
# Re-measured frame-accurately -- frames pulled BY INDEX and compared directly, with a
# self-comparison as the control (SSIM 1.000000, PSNR inf, as it must be):
#
#     CRF 22   35.5MB   SSIM 0.9694   PSNR 39.6 dB
#     CRF 24   26.2MB   SSIM 0.9644   PSNR 38.3 dB     <-- shipped
#     CRF 25   22.7MB   SSIM 0.9618   PSNR 37.7 dB
#     CRF 26   19.7MB   SSIM 0.9592   PSNR 37.1 dB
#     CRF 28   15.0MB   SSIM 0.9541   PSNR 35.8 dB
#
# The ORDERING and the sizes are exactly as the VMAF table said, and the choice of 24
# is unchanged -- but treat the absolute VMAF figures above as indicative only, and use
# a frame-indexed comparison for anything new.
# ---------------------------------------------------------------------------
DELIVERY_CRF = "24"

# ---------------------------------------------------------------------------
# The hard ceiling on what we will hand the storage bucket.
#
# Supabase's limit is 50MB (confirmed by Jane, 2026-10-20). Going over it is an
# EXPENSIVE failure, not a tidy one: _store_rendered_video uploads the whole file and
# retries four times, re-uploading all of it each time, then fails the job -- at which
# point video_jobs retries the job itself, so the agent waits through several complete
# re-renders and ends up with an error they can do nothing about.
#
# 40MB is the ceiling we act on, leaving 20% of real headroom under the limit. It is
# reachable: a real Prestige listing (the demo semi-D, pool, garden, mature planting)
# came out at 23.7MB, but a deliberately texture-heavy source reached 43.91MB, and
# landed property with mature planting is exactly the texture-heavy case.
#
# MEASURED COST OF THE SHRINK PASS, re-encoding a finished 26.2MB CRF 24 film (same
# frame-indexed method as above; the fresh-encode table does NOT transfer, because
# re-compressing an already-compressed file behaves quite differently -- CRF 25 takes
# 34% off here against 13% there, so stepping from the fresh table would soften a
# video far more than the overshoot needs):
#
#     CRF 25   -34%   SSIM 0.9546      CRF 28   -56%   SSIM 0.9494
#     CRF 26   -42%   SSIM 0.9530      CRF 30   -66%   SSIM 0.9450
#     CRF 27   -50%   SSIM 0.9513
#
# One step at 25 costs about what a fresh CRF 28 would (0.9546 vs 0.9541) and buys a
# third of the file, so for any realistic overshoot one pass is enough.
#
# PROVED END TO END on a deliberately texture-heavy ten-photo render (the dense
# fine detail that mature planting puts in a landed-property frame):
#     without this pass   56.1MB   -- over Supabase's 50MB limit, upload would fail
#     with it             30.7MB   -- CRF 26 chosen, SSIM 0.9683 against the 56MB
#                                     original, frame count and duration identical
#                                     (1087 frames, 45.29s), +4.5s of render time
DELIVERY_MAX_BYTES = 40 * 1024 * 1024
# (reduction, crf) measured above, mildest first. The pass picks the FIRST row that
# clears the target with the margin below, so a file barely over the line is softened
# as little as possible.
_SHRINK_STEPS = ((0.34, "25"), (0.42, "26"), (0.50, "27"), (0.56, "28"), (0.66, "30"))
# The table came from one film. Real footage varies, so aim this much under the ceiling
# rather than exactly at it.
_SHRINK_MARGIN = 0.90
TIMEOUT_SHRINK = 180

# Say bt709 OUT LOUD, so the film's colour description is never INHERITED.
#
# This is the structural half of the HDR fix further down (see HDR_TRANSFERS). The
# assembly is a stream-copy concat, and a stream copy takes its colour description from
# the FIRST input -- so one clip carrying surprising colour tags relabels the ENTIRE
# film. That is exactly how an HLG presenter clip published a 41s film tagged HDR and
# turned the closing contact card teal.
#
# APPLIED ONLY TO THE PRESENTER OPENING CLIP, which is deliberate and was arrived at by
# measurement, not taste. The presenter clip is the only EXTERNAL video that ever enters
# the timeline -- every other clip is built here from a BMP or a JPEG we just wrote, so
# it has no colour tags to smuggle in. Tagging slide zero is therefore enough to
# guarantee the published film says bt709, and it has the large extra benefit that a
# clip uploaded BEFORE this fix (Janel's is stored HLG right now) stops relabelling the
# film as HDR too: her card comes out right on her next render, with only her own
# colour still flat until she re-uploads.
#
# WHY NOT ON EVERY CLIP (_ENCODE_ARGS). Tried, measured, REVERTED. Today's clips are
# encoded through swscale's bt601 matrix and shipped with NO tag, and the two things
# that read them disagree: ffmpeg/VLC assume bt601 and show the designed colours, while
# phones, browsers and social players assume bt709 and show them slightly
# over-saturated (measured up to 21/255 on a saturated patch). Stating bt709 on every
# clip forces everyone onto the bt709 reading -- which moved a real Classic render on
# EVERY ONE of its 445 frames, worst channel 73/255. There is no spelling that is both
# unambiguous and unchanged for every viewer, because the ambiguity IS the difference.
# Resolving it is a grading decision about hundreds of films Jane has already approved,
# so it is written up for her rather than bundled into a colour hotfix.
#
# WHY -x264-params AND NOT -colorspace. MEASURED, all four spellings, same source:
#
#   form                                            tags written          pixels
#   -colorspace bt709                               matrix only           CHANGED
#   -colorspace/-color_primaries/-color_trc         matrix only           CHANGED
#   same, stream-suffixed (:v)                      matrix only           CHANGED
#   -x264-params colorprim/transfer/colormatrix     all three             identical
#
# Two separate traps there. First, ffmpeg SILENTLY IGNORES -color_primaries and
# -color_trc when the frames reaching the encoder do not already state them, so the
# obvious spelling writes the matrix and drops the other two -- which are precisely the
# two that carried the HLG curve. Second, -colorspace does not merely label: ffmpeg
# hands it to swscale, which then really converts the matrix. -x264-params only labels.
#
# Not probed, unlike the `cas` filter: this is a libx264 private option, and libx264 is
# already a hard dependency of every encode in this file, so there is no build where
# this option is missing and the renderer otherwise works.
COLOUR_TAG_ARGS = ["-x264-params",
                   "colorprim=bt709:transfer=bt709:colormatrix=bt709"]

# Identical encoder settings on every clip, so the final concat can be a stream copy
# (no re-encode) without mismatched stream parameters.
_ENCODE_ARGS = [
    "-c:v", "libx264", "-preset", "fast", "-crf", DELIVERY_CRF,
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
# Ceiling on the whole caption pass, both keys included. This one runs INSIDE the
# render slot and against RENDER_BUDGET_SECONDS, so an unbounded caption call is worse
# than an unbounded survey: it holds a slot the fleet only has four of. Generous enough
# that a healthy pass over ten photos never approaches it.
CAPTION_BUDGET_SECONDS = 150
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
# Music bed selection
# ---------------------------------------------------------------------------
def _music_library():
    """Every usable track in audio/, sorted by filename. [] if there are none.

    Read fresh on each render rather than cached at import: a render takes a minute,
    so one listdir costs nothing, and a cache would mean a track added to the volume
    stayed invisible until the worker restarted.
    """
    try:
        names = sorted(
            name for name in os.listdir(AUDIO_DIR)
            if not name.startswith(".")
            and name.lower().endswith(AUDIO_EXTENSIONS)
            and os.path.isfile(os.path.join(AUDIO_DIR, name))
        )
    except OSError as e:
        logger.warning("could not read the music directory %s (%s)", AUDIO_DIR, e)
        return []
    return [os.path.join(AUDIO_DIR, name) for name in names]


def _music_tier(property_type):
    """Which mood register this property sits in. Anything unconfirmed is WARM.

    Blank is a common, deliberate state, not an edge case: the vision prompt in
    main.py explicitly tells agents to leave property_type empty rather than guess
    between similar landed types. Defaulting those to the grand tier would score
    unconfirmed properties as prestige, which is exactly the overclaim the brand
    cannot make -- so unknown, blank and unrecognised all land on WARM.
    """
    text = str(property_type or "").strip().lower()
    if not text:
        return TIER_WARM
    # Order matters: "Semi-Detached" contains "detached".
    if "semi" in text or "terrace" in text:
        return TIER_WARM
    if any(token in text for token in _GRAND_TYPE_TOKENS):
        return TIER_GRAND
    return TIER_WARM


def _load_manifest():
    """audio/tracks.json -> {filename: {"tier", "mood", "feel", "auto"}}. {} if untrusted.

    An entry is EITHER the original plain string ("minimal_piano.mp3": "warm", which is
    a property-tier tag and nothing else) OR an object that can carry any of:
      "tier"  "warm" | "grand"  -- the property-tier axis, exactly as before
      "mood"  "Warm", "Upbeat"  -- the label an agent sees in the music picker; a track
                                   is pickable only if it has one
      "feel"  one line shown under the label in the picker
      "auto"  false             -- keep this track out of the automatic pick, so it only
                                   plays when an agent chooses it
      "raw"   true              -- the file is an unprocessed download: at render time it
                                   is trimmed to the video length and loudness-normalized
                                   to RAW_TRACK_TARGET_LUFS. Absent/false (every existing
                                   track) means "already processed, use as-is".
    Every failure returns {} (or skips the one bad entry) and is logged, which the
    callers read as "no opinion". A typo in this file must never cost an agent their
    music bed.
    """
    try:
        with open(TRACKS_MANIFEST, "rb") as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("music manifest %s is unreadable (%s); treating every track "
                       "as eligible for every property type", TRACKS_MANIFEST, e)
        return {}
    if not isinstance(raw, dict):
        logger.warning("music manifest %s is not a JSON object; ignoring it",
                       TRACKS_MANIFEST)
        return {}
    entries = {}
    for name, value in raw.items():
        if str(name).startswith("_"):
            continue          # "_comment" and friends: notes for humans, not tracks
        if isinstance(value, dict):
            tier, mood, feel = value.get("tier"), value.get("mood"), value.get("feel")
            auto = value.get("auto", True) is not False
            raw_track = value.get("raw", False) is True
        else:
            tier, mood, feel, auto, raw_track = value, None, None, True, False
        tier = str(tier or "").strip().lower()
        if tier and tier not in MUSIC_TIERS:
            # An unrecognised tier name is treated as "untagged" rather than dropped,
            # so a typo widens that track's eligibility instead of silencing it.
            logger.warning("music manifest lists %r under unknown tier %r; leaving "
                           "that track eligible for every property type", name, tier)
            tier = ""
        entries[str(name)] = {
            "tier": tier or None,
            "mood": str(mood).strip() if mood else None,
            "feel": str(feel).strip() if feel else None,
            "auto": auto,
            "raw": raw_track,
        }
    return entries


def _track_tiers():
    """Filename -> tier, from audio/tracks.json. {} whenever it can't be trusted."""
    return {name: e["tier"] for name, e in _load_manifest().items() if e["tier"]}


def pickable_tracks():
    """The tracks an agent may choose in the music picker, in filename order.

    [{"id": "soft_piano.mp3", "mood": "Warm", "feel": "..."}, ...]. A track is listed
    only if the manifest gives it a mood AND the file is actually in audio/ right now,
    so a manifest line for a file that has not been dropped in yet (or was removed)
    never shows up as a choice that cannot play. Never raises.
    """
    try:
        present = {os.path.basename(p) for p in _music_library()}
        manifest = _load_manifest()
        return [
            {"id": name, "mood": e["mood"], "feel": e["feel"] or ""}
            for name, e in sorted(manifest.items())
            if e["mood"] and name in present
        ]
    except Exception as e:
        logger.warning("could not build the pickable music list (%s)", e)
        return []


def _is_raw_track(track_path):
    """True only if the manifest explicitly flags this file "raw": true. Any doubt (no
    manifest, unreadable manifest, no entry) answers False, i.e. the existing as-is path."""
    try:
        return bool(_load_manifest().get(os.path.basename(track_path), {}).get("raw"))
    except Exception:
        return False


def is_auto_music_choice(music_track):
    """True for the 'let NestList choose' values: missing, blank or 'auto'."""
    return str(music_track or "").strip().lower() in ("", "auto")


def resolve_pickable_track(track_id):
    """Full path of a pickable track, or None if the id is not one. Exact id match only,
    so nothing outside the curated list (or outside audio/) can be reached."""
    wanted = str(track_id or "").strip()
    for t in pickable_tracks():
        if t["id"] == wanted:
            return os.path.join(AUDIO_DIR, wanted)
    return None


# ---------------------------------------------------------------------------
# Agent-chosen photos (which listing photos go into the video, and in what order)
# ---------------------------------------------------------------------------
def _photo_choice_items(video_photos):
    """The raw request value -> a flat list of stripped, non-blank identifiers (order
    preserved, 'auto' still present). A client may send repeated params (list), one
    string, or JSON-array strings (e.g. "[]" or '["a","b"]'); JSON arrays are spliced in.
    Anything that is not a string or an iterable of strings yields []."""
    if video_photos is None:
        return []
    if isinstance(video_photos, str):
        video_photos = [video_photos]
    try:
        raw = [str(x).strip() for x in video_photos if x is not None]
    except TypeError:
        return []
    items = []
    for x in raw:
        if x.startswith("["):
            try:
                parsed = json.loads(x)
            except Exception:
                parsed = None
            if isinstance(parsed, list):
                items.extend(str(y).strip() for y in parsed if y is not None)
                continue
        items.append(x)
    return [x for x in items if x]


def is_auto_photo_choice(video_photos):
    """True for the 'let NestList choose' values: missing, empty, or 'auto'.

    Accepts None, "", "auto" (any case), [], [""], ["auto"], and the JSON-array spellings
    "[]" / '["auto"]'. Anything else is a real choice. Kept deliberately tolerant on the
    'auto' side: an unclear request must land on today's behaviour, never on a
    half-interpreted custom one.
    """
    return all(x.lower() == "auto" for x in _photo_choice_items(video_photos))


def normalize_photo_choice(video_photos):
    """The raw request value -> a flat list of non-blank identifiers, order preserved.
    Returns [] for any auto value."""
    items = _photo_choice_items(video_photos)
    if all(x.lower() == "auto" for x in items):
        return []
    return [x for x in items if x.lower() != "auto"]


def resolve_photo_choice(video_photos, image_urls):
    """Map an agent's choice onto this listing's own photos.

    Each identifier is either one of the listing's photo URLs (exact match) or a
    zero-based index into `image_urls`. Returns (urls, ignored): `urls` are the listing's
    own URLs in the order the agent gave them, first occurrence kept (a photo cannot be
    in the video twice); `ignored` are identifiers that matched nothing on this listing.
    Nothing outside `image_urls` can ever come back, so a choice can never pull in
    another listing's (or another agent's) photo.
    """
    own = [u for u in (image_urls or []) if isinstance(u, str)]
    own_set = set(own)
    urls, ignored, seen = [], [], set()
    for ident in normalize_photo_choice(video_photos):
        url = None
        if ident in own_set:
            url = ident
        elif ident.isdigit() and int(ident) < len(image_urls or []):
            cand = (image_urls or [])[int(ident)]
            url = cand if isinstance(cand, str) else None
        if url is None:
            ignored.append(ident)
        elif url not in seen:
            seen.add(url)
            urls.append(url)
    return urls, ignored


# The plain-language lines the renderer adds to `degradations` when an agent's photo
# pick could not be honoured exactly. Constants so main.py can recognise them and tell
# the agent (photo_choice_note) rather than let a video quietly show other photos.
PHOTO_NOTE_GONE = ("the photos you picked are no longer on this listing, "
                   "so the automatic photo selection was used")
PHOTO_NOTE_PARTIAL = "some of the photos you picked are no longer on this listing and were left out"
PHOTO_NOTE_UNREADABLE = ("the photos you picked could not be read, "
                         "so the automatic photo selection was used")


def photo_choice_note(degradations):
    """The first photo-pick note in a degradations list, or None if the pick was honoured
    (or the agent chose Auto)."""
    for d in degradations or []:
        if d in (PHOTO_NOTE_GONE, PHOTO_NOTE_PARTIAL, PHOTO_NOTE_UNREADABLE):
            return d
    return None


def choose_video_photos(video_photos, image_urls):
    """Render-time choice: (urls_or_None, note_or_None).

    urls is None whenever the automatic selection should be used -- either because the
    agent chose Auto, or because the choice cannot be honoured (every chosen photo has
    since been deleted, or the agent asked for two or more and fewer than two remain).
    The note, when set, is a plain-language degradation line. A stale choice must never
    fail a render."""
    if is_auto_photo_choice(video_photos):
        return None, None
    wanted = normalize_photo_choice(video_photos)
    urls, _ignored = resolve_photo_choice(wanted, image_urls)
    urls = urls[:MAX_VIDEO_PHOTOS]
    if not urls or (len(set(wanted)) >= 2 and len(urls) < 2):
        return None, PHOTO_NOTE_GONE
    if len(urls) < len(set(wanted)):
        return urls, PHOTO_NOTE_PARTIAL
    return urls, None


def _select_music_track(seed, property_type=None):
    """Pick this listing's track. Same seed + same library -> always the same file.

    Determinism is the requirement, not variety-for-its-own-sake: an agent who
    regenerates a video must get back the film they already approved, so a random
    pick is wrong. Python's built-in hash() is also wrong -- it is salted per process
    (PYTHONHASHSEED), so two uvicorn workers would disagree about the same listing.
    sha256 is stable across processes, machines and Python versions.

    The pick is rendezvous ("highest random weight") hashing rather than
    sha256(seed) % len(tracks), because the modulus form is keyed on a track's
    POSITION in the list: adding one file to the library renumbers everything and
    silently reassigns the music of nearly every existing listing. Rendezvous hashing
    keys on the track's own filename instead, so adding a track moves only the ~1/N of
    listings that land on it, and removing a track moves only the listings that had it.
    Everyone else keeps the music they had. Distribution is uniform either way.

    Scoping to the tier happens BEFORE the hash, so the minimal-churn property above
    holds within a tier. Retagging a track from one tier to the other is itself a
    reassignment event -- it moves that track's listings out and pulls roughly 1/N of
    the destination tier's listings in -- so tiers are worth settling before the
    library grows, not after.
    """
    tracks = _music_library()
    if not tracks:
        return None, None
    manifest = _load_manifest()
    # Tracks flagged "auto": false are picker-only. If that somehow excludes everything,
    # use the whole library rather than ship a silent video.
    auto_ok = [p for p in tracks
               if manifest.get(os.path.basename(p), {}).get("auto", True)]
    tracks = auto_ok or tracks
    tier = _music_tier(property_type)
    tagged = {n: e["tier"] for n, e in manifest.items() if e["tier"]}
    # A track with no usable entry stays eligible everywhere. That way an untagged
    # file is over-used rather than unplayable, and a missing or broken manifest
    # degrades to exactly the behaviour we had before tiers existed.
    candidates = [p for p in tracks
                  if tagged.get(os.path.basename(p), tier) == tier]
    if not candidates:
        # Nothing is tagged for this tier. Better a track from the wrong register
        # than a silent video.
        logger.warning("no music track is tagged for the %r tier; choosing from the "
                       "whole library instead", tier)
        candidates = tracks
    if len(candidates) == 1:
        return candidates[0], tier
    key = str(seed or "")

    def weight(path):
        blob = f"{key}\x00{os.path.basename(path)}".encode("utf-8")
        return hashlib.sha256(blob).digest()

    return max(candidates, key=weight), tier


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


# ---------------------------------------------------------------------------
# The agent's presenting clip
# ---------------------------------------------------------------------------
# The Signature tier opens with the agent themselves, speaking to camera. That clip
# is REAL FOOTAGE the agent uploads once -- not a generated avatar. Every attempt to
# synthesise an agent into a property failed the same way: the likeness drifts within
# two seconds, or the room has to be invented to keep the person convincing. Real
# footage has neither problem, and it is unarguably them.
PRESENTER_MAX_SECONDS = 12.0      # what we keep; the film uses ~5s of it
PRESENTER_LONG_EDGE = 1280        # a phone's 4K is wasted here and costs upload time
PRESENTER_MIN_SECONDS = 1.5       # shorter than this is a mis-tap, not a recording

# WHERE the kept seconds start. People do not begin talking the instant they tap record:
# measured on Janel's own recording, she is silent for the first 2 seconds and speaks
# from about 2s to 9s. Taking the first SECONDS_PER_PHOTO from zero would therefore have
# opened the film with two seconds of her standing there saying nothing and then cut her
# off mid-sentence -- using the wrong five seconds of a perfectly good take.
#
# So the clip is trimmed to where the speech starts. This is deliberately automatic
# rather than a "choose your start point" control: the agent has already done the work by
# recording, and every manual step we add is one more thing for them to get wrong.
# "Quiet" has to be measured against the clip's OWN loudness, not a fixed number. A
# fixed -45dB threshold was tried first and never fired on Janel's recording: her room's
# noise floor peaks at -42dB, so the opening silence was never quiet enough to count. A
# fixed -40dB worked on that one clip and would simply have been tuned to that one room
# -- a noisier street-side recording would fail it again, and a studio-quiet one would
# have been trimmed too eagerly. So the threshold is set this far below the loudest
# moment in the clip, which is their own voice.
PRESENTER_SILENCE_BELOW_PEAK = 20.0
# ...clamped, so a clip that is loud throughout (or almost silent throughout) cannot
# produce an absurd threshold.
PRESENTER_SILENCE_DB_RANGE = (-60.0, -25.0)
PRESENTER_SILENCE_MIN = 0.4       # a gap has to last this long to count as a gap
PRESENTER_LEAD_IN = 0.25          # kept in front of the first word, so it is not clipped

# How loud the agent ends up is otherwise decided by how they filmed: phone at arm's
# length in a quiet room is a FAR lower level than one held close. Measured on Janel's
# own recording, her voice landed at -33.5dB in the finished film while the music -- even
# ducked to PRESENTER_MUSIC_DUCK -- sat at -25.4dB, so the music was comfortably louder
# than the person speaking. No fixed duck can fix that, because the problem is the source
# level, not the balance.
#
# So the voice is normalised ONCE, on upload, to a speech loudness target -- the same
# treatment the "raw" music tracks get, and for the same reason. Baking it into the
# stored clip rather than doing it per render means the work happens once, and the render
# path needs to know nothing about it.
#
# -18 rather than a broadcast -16: it is a little conservative on purpose. Normalisation
# lifts the room tone along with the voice, and a quiet recording needs a lot of gain to
# reach -16.
PRESENTER_VOICE_LUFS = -18.0
PRESENTER_VOICE_TRUE_PEAK_DB = -2.0
PRESENTER_VOICE_LRA = 11

# How the agent's voice sits against the music bed. Their clip is real footage of them
# speaking, so the sound is the point of it -- a presenting clip with its audio stripped
# is just a photo that moves. The music ducks under them and comes back up once the film
# moves on to the rooms.
PRESENTER_VOICE_VOLUME = 0.92     # leaves headroom so voice + ducked music cannot clip
PRESENTER_MUSIC_DUCK = 0.30       # music multiplier while they are speaking
PRESENTER_DUCK_RELEASE = 0.9      # seconds the music takes to come back to full
PRESENTER_VOICE_FADE = 0.25       # so their last syllable is not cut off mid-word
# Phones record mono or stereo depending on the handset, and that difference silently
# changed how loud the agent was. MEASURED on a mono source (a 4.5s window, mean volume):
#
#     no conversion                                       -21.1 dB
#     aformat=channel_layouts=stereo                      -24.1 dB   <- 3dB lost
#     aformat=channel_layouts=mono,pan=stereo|c0=c0|c1=c0 -21.1 dB
#
# The 3dB is the resampler's power-preserving rematrix: spreading one channel across two
# halves the power in each. aformat does NOT avoid it -- it asks for stereo and gets the
# same rematrix -- so pan is the fix, duplicating the channel verbatim instead. Forcing
# mono FIRST makes the same string correct for a stereo recording (a downmix of
# correlated channels keeps its level; measured at -21.1 dB too), which matters because
# we cannot know what the agent filmed on.
#
# A single speaking voice has no stereo image worth preserving, so collapsing it costs
# nothing. The music is NOT put through this -- it is a real stereo mix.
PRESENTER_VOICE_TO_STEREO = "aformat=channel_layouts=mono,pan=stereo|c0=c0|c1=c0"
# Shared sample rate so the two layers meet amix already agreeing, rather than through
# whichever conversion it would have inserted. Deliberately says nothing about channels.
PRESENTER_MIX_FORMAT = "aformat=sample_fmts=fltp:sample_rates=44100"


def _speech_start_seconds(path, duration, keep_at_least):
    """How far into `path` the agent actually starts talking. 0.0 whenever unsure.

    Every failure mode answers 0.0, which is exactly the behaviour this function
    replaced -- so a clip that cannot be analysed is kept whole rather than refused or
    trimmed on a guess.
    """
    if not duration or duration <= 0:
        # Without a duration there is no way to guarantee the clamp below leaves enough
        # material, and trimming past the end would turn a good recording into one the
        # minimum-length check rejects. Not trimming is always safe.
        return 0.0
    try:
        # -vn on both passes: these measure AUDIO, and without it ffmpeg decodes every
        # video frame of the upload to throw it away. Measured on a 180MB phone clip:
        # 0.60s with the video decode, 0.03s without, per pass, per upload.
        peak = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", path, "-vn",
             "-af", "volumedetect", "-f", "null", "-"],
            capture_output=True, timeout=TIMEOUT_PROBE).stderr.decode("utf-8", "replace")
        loudest = re.search(r"max_volume:\s*(-?[\d.]+) dB", peak)
        if not loudest:
            logger.warning("could not measure the presenter clip's level; keeping it "
                           "from the beginning")
            return 0.0
        low, high = PRESENTER_SILENCE_DB_RANGE
        threshold = min(high, max(low, float(loudest.group(1)) - PRESENTER_SILENCE_BELOW_PEAK))
        err = subprocess.run(
            ["ffmpeg", "-hide_banner", "-i", path, "-vn", "-af",
             f"silencedetect=noise={threshold:.1f}dB:d={PRESENTER_SILENCE_MIN}",
             "-f", "null", "-"],
            capture_output=True, timeout=TIMEOUT_PROBE).stderr.decode("utf-8", "replace")
    except Exception as e:
        logger.warning("could not look for the start of speech in %s (%s); "
                       "keeping the clip from the beginning", path, e)
        return 0.0

    # Only a silence that the clip OPENS with is of any interest. A pause in the middle
    # is part of how they speak, and skipping to it would drop their first sentence.
    opening = re.search(r"silence_start:\s*(-?[\d.]+)", err)
    if not opening or float(opening.group(1)) > PRESENTER_SILENCE_MIN:
        return 0.0
    ends = re.search(r"silence_end:\s*([\d.]+)", err)
    if not ends:
        # Silent from start to finish: either a muted recording or one filmed somewhere
        # very quiet. Trimming would leave nothing, and the clip may still be usable
        # (the film has music over it either way), so keep it whole.
        logger.info("presenter clip has no detectable speech; keeping it from the beginning")
        return 0.0

    start = max(0.0, float(ends.group(1)) - PRESENTER_LEAD_IN)
    # Never trim so far in that the film has less than a full slide to open with.
    start = min(start, max(0.0, duration - keep_at_least))
    if start < PRESENTER_SILENCE_MIN:
        return 0.0            # nothing worth skipping
    logger.info("presenter clip: speech starts at %.2fs; trimming the lead-in", start)
    return start


# ---------------------------------------------------------------------------
# HDR (HLG/PQ) presenter clips -> true SDR bt709
# ---------------------------------------------------------------------------
# WHAT WENT WRONG, so nobody re-derives it from the symptom. Modern iPhones record HDR
# by default: Janel's clip probes as yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67) --
# arib-std-b67 is HLG. The old transcode here brought it down to 8-bit yuv420p but did
# NOT tonemap and did NOT retag, so the stored clip kept HLG-curve samples AND the
# bt2020/HLG labels. That clip becomes slide zero of a Signature, and because the
# assembly is a stream-copy concat (which takes its stream parameters from the FIRST
# input), the whole 41s film was published tagged as HDR. Players then applied an
# HDR->SDR transform to footage that was mostly ordinary SDR, and the closing contact
# card -- designed warm off-white -- came out teal on Jane's screen.
#
# MEASURED, so the blame is not guesswork: the card's PIXELS are correct and byte-for-
# byte identical to the ones a Classic ships (same md5 on the raw YUV), and a Classic
# carries no colour tags at all. Same pixels, different label, different picture. The
# card renderer is not involved.
#
# Relabelling the finished film bt709 does fix the card, but it leaves the AGENT flat
# and washed out, because her samples really are HLG-encoded: measured on the real HDR
# sample, mean colourfulness 0.105 relabelled against 0.163 properly tonemapped. The
# agent is the whole point of a Signature, so the conversion belongs HERE, once, at
# upload -- after which the stored clip matches everything else and nothing downstream
# has to know.
#
# The transfer characteristics that mean "this is HDR and needs converting". Anything
# else -- bt709, smpte170m, an untagged recording -- is left COMPLETELY alone, argument
# for argument, so an SDR upload comes through exactly as it did before this change.
HDR_TRANSFERS = ("arib-std-b67", "smpte2084")   # HLG, PQ

# Rung 1. zscale (libzimg) to linear light, tonemap, back out to bt709.
#
# The numbers are MEASURED on the real HLG sample (an iPhone 16 Pro Max HLG recording),
# averaged over four frames, against the relabel-only result as the baseline. Do not
# re-tune these from first principles; the alternatives were tested and rejected:
#
#   tonemap   npl   desat   mean luma   contrast   colourfulness   blown highlights
#   (none)      -       -       180.4      203.5         0.105            0.0%   <- relabel only
#   hable     100     0.0       182.4      208.5         0.163            0.1%   <- SHIPPED
#   hable     203     0.0       155.1      188.9         0.163            0.0%   too dark
#   hable     100     0.5       208.2      222.5         0.092           59.1%   blows out
#   mobius    100     0.0       205.3      203.1         0.161           51.2%   blows out
#   reinhard  100     0.0       200.8      200.3         0.162           24.5%   blows out
#   clip      100       -       211.9      206.0         0.151           79.8%   blows out
#
# hable at npl=100 with desat=0 is the only row that holds the exposure Jane already
# approved (182 against 180) while recovering the colour (0.163 against 0.105) and
# blowing nothing. desat defaults to 2, which rolls highlights towards white to protect
# hue -- on a face lit by a window that clipped 59% of the frame, so it is turned OFF
# deliberately. npl is the nominal peak the source is read against: 1000 (HLG's nominal
# peak) renders her two stops dark, because HLG is scene-referred and this footage is
# graded for a normal room, not a 1000-nit highlight.
#
# zscale needs its INPUT colour fully specified or zimg answers "no path between
# colorspaces" and the encode fails -- which is why _source_colour reads the real tags
# off the source rather than letting the filter guess.
TONEMAP_ALGO = "hable"
TONEMAP_PEAK_NITS = 100
# Rung 2. No zscale (libzimg is a build-time option and Railway installs Debian's
# ffmpeg), so linear light is unreachable and so is tonemap, which only eats float.
# `colorspace` is core libavfilter and can still fix the MATRIX and the GAMUT, which is
# most of the cast; it cannot undo the HLG curve, so the agent stays slightly flat.
# MEASURED on the same sample: colourfulness 0.131 against 0.105 relabelled and 0.163
# properly tonemapped -- about 45% of the gap closed, exposure and contrast untouched,
# nothing blown. HLG only: PQ read as a gamma curve comes out very dark, so a PQ clip
# skips this rung.
GAMUT_ONLY_FILTER = "colorspace=all=bt709:iall=bt2020:fast=0"
# Rung 3 is no filter at all -- the clip is merely RELABELLED bt709. Strictly better
# than today (the card and the photos come out right), but the agent is flat, so it is
# logged and reported as a degradation rather than passing silently.
VIDEO_NOTE_HDR_NOT_TONEMAPPED = (
    "your clip was recorded in HDR and this server could not convert it fully, so you "
    "may look a little flat in the video"
)

_hdr_rung_cache = None
_hdr_rung_lock = threading.Lock()


def _source_colour(path):
    """The colour tags ffmpeg reports for `path`, read off its BANNER.

    Returns {"range","matrix","primaries","transfer"} with None for anything the file
    does not state, or None if the banner could not be read at all.

    Banner, not ffprobe. ffprobe is a separate binary and this codebase has already
    been bitten twice by assuming it is installed -- see _duration_via_ffmpeg. ffmpeg
    prints the colour description inside the pixel-format parentheses, in one of two
    shapes depending on whether the three values agree:

        yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67)   <- matrix/primaries/transfer
        yuv420p(tv, bt709, progressive)                 <- all three are bt709
        yuvj420p(pc, bt470bg/unknown/unknown, progressive)
        yuv420p(progressive)                            <- states nothing at all
    """
    try:
        err = subprocess.run(["ffmpeg", "-hide_banner", "-i", path],
                             capture_output=True, timeout=TIMEOUT_PROBE
                             ).stderr.decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("could not read colour tags from ffmpeg for %s (%s)", path, e)
        return None

    line = next((l for l in err.splitlines() if "Video:" in l), None)
    if not line:
        return None
    # The range flag is the anchor: ffmpeg only prints a colour description when it
    # prints tv/pc in front of it, so finding one finds the other.
    m = re.search(r"\b(tv|pc|limited|full)\s*,\s*([\w-]+)(?:/([\w-]+)/([\w-]+))?", line)
    if not m:
        return {"range": None, "matrix": None, "primaries": None, "transfer": None}

    def clean(v):
        return None if (not v or v == "unknown") else v

    rng, first, second, third = m.group(1), m.group(2), m.group(3), m.group(4)
    if second is None:
        # The single-value shape: one name standing for matrix, primaries and transfer.
        matrix = primaries = transfer = clean(first)
    else:
        matrix, primaries, transfer = clean(first), clean(second), clean(third)
    return {"range": {"limited": "tv", "full": "pc"}.get(rng, rng),
            "matrix": matrix, "primaries": primaries, "transfer": transfer}


def _hdr_to_sdr_filter(colour):
    """The best HDR->SDR chain this ffmpeg build can actually run for `colour`.

    Returns (filter_string, note). An empty filter_string means rung 3 -- relabel only
    -- and `note` is then the degradation to report. Worked out once per process and
    cached, because it is two filter probes and it would otherwise run per upload.

    Probing rather than assuming is the house rule here: an unknown filter name makes
    ffmpeg exit non-zero, and this string goes into the ONE encode an agent is sitting
    and waiting for. See _clarity_filter, which does the same thing for `cas` and for
    the same reason.
    """
    global _hdr_rung_cache
    with _hdr_rung_lock:
        if _hdr_rung_cache is None:
            if _has_ffmpeg_filter("zscale") and _has_ffmpeg_filter("tonemap"):
                _hdr_rung_cache = "tonemap"
            elif _has_ffmpeg_filter("colorspace"):
                logger.warning("ffmpeg has no 'zscale'/'tonemap' filter; HDR clips will "
                               "be gamut-corrected only and will look slightly flat")
                _hdr_rung_cache = "gamut"
            else:
                logger.warning("ffmpeg can neither tonemap nor convert gamut; HDR clips "
                               "will only be relabelled bt709")
                _hdr_rung_cache = "relabel"
        rung = _hdr_rung_cache

    matrix = colour.get("matrix") or "bt2020nc"
    primaries = colour.get("primaries") or "bt2020"
    transfer = colour.get("transfer")
    rng = colour.get("range") or "tv"

    if rung == "tonemap":
        logger.info("presenter clip is HDR (%s); tonemapping to bt709", transfer)
        return (
            # Into linear light, with the source's own tags spelled out -- zimg refuses
            # to work from guesses.
            f"zscale=min={matrix}:pin={primaries}:tin={transfer}:rin={rng}"
            f":t=linear:npl={TONEMAP_PEAK_NITS}:p={primaries}:m={matrix},"
            # tonemap only accepts floating-point linear light.
            "format=gbrpf32le,"
            f"tonemap=tonemap={TONEMAP_ALGO}:desat=0,"
            # ...and back out to a plain SDR bt709 video range.
            "zscale=p=bt709:t=bt709:m=bt709:r=tv"
        ), None
    if rung == "gamut" and transfer == "arib-std-b67":
        logger.warning("presenter clip is HLG and this ffmpeg cannot tonemap; "
                       "correcting the gamut only -- the agent will look a little flat")
        return GAMUT_ONLY_FILTER, VIDEO_NOTE_HDR_NOT_TONEMAPPED
    logger.warning("presenter clip is HDR (%s) and this ffmpeg cannot convert it; "
                   "relabelling bt709 so the rest of the film is right, but the agent "
                   "will look flat", transfer)
    return "", VIDEO_NOTE_HDR_NOT_TONEMAPPED


def prepare_presenter_clip(raw_bytes, workdir):
    """Normalise an agent's uploaded clip into something the renderer can always read.

    Phones produce HEVC in a .MOV at whatever resolution they feel like; some of that
    will not decode on the render worker, and none of it should be trusted to be a
    sensible length. So the upload is transcoded ONCE, here, rather than hoping at
    render time: trimmed, scaled, re-encoded to H.264/AAC.

    Doing it on upload rather than per render also means a bad file fails while the
    agent is still looking at the screen, instead of silently breaking a video they
    ordered three days later.

    Returns (mp4_bytes, seconds, note). `note` is None on the normal path, and a
    sentence for the agent when the clip was HDR and this ffmpeg could not convert it
    all the way -- see HDR_TRANSFERS above for why that matters and what it costs.

    Raises ValueError with a sentence the agent can act on -- this runs on a path where
    a human is waiting.
    """
    src = os.path.join(workdir, "presenter_in")
    out = os.path.join(workdir, "presenter_out.mp4")
    with open(src, "wb") as f:
        f.write(raw_bytes)

    # Skip the dead air before they start talking, so the kept seconds are the ones the
    # film actually opens on. Answers 0.0 on any doubt, which is what this did before.
    # Read from ffmpeg's banner, not ffprobe -- ffprobe is a separate binary and may not
    # be installed on the worker at all.
    start_at = _speech_start_seconds(src, _duration_via_ffmpeg(src), SECONDS_PER_PHOTO)

    # Is this an HDR recording? Modern iPhones say yes by default. Only an HDR clip gets
    # converted; an SDR one takes the branch below with the argument list it has always
    # had, untouched, so nothing about an ordinary upload changes.
    colour = _source_colour(src) or {}
    is_hdr = colour.get("transfer") in HDR_TRANSFERS
    note = None
    hdr_chain = ""
    if is_hdr:
        hdr_chain, note = _hdr_to_sdr_filter(colour)

    # The scale is unchanged; the conversion (when there is one) goes AFTER it, so the
    # expensive float tonemap runs on the 1280-long-edge frame rather than the phone's
    # full 4K -- same result, a fraction of the work.
    scale = ("scale='if(gt(iw,ih),min(%d,iw),-2)':'if(gt(iw,ih),-2,min(%d,ih))'"
             % (PRESENTER_LONG_EDGE, PRESENTER_LONG_EDGE))

    # ffmpeg IS the validator. An earlier version probed the input first and treated a
    # failed probe as "not a video" -- which would have rejected every upload on a
    # worker where ffprobe was missing, while ffmpeg itself was working fine. Let the
    # transcode decide: if it cannot read the file, it is not a file we can use.
    def transcode(chain):
        _run_ffmpeg([
            "ffmpeg", "-y", "-v", "error",
            # Before -i: seek the INPUT, so -t below counts from the first word rather
            # than from the start of the file.
            ] + (["-ss", "%.3f" % start_at] if start_at else []) + [
            "-i", src, "-t", "%.2f" % PRESENTER_MAX_SECONDS,
            # Scale the LONG edge, whichever way up it was filmed, and keep both
            # dimensions even -- H.264 refuses odd ones.
            "-vf", ",".join([scale] + ([chain] if chain else []) + ["format=yuv420p"]),
            "-c:v", "libx264", "-crf", "20", "-preset", "medium",
            # Only on the HDR branch, so an SDR upload runs the argument list it always
            # has. On an HDR one the tag is the whole point: even when the conversion
            # falls through to the bottom rung, the film must stop claiming to be HDR.
            ] + (COLOUR_TAG_ARGS if is_hdr else []) + [
            # Even out how loud they recorded, so the film's balance does not depend on
            # how they held the phone. Single-pass loudnorm: approximate, but the gap it
            # is closing is tens of dB, not tenths.
            "-af", (f"loudnorm=I={PRESENTER_VOICE_LUFS}:TP={PRESENTER_VOICE_TRUE_PEAK_DB}"
                    f":LRA={PRESENTER_VOICE_LRA}"),
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
            "-movflags", "+faststart", out,
        ], timeout=240)

    try:
        transcode(hdr_chain)
    except RuntimeError as e:
        if hdr_chain:
            # The colour conversion is the ONLY thing that can have gone wrong that is
            # not the agent's file -- zimg is fussy about unusual inputs, and a tonemap
            # that will not run must never cost a good recording. Drop to the bottom
            # rung (relabel bt709, no pixel change) and try once more; the agent still
            # gets a usable clip, and the card still comes out right.
            logger.warning("HDR conversion failed for this presenter clip (%s); "
                           "falling back to relabelling it bt709", str(e)[:200])
            note = VIDEO_NOTE_HDR_NOT_TONEMAPPED
            try:
                transcode("")
            except RuntimeError as e2:
                logger.warning("presenter clip rejected: %s", str(e2)[:300])
                raise ValueError("That file does not look like a video we can read. "
                                 "An MP4 or MOV straight from your phone works best.")
        else:
            logger.warning("presenter clip rejected: %s", str(e)[:300])
            raise ValueError("That file does not look like a video we can read. "
                             "An MP4 or MOV straight from your phone works best.")

    if not os.path.exists(out) or os.path.getsize(out) < 1000:
        raise ValueError("That file does not look like a video we can read. "
                         "An MP4 or MOV straight from your phone works best.")

    # Duration from the OUTPUT, which we know is readable. ffprobe first, then
    # ffmpeg's own banner -- NOT a constant, because falling back to the cap would
    # quietly disable the minimum-length check below on any worker without ffprobe.
    seconds = _probe_duration(out, -1.0)
    if seconds <= 0:
        seconds = _duration_via_ffmpeg(out)
    if not seconds or seconds <= 0:
        # Both readers failed. Let it through at the cap rather than refuse a clip
        # that transcoded perfectly well; the film trims to what it needs anyway.
        logger.warning("could not determine presenter clip duration; assuming the cap")
        seconds = PRESENTER_MAX_SECONDS
    if seconds < PRESENTER_MIN_SECONDS:
        raise ValueError("That clip is too short. Record about ten seconds of yourself "
                         "speaking to the camera, and we will use the best few.")

    with open(out, "rb") as f:
        data = f.read()
    return data, seconds, note


def _duration_via_ffmpeg(path):
    """Duration parsed out of ffmpeg's own banner. Returns None if it cannot tell.

    Exists because _probe_duration needs ffprobe, and ffprobe is a SEPARATE binary --
    a worker can have a perfectly good ffmpeg and no ffprobe. When that happened the
    duration silently fell back to the cap, which quietly disabled the minimum-length
    check and let a half-second mis-tap through as if it were a recording.
    """
    try:
        err = subprocess.run(["ffmpeg", "-hide_banner", "-i", path],
                             capture_output=True, timeout=TIMEOUT_PROBE).stderr
        m = re.search(rb"Duration: (\d+):(\d\d):(\d\d(?:\.\d+)?)", err)
        if m:
            h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
            return h * 3600 + mi * 60 + s
    except Exception as e:
        logger.warning("could not read duration from ffmpeg for %s (%s)", path, e)
    return None


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


# One photo download may take this long before it is cut off, and a whole _fetch_image
# call (both attempts plus the sleep between them) this long. These are DEADLINES, not
# read timeouts: requests' timeout=(connect, read) is per READ, so a server dribbling a
# byte just inside the read timeout resets the clock forever and the download never
# ends. See slow_read for the measurements.
#
# 30s is deliberately loose. A listing photo is 100KB-3MB and its 4x upscaled twin a
# few MB more; on a slow mobile-tethered CDN read that is still minutes of headroom per
# megabyte, so a healthy fetch never comes near this. The number only has to be small
# enough that a stuck one cannot pin a worker.
FETCH_ATTEMPT_SECONDS = 30
FETCH_TOTAL_SECONDS = 70


def _fetch_image(url, attempts=2, deadline=None):
    """Downloads one photo. Streams with a hard byte cap so a mis-uploaded 200MB file
    can't exhaust memory, and retries once because a single flaky CDN read shouldn't
    cost the agent a whole video.

    `deadline` is an absolute time.monotonic() value and bounds the WHOLE call,
    attempts included -- the survey pass uses it to share one budget across fifteen
    candidate photos. Omitted, the call bounds itself at FETCH_TOTAL_SECONDS.
    """
    overall = deadline if deadline is not None else time.monotonic() + FETCH_TOTAL_SECONDS
    last_error = None
    for attempt in range(attempts):
        if time.monotonic() >= overall:
            last_error = last_error or TimeoutError("ran out of time before starting")
            break
        attempt_deadline = min(time.monotonic() + FETCH_ATTEMPT_SECONDS, overall)
        try:
            # NOT a `with` block. Letting requests close a streamed response tidily
            # means waiting out a body that is still arriving in a trickle -- measured
            # at 80s past the deadline during the Signature work, which is exactly what
            # this must not do. The `finally` abandons the connection instead.
            response = requests.get(url, timeout=(5, 25), stream=True)
            try:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"photo is {int(declared) // 1024 // 1024}MB, over the {MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit")
                buffer = io.BytesIO()
                # THE WATCHDOG. The deadline check inside the loop cannot fire while a
                # read is blocked on a chunk that is filling too slowly; shutting the
                # socket down from another thread is what unblocks it.
                try:
                    with slow_read.deadline_watchdog(response, attempt_deadline):
                        for chunk in response.iter_content(chunk_size=64 * 1024):
                            if time.monotonic() > attempt_deadline:
                                raise TimeoutError("photo download passed its deadline")
                            buffer.write(chunk)
                            if buffer.tell() > MAX_DOWNLOAD_BYTES:
                                raise ValueError(f"photo exceeded the {MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit while downloading")
                except ValueError:
                    raise
                except Exception:
                    # A read the watchdog interrupted surfaces here as an ordinary
                    # transport error ("Response ended prematurely"). Past the deadline
                    # that is what it is, and saying so is far more use in the logs than
                    # leaving someone to wonder which CDN broke.
                    if time.monotonic() > attempt_deadline:
                        raise TimeoutError("photo download passed its deadline")
                    raise
            finally:
                # Always, on every path: this connection is never reused, and the tidy
                # close is the slow one.
                slow_read.abandon_response(response)
            if time.monotonic() > attempt_deadline:
                # Either the watchdog cut the read, or the body ended just as the
                # deadline passed. An interrupted read can look like a clean end of
                # body, so a download that ran past its deadline is never trusted to
                # be complete -- decoding it would risk half a photo in the film.
                raise TimeoutError("photo download passed its deadline")
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
            if attempt + 1 < attempts and time.monotonic() + 1.0 < overall:
                time.sleep(1.0)
    raise RuntimeError(f"could not fetch photo: {last_error}")


def _fetch_listing_photo(url):
    """A listing photo, preferring its upscaled twin when one exists.

    Agents are given portal-resized ~800x600 photos, which this renderer then has to
    STRETCH into a 2808x3840 working frame. photo_upscale stores a 4x version alongside
    the original at upload time; using it means the working frame is DOWNSAMPLED instead,
    which is where the measured sharpness gain comes from.

    Strictly best-effort. A miss here is not an error and is not a degradation worth
    reporting to the agent -- the original is the photo the listing has always used, and
    a video built from it is exactly what we shipped before. Reasons a miss is normal:
    upscaling is off, the photo predates the feature, the background job hasn't reached
    it yet, or its upscale failed the fidelity check.
    """
    hires = photo_upscale.hires_url_for(url)
    if hires:
        try:
            # One attempt only: the hires file either exists or it doesn't, and retrying
            # a 404 just delays a render that is going to use the original anyway.
            img = _fetch_image(hires, attempts=1)
            logger.info("using the upscaled copy of a photo (%dx%d)", img.width, img.height)
            return img
        except Exception as e:
            logger.debug("no upscaled copy available (%s); using the original", e)
    return _fetch_image(url)


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
    itself. Returns (composite, foreground_fills_width).

    Nothing is cropped out of the foreground -- the sides a 9:16 cover-crop
    would lose are all still there, and the backdrop fills the frame so there are no
    bars.

    The second return value tells the clip builder whether the sharp photo reaches
    both edges of the composite. It normally does; it does not for a source TALLER
    than 9:16, which is bounded by height and leaves blurred margins at the sides.
    Drifting such a frame sideways would slide the sharp photo off-centre inside its
    own margins, which reads as a layout mistake rather than a camera move, so those
    shots keep the centred zoom.

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
    return background, fg_w >= w


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
- NEVER name a room type you cannot actually see the evidence for. This is the rule
  that matters most: a caption is a claim about the property, and a wrong one is a
  misrepresentation the agent has to answer for.
    * "the kitchen" needs a worktop, a sink or a hob IN THE PHOTO.
    * "the dining hall" needs a dining table laid out as a dining table -- a desk, a
      work table or a table pushed against a wall is NOT a dining hall.
    * "the master suite" or "the bedroom" needs a bed.
    * "the pool" needs water.
  If the defining feature is not visible, do NOT guess the room from the style of the
  furniture, the flooring, or what a house "usually" has next to it.
- When you cannot tell what a space is, say something true about what IS there --
  "the open living space", "morning light through the windows", "the upper landing",
  "where the day begins". A quieter caption that is true always beats a specific one
  that is wrong.
- A space can serve two purposes. If a room holds both a work table and a dining
  table, do not pick one and hide the other -- describe the room as it is
  ("the flexible room ... where work and home sit side by side").

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


def _anthropic_keys():
    """The keys to try, in order. Empty means the feature is off, not broken."""
    return [k for k in (os.environ.get("ANTHROPIC_API_KEY", ""),
                        os.environ.get("ANTHROPIC_API_KEY_BACKUP", "")) if k]


def _photo_content(photos, prompt):
    """The message body for a batched vision call, or None if there is no key to send
    it with. Numbered so the model's "n" refers to a photo we can identify."""
    if not _anthropic_keys():
        return None
    content = []
    for i, photo in enumerate(photos):
        content.append({"type": "text", "text": f"Photo {i + 1}:"})
        content.append(_photo_to_block(photo))
    content.append({"type": "text", "text": prompt})
    return content


def _vision_call(content, timeout, max_tokens=1024, budget=None):
    """One batched vision call -> (raw text, last error). Never raises.

    Shared by the caption pass and the survey pass so there is one place that knows
    about keys, timeouts and retry policy.

    `budget` is a wall-clock ceiling in seconds for the WHOLE attempt sequence, and it
    is ENFORCED, not merely checked between attempts.

    That distinction is the whole point. The SDK's `timeout` is handed to httpx, where
    a read timeout is PER READ: an endpoint dribbling a byte just inside it resets the
    clock forever, so one attempt can run without limit while every individual read
    looks healthy. Checking the budget between keys cannot help, because the check does
    not run while the call is blocked. Measured consequence of leaving it advisory: one
    key at timeout=75 with max_retries=1 can burn ~150s before the budget is consulted
    at all, and a dribbling endpoint has no bound whatsoever.

    So each attempt runs under an absolute deadline that returns control whether or not
    the SDK is finished (see slow_read.call_with_deadline). Unlike a photo download
    there is no socket we can reach -- the SDK owns its httpx client -- so the stuck
    attempt is abandoned rather than interrupted: we stop waiting, the caller falls
    back, and the daemon thread dies when httpx eventually gives up. It holds no lock
    the render needs and writes nothing.
    """
    deadline = time.monotonic() + (budget if budget is not None else timeout * 2 + 30)
    raw = None
    last_error = None
    for key in _anthropic_keys():
        if time.monotonic() >= deadline:
            logger.warning("vision call: budget spent, not trying another key")
            break

        def attempt(key=key):
            import anthropic
            # max_retries=1: the render already has a wall-clock budget, and the SDK's
            # default of 2 retries on top of the timeout can burn three minutes on a
            # degraded API before we fall back.
            client = anthropic.Anthropic(api_key=key, timeout=timeout, max_retries=1)
            response = client.messages.create(
                model=CAPTION_MODEL,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": content}],
            )
            return "".join(b.text for b in response.content
                           if getattr(b, "type", "") == "text")

        try:
            raw = slow_read.call_with_deadline(attempt, deadline, name="vision call")
            break
        except Exception as e:
            last_error = e
            continue
    return raw, last_error


def _clean_caption(value, copy_guard, index=0):
    """One model-written caption -> a caption safe to burn into a video, or None.

    Pulled out of _generate_room_captions so the survey pass (which writes captions in
    the same call that classifies the rooms) goes through EXACTLY these guards rather
    than a second copy of them that can drift.
    """
    try:
        text = str(value).strip().lower()
    except Exception:
        return None
    text = re.sub(r"\s*\.\.\.\s*", CAPTION_SEPARATOR, text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if copy_guard:
        # The same guard every other copy surface runs through (house/unit numbers,
        # price leaks). It never raises and never returns empty.
        try:
            text = (copy_guard(text, context="video-caption") or "").strip().lower()
        except Exception as e:
            logger.warning("caption copy guard failed (%s); dropping caption", e)
            return None
    ok, reason = _caption_is_safe(text)
    if not ok:
        logger.warning("caption %d rejected (%s): %r", index + 1, reason, text)
        return None
    return text


def _generate_room_captions(photos, copy_guard=None):
    """One batched vision call for the whole video: all photos in, one caption each
    out. Returns a list the same length as `photos`, with None wherever no safe caption
    could be produced. Never raises -- a caption-free video is a valid outcome.

    Batching matters for more than cost: it is one network dependency and one timeout
    for the whole render, instead of one per photo, and it lets the model see the
    property as a set so it does not caption three different rooms "the living hall"."""
    content = _photo_content(photos, _CAPTION_PROMPT.format(n=len(photos)))
    if content is None:
        logger.warning("captions skipped: no Anthropic API key configured")
        return [None] * len(photos), "no API key"

    raw, last_error = _vision_call(content, CAPTION_TIMEOUT_SECONDS,
                                   budget=CAPTION_BUDGET_SECONDS)
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
        except Exception:
            dropped += 1
            continue
        if not 0 <= index < len(photos):
            dropped += 1
            continue
        text = _clean_caption(entry.get("caption"), copy_guard, index)
        if text is None:
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
# Smart photo selection (the same batched call that writes the captions)
# ---------------------------------------------------------------------------
# What this fixes: _auto_selection is "hero photo, then everything else in upload
# order, truncate". It has no idea what is in any photo, so on the demo listing it put
# TWO SWIMMING POOL SHOTS in and left the dining room out. A film that shows the same
# space twice while missing a room the buyer cares about is the single most visible
# quality problem the Classic had after the motion.
#
# The fix reuses a capability we already paid for. Captions already require a batched
# vision call that works out what each room is; selection needs exactly the same
# knowledge. So rather than add a second call, the AUTO path makes ONE call over all of
# the listing's photos that returns a space label AND a caption for each, picks a
# varied set from the labels, and keeps the captions it already has. The caption-only
# path (_generate_room_captions) is untouched and still serves the agent's explicit
# pick, where there is nothing to select.
#
# WHAT IT COSTS, measured on the real prompts and real 768px thumbnails (the vision
# token estimate is width*height/750, and _photo_to_block sends 768x432 for a 16:9
# listing photo), at claude-sonnet-4-5 prices:
#
#     today, captions only, 6 photos      3409 in +  310 out   $0.0149/video
#     survey, 7-photo listing             4052 in +  355 out   $0.0175/video
#     survey, 10-photo listing            5380 in +  490 out   $0.0235/video
#     survey, full 15-photo listing       7592 in +  715 out   $0.0335/video
#
# So the worst case is 2.3x today's cost, and Jane's 50 agents x 10 listings goes from
# about $7.44 to about $16.75 in total. The request itself grows from 0.4MB to 1.1MB.
# This is cheap because it is ONE call that does BOTH jobs -- classifying every photo
# and writing the captions for the ones it keeps. Two separate calls would have cost
# more and, worse, doubled the number of things that have to be up for a render to go
# at full quality. The explicit-pick path still makes the old caption-only call.
#
# The vocabulary is fixed rather than free text. "living hall" and "living room" are
# the same space and must dedupe, which they cannot do reliably as free strings.
# SPACE_UNKNOWN is deliberately never deduped: if the model cannot tell what a space
# is, two of them are not evidence of repetition.
SPACE_UNKNOWN = "other"
_SPACE_VOCABULARY = (
    "living", "dining", "kitchen", "bedroom", "bathroom", "study", "balcony",
    "terrace", "garden", "pool", "gym", "view", "entrance", "stairs", "carpark",
    "exterior", SPACE_UNKNOWN,
)

# Built from _CAPTION_PROMPT rather than written out again, so the caption RULES have
# exactly one home. If someone tightens the no-superlatives rule or the "never name a
# room you cannot see" rule, both passes get it; a second copy would drift, and the
# rule that matters most here is the one about not claiming a room that isn't there.
_SURVEY_PROMPT = _CAPTION_PROMPT.split("Reply with JSON only")[0] + """Also label each
photo with the kind of space it shows, chosen from exactly this list:

    """ + ", ".join(_SPACE_VOCABULARY) + """

The label is used to pick a varied set of photos for the video, so it matters that two
photos of the same space get the SAME label and two photos of different spaces get
DIFFERENT ones. Apply the same honesty rule as the caption: the label must be
something you can actually see evidence for. Use "{unknown}" whenever you are not
sure -- an honest "{unknown}" is always better than a guess, because a wrong label
drops a photo the agent wanted or keeps one they did not.

Reply with JSON only, no other text, in exactly this shape:
{{"photos": [{{"n": 1, "space": "...", "caption": "..."}}, {{"n": 2, "space": "...", "caption": "..."}}]}}
with one entry per photo, in order, for all {n} photos.""".replace(
    "{unknown}", SPACE_UNKNOWN)

# Longer than CAPTION_TIMEOUT_SECONDS because this call carries up to 15 photos rather
# than 6. The budget is the ceiling on the WHOLE pass including a second key, and it is
# the number that matters: past it we stop and use upload order, which is a perfectly
# good video. Selection is never worth making an agent wait.
SURVEY_TIMEOUT_SECONDS = 75
SURVEY_BUDGET_SECONDS = 100
# Wall-clock ceiling on downloading the candidates to look at them. These are the
# ORIGINAL photos (not the 4x upscaled twins), so they are small, but a slow CDN must
# not be able to stall a render on a step that is only an optimisation.
SURVEY_FETCH_BUDGET_SECONDS = 45
# Below this many readable candidates there is nothing to choose BETWEEN, so the call
# is not worth making.
SURVEY_MIN_CANDIDATES = 3
# Must match _photo_to_block's own max_side: the candidates are shrunk to this as soon
# as they are downloaded so that fifteen decoded listing photos never sit in memory at
# once, and _photo_to_block then has nothing left to do.
SURVEY_THUMB_PX = 768


def _normalise_space(value):
    """A model-written label -> one of _SPACE_VOCABULARY. Anything unrecognised becomes
    SPACE_UNKNOWN, which never dedupes, so a bad label can only cost variety -- it can
    never silently drop a photo as a duplicate of something it is not."""
    try:
        text = str(value).strip().lower()
    except Exception:
        return SPACE_UNKNOWN
    if text in _SPACE_VOCABULARY:
        return text
    # "living hall", "master bedroom", "swimming pool" -- the vocabulary word is in
    # there somewhere. Checked longest-first so "bathroom" never matches as "bath".
    for word in sorted(_SPACE_VOCABULARY, key=len, reverse=True):
        if word != SPACE_UNKNOWN and word in text:
            return word
    return SPACE_UNKNOWN


def _survey_photos(photos, copy_guard=None):
    """One batched vision call: (list of (space, caption), note). Never raises.

    The list is always len(photos) long. A (SPACE_UNKNOWN, None) entry means the model
    said nothing usable about that photo; it stays eligible for the film.
    """
    blank = [(SPACE_UNKNOWN, None)] * len(photos)
    content = _photo_content(photos, _SURVEY_PROMPT.format(n=len(photos)))
    if content is None:
        logger.warning("photo survey skipped: no Anthropic API key configured")
        return blank, "no API key"

    # max_tokens scales with the photo count: 15 entries of space + caption do not fit
    # in the 1024 the six-photo caption call was sized for, and a reply truncated
    # mid-JSON parses as nothing at all.
    raw, last_error = _vision_call(
        content, SURVEY_TIMEOUT_SECONDS,
        max_tokens=256 + 110 * len(photos), budget=SURVEY_BUDGET_SECONDS)
    if raw is None:
        logger.warning("photo survey skipped: Claude call failed (%s)", last_error)
        return blank, f"AI call failed ({type(last_error).__name__})"

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        parsed = json.loads(match.group(0) if match else raw)
        entries = parsed["photos"]
    except Exception as e:
        logger.warning("photo survey skipped: could not parse model reply (%s)", e)
        return blank, "AI reply unparseable"

    out = list(blank)
    dropped = 0
    for entry in entries:
        try:
            index = int(entry["n"]) - 1
        except Exception:
            dropped += 1
            continue
        if not 0 <= index < len(photos):
            dropped += 1
            continue
        caption = _clean_caption(entry.get("caption"), copy_guard, index)
        if caption is None:
            dropped += 1
        out[index] = (_normalise_space(entry.get("space")), caption)

    note = None
    if dropped:
        note = f"{dropped} caption(s) rejected by the copy guards"
    if all(c is None for _space, c in out):
        note = "every caption was rejected"
    return out, note


def _varied_order(spaces, limit, minimum):
    """Indices of a varied set of photos, hero (index 0) first and always kept.

    `spaces` is in the order the photos should be considered -- hero first, then the
    listing's own order. Returns indices INTO that list.

    One photo per distinct space, in the agent's own order, which is the rule Jane
    asked for. The one place it yields is at the bottom: if being strict would leave a
    film SHORTER than `minimum` (today's six shots), the spaces already shown are
    opened up again until it is long enough. That is deliberate -- a listing whose
    photos only cover three spaces would otherwise drop from a 30-second film to a
    15-second one, which is a worse product than a second angle on the living room,
    and it means this change can never make a video shorter than the one it replaces.

    The top-up goes round the spaces in turn -- every space's second photo before any
    space's third -- rather than straight down the list. Taking them in list order
    re-creates the exact bug this function exists to fix: a listing with four living
    room shots first would spend its whole top-up on the living room and still never
    reach the kitchen.
    """
    chosen, seen, held_back = [], set(), []
    for i, space in enumerate(spaces):
        if len(chosen) >= limit:
            held_back.extend(range(i, len(spaces)))
            break
        # The hero is never judged against the rule: it is the agent's own pick of the
        # photo that opens the film (My Listings' star picker) and it always leads.
        if i == 0:
            chosen.append(i)
            seen.add(space)
            continue
        if space != SPACE_UNKNOWN and space in seen:
            held_back.append(i)
            continue
        chosen.append(i)
        seen.add(space)

    # Round-robin the leftovers by space, keeping each space's own photos in order.
    by_space = {}
    for i in held_back:
        by_space.setdefault(spaces[i], []).append(i)
    target = min(limit, minimum)
    while len(chosen) < target and by_space:
        for space in list(by_space):
            if len(chosen) >= target:
                break
            chosen.append(by_space[space].pop(0))
            if not by_space[space]:
                del by_space[space]
    # Back into the agent's own order, with the hero still leading. Agents order their
    # photos as a walkthrough, and a film that jumps around that order reads as
    # shuffled even when every individual photo is a good choice.
    return [0] + sorted(i for i in chosen if i != 0)


def _smart_auto_selection(image_urls, photo_index, copy_guard=None):
    """The AUTO path's photo choice: (selected urls, {url: caption}, note).

    (None, None, None) means "use the plain upload-order selection" and is the answer
    whenever anything at all goes wrong -- no key, a slow or failed model call, a
    reply that does not parse, too few readable photos, an unexpected exception. THAT
    IS THE WHOLE DESIGN: a worse-chosen video is a small loss, a failed render is a
    large one, and this feature is never allowed to turn the second into the first.
    Nothing here is reported to the agent as a degradation, because from their side
    nothing degraded -- they get the video the old code would have given them.

    Only ever called for AUTO. An agent who picked their own photos has already said
    what they want, and choose_video_photos decides that case before this runs.

    DELIBERATELY OUTSIDE THE RENDER SLOT. This runs before _RENDER_SLOTS.acquire, so a
    model call that takes a minute does not sit on one of the four ffmpeg slots the
    whole fleet has. What bounds the concurrency here instead is video_jobs, which
    admits at most PER_PROCESS_RENDER_LIMIT Classic jobs per worker.

    WHICH IS EXACTLY WHY EVERY STEP BELOW HAS AN ENFORCED DEADLINE. That per-worker
    limit is 1, so this function stalling does not slow a worker down, it stops it:
    the job stays claimed, and because the heartbeat runs on its own thread it keeps
    reporting healthy, so sweep_stale (which selects on heartbeat_at alone) never
    reclaims it. A worker can therefore sit on a dribbling CDN read or a hung API call
    indefinitely while looking perfectly alive from the outside. The budgets here used
    to be advisory -- checked only between steps, which is no check at all while a read
    is blocked -- and are now absolute; see slow_read for why a timeout is not enough.
    """
    try:
        hero = image_urls[photo_index]
        candidates = ([hero] + [u for i, u in enumerate(image_urls) if i != photo_index]
                      )[:MAX_SELECTION_CANDIDATES]
        if len(candidates) <= MIN_VIDEO_PHOTOS:
            # Every photo is going in anyway, so there is nothing to choose and no
            # reason to spend a vision call working that out. The captions still get
            # written by the normal caption pass downstream.
            return None, None, None

        # One deadline shared by all fifteen candidate fetches, passed INTO each one so
        # it bounds the read itself rather than only the gap between reads.
        fetch_deadline = time.monotonic() + SURVEY_FETCH_BUDGET_SECONDS
        photos, urls = [], []
        for url in candidates:
            if time.monotonic() >= fetch_deadline:
                logger.warning("photo survey: fetch budget spent after %d of %d photos",
                               len(urls), len(candidates))
                break
            try:
                # The ORIGINAL, not the 4x upscaled twin _fetch_listing_photo prefers:
                # this image is only ever thumbnailed down to 768px to be looked at, so
                # the big one would be bytes and memory spent for nothing. attempts=1
                # because a flaky read on a candidate should cost that candidate, not
                # the render's time budget.
                img = _fetch_image(url, attempts=1, deadline=fetch_deadline)
            except Exception as e:
                logger.debug("photo survey: skipping unreadable candidate (%s)", e)
                continue
            try:
                # Thumbnail now and drop the full image immediately: holding 15
                # decoded listing photos would be hundreds of MB on an instance that
                # also has to run ffmpeg. _photo_to_block downstream thumbnails to the
                # same 768px, so this costs nothing and bounds the memory here.
                small = img.convert("RGB").copy()
                small.thumbnail((SURVEY_THUMB_PX, SURVEY_THUMB_PX), Image.LANCZOS)
                photos.append(small)
            finally:
                img.close()
            urls.append(url)

        if hero not in urls or len(urls) < SURVEY_MIN_CANDIDATES:
            # Without the hero this cannot honour "the agent's star photo opens the
            # film", and with two or three candidates there is nothing to choose.
            logger.info("photo survey: not enough readable candidates (%d); using "
                        "upload order", len(urls))
            return None, None, None

        survey, note = _survey_photos(photos, copy_guard)
        spaces = [s for s, _c in survey]
        if all(s == SPACE_UNKNOWN for s in spaces):
            # The call came back but told us nothing we can select on. Upload order is
            # a better answer than a set chosen from labels we do not believe.
            # Falling back here means the normal caption pass runs downstream and
            # makes its own call, so this is the one AUTO path that can cost two. It
            # is rare (the model answered but labelled nothing), and fresh captions
            # for the photos actually chosen beat reusing captions written for a
            # different set. The note is dropped for the same reason: the caption
            # pass will report its own outcome and must not be double-counted.
            logger.info("photo survey: no usable room labels; using upload order")
            return None, None, None

        # urls[0] is the hero: the candidate list was built with it first, and the
        # fetch loop keeps `urls` and `photos` in step, so index 0 survives a skip.
        picked = _varied_order(spaces, MAX_VIDEO_PHOTOS, MIN_VIDEO_PHOTOS)
        selected = [urls[i] for i in picked]
        # Keyed by URL, not by position. The render re-fetches these photos (at hires)
        # and may drop one it cannot read, which shifts every position after it -- a
        # caption on the wrong photo is a caption that lies about the property.
        captions = {urls[i]: survey[i][1] for i in picked if survey[i][1]}
        logger.info("photo survey: chose %d of %d photos covering %d distinct spaces",
                    len(selected), len(urls),
                    len({spaces[i] for i in picked if spaces[i] != SPACE_UNKNOWN}))
        # The note travels with the result. Without it a survey that selected well
        # but had its captions rejected by the copy guards shipped a film of bare
        # photos and an EMPTY degradation list -- a missing caption with no trace,
        # which is a complaint nobody can diagnose afterwards.
        return selected, captions, note
    except Exception as e:
        logger.warning("photo survey failed (%s); using upload order", e)
        return None, None, None


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


def _caption_scrim_filters(first_line_y, last_line_y, size):
    """The soft dark band that sits behind the caption rows.

    Stacked drawbox strips rather than one rectangle, so the band feathers out
    instead of drawing a hard line across the frame. Not drawtext's own `box=`:
    at this line spacing a per-line box would be 70px of glyph plus two borders
    against only 71px between the rows, so the two boxes would overlap and the
    overlap would compound into a darker stripe between the lines.

    drawbox has no per-frame alpha, so the band is drawn for the whole clip
    rather than only inside style B's caption window. That is deliberate: it
    cannot pop in or out, and two clips dissolving into each other carry an
    identical band, so the band holds still while the photos cross-fade.
    """
    glyph_h = round(size * CAPTION_GLYPH_HEIGHT)
    core_top = first_line_y - CAPTION_SCRIM_PAD
    core_bottom = last_line_y + glyph_h + CAPTION_SCRIM_PAD
    step = max(1, round(CAPTION_SCRIM_FEATHER / CAPTION_SCRIM_STEPS))

    def strip(y, height, alpha):
        # Clamp into the frame: a caption pushed high by a three-line block must
        # not hand ffmpeg a negative y, which it rejects outright.
        top = max(0, int(y))
        bottom = min(VH, int(y) + int(height))
        if bottom <= top or alpha < 0.005:
            return None
        return (f"drawbox=x=0:y={top}:w=iw:h={bottom - top}"
                f":color=black@{alpha:.3f}:t=fill")

    out = [strip(core_top, core_bottom - core_top, CAPTION_SCRIM_ALPHA)]
    for j in range(CAPTION_SCRIM_STEPS):
        # j=0 is the strip touching the core and carries full strength; each step
        # further out is fainter, easing to nothing at the edge of the feather.
        #
        # Smoothstep, not a straight line: it flattens at BOTH ends, so there is
        # no seam where the feather meets the core and none where it meets the
        # photo. A linear ramp in this few steps showed visible stair-stepping on
        # a white frame; this keeps every step under ~4 levels of 255.
        t = (CAPTION_SCRIM_STEPS - j) / CAPTION_SCRIM_STEPS
        alpha = CAPTION_SCRIM_ALPHA * t * t * (3 - 2 * t)
        out.append(strip(core_top - (j + 1) * step, step, alpha))
        out.append(strip(core_bottom + j * step, step, alpha))
    return [f for f in out if f]


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
    # Band first: everything after it draws on top, so the shadow and the type
    # still land over the photo-plus-band rather than under it.
    out = _caption_scrim_filters(first_line_y, CAPTION_LAST_LINE_Y, size)
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


def _presenter_opening_clip(src_path, out_path, timeout=TIMEOUT_CLIP):
    """The agent's own footage, as the opening slide of a Signature.

    Exactly SECONDS_PER_PHOTO long and encoded with _ENCODE_ARGS, because that is what
    makes it just another slide: _dissolve_segments cuts every clip at fixed offsets
    derived from SECONDS_PER_PHOTO, so a clip of any other length would be silently
    truncated or run short. Matching the encode also keeps the assembly a stream copy.

    Given the same contain-over-blur treatment the photos get -- shown WHOLE over a
    blurred copy of itself -- so the opening looks like the rest of the film rather than
    a different video bolted on the front. Containing is also the only treatment that is
    SAFE here: an earlier cut cover-cropped to fill the frame and took the top of Janel's
    head off, because phone footage is portrait and the agent is close to the lens. A
    contain can crop nothing, whichever way up the clip was filmed.

    Silent (_ENCODE_ARGS carries -an). Their voice is mixed back over the finished film,
    where it can duck the music rather than fight it.

    Returns a note for the agent, or None. A clip stored BEFORE the HDR fix is still
    HLG on disk, and it is repaired HERE rather than left for the agent to re-upload --
    MEASURED, the repair of the stored 8-bit clip is as good as converting the 10-bit
    original again (colourfulness 0.166 against 0.165, contrast 208.0 against 207.1,
    248 against 249 distinct luma levels, so the 8-bit round trip costs nothing
    visible). That matters because the alternative was asking every agent who uploaded
    before today to go and do it again. A clip stored since the fix already reads
    bt709, so this costs a healthy render nothing but one header read.
    """
    colour = _source_colour(src_path) or {}
    note = None
    repair = ""
    if colour.get("transfer") in HDR_TRANSFERS:
        logger.warning("stored presenter clip is still HDR (%s) -- it predates the "
                       "colour fix; converting it on the way into the film",
                       colour.get("transfer"))
        repair, note = _hdr_to_sdr_filter(colour)
    # Backdrop: cover-scale, then crop. force_original_aspect_ratio=increase guarantees
    # the scaled frame is at least as large as the crop in BOTH dimensions -- the first
    # cut used `scale=1080:-1,crop=1080:1920`, which on any clip squarer than 9:16
    # (a 3:4 portrait, a landscape held sideways) produced a frame shorter than 1920 and
    # ffmpeg refused the crop outright, failing the whole render.
    #
    # Blurred small and scaled back up, as the photo path does: a wide-radius blur over
    # a full-resolution frame is enormously expensive and, since blurring is destroying
    # detail, indistinguishable from blurring a thumbnail. Per FRAME of video that
    # saving is 120x what it is for a still.
    bg = (f"scale=w={VW // 4}:h={VH // 4}:force_original_aspect_ratio=increase,"
          f"crop={VW // 4}:{VH // 4},"
          f"boxblur={CONTAIN_BLUR_RADIUS - 2}:2,"
          f"scale={VW}:{VH},"
          f"eq=brightness=-{CONTAIN_BG_DARKEN / 2:.3f}")
    # Foreground: contain. decrease fits the clip INSIDE 1080x1920 whole, so a portrait
    # clip fills the frame, a squarer one gets blurred bands, and nothing is ever cut.
    fg = f"scale=w={VW}:h={VH}:force_original_aspect_ratio=decrease:flags=lanczos"
    def build(with_repair, with_tags):
        return [
            "ffmpeg", "-y", "-v", "error",
            # -stream_loop so a clip shorter than a slide still fills one rather than
            # freezing on a black tail.
            "-stream_loop", "-1", "-i", src_path, "-t", "%.3f" % SECONDS_PER_PHOTO,
            "-filter_complex",
            # The repair, when there is one, goes in BEFORE the split, so it is done
            # once and both the sharp foreground and the blurred backdrop come from
            # converted frames. Empty for every healthy clip, which leaves this graph
            # exactly as it was.
            (f"[0:v]{repair + ',' if (with_repair and repair) else ''}fps={FPS},"
             f"split=2[bgin][fgin];"
             f"[bgin]{bg}[b];"
             f"[fgin]{fg}[f];"
             f"[b][f]overlay=(W-w)/2:(H-h)/2:shortest=0,setsar=1,format=yuv420p[v]"),
            "-map", "[v]",
            # Slide zero states bt709 explicitly, so the stream-copy concat can never
            # publish the film under whatever colour description the agent's phone
            # chose. See COLOUR_TAG_ARGS for why it is here and not in _ENCODE_ARGS.
        ] + _ENCODE_ARGS + (COLOUR_TAG_ARGS if with_tags else []) + [out_path]

    # COLOUR MUST NEVER COST THE VIDEO.
    #
    # This is the one stage of a Signature that cannot degrade -- a film without the
    # agent in it is a Classic -- so anything that CAN fail here fails the whole render
    # and the agent is told their clip could not be read. That is exactly what happened
    # on the first production Signature after the colour fix shipped: the repair chain
    # was refused by Railway's ffmpeg build, and an agent with a perfectly good
    # recording was told to go and record another one.
    #
    # The repair and the colour tags are both COSMETIC. They are worth having, they are
    # never worth a failed render, and a probe that says a filter exists is not a
    # promise that this particular graph will run. So they are attempted in descending
    # order of niceness and the last rung is byte-for-byte the command that shipped
    # before any of this existed.
    attempts = [
        (True, True),    # converted and tagged -- the right answer
        (False, True),   # tagged only: the card and photos are right, the agent flat
        (False, False),  # exactly the pre-colour-fix command; wrong colours, but a film
    ]
    if not repair:
        attempts = [(False, True), (False, False)]
    last_error = None
    for i, (with_repair, with_tags) in enumerate(attempts):
        try:
            _run_ffmpeg(build(with_repair, with_tags), timeout)
        except Exception as e:
            last_error = e
            logger.warning("presenter opening clip failed (repair=%s tags=%s): %s",
                           with_repair, with_tags, str(e)[:300])
            continue
        if with_repair:
            return note
        if with_tags:
            return VIDEO_NOTE_HDR_NOT_TONEMAPPED if repair else note
        logger.error("presenter opening clip fell all the way back to the untagged "
                     "command; this film will carry the clip's own colour description")
        return VIDEO_NOTE_HDR_NOT_TONEMAPPED if repair else note
    # Every rung failed, so the clip really is unreadable and raising is correct.
    raise last_error


def _contain_clip(photo, out_path, workdir, index, caption=None, caption_size=None,
                  zoom_in=True, drift_right=True, first=True, last=True):
    """STYLE B: one photo -> one 5s contain-over-blur clip that pushes in (or pulls
    out) while drifting sideways across the photo.

    Every clip, the first included, opens fully composed -- sharp photo already in
    place over its backdrop. Jane rejected the earlier fade-up-over-backdrop opening
    ("i don't want the video to open with a blur screen"): the video starts cold on
    slide one.

    zoom_in / drift_right are alternated by the caller so a ten-shot film has rhythm
    instead of ten copies of one effect. Both senses belong to the SAME family of move
    -- a zoom plus a lateral drift -- which is what makes the sequence read as one
    camera rather than a bag of tricks.
    """
    composite, fills_width = _contain_over_blur(photo)
    comp_path = os.path.join(workdir, f"b_comp_{index:02d}.bmp")
    composite.save(comp_path, format="BMP")

    # THE ZOOM IS WRITTEN AS A FUNCTION OF THE FRAME NUMBER, NOT AS A FEEDBACK LOOP.
    # This matters, and it is why the Classic looked static before 2026-10-17.
    #
    # The idiom this replaces -- z='min(zoom+step,max)' -- reads zoompan's `zoom`
    # variable, which holds the zoom of the PREVIOUS OUTPUT FRAME OF THE SAME INPUT
    # FRAME. With d=1 every input frame produces exactly one output frame, so there is
    # no previous output frame to read and `zoom` resets to 1.0 every time: the
    # expression evaluates to 1+step on all 120 frames and the shot never moves. The
    # d=120 form in _kenburns_clip does accumulate, which is why style A moved and the
    # style B that actually ships did not. MEASURED on a ruler frame, shipped
    # expression, frames 0 / 59 / 119: the crop window was 2158px wide at all three.
    # Nobody saw it because a 6% zoom that fails is indistinguishable from a 6% zoom.
    #
    # `on` is the output frame index and does count 0..FRAMES_PER_PHOTO-1 correctly, so
    # a linear ramp over it is both frame-exact and immune to the trap above.
    amplitude = round(CONTAIN_ZOOM_MAX - 1.0, 6)
    span = FRAMES_PER_PHOTO - 1
    if zoom_in:
        z = f"1+{amplitude}*on/{span}"
    else:
        z = f"{CONTAIN_ZOOM_MAX}-{amplitude}*on/{span}"
    # The horizontal drift. `on` is the output frame index (0..FRAMES_PER_PHOTO-1), so
    # `p` ramps linearly from 0 to 1 across the clip (or 1 to 0 drifting the other way)
    # and is multiplied by whatever spare width the CURRENT zoom leaves:
    #
    #     x = p * (iw - iw/zoom)        p in [0,1]
    #
    # p=0 puts the window hard against the left of the composite, p=1 hard against the
    # right, and at zoom 1.0 the spare is zero so x collapses to 0 -- the whole photo,
    # centred, with no special case needed. Nothing can leave the composite at any
    # zoom, so there is no clamp to get wrong and no black edge to guard against.
    #
    # A source taller than 9:16 does not reach the sides of its own composite
    # (_contain_over_blur says so), and sliding it inside its blurred margins looks
    # like a mistake, so those shots keep the centred zoom they had.
    if fills_width and CONTAIN_DRIFT_SPAN > 0:
        ramp = f"on/{span}" if drift_right else f"(1-on/{span})"
        # `zoom` IS safe to read here: inside x/y it is the zoom already computed for
        # THIS frame, not a previous-frame feedback value. Verified on a ruler frame --
        # at frame 119 the window lands at composite x=389.5 with the band at
        # composite 420 appearing at output x=20, which is the drift to the pixel.
        x = f"{CONTAIN_DRIFT_SPAN}*{ramp}*(iw-iw/zoom)"
    else:
        x = "iw/2-(iw/zoom/2)"
    # d=1 (one output frame per input frame): the looped still arrives as a frame
    # stream, so the zoom accumulates per frame rather than inside one d=120 hold.
    zoompan = (f"zoompan=z='{z}':d=1:s={VW}x{VH}:fps={FPS}"
               f":x='{x}':y='ih/2-(ih/zoom/2)'")
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
    # Clarity after the zoom has produced the final 1080x1920 frame and BEFORE the
    # caption, for the same reason as style A: drawtext type is already crisp and
    # unsharp would only halo the letterforms.
    #
    # This is the path that actually ships. STYLE B IS THE DEFAULT AND ONLY AGENT-FACING
    # STYLE -- main.py's VIDEO_TEMPLATES contains "classic" alone, and
    # `dissolve = (style != STYLE_A_ID)` sends it here. The first cut of this change put
    # the clarity pass in _kenburns_clip only, which is style A, which nothing reaches.
    # If you add a filter for photo quality, it belongs HERE first.
    #
    # The open question on this was whether unsharp would halo the hard luminance edge
    # where the sharp photo meets its blurred backdrop. MEASURED on a production render:
    # detail energy at the border row rose 28.54 -> 46.0 with clarity on, while the rows
    # on the BLURRED side went down (2.42 -> 1.88 and 7.06 -> 2.74). A halo bleeds
    # outward and would have raised those rows; it didn't. The edge is simply crisper.
    clarity = _clarity_filter()
    tail = ",".join(
        [zoompan, "setsar=1"]
        + ([clarity] if clarity else [])
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

    # Clarity BEFORE the caption, deliberately. The caption is vector type drawn by
    # drawtext at final resolution and is already perfectly crisp -- running unsharp over
    # it would only put haloes around the letterforms. Everything above this line is the
    # photo; everything below is overlay.
    clarity = _clarity_filter()
    if clarity:
        filters.append(clarity)

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


VIDEO_NOTE_SHRUNK = ("the video was larger than the storage limit allows, so it was "
                     "compressed a little further and is very slightly softer")


def _fit_delivery_size(path, workdir):
    """Keep the finished film under DELIVERY_MAX_BYTES. Returns (path, note).

    Engages ONLY on an oversize file. A normal render reaches the first `return` below
    having done nothing at all -- no extra ffmpeg call, no change to the bytes -- which
    is the point: this must be invisible to the 24MB film that is the ordinary case.

    One pass, on the FINISHED file, so it is a straight re-compress rather than a
    re-render; the clips, captions, card and soundtrack are all already baked in.

    NEVER RAISES, AND NEVER LOSES THE VIDEO. If the re-encode fails, times out, or
    somehow comes out larger, the original is returned and the upload is attempted with
    it. A video that MIGHT be rejected is worth more than one we definitely threw away.
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return path, None
    if size <= DELIVERY_MAX_BYTES:
        return path, None

    target = DELIVERY_MAX_BYTES * _SHRINK_MARGIN
    needed = 1.0 - (target / size)
    crf = _SHRINK_STEPS[-1][1]
    for reduction, step in _SHRINK_STEPS:
        if reduction >= needed:
            crf = step
            break
    logger.warning("video is %.1fMB, over the %.0fMB delivery ceiling; re-encoding "
                   "once at CRF %s (needs %.0f%% off)",
                   size / 1024 / 1024, DELIVERY_MAX_BYTES / 1024 / 1024, crf,
                   needed * 100)

    smaller = os.path.join(workdir, "delivery_shrunk.mp4")
    try:
        _run_ffmpeg([
            "ffmpeg", "-y", "-i", path,
            "-c:v", "libx264", "-preset", "fast", "-crf", crf, "-pix_fmt", "yuv420p",
            # -vsync 0: pass the frames through exactly as they are. This film is
            # assembled by stream-copy concatenation and carries non-monotonic DTS at
            # the joins, so letting ffmpeg retime it does real damage -- MEASURED, the
            # same re-encode produced 1089 frames with "-r 24 -vsync cfr" and 1085 with
            # the default, against the source's 1087. Dropping or duplicating frames to
            # save bytes would be fixing the wrong problem.
            "-vsync", "0",
            # The soundtrack is already correct and is not what makes the file big.
            "-c:a", "copy", "-movflags", "+faststart", smaller,
        ], TIMEOUT_SHRINK)
        new_size = os.path.getsize(smaller)
    except Exception as e:
        logger.error("could not shrink the oversize video (%s); uploading it as it is", e)
        return path, None

    if new_size >= size:
        # Re-compressing made it bigger, which can happen on footage the first encode
        # already handled well. Nothing gained, so keep the better picture.
        logger.warning("the shrink pass did not help (%.1fMB -> %.1fMB); keeping the "
                       "original", size / 1024 / 1024, new_size / 1024 / 1024)
        return path, None
    logger.info("shrank the delivery video %.1fMB -> %.1fMB at CRF %s",
                size / 1024 / 1024, new_size / 1024 / 1024, crf)
    return smaller, VIDEO_NOTE_SHRUNK


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
    invitation, contact in gold. Layout and colours are the prototype's.

    agent_contact_line may carry extra lines after a "\n" (NestList Home condo listings:
    the price, then the CEA registration and agency licence lines). The first line is the
    contact exactly as before; the rest are drawn smaller underneath. A single-line contact
    (every landed listing) renders exactly as it always did."""
    agent_contact_line, *extra_lines = str(agent_contact_line or "").split("\n")
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
    y = contact_y + 100
    for line in (l.strip() for l in extra_lines[:4]):
        if line:
            centred(line, y, _fit_text(draw, line, SERIF_REGULAR, 40, max_width), CARD_INK)
            y += 58
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

    KNOWN, NOT FIXED HERE: both inputs are JPEGs, and JPEG is FULL range, so this is the
    one clip in the film encoded full-range -- it probes yuvj420p(pc) while every photo
    clip probes limited. The stream-copy concat throws that label away and keeps the
    first clip's, so the card's samples are read as limited range and the designed
    (250,248,245) off-white clips towards white (the charcoal name text measures 43 ->
    31). It is pre-existing, it is sub-perceptual on a flat off-white, and it is NOT the
    teal bug -- but it is real. Fixing it means either converting these two stills to
    limited range or feeding the encoder BMP instead of JPEG, both of which change how
    every Classic's closing card decodes, so it is written up for Jane to decide rather
    than smuggled into a colour hotfix. See the report that shipped with this change.
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
                          copy_guard=None, music_seed=None, caption_overrides=None,
                          music_track=None, video_photos=None, presenter_path=None):
    """Renders the Classic-tier listing video and returns (mp4_bytes, degradations, captions).

    `caption_overrides` maps a photo's URL to a caption the AGENT wrote. Keyed on the
    URL rather than the photo's position because an agent who reorders or deletes a
    photo must not silently inherit someone else's caption. Any photo without an
    override is captioned by the model as before, so an agent who edits one line does
    not lose the other five.

    `captions` comes back as {photo url: caption} for the photos actually used, so the
    caller can store them and show the agent what their video says.

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
    music_seed: chooses the music bed from the audio/ library, deterministically --
    main.py passes the listing id, so regenerating a listing's video always brings
    back the same track. The candidate list is first narrowed to the property's mood
    tier (audio/tracks.json, advisory). Omitting the seed is safe (every listing then
    shares one track); it only matters once audio/ holds more than one file.
    music_track: the agent's own pick from the music picker (an id from
    pickable_tracks()). None, blank or "auto" keeps the automatic selection above
    exactly as it was. An id that is no longer pickable (file removed between the
    request and the render) falls back to the automatic pick and is reported in
    `degradations` -- a stale choice must never fail a render.
    video_photos: the agent's own choice of WHICH listing photos go in the video and in
    what ORDER -- a list of photo URLs drawn from `image_urls` (see choose_video_photos).
    None, empty or "auto" keeps the automatic selection exactly as it was (hero first,
    then the rest in stored order, capped at MAX_VIDEO_PHOTOS). With a choice, those
    photos are used in that order and photo_index is not consulted (the first chosen
    photo opens the video). A choice that cannot be honoured -- chosen photos deleted
    since the request, or too few of them readable -- falls back to the automatic
    selection and is reported in `degradations`; it never fails the render.
    copy_guard: optional callable(text, context=...) -> text. main.py passes
    apply_listing_copy_guards so captions go through the same house-number/price
    stripping as every other copy surface. Passed in rather than imported because
    main.py imports this module.

    property_type / district / price_text / stats are not drawn: the Classic tier's
    on-screen text is the room captions, and a stats block in the same lower third
    would collide with them. district / price_text / stats are kept for signature
    compatibility only -- but property_type is no longer inert, because it now picks
    the music's mood register (see _music_tier). Blank is fine and common; it reads
    as the warmer of the two registers.
    """
    if not image_urls:
        raise ValueError("At least one photo is required to generate a video")
    if photo_index < 0 or photo_index >= len(image_urls):
        photo_index = 0

    # Hero photo first, then the rest in their existing order, then truncate. Slicing
    # before honouring photo_index would silently drop the hero whenever the listing
    # has more photos than MAX_VIDEO_PHOTOS.
    def _auto_selection():
        hero_url = image_urls[photo_index]
        rest_urls = [u for i, u in enumerate(image_urls) if i != photo_index]
        return ([hero_url] + rest_urls)[:MAX_VIDEO_PHOTOS]

    degradations = []
    # The agent's own pick, when there is one and it still holds. Everything else
    # (Auto, blank, a stale pick) takes the automatic selection below.
    #
    # PRECEDENCE IS UNCHANGED BY THE SMART SELECTION: an explicit pick wins outright
    # and is never second-guessed by a model. choose_video_photos still decides first,
    # exactly as it did, and _smart_auto_selection is only consulted on the AUTO path.
    chosen_urls, choice_note = choose_video_photos(video_photos, image_urls)
    if choice_note:
        degradations.append(choice_note)
    # Captions the survey already wrote, keyed by url, plus whether the survey is the
    # thing that wrote them.
    #
    # survey_used IS NOT "survey_captions is non-empty". A survey can choose a good
    # set of photos and still have every caption rejected by the copy guards, and an
    # empty dict read as a falsy "no survey" sends the render into the caption pass
    # for a SECOND model call -- one that, unlike the survey, runs inside the render
    # slot and against RENDER_BUDGET_SECONDS. The flag separates "the survey ran"
    # from "the survey produced captions", which are different questions.
    survey_used = False
    survey_captions = {}
    survey_note = None
    if chosen_urls:
        selected_urls = chosen_urls
    else:
        smart_urls, smart_captions, survey_note = _smart_auto_selection(
            image_urls, photo_index, copy_guard=copy_guard)
        if smart_urls:
            selected_urls = smart_urls
            survey_captions = smart_captions or {}
            survey_used = True
        else:
            selected_urls = _auto_selection()

    # The dissolve is the house style. Every agent-facing id ("classic", the retired
    # "card", or nothing at all) renders it; only an explicit internal "driftcut"
    # opts out. Defaulting this way round means an old listing row carrying a retired
    # template id gets today's look rather than an error or a stale one.
    dissolve = (style != STYLE_A_ID)

    started = time.monotonic()

    if not _RENDER_SLOTS.acquire(timeout=RENDER_SLOT_WAIT_SECONDS):
        raise RenderSlotBusy(
            "The video service is busy with other renders right now. Please try again "
            "in a few minutes."
        )
    try:
        photos = []
        photo_urls = []          # kept in step with `photos`; a skipped photo drops both
        for url in selected_urls:
            try:
                photos.append(_fetch_listing_photo(url))
                photo_urls.append(url)
            except Exception as e:
                logger.warning("skipping unreadable photo %s: %s", url, e)
                degradations.append("a photo could not be read and was skipped")
        if chosen_urls and len(photos) < min(2, len(selected_urls)):
            # The agent's pick is mostly unreadable (zero, or one of two or more). A
            # one-photo video they did not ask for is worse than the automatic one.
            logger.warning("chosen photos mostly unreadable (%d of %d); using the "
                           "automatic selection", len(photos), len(selected_urls))
            degradations.append(PHOTO_NOTE_UNREADABLE)
            photos, photo_urls = [], []
            for url in _auto_selection():
                try:
                    photos.append(_fetch_listing_photo(url))
                    photo_urls.append(url)
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

        overrides = {}
        for url, text in (caption_overrides or {}).items():
            text = (text or "").strip()
            ok, _reason = _caption_is_safe(text)
            if ok:
                overrides[url] = text
        if survey_used:
            # Smart selection already made this render's model call, and it wrote the
            # captions while it was there. Looked up BY URL rather than by position: a
            # photo that turned out to be unreadable above is missing from `photos`,
            # and anything keyed on index would hand its caption to the next photo
            # along -- a caption is a claim about a room, and putting it on the wrong
            # one misdescribes the property.
            captions = [survey_captions.get(u) for u in photo_urls]
            # The survey's own note, carried through so that captions dropped by the
            # copy guards are reported exactly as the caption pass would have reported
            # them. Silently shipping bare photos leaves nothing to diagnose later.
            caption_note = survey_note
            for i, u in enumerate(photo_urls):
                if u in overrides:
                    captions[i] = overrides[u]
        elif overrides:
            todo = [i for i, u in enumerate(photo_urls) if u not in overrides]
            if todo:
                generated, caption_note = _generate_room_captions(
                    [photos[i] for i in todo], copy_guard=copy_guard)
            else:
                # Every photo carries an agent caption: no model call, no cost, no wait.
                generated, caption_note = [], None
            captions = [None] * len(photos)
            for slot, i in enumerate(todo):
                captions[i] = generated[slot] if slot < len(generated) else None
            for i, u in enumerate(photo_urls):
                if u in overrides:
                    captions[i] = overrides[u]
        else:
            captions, caption_note = _generate_room_captions(photos, copy_guard=copy_guard)
        # Filled in after the clip loop, from the photos that actually reached the
        # film. Computing it here over the whole PLAN meant a render that ran out of
        # budget, or lost a clip to an encode failure, still reported and STORED
        # captions for photos the agent cannot see in their video -- and those stored
        # captions are what the caption editor shows them next time.
        captions_used = {}
        if caption_note:
            degradations.append(f"captions: {caption_note}")
        caption_size = _caption_font_size(captions)

        with tempfile.TemporaryDirectory() as workdir:
            clip_paths = []
            # --- the Signature tier's opening slide: the agent themselves ---
            # Built here, into this render's workdir, so the file the timeline consumes
            # is OURS. An earlier cut took a caller-owned path straight into clip_paths,
            # where the single-clip shortcut below (os.replace) would have MOVED the
            # agent's stored clip out from under them.
            #
            # This is the one stage of a Signature that does not degrade. A Signature
            # without the agent in it is a Classic, and quietly shipping one as the other
            # means an agent posts a film believing they are in it. Better to fail and
            # say why.
            opening_clip = None
            if presenter_path:
                opening_clip = os.path.join(workdir, "clip_presenter.mp4")
                try:
                    # Answers a note only when the stored clip was HDR AND this ffmpeg
                    # could not convert it properly. A clip stored before the HDR fix is
                    # repaired in there rather than reported, so the agent does not have
                    # to re-upload anything.
                    hdr_note = _presenter_opening_clip(presenter_path, opening_clip)
                    if hdr_note:
                        degradations.append(hdr_note)
                except Exception as e:
                    logger.error("presenter opening clip failed (%s)", e)
                    raise RuntimeError(
                        "Your presenting clip could not be read. Try uploading it again "
                        "in My Profile, or record a new one."
                    ) from e
                clip_paths.append(opening_clip)
            # Which photos actually reached a clip. `photo_urls` is the PLAN; this is
            # the film. They diverge when the budget runs out or a clip fails to
            # encode, and the captions we return and store have to describe the film
            # the agent can watch, not the one we set out to make.
            rendered_urls = []
            loop_started = time.monotonic()
            for i, photo in enumerate(photos):
                elapsed = time.monotonic() - started
                if i > 0 and elapsed > RENDER_BUDGET_SECONDS:
                    logger.warning("render budget reached after %d photos; finishing early", i)
                    degradations.append(f"took too long, so only the first {i} photos were used")
                    break
                # Is this the final clip of the film? Not just "the last photo in the
                # plan": if the budget will not stretch to another clip after this one,
                # this one ENDS the film and has to be built as the ending -- `last`
                # decides how long the caption stays up, and a clip built as a middle
                # clip drops its caption XFADE_SECONDS early.
                #
                # The prediction is made BINDING below (we stop after it) rather than
                # re-checked next time round. That matters: predicting "last" and then
                # rendering another clip would leave a caption running through the
                # dissolve into it, which double-strikes. Being wrong in the other
                # direction only costs a caption 1.25s of screen time.
                per_clip = (time.monotonic() - loop_started) / i if i else 0.0
                budget_is_spent = (
                    i > 0 and elapsed + 2 * per_clip > RENDER_BUDGET_SECONDS)
                is_last = (i == len(photos) - 1) or budget_is_spent
                clip_path = os.path.join(workdir, f"clip_{i:02d}.mp4")
                try:
                    if dissolve:
                        _contain_clip(
                            photo, clip_path, workdir, i,
                            caption=captions[i] if i < len(captions) else None,
                            caption_size=caption_size,
                            # The two senses of the move alternate on DIFFERENT
                            # periods -- the zoom every clip, the drift every two --
                            # so the sequence cycles through all four combinations
                            # (in-right, out-right, in-left, out-left) before it
                            # repeats. Alternating both on the same period would give
                            # two shapes taking turns, which at ten shots is a visible
                            # tick-tock.
                            zoom_in=(i % 2 == 0),
                            drift_right=(i // 2 % 2 == 0),
                            # `first` means "this clip is not dissolved INTO", which
                            # decides when the caption may appear. On a Signature the
                            # first PHOTO is the second CLIP, so its caption has to wait
                            # out the dissolve from the presenter or it double-strikes
                            # through it.
                            first=(i == 0 and opening_clip is None),
                            last=is_last,
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
                rendered_urls.append(photo_urls[i])
                if budget_is_spent and i < len(photos) - 1:
                    # Binding, as promised above: this clip was built as the ending, so
                    # the film ends here.
                    logger.warning("render budget will not reach another clip; "
                                   "finishing after %d photos", i + 1)
                    degradations.append(
                        f"took too long, so only the first {i + 1} photos were used")
                    break

            by_url = {u: captions[i] for i, u in enumerate(photo_urls) if captions[i]}
            captions_used = {u: by_url[u] for u in rendered_urls if u in by_url}

            photo_clips = len(clip_paths) - (1 if opening_clip else 0)
            if not photo_clips:
                # Also the guard that keeps the os.replace shortcut below from being
                # handed the presenter clip as the entire film.
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

            # --- soundtrack: music bed, plus the agent's voice on a Signature ---
            # Everything here degrades. The ladder, best first: voice over ducked music
            # -> music alone -> silence. A problem with one layer never costs the other
            # and never costs the video.
            final_path = os.path.join(workdir, f"final_{uuid.uuid4().hex[:8]}.mp4")
            wrote_final = False
            track_path = None
            if not is_auto_music_choice(music_track):
                track_path = resolve_pickable_track(music_track)
                music_tier = "chosen by agent"
                if track_path is None:
                    logger.warning("chosen music %r is not available; using the automatic pick",
                                   music_track)
                    degradations.append(
                        "the music you picked was not available, so a matching track was used")
            if track_path is None:
                track_path, music_tier = _select_music_track(music_seed, property_type)

            # The voice comes from the agent's stored clip, not from the opening slide --
            # the slide is silent by construction (_ENCODE_ARGS carries -an). Only offered
            # when that slide actually made it into the film, so the voice can never play
            # over a video the agent is not in.
            voice_path = presenter_path if opening_clip else None
            fade_out_start = max(0.0, silent_duration - AUDIO_FADE_OUT_SECONDS)
            voice_end = min(SECONDS_PER_PHOTO, silent_duration)
            duck_release = max(0.1, PRESENTER_DUCK_RELEASE)

            def _music_chain(normalize):
                """[1:a] -> [music]. `normalize` is True ONLY for tracks flagged "raw" in
                tracks.json; without it this is byte-for-byte the filter every existing
                track has always used."""
                norm = (
                    f"loudnorm=I={RAW_TRACK_TARGET_LUFS}:TP={RAW_TRACK_TRUE_PEAK_DB}"
                    f":LRA={RAW_TRACK_LRA},aresample=44100,"
                ) if normalize else ""
                return (
                    f"[1:a]atrim=0:{silent_duration:.3f},asetpts=PTS-STARTPTS,"
                    f"{norm}"
                    f"volume={AUDIO_VOLUME},"
                    f"afade=t=in:st=0:d={AUDIO_FADE_IN_SECONDS},"
                    f"afade=t=out:st={fade_out_start:.3f}:d={AUDIO_FADE_OUT_SECONDS}"
                )

            def _duck():
                """A second volume stage that holds the music down while the agent speaks
                and eases it back up over PRESENTER_DUCK_RELEASE once the film reaches the
                rooms. Multiplies the stage before it, so AUDIO_VOLUME still sets the
                ceiling and nothing about the music's own level changes."""
                return (
                    f",volume='min(1,max({PRESENTER_MUSIC_DUCK},"
                    f"{PRESENTER_MUSIC_DUCK}+(1-{PRESENTER_MUSIC_DUCK})"
                    f"*(t-{voice_end:.3f})/{duck_release:.3f}))':eval=frame"
                )

            def _voice_chain(index):
                """[index:a] -> [voice]: the opening slide's worth of the agent speaking,
                padded with silence to the full length of the film.

                Padded rather than mixed short because amix's own normalisation is what
                the volume=2 below undoes, and a stream that drops out mid-mix makes that
                correction wrong for the rest of the film."""
                fade = min(PRESENTER_VOICE_FADE, voice_end / 2)
                return (
                    f"[{index}:a]atrim=0:{voice_end:.3f},asetpts=PTS-STARTPTS,"
                    f"volume={PRESENTER_VOICE_VOLUME},"
                    f"afade=t=out:st={voice_end - fade:.3f}:d={fade:.3f},"
                    f"apad,atrim=0:{silent_duration:.3f},asetpts=PTS-STARTPTS"
                )

            def _soundtrack(normalize, with_voice):
                """(extra ffmpeg inputs, filter_complex) for one attempt."""
                inputs, filters = [], []
                both = bool(track_path) and with_voice
                if track_path:
                    # -stream_loop so a track shorter than the video still covers it.
                    inputs += ["-stream_loop", "-1", "-i", track_path]
                    filters.append(_music_chain(normalize)
                                   + (_duck() if with_voice else "")
                                   + (f",{PRESENTER_MIX_FORMAT}[music]" if both else "[aout]"))
                if with_voice:
                    inputs += ["-i", voice_path]
                    filters.append(_voice_chain(2 if track_path else 1)
                                   + (f",{PRESENTER_VOICE_TO_STEREO}"
                                      f",{PRESENTER_MIX_FORMAT}[voice]"
                                      if both else "[aout]"))
                if both:
                    # amix scales by 1/inputs, so volume=2 puts both layers back at the
                    # level their own chains set. Worst case is the ducked music under
                    # the voice, which lands just under full scale -- see
                    # PRESENTER_VOICE_VOLUME.
                    filters.append("[music][voice]amix=inputs=2:duration=first,volume=2[aout]")
                # A music-only soundtrack is therefore the exact filter string, and the
                # exact command, that shipped before Signature existed.
                return inputs, ";".join(filters)

            # Attempts, best first, deduplicated. A raw track tries normalization first,
            # so a normalization problem costs loudness consistency rather than the music.
            # A normal Classic has exactly one attempt, identical to before this change.
            is_raw = _is_raw_track(track_path) if track_path else False
            attempts = []
            for with_voice in ([True, False] if voice_path else [False]):
                for normalize in ([True, False] if is_raw else [False]):
                    if not track_path and not with_voice:
                        # Nothing left to put in the soundtrack: that attempt would be an
                        # empty -filter_complex with -map "[aout]", which cannot succeed
                        # and only spends an ffmpeg call on the way to the silent
                        # fallback below. Reached when the music file is missing from the
                        # deploy and the voice layer has already failed.
                        continue
                    attempts.append((normalize, with_voice))
            if not track_path and not voice_path:
                attempts = []
                logger.warning("no usable music track found in %s", AUDIO_DIR)
                degradations.append("the background music file is missing from the deploy")

            used_voice = False
            for attempt_no, (normalize, with_voice) in enumerate(attempts):
                extra_inputs, filter_complex = _soundtrack(normalize, with_voice)
                try:
                    _run_ffmpeg([
                        "ffmpeg", "-y", "-i", silent_path,
                    ] + extra_inputs + [
                        "-filter_complex", filter_complex,
                        "-map", "0:v", "-map", "[aout]",
                        "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                        "-movflags", "+faststart", final_path,
                    ], TIMEOUT_MUSIC)
                    wrote_final = True
                    used_voice = with_voice
                    # Logged on every render so "why does my video sound different from my
                    # colleague's?" is answerable from Railway's logs alone.
                    logger.info("soundtrack: %s (%s tier, property_type %r, seed %r)%s%s",
                                os.path.basename(track_path) if track_path else "no music",
                                music_tier if track_path else "n/a",
                                str(property_type or ""), str(music_seed or ""),
                                ", raw track loudness-normalized" if normalize else "",
                                ", agent's voice over ducked music" if with_voice else "")
                    break
                except Exception as e:
                    last = attempt_no == len(attempts) - 1
                    if with_voice and not last:
                        logger.warning("could not mix the agent's voice in (%s); "
                                       "trying the next soundtrack option", e)
                        continue
                    if normalize and not last:
                        logger.warning("loudness normalization of raw track %s failed "
                                       "(%s); retrying with the track as-is",
                                       os.path.basename(track_path), e)
                        continue
                    # Covers an unreadable or corrupt track as well as a mux failure:
                    # either way the video ships, silent.
                    logger.warning("soundtrack failed (%s); shipping a silent video", e)
                    degradations.append("the background music could not be added")

            # Said once, from what actually shipped, rather than once per failed attempt.
            if voice_path and wrote_final and not used_voice:
                degradations.append(
                    "your voice could not be added, so the video has music only")

            if not wrote_final:
                _run_ffmpeg(["ffmpeg", "-y", "-i", silent_path, "-c", "copy",
                             "-movflags", "+faststart", final_path], TIMEOUT_MUSIC)

            # Last thing before the bytes leave: never hand storage a file it will
            # reject for size. Does nothing at all to a normal-sized film, and applies
            # to a Signature exactly as to a Classic -- a Signature is one slide longer,
            # so if anything it needs this more.
            final_path, shrink_note = _fit_delivery_size(final_path, workdir)
            if shrink_note:
                degradations.append(shrink_note)

            with open(final_path, "rb") as f:
                data = f.read()

        if degradations:
            logger.warning("video rendered with degradations: %s", "; ".join(degradations))
        logger.info(
            "video rendered: %d photo(s)%s, %.1fs, %.1fMB, %.0fs wall clock",
            photo_clips, " + the agent's own clip" if opening_clip else "",
            silent_duration, len(data) / 1024 / 1024,
            time.monotonic() - started,
        )
        return data, degradations, captions_used
    finally:
        _RENDER_SLOTS.release()
