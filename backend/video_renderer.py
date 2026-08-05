"""NestList property video renderer -- turns a listing's photos into a branded vertical
slideshow video (1080x1920, suitable for Instagram Reels/Stories, TikTok, LinkedIn).

Each photo becomes a slow-zoom "Ken Burns" clip via ffmpeg's zoompan filter; clips are
crossfaded together and concatenated with a branded outro slide, with a soft looping
background track underneath. The bundled track (audio/soft_piano.mp3, "Piano Soft
Gentle Morning Keys" by Alex Morgan) is downloaded from Pixabay, whose Content License
permits commercial use, including in videos posted to social media, without attribution
-- see https://pixabay.com/service/license-summary/.

Requires the `ffmpeg` binary on PATH (see railpack.json).
"""
import io
import os
import subprocess
import tempfile
import uuid

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

VW, VH = 1080, 1920
FPS = 25
SECONDS_PER_PHOTO = 5.5
OUTRO_SECONDS = 5.5
MAX_PHOTOS = 15  # matches the app's overall per-listing photo upload cap
TRANSITION_SECONDS = 1.8
ZOOM_TARGET = 1.15
AUDIO_FADE_SECONDS = 1.5
AUDIO_VOLUME = 0.35

AUDIO_PATH = os.path.join(os.path.dirname(__file__), "audio", "soft_piano.mp3")

GOLD = (240, 200, 74, 255)
WHITE = (248, 244, 236, 255)
PALE = (255, 255, 255, 255)
CREAM_BG = (250, 248, 243, 255)
INK = (28, 27, 23, 255)
MUTED = (114, 110, 96, 255)
DEEP_GOLD = (184, 145, 46, 255)  # readable gold-on-cream, same shift poster_renderer uses for text on light backgrounds

FONT_DIR = os.path.join(os.path.dirname(__file__), "fonts")
PLAYFAIR = os.path.join(FONT_DIR, "PlayfairDisplay-Variable.ttf")
INTER = os.path.join(FONT_DIR, "Inter-Variable.ttf")


def _load_font(path, size, weight=None, opsz=None):
    font = ImageFont.truetype(path, size)
    try:
        axes = font.get_variation_axes()
        names = [a["name"].decode() if isinstance(a["name"], bytes) else a["name"] for a in axes]
        values = []
        for name in names:
            if name == "Weight" and weight is not None:
                values.append(weight)
            elif name == "Optical size" and opsz is not None:
                values.append(opsz)
            else:
                values.append(next(a["default"] for a in axes if (a["name"].decode() if isinstance(a["name"], bytes) else a["name"]) == name))
        font.set_variation_by_axes(values)
    except Exception:
        pass
    return font


TITLE_FONT = _load_font(PLAYFAIR, 64, weight=700)
PRICE_FONT = _load_font(INTER, 58, weight=700, opsz=32)
STATS_FONT = _load_font(INTER, 34, weight=500, opsz=18)
EYEBROW_FONT = _load_font(INTER, 28, weight=600, opsz=14)
OUTRO_NAME_FONT = _load_font(INTER, 46, weight=700, opsz=20)
OUTRO_CONTACT_FONT = _load_font(INTER, 34, weight=600, opsz=16)
OUTRO_TAGLINE_FONT = _load_font(PLAYFAIR, 34, weight=500)

# "card" style -- a fixed, static background photo with a floating info-card on top;
# a small window inset into the card is the only part that actually moves (a Ken Burns
# clip through the listing's other photos), the same layered look as a developer's
# boosted Facebook/Instagram ad: the ad itself is a still, only the inset photo plays.
CARD_TITLE_FONT = _load_font(PLAYFAIR, 54, weight=700)
CARD_PRICE_FONT = _load_font(INTER, 44, weight=700, opsz=24)
CARD_STATS_FONT = _load_font(INTER, 26, weight=500, opsz=14)
CARD_MONOGRAM_FONT = _load_font(PLAYFAIR, 24, weight=600)
CARD_CTA_FONT = _load_font(INTER, 30, weight=700, opsz=16)


