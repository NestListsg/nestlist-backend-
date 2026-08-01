"""NestList property video renderer -- turns a listing's photos into a branded vertical
slideshow video (1080x1920, suitable for Instagram Reels/Stories, TikTok, LinkedIn).

Each photo becomes a slow-zoom "Ken Burns" clip via ffmpeg's zoompan filter; clips are
concatenated with a branded outro slide. No background music is bundled -- adding one
requires a properly licensed track (royalty-free library), which isn't set up yet, so
shipping silent avoids a copyright takedown risk on the exact platforms this is posted to.

Requires the `ffmpeg` binary on PATH (see nixpacks.toml).
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
SECONDS_PER_PHOTO = 3.5
OUTRO_SECONDS = 3.5
MAX_PHOTOS = 6
ZOOM_TARGET = 1.15

GOLD = (240, 200, 74, 255)
WHITE = (248, 244, 236, 255)
PALE = (255, 255, 255, 255)

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


def _fetch_image(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content))


def _fit(img, w, h, centering=(0.5, 0.4)):
    return ImageOps.fit(img.convert("RGB"), (w, h), centering=centering)


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
    base = _fit(photo, VW, VH).convert("RGBA")
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
    return _fit(photo, VW, VH).convert("RGB")


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


def _zoompan_clip(slide_img, out_path, duration, zoom_in=True):
    """Renders a single still image into a slow-zoom video clip via ffmpeg's zoompan
    filter. Centered zoom (not the default top-left) so the motion reads as a deliberate
    push-in, not a drift toward a corner."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        slide_img.save(tmp.name, format="PNG")
        src_path = tmp.name

    total_frames = max(1, round(duration * FPS))
    z_expr = f"min(zoom+0.0020,{ZOOM_TARGET})" if zoom_in else f"if(lte(zoom,1.0),{ZOOM_TARGET},max(zoom-0.0020,1.0))"

    cmd = [
        "ffmpeg", "-y", "-loop", "1", "-i", src_path,
        "-vf",
        f"zoompan=z='{z_expr}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={VW}x{VH}:fps={FPS},format=yuv420p",
        "-frames:v", str(total_frames),
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        out_path,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=60)
    finally:
        os.unlink(src_path)


def render_property_video(image_urls, property_type, district, price_text, stats, agent_name, agent_contact_line):
    """Returns the finished video as bytes (MP4, H.264, silent, 1080x1920).

    stats: list of strings, same shape as poster_renderer's (empty entries dropped).
    """
    photos = [_fetch_image(u) for u in image_urls[:MAX_PHOTOS]]
    if not photos:
        raise ValueError("At least one photo is required to generate a video")

    stats_line = "   ·   ".join(s for s in stats if s)

    with tempfile.TemporaryDirectory() as workdir:
        clip_paths = []

        for i, photo in enumerate(photos):
            if i == 0:
                slide = _render_photo_slide(photo, district, property_type, price_text, stats_line)
            else:
                slide = _render_plain_slide(photo)
            clip_path = os.path.join(workdir, f"clip_{i:02d}.mp4")
            _zoompan_clip(slide, clip_path, SECONDS_PER_PHOTO, zoom_in=(i % 2 == 0))
            clip_paths.append(clip_path)

        outro = _render_outro_slide(agent_name, agent_contact_line)
        outro_path = os.path.join(workdir, "clip_outro.mp4")
        _zoompan_clip(outro, outro_path, OUTRO_SECONDS, zoom_in=True)
        clip_paths.append(outro_path)

        list_path = os.path.join(workdir, "list.txt")
        with open(list_path, "w") as f:
            for p in clip_paths:
                f.write(f"file '{p}'\n")

        final_path = os.path.join(workdir, f"final_{uuid.uuid4().hex[:8]}.mp4")
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path,
             "-c", "copy", "-movflags", "+faststart", final_path],
            check=True, capture_output=True, timeout=60,
        )

        with open(final_path, "rb") as f:
            return f.read()