def _run_ffmpeg(cmd, timeout):
    """subprocess.run's default CalledProcessError str() is just the command and
    exit code -- useless for diagnosing an actual ffmpeg failure. This surfaces the
    real stderr (truncated) so both Railway logs and the API error response say
    what actually went wrong."""
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else "(no stderr captured)"
        raise RuntimeError(f"ffmpeg exited {e.returncode}: {stderr[-1500:]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"ffmpeg timed out after {timeout}s") from e


def _fetch_image(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content))


def _fit(img, w, h, centering=(0.5, 0.4)):
    return ImageOps.fit(img.convert("RGB"), (w, h), centering=centering)


def _fit_with_letterbox(img, w, h, centering=(0.5, 0.4), blur_radius=40, darken=0.55):
    """Fits a photo into a (w, h) frame without cropping wide/landscape shots.

    Most listing photos are landscape while the video canvas is a tall 9:16 portrait
    (1080x1920) -- a plain cover-crop would slice off both sides of the photo (a square
    photo already loses ~44% of its width; a typical 3:2 landscape shot loses ~62%),
    which can cut a house frontage right out of frame. Instead, when the photo is wider
    than the target aspect, the full photo is shown letterboxed in the middle, with the
    empty top/bottom bars filled by a blurred, darkened copy of the same photo -- the
    same technique Instagram/TikTok use for landscape media in Stories/Reels, so nothing
    in the original shot is lost.

    Photos already at or near the target aspect (portrait phone shots) skip the
    letterbox and get a plain cover-fit, since there's nothing meaningful to preserve.
    """
    img = img.convert("RGB")
    src_w, src_h = img.size
    src_aspect = src_w / src_h
    target_aspect = w / h

    if src_aspect <= target_aspect * 1.05:
        return ImageOps.fit(img, (w, h), centering=centering)

    background = ImageOps.fit(img, (w, h), centering=centering)
    background = background.filter(ImageFilter.GaussianBlur(blur_radius))
    background = Image.blend(background, Image.new("RGB", (w, h), (0, 0, 0)), darken)

    fg_w = w
    fg_h = round(w / src_aspect)
    foreground = img.resize((fg_w, fg_h), Image.LANCZOS)

    canvas = background
    canvas.paste(foreground, (0, (h - fg_h) // 2))
    return canvas


def _gradient_scrim(base, y0, y1, from_alpha, to_alpha, color=(8, 12, 10)):
    height = y1 - y0
    if height <= 0:
        return
    gradient = Image.new("L", (1, height))
    gradient.putdata([int(from_alpha + (to_alpha - from_alpha) * (row / max(height - 1, 1))) for row in range(height)])
    alpha = gradient.resize((base.width, height))
    overlay = Image.new("RGBA", (base.width, height), color)
    overlay.putalpha(alpha)
    base.alpha_composite(overlay, dest=(0, y0))


def _text_with_shadow(base, draw, pos, text, font, fill, blur=6, offset=(0, 2)):
    if not text:
        return
    x, y = pos
    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow_layer).text((x + offset[0], y + offset[1]), text, font=font, fill=(0, 0, 0, 170))
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(shadow_layer)
    draw.text((x, y), text, font=font, fill=fill)


def _render_photo_slide(photo, eyebrow, title, price_text, stats_line):
    """First slide gets the full text treatment (eyebrow + title + price + stats);
    later slides get no text so the property photos read cleanly without repeating
    the same overlay on every frame."""
    base = _fit_with_letterbox(photo, VW, VH).convert("RGBA")
    draw = ImageDraw.Draw(base)

    if eyebrow or title or price_text or stats_line:
        _gradient_scrim(base, VH - 620, VH, 0, 210)
        x = 64
        y = VH - 540
        if eyebrow:
            draw.text((x, y), eyebrow.upper(), font=EYEBROW_FONT, fill=GOLD)
            y += 46
        if title:
            _text_with_shadow(base, draw, (x, y), title, TITLE_FONT, WHITE, blur=8)
            y += 90
        if price_text:
            _text_with_shadow(base, draw, (x, y), price_text, PRICE_FONT, GOLD, blur=6)
            y += 76
        if stats_line:
            _text_with_shadow(base, draw, (x, y), stats_line, STATS_FONT, WHITE, blur=5)

    return base.convert("RGB")


def _render_plain_slide(photo):
    return _fit_with_letterbox(photo, VW, VH).convert("RGB")


def _wrap_text(draw, text, font, max_width):
    """Minimal word-wrapper -- kept local rather than imported from poster_renderer
    so the two renderers stay independent of each other, same as the font/color
    constants above are each defined here rather than shared."""
    if not text:
        return []
    words = text.split()
    lines = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _build_static_card_base(hero_photo, property_type, district, price_text, stats_line):
    """Renders the still background + floating info-card + bottom CTA bar, all fixed
    for the whole video -- everything except the inset window below is a single frame,
    same as a developer's boosted Facebook/Instagram ad is a still image with only a
    small photo panel actually moving. Returns (image, inset_rect) where inset_rect is
    the (x, y, w, h) canvas position the caller must overlay a moving clip onto -- left
    empty here since the video composited on top will cover it completely.

    Uses a plain cover-crop rather than _fit_with_letterbox's blur treatment --
    letterboxing exists to avoid cropping when the whole photo has to read clearly
    (the classic style's full-bleed slides), but here the card already covers most
    of the frame, so cropping the edges costs little and a sharp, filled background
    reads far better than a mostly-blurred one behind a card that dominates anyway."""
    base = _fit(hero_photo, VW, VH).convert("RGBA")
    draw = ImageDraw.Draw(base)

    margin, pad_x = 90, 56
    card_x0, card_x1 = margin, VW - margin
    content_w = (card_x1 - card_x0) - 2 * pad_x
    cx = VW // 2

    title = (district or property_type or "").upper()
    title_lines = _wrap_text(draw, title, CARD_TITLE_FONT, content_w)[:2]
    stats_lines = _wrap_text(draw, stats_line, CARD_STATS_FONT, content_w)[:2] if stats_line else []

    title_line_h = CARD_TITLE_FONT.size + 14
    inset_w = content_w
    inset_h = round(inset_w * 0.6 / 2) * 2  # libx264 + yuv420p requires even width/height

    content_h = len(title_lines) * title_line_h + 66  # + divider row
    if price_text:
        content_h += CARD_PRICE_FONT.size + 20
    content_h += inset_h + 20
    for _ in stats_lines:
        content_h += CARD_STATS_FONT.size + 10

    pad_top, pad_bottom = 52, 48
    card_h = pad_top + content_h + pad_bottom
    card_y0 = (VH - card_h) // 2  # vertically centered in the full canvas
    card_y1 = card_y0 + card_h
    draw.rectangle((card_x0, card_y0, card_x1, card_y1), fill=CREAM_BG, outline=GOLD, width=2)

    y = card_y0 + pad_top
    for line in title_lines:
        tw = draw.textbbox((0, 0), line, font=CARD_TITLE_FONT)[2]
        draw.text((cx - tw / 2, y), line, font=CARD_TITLE_FONT, fill=INK)
        y += title_line_h

    y += 16
    circle_d = 32
    draw.line((cx - 130, y + circle_d / 2, cx - circle_d / 2 - 10, y + circle_d / 2), fill=DEEP_GOLD, width=2)
    draw.line((cx + circle_d / 2 + 10, y + circle_d / 2, cx + 130, y + circle_d / 2), fill=DEEP_GOLD, width=2)
    draw.ellipse((cx - circle_d / 2, y, cx + circle_d / 2, y + circle_d), outline=DEEP_GOLD, width=2)
    mw = draw.textbbox((0, 0), "N", font=CARD_MONOGRAM_FONT)[2]
    draw.text((cx - mw / 2, y + circle_d / 2 - CARD_MONOGRAM_FONT.size / 2 - 2), "N", font=CARD_MONOGRAM_FONT, fill=DEEP_GOLD)
    y += circle_d + 20

    if price_text:
        pw = draw.textbbox((0, 0), price_text, font=CARD_PRICE_FONT)[2]
        draw.text((cx - pw / 2, y), price_text, font=CARD_PRICE_FONT, fill=DEEP_GOLD)
        y += CARD_PRICE_FONT.size + 20

    inset_x = card_x0 + pad_x
    inset_y = y
    # inset window itself is left blank -- the moving clip overlaid on top in
    # _compose_card_intro covers this rectangle completely, pixel for pixel
    y += inset_h + 20

    for line in stats_lines:
        sw = draw.textbbox((0, 0), line, font=CARD_STATS_FONT)[2]
        draw.text((cx - sw / 2, y), line, font=CARD_STATS_FONT, fill=MUTED)
        y += CARD_STATS_FONT.size + 10

    bar_h = 130
    draw.rectangle((0, VH - bar_h, VW, VH), fill=GOLD)
    bar_text_y = VH - bar_h / 2 - CARD_CTA_FONT.size / 2 - 4
    draw.text((64, bar_text_y), "VIEW LISTING", font=CARD_CTA_FONT, fill=INK)
    arrow = "›"
    aw = draw.textbbox((0, 0), arrow, font=CARD_CTA_FONT)[2]
    draw.text((VW - 64 - aw, bar_text_y), arrow, font=CARD_CTA_FONT, fill=INK)

    return base.convert("RGB"), (inset_x, inset_y, inset_w, inset_h)


def _build_inset_video(photos, inset_w, inset_h, workdir):
    """Builds the small Ken Burns clip that plays inside the card's inset window --
    the only moving part of 'card' style. Reuses _zoompan_clip/_xfade_pair at the
    inset's own (smaller) size rather than the full canvas size."""
    clip_paths = []
    clip_durations = []
    for i, photo in enumerate(photos):
        slide = ImageOps.fit(photo.convert("RGB"), (inset_w, inset_h), centering=(0.5, 0.4))
        clip_path = os.path.join(workdir, f"inset_{i:02d}.mp4")
        _zoompan_clip(slide, clip_path, SECONDS_PER_PHOTO, zoom_in=(i % 2 == 0), w=inset_w, h=inset_h)
        clip_paths.append(clip_path)
        clip_durations.append(SECONDS_PER_PHOTO)

    if len(clip_paths) == 1:
        return clip_paths[0], clip_durations[0]

    running_path = clip_paths[0]
    running_duration = clip_durations[0]
    for i in range(1, len(clip_paths)):
        merged_path = os.path.join(workdir, f"inset_merged_{i:02d}.mp4")
        _xfade_pair(running_path, clip_paths[i], merged_path, running_duration, TRANSITION_SECONDS, 90)
        running_path = merged_path
        running_duration = running_duration + clip_durations[i] - TRANSITION_SECONDS
    return running_path, running_duration


def _compose_card_intro(base_img, inset_path, inset_rect, duration, workdir):
    """Overlays the moving inset clip onto the still card background for the inset
    clip's full duration, producing the 'card' style's single opening clip."""
    base_path = os.path.join(workdir, "card_base.png")
    base_img.save(base_path, format="PNG")

    x, y, w, h = inset_rect
    out_path = os.path.join(workdir, "card_intro.mp4")
    cmd = [
        "ffmpeg", "-y",
        "-loop", "1", "-i", base_path,
        "-i", inset_path,
        "-filter_complex",
        f"[0:v]scale={VW}:{VH},format=yuv420p[bg];[1:v]format=yuv420p[fg];[bg][fg]overlay=x={x}:y={y}:shortest=1",
        "-t", str(duration),
        "-r", str(FPS), "-vsync", "cfr",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out_path,
    ]
    _run_ffmpeg(cmd, 90)
    return out_path


def _render_outro_slide(agent_name, agent_contact_line):
    base = Image.new("RGBA", (VW, VH), (13, 43, 29, 255))
    draw = ImageDraw.Draw(base)

    cx = VW // 2
    cy = VH // 2

    tagline = "Smarter Listings. Better Results."
    tw = draw.textbbox((0, 0), tagline, font=OUTRO_TAGLINE_FONT)[2]
    draw.text((cx - tw / 2, cy - 140), tagline, font=OUTRO_TAGLINE_FONT, fill=(212, 175, 55, 230))

    draw.line((cx - 60, cy - 60, cx + 60, cy - 60), fill=GOLD, width=2)

    name_text = (agent_name or "").upper()
    nw = draw.textbbox((0, 0), name_text, font=OUTRO_NAME_FONT)[2]
    draw.text((cx - nw / 2, cy - 20), name_text, font=OUTRO_NAME_FONT, fill=PALE)

    if agent_contact_line:
        cw = draw.textbbox((0, 0), agent_contact_line, font=OUTRO_CONTACT_FONT)[2]
        draw.text((cx - cw / 2, cy + 40), agent_contact_line, font=OUTRO_CONTACT_FONT, fill=GOLD)

    return base.convert("RGB")


def _zoompan_clip(slide_img, out_path, duration, zoom_in=True, w=None, h=None):
    """Renders a single still image into a slow-zoom video clip via ffmpeg's zoompan
    filter. Centered zoom (not the default top-left) so the motion reads as a deliberate
    push-in, not a drift toward a corner. w/h default to the full canvas size, but the
    'card' style's inset clip passes its own smaller box dimensions."""
    w = w or VW
    h = h or VH
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        slide_img.save(tmp.name, format="PNG")
        src_path = tmp.name

    z_expr = f"min(zoom+0.0020,{ZOOM_TARGET})" if zoom_in else f"if(lte(zoom,1.0),{ZOOM_TARGET},max(zoom-0.0020,1.0))"

    # -frames:v + zoompan's internal per-frame PTS is a known source of malformed/
    # non-monotonic timestamps that some players (Chrome included) refuse to start
    # playback on even though the container itself parses fine. Cutting by -t plus
    # forcing a constant output frame rate with -r/-vsync restamps clean, regular
    # PTS instead of trusting zoompan's own.
    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", src_path,
        "-vf",
        f"zoompan=z='{z_expr}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={w}x{h}:fps={FPS},format=yuv420p",
        "-t", str(duration),
        "-r", str(FPS), "-vsync", "cfr",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out_path,
    ]
    try:
        _run_ffmpeg(cmd, 60)
    finally:
        os.unlink(src_path)


def _xfade_pair(base_path, next_path, out_path, base_duration, transition_seconds, timeout):
    """Crossfades two clips into one file. Used to build the final video by folding
    clips together one at a time (2 open input streams per call) instead of handing
    ffmpeg one filter graph with every clip open at once -- at ~15 photos that single
    giant graph needs that many concurrent decoders live simultaneously, which is
    enough to exhaust memory/resources on a small Railway instance and fail outright
    (confirmed: reliable up to ~6 clips, hard-failed at 15). Folding pairwise keeps
    peak resource use constant no matter how many photos a listing has."""
    offset = max(0.0, base_duration - transition_seconds)
    filter_complex = (
        f"[0:v]fps={FPS},format=yuv420p[v0];"
        f"[1:v]fps={FPS},format=yuv420p[v1];"
        f"[v0][v1]xfade=transition=fade:duration={transition_seconds}:offset={offset:.3f}[vout]"
    )
    cmd = [
        "ffmpeg", "-y", "-i", base_path, "-i", next_path,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
        out_path,
    ]
    _run_ffmpeg(cmd, timeout)


def render_property_video(image_urls, property_type, district, price_text, stats, agent_name, agent_contact_line, style="classic"):
    """Returns the finished video as bytes (MP4, H.264 + AAC, 1080x1920).

    stats: list of strings, same shape as poster_renderer's (empty entries dropped).
    style: "classic" (default) -- first slide gets a text overlay, later slides are
    clean photos, same as the original Ken Burns slideshow. "card" -- a still
    background photo with a floating info-card on top; only a small window inset
    into the card actually moves (a Ken Burns clip through the other photos), same
    as a developer's boosted Facebook/Instagram ad -- the ad itself is a still, only
    the inset photo plays.
    """
    photos = [_fetch_image(u) for u in image_urls[:MAX_PHOTOS]]
    if not photos:
        raise ValueError("At least one photo is required to generate a video")

    stats_line = "   ·   ".join(s for s in stats if s)

    with tempfile.TemporaryDirectory() as workdir:
        clip_paths = []
        clip_durations = []

        if style == "card":
            hero = photos[0]
            inset_photos = photos[1:] if len(photos) > 1 else photos[:1]
            base_img, inset_rect = _build_static_card_base(hero, property_type, district, price_text, stats_line)
            inset_path, inset_duration = _build_inset_video(inset_photos, inset_rect[2], inset_rect[3], workdir)
            intro_path = _compose_card_intro(base_img, inset_path, inset_rect, inset_duration, workdir)
            clip_paths.append(intro_path)
            clip_durations.append(inset_duration)
        else:
            for i, photo in enumerate(photos):
                if i == 0:
                    slide = _render_photo_slide(photo, district, property_type, price_text, stats_line)
                else:
                    slide = _render_plain_slide(photo)
                clip_path = os.path.join(workdir, f"clip_{i:02d}.mp4")
                _zoompan_clip(slide, clip_path, SECONDS_PER_PHOTO, zoom_in=(i % 2 == 0))
                clip_paths.append(clip_path)
                clip_durations.append(SECONDS_PER_PHOTO)

        outro = _render_outro_slide(agent_name, agent_contact_line)
        outro_path = os.path.join(workdir, "clip_outro.mp4")
        _zoompan_clip(outro, outro_path, OUTRO_SECONDS, zoom_in=True)
        clip_paths.append(outro_path)
        clip_durations.append(OUTRO_SECONDS)

        final_path = os.path.join(workdir, f"final_{uuid.uuid4().hex[:8]}.mp4")
        has_audio = os.path.exists(AUDIO_PATH)

        def _audio_filter(input_index, total_duration):
            fade_out_start = max(0.0, total_duration - AUDIO_FADE_SECONDS)
            return (
                f"[{input_index}:a]atrim=0:{total_duration:.3f},asetpts=PTS-STARTPTS,"
                f"volume={AUDIO_VOLUME},afade=t=in:d={AUDIO_FADE_SECONDS},"
                f"afade=t=out:st={fade_out_start:.3f}:d={AUDIO_FADE_SECONDS}[aout]"
            )

        if len(clip_paths) == 1:
            total_duration = clip_durations[0]
            if has_audio:
                cmd = [
                    "ffmpeg", "-y", "-i", clip_paths[0], "-stream_loop", "-1", "-i", AUDIO_PATH,
                    "-filter_complex", _audio_filter(1, total_duration),
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", final_path,
                ]
            else:
                cmd = ["ffmpeg", "-y", "-i", clip_paths[0], "-c", "copy", "-movflags", "+faststart", final_path]
            _run_ffmpeg(cmd, 60)
        else:
            # Fold clips together pairwise (2 open input streams per ffmpeg call) rather
            # than handing ffmpeg one filter graph with every clip open simultaneously --
            # see _xfade_pair's docstring for why.
            running_path = clip_paths[0]
            running_duration = clip_durations[0]
            for i in range(1, len(clip_paths)):
                merged_path = os.path.join(workdir, f"merged_{i:02d}.mp4")
                _xfade_pair(running_path, clip_paths[i], merged_path, running_duration, TRANSITION_SECONDS, 90)
                running_path = merged_path
                running_duration = running_duration + clip_durations[i] - TRANSITION_SECONDS

            if has_audio:
                cmd = [
                    "ffmpeg", "-y", "-i", running_path, "-stream_loop", "-1", "-i", AUDIO_PATH,
                    "-filter_complex", _audio_filter(1, running_duration),
                    "-map", "0:v", "-map", "[aout]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", final_path,
                ]
            else:
                cmd = ["ffmpeg", "-y", "-i", running_path, "-c", "copy", "-movflags", "+faststart", final_path]
            _run_ffmpeg(cmd, 60)

        with open(final_path, "rb") as f:
            return f.read()
