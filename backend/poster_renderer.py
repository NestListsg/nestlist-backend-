"""NestList poster renderer -- Pillow-based replacement for the Placid.app integration.

Ten templates, selected by template_id (matches agent.poster_template_id / POSTER_TEMPLATES
in main.py). Each _render_* function takes the same normalized inputs and returns an RGB
Pillow Image ready to save as JPEG. Shared helpers (fonts, scrims, text-with-shadow, frames)
live at the top; template-specific layout code lives in each _render_* function.
"""
import hashlib
import io
import os
import time

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont, ImageFilter, ImageOps, ImageStat

import fal_images
import photo_upscale

W, H = 1200, 1500

GOLD = (240, 200, 74, 255)
WHITE = (248, 244, 236, 255)
PALE = (255, 255, 255, 255)
CREAM_BG = (250, 248, 243, 255)
INK = (28, 27, 23, 255)
MUTED = (114, 110, 96, 255)

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


TITLE_FONT = _load_font(PLAYFAIR, 82, weight=700)
DISTRICT_FONT = _load_font(PLAYFAIR, 46, weight=500)
PRICE_FONT = _load_font(INTER, 68, weight=700, opsz=32)
STATS_FONT = _load_font(INTER, 38, weight=500, opsz=18)
NAME_FONT = _load_font(INTER, 52, weight=700, opsz=16)
CONTACT_FONT = _load_font(INTER, 76, weight=700, opsz=32)

EYEBROW_FONT = _load_font(INTER, 30, weight=600, opsz=14)
EYEBROW_FONT_SM = _load_font(INTER, 24, weight=600, opsz=12)
GALLERY_TITLE_FONT = _load_font(PLAYFAIR, 60, weight=500)
GALLERY_PRICE_FONT = _load_font(INTER, 40, weight=700, opsz=20)
GALLERY_STATS_FONT = _load_font(INTER, 24, weight=500, opsz=12)
GALLERY_CONTACT_FONT = _load_font(INTER, 26, weight=700, opsz=14)
HUGE_TITLE_FONT = _load_font(INTER, 96, weight=800, opsz=32)
BOLD_PRICE_FONT = _load_font(INTER, 44, weight=700, opsz=20)
NUMERAL_PRICE_FONT = _load_font(PLAYFAIR, 92, weight=700)
COLUMN_TITLE_FONT = _load_font(PLAYFAIR, 44, weight=600)
COLUMN_PRICE_FONT = _load_font(INTER, 38, weight=700, opsz=18)
LABEL_FONT = _load_font(INTER, 20, weight=600, opsz=10)
VALUE_FONT = _load_font(INTER, 32, weight=700, opsz=16)
BADGE_FONT = _load_font(INTER, 26, weight=600, opsz=13)
STRIP_NAME_FONT = _load_font(INTER, 34, weight=700, opsz=16)
STRIP_CONTACT_FONT = _load_font(INTER, 30, weight=600, opsz=14)


# Guards the video renderer already had and this one did not: an unbounded
# requests.get() on an agent-supplied URL would happily read a mis-uploaded 200MB file
# straight into memory, and a decompression bomb past that.
MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024
MAX_SOURCE_PIXELS = 80_000_000


def _fetch_image(url, attempts=2):
    """Downloads one photo, with a hard byte cap and one retry -- a single flaky CDN read
    shouldn't cost the agent a whole poster."""
    last_error = None
    for attempt in range(attempts):
        try:
            with requests.get(url, timeout=(5, 25), stream=True) as response:
                response.raise_for_status()
                declared = response.headers.get("content-length")
                if declared and int(declared) > MAX_DOWNLOAD_BYTES:
                    raise ValueError(f"photo is over the {MAX_DOWNLOAD_BYTES // 1024 // 1024}MB limit")
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


def _fetch_listing_photo(url):
    """The property photo, preferring its upscaled twin when one exists.

    A poster is 1200x1500 and _fit() crops to that aspect, so an 800x600 source is being
    stretched roughly 2.5x -- posters gain even more from the upload-time upscale than
    videos do, and get it for free from the same stored file.

    A miss is normal and silent: the original is what posters have always used.
    """
    hires = photo_upscale.hires_url_for(url)
    if hires:
        try:
            return _fetch_image(hires, attempts=1)
        except Exception:
            pass
    return _fetch_image(url)


def _fit(img, w, h, centering=(0.5, 0.42)):
    return ImageOps.fit(img.convert("RGB"), (w, h), centering=centering)


def _vertical_gradient_scrim(base, y0, y1, from_alpha, to_alpha, color=(8, 8, 6)):
    height = y1 - y0
    if height <= 0:
        return
    gradient = Image.new("L", (1, height))
    gradient.putdata([int(from_alpha + (to_alpha - from_alpha) * (row / max(height - 1, 1))) for row in range(height)])
    alpha = gradient.resize((base.width, height))
    overlay = Image.new("RGBA", (base.width, height), color)
    overlay.putalpha(alpha)
    base.alpha_composite(overlay, dest=(0, y0))


def _solid_band(base, box, color, alpha):
    x0, y0, x1, y1 = box
    overlay = Image.new("RGBA", (x1 - x0, y1 - y0), (*color, alpha))
    base.alpha_composite(overlay, dest=(x0, y0))


def _text_with_shadow(base, draw, pos, text, font, fill, shadow_color=(0, 0, 0, 160), blur=6, offset=(0, 2)):
    if not text:
        return
    x, y = pos
    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow_layer).text((x + offset[0], y + offset[1]), text, font=font, fill=shadow_color)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(shadow_layer)
    draw.text((x, y), text, font=font, fill=fill)


def _text_width(draw, text, font):
    if not text:
        return 0
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0]


def _centered_text(base, draw, cx, y, text, font, fill, blur=6, offset=(0, 2), shadow=True):
    if not text:
        return
    w = _text_width(draw, text, font)
    x = cx - w / 2
    if shadow:
        _text_with_shadow(base, draw, (x, y), text, font, fill, blur=blur, offset=offset)
    else:
        draw.text((x, y), text, font=font, fill=fill)


def _right_text(base, draw, right_x, y, text, font, fill, blur=6, offset=(0, 2), shadow=True):
    if not text:
        return
    w = _text_width(draw, text, font)
    x = right_x - w
    if shadow:
        _text_with_shadow(base, draw, (x, y), text, font, fill, blur=blur, offset=offset)
    else:
        draw.text((x, y), text, font=font, fill=fill)


def _square_photo(base, agent_photo, cx, cy, size, border_color=GOLD, border_width=3):
    """Full-opacity square-cropped agent headshot with a thin border, centered
    at (cx, cy). Sits inside the existing text zone rather than blended onto the
    property photo -- overlaying it on the photo is what made an earlier attempt read
    as an accidental floating face instead of a designed element."""
    if not agent_photo:
        return
    half = size / 2
    photo = ImageOps.fit(agent_photo.convert("RGB"), (size, size), centering=(0.5, 0.42))
    dest = (round(cx - half), round(cy - half))
    base.paste(photo, dest)
    x0, y0 = dest
    ImageDraw.Draw(base).rectangle((x0, y0, x0 + size, y0 + size), outline=border_color, width=border_width)


def _agent_strip(base, draw, x0, y0, width, height, agent_name, agent_contact_line, agent_photo,
                  translucent, align="left", edge_pad=24, name_color=WHITE, contact_color=GOLD,
                  band_color=(8, 8, 6), band_alpha=165, photo_border=GOLD):
    """A self-contained agent signature strip -- square photo plus name/contact, occupying
    its own reserved box that nothing else is ever laid out into. This replaces the earlier
    approach of squeezing the photo into the same row as stats/contact text, which kept
    colliding once the photo grew: price/stats and the strip can no longer overlap because
    they don't share vertical space at all.

    When `translucent`, the strip gets its own semi-transparent dark backing (mirrors
    Editorial's proven bottom bar) so the property photo still reads through behind it,
    rather than the strip looking like an opaque sticker dropped on top of the listing photo.

    `edge_pad` is the horizontal margin from the strip's box edge to where the photo+text
    block starts/ends (depending on `align`) -- kept separate from the photo's own vertical
    inset so a wide edge_pad (e.g. for right-aligned text near a canvas margin) doesn't also
    shrink the photo.
    """
    if translucent:
        _solid_band(base, (x0, y0, x0 + width, y0 + height), band_color, band_alpha)

    photo_vpad = min(18, height / 2 - 10)
    photo_size = height - photo_vpad * 2
    cy = y0 + height / 2

    name_text = agent_name.upper()
    contact_text = agent_contact_line or ""
    name_w = _text_width(draw, name_text, STRIP_NAME_FONT)
    contact_w = _text_width(draw, contact_text, STRIP_CONTACT_FONT) if contact_text else 0
    text_w = max(name_w, contact_w)
    block_w = (photo_size + 20 + text_w) if agent_photo else text_w

    if align == "center":
        block_x0 = x0 + (width - block_w) / 2
    elif align == "right":
        block_x0 = x0 + width - edge_pad - block_w
    else:
        block_x0 = x0 + edge_pad

    if agent_photo:
        _square_photo(base, agent_photo, block_x0 + photo_size / 2, cy, photo_size, border_color=photo_border)
        text_x = block_x0 + photo_size + 20
    else:
        text_x = block_x0

    name_bbox = draw.textbbox((0, 0), name_text, font=STRIP_NAME_FONT)
    name_h = name_bbox[3] - name_bbox[1]
    line_gap = 8
    if contact_text:
        contact_bbox = draw.textbbox((0, 0), contact_text, font=STRIP_CONTACT_FONT)
        contact_h = contact_bbox[3] - contact_bbox[1]
        total_h = name_h + line_gap + contact_h
    else:
        total_h = name_h

    name_y = cy - total_h / 2
    draw.text((text_x, name_y), name_text, font=STRIP_NAME_FONT, fill=name_color)
    if contact_text:
        contact_y = name_y + name_h + line_gap
        draw.text((text_x, contact_y), contact_text, font=STRIP_CONTACT_FONT, fill=contact_color)


def _double_gold_frame(base, margin=40, gap=10, color=GOLD, width=2):
    draw = ImageDraw.Draw(base)
    draw.rectangle((margin, margin, base.width - margin, base.height - margin), outline=color, width=width)
    m2 = margin + gap
    draw.rectangle((m2, m2, base.width - m2, base.height - m2), outline=color, width=width)


def _wrap_text(draw, text, font, max_width):
    # Break on spaces first; if a single "word" still overflows max_width (e.g. a long
    # hyphenated property type like "Semi-Detached" in a narrow column), fall back to
    # breaking it at hyphens too, so nothing runs off the edge of the canvas.
    words = []
    for word in text.split():
        if _text_width(draw, word, font) <= max_width or "-" not in word:
            words.append(word)
        else:
            parts = word.split("-")
            for i, part in enumerate(parts):
                words.append(part + "-" if i < len(parts) - 1 else part)

    lines, cur = [], ""
    for w in words:
        sep = "" if cur.endswith("-") else " "
        trial = (cur + sep + w).strip() if cur else w
        if _text_width(draw, trial, font) <= max_width or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def _stat_pairs(stats):
    labels = ["ROOMS", "BATHS", "BUILT-UP", "PSF"]
    pairs = []
    for label, s in zip(labels, stats):
        if not s:
            continue
        parts = s.split(" ", 1)
        value = parts[0] if label in ("ROOMS", "BATHS") and len(parts) == 2 else s
        pairs.append((label, value))
    return pairs


def _stats_line(stats):
    return "   ·   ".join(s for s in stats if s)


def _mixed(text):
    return text.title() if text else text


# ================================
# 1. EDITORIAL (existing, unchanged)
# ================================
def _render_editorial(property_type, district, price_text, stats, agent_name, agent_contact_line,
                       property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))

    bar_h = 320
    bar_top = H - bar_h

    _vertical_gradient_scrim(base, 0, 300, 150, 0)
    _vertical_gradient_scrim(base, H - 520, bar_top, 0, 190)

    draw = ImageDraw.Draw(base)

    x, y = 56, 56
    _text_with_shadow(base, draw, (x, y), property_type.upper(), TITLE_FONT, WHITE, blur=8)
    y += 96
    _text_with_shadow(base, draw, (x, y), district.upper(), DISTRICT_FONT, GOLD, blur=6)

    py = H - 480
    _text_with_shadow(base, draw, (x, py), price_text, PRICE_FONT, GOLD, blur=6)

    sy = py + 92
    _text_with_shadow(base, draw, (x, sy), _stats_line(stats), STATS_FONT, WHITE, blur=5)

    photo_w = 380
    right_overlay = Image.new("RGBA", (W - photo_w, bar_h), (10, 10, 8, 150))
    base.alpha_composite(right_overlay, dest=(photo_w, bar_top))

    if agent_photo:
        photo = _fit(agent_photo, photo_w, bar_h)
        base.paste(photo, (0, bar_top))
    else:
        draw.rectangle((0, bar_top, photo_w, H), fill=(40, 40, 36, 255))

    tx = photo_w + 48
    ty = bar_top + 84
    _text_with_shadow(base, draw, (tx, ty), agent_name.upper(), NAME_FONT, PALE, blur=3, offset=(0, 1))
    ty += 78
    _text_with_shadow(base, draw, (tx, ty), agent_contact_line, CONTACT_FONT, PALE, blur=3, offset=(0, 1))

    return base.convert("RGB")


# ================================
# 2. GALLERY FRAME
# ================================
def _render_gallery_frame(property_type, district, price_text, stats, agent_name, agent_contact_line,
                           property_photo, agent_photo):
    base = Image.new("RGBA", (W, H), CREAM_BG)
    draw = ImageDraw.Draw(base)

    margin_x, margin_top, photo_h = 70, 70, 970
    if property_photo:
        photo = _fit(property_photo, W - margin_x * 2, photo_h)
        base.paste(photo, (margin_x, margin_top))
        draw.rectangle((margin_x, margin_top, W - margin_x, margin_top + photo_h), outline=(230, 226, 214, 255), width=1)

    cx = W // 2
    y = margin_top + photo_h + 40
    draw.line((cx - 30, y, cx + 30, y), fill=GOLD, width=3)
    y += 26
    _centered_text(base, draw, cx, y, district.upper(), EYEBROW_FONT, (184, 145, 46, 255), shadow=False)
    y += 44
    _centered_text(base, draw, cx, y, _mixed(property_type), GALLERY_TITLE_FONT, INK, shadow=False)
    y += 74
    _centered_text(base, draw, cx, y, price_text, GALLERY_PRICE_FONT, INK, shadow=False)
    y += 52
    _centered_text(base, draw, cx, y, _stats_line(stats), GALLERY_STATS_FONT, MUTED, shadow=False)
    y += 70
    _agent_strip(base, draw, margin_x, y, W - margin_x * 2, 130, agent_name, agent_contact_line, agent_photo,
                 translucent=False, align="center", name_color=INK, contact_color=(184, 145, 46, 255))

    return base.convert("RGB")


# ================================
# 3. BOLD TYPE
# ================================
def _render_bold_type(property_type, district, price_text, stats, agent_name, agent_contact_line,
                       property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))
    draw = ImageDraw.Draw(base)

    _solid_band(base, (0, 0, W, 76), (12, 14, 12), 165)
    draw.text((44, 24), district.upper(), font=EYEBROW_FONT_SM, fill=WHITE)

    _vertical_gradient_scrim(base, H - 580, H, 0, 220)

    x = 48
    y = H - 500
    lines = _wrap_text(draw, property_type.upper(), HUGE_TITLE_FONT, W - 96)
    for line in lines[:2]:
        _text_with_shadow(base, draw, (x, y), line, HUGE_TITLE_FONT, WHITE, blur=10)
        y += 100

    y += 6
    _text_with_shadow(base, draw, (x, y), price_text, BOLD_PRICE_FONT, GOLD, blur=6)
    y += 60
    _text_with_shadow(base, draw, (x, y), _stats_line(stats), STATS_FONT, WHITE, blur=5)

    y += 84
    _agent_strip(base, draw, 0, y, W, 130, agent_name, agent_contact_line, agent_photo,
                 translucent=True, align="left")

    return base.convert("RGB")


# ================================
# 4. VIGNETTE FRAME
# ================================
def _render_vignette_frame(property_type, district, price_text, stats, agent_name, agent_contact_line,
                            property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))
    draw = ImageDraw.Draw(base)

    _vertical_gradient_scrim(base, 0, 240, 150, 0)
    _vertical_gradient_scrim(base, H - 420, H, 0, 200)

    cx = W // 2
    _centered_text(base, draw, cx, 70, property_type.upper(), EYEBROW_FONT, WHITE, blur=5)
    _centered_text(base, draw, cx, 112, district.upper(), DISTRICT_FONT, GOLD, blur=5)

    y = H - 360
    _centered_text(base, draw, cx, y, price_text, PRICE_FONT, GOLD, blur=6)
    y += 92
    _centered_text(base, draw, cx, y, _stats_line(stats), STATS_FONT, WHITE, blur=5)

    y += 66
    _agent_strip(base, draw, 50, y, W - 100, 100, agent_name, agent_contact_line, agent_photo,
                 translucent=True, align="center", edge_pad=18)

    _double_gold_frame(base, margin=40, gap=10)

    return base.convert("RGB")


# ================================
# 5. POSTCARD
# ================================
def _render_postcard(property_type, district, price_text, stats, agent_name, agent_contact_line,
                      property_photo, agent_photo):
    base = Image.new("RGBA", (W, H), CREAM_BG)
    draw = ImageDraw.Draw(base)

    margin_x, margin_top, photo_h = 60, 60, 980
    if property_photo:
        photo = _fit(property_photo, W - margin_x * 2, photo_h)
        base.paste(photo, (margin_x, margin_top))
        draw.rectangle((margin_x, margin_top, W - margin_x, margin_top + photo_h), outline=(226, 220, 204, 255), width=2)

    x = margin_x
    y = margin_top + photo_h + 44
    draw.text((x, y), f"Now Available in {district}", font=EYEBROW_FONT, fill=(184, 145, 46, 255))
    y += 46
    draw.text((x, y), _mixed(property_type), font=GALLERY_TITLE_FONT, fill=INK)
    y += 66
    draw.text((x, y), price_text, font=GALLERY_PRICE_FONT, fill=INK)
    y += 48
    draw.text((x, y), _stats_line(stats), font=GALLERY_STATS_FONT, fill=MUTED)

    y += 66
    _agent_strip(base, draw, margin_x, y, W - margin_x * 2, 130, agent_name, agent_contact_line, agent_photo,
                 translucent=False, align="left", name_color=INK, contact_color=(184, 145, 46, 255))

    return base.convert("RGB")


# ================================
# 6. GOLD FRAME
# ================================
def _render_gold_frame(property_type, district, price_text, stats, agent_name, agent_contact_line,
                        property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))
    draw = ImageDraw.Draw(base)

    _vertical_gradient_scrim(base, 0, 200, 130, 0)
    _vertical_gradient_scrim(base, H - 420, H, 0, 200)

    x = 70
    draw.text((x, 66), property_type.upper(), font=EYEBROW_FONT_SM, fill=WHITE)
    dw = _text_width(draw, property_type.upper(), EYEBROW_FONT_SM)
    draw.text((x + dw + 26, 66), district, font=EYEBROW_FONT_SM, fill=(220, 216, 206, 255))

    # Single left-aligned column, stacked -- keeps price/stats from ever colliding with
    # each other regardless of how long the real listing's text is.
    y = H - 330
    draw.text((x, y), price_text, font=PRICE_FONT, fill=GOLD)
    y += 84
    draw.text((x, y), _stats_line(stats), font=STATS_FONT, fill=WHITE)

    y += 60
    _agent_strip(base, draw, 70, y, W - 140, 100, agent_name, agent_contact_line, agent_photo,
                 translucent=True, align="left", edge_pad=18, name_color=PALE)

    _double_gold_frame(base, margin=36, gap=8)

    return base.convert("RGB")


# ================================
# 7. TOP BANNER MINIMAL
# ================================
def _render_top_banner_minimal(property_type, district, price_text, stats, agent_name, agent_contact_line,
                                property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))
    draw = ImageDraw.Draw(base)

    _solid_band(base, (0, 0, W, 78), (18, 26, 34), 150)
    cx = W // 2
    banner_text = f"{district.upper()}   ·   {price_text}" if district else price_text
    _centered_text(base, draw, cx, 28, banner_text, EYEBROW_FONT_SM, WHITE, shadow=False)

    _vertical_gradient_scrim(base, H - 460, H, 0, 210)

    x = 56
    y = H - 380
    draw_gold = _load_font(PLAYFAIR, 74, weight=700)
    _text_with_shadow(base, draw, (x, y), _mixed(property_type), draw_gold, GOLD, blur=6)
    y += 96
    _text_with_shadow(base, draw, (x, y), _stats_line(stats), STATS_FONT, WHITE, blur=5)

    y += 84
    _agent_strip(base, draw, 0, y, W, 130, agent_name, agent_contact_line, agent_photo,
                 translucent=True, align="left")

    return base.convert("RGB")


# ================================
# 8. NUMERAL FOCUS
# ================================
def _render_numeral_focus(property_type, district, price_text, stats, agent_name, agent_contact_line,
                           property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))
    draw = ImageDraw.Draw(base)

    _solid_band(base, (0, 0, W, 70), (10, 10, 8), 140)
    cx = W // 2
    top_text = f"{property_type.upper()}   ·   {district.upper()}" if district else property_type.upper()
    _centered_text(base, draw, cx, 24, top_text, EYEBROW_FONT_SM, WHITE, shadow=False)

    _vertical_gradient_scrim(base, H - 500, H, 0, 215)

    y = H - 420
    _centered_text(base, draw, cx, y, price_text, NUMERAL_PRICE_FONT, GOLD, blur=8)
    y += 118
    _centered_text(base, draw, cx, y, _stats_line(stats), STATS_FONT, WHITE, blur=5)

    y += 84
    _agent_strip(base, draw, 0, y, W, 130, agent_name, agent_contact_line, agent_photo,
                 translucent=True, align="center")

    return base.convert("RGB")


# ================================
# 9. CORNER BADGE
# ================================
def _render_corner_badge(property_type, district, price_text, stats, agent_name, agent_contact_line,
                          property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))
    draw = ImageDraw.Draw(base)

    if district:
        pad_x, pad_y = 24, 14
        tw = _text_width(draw, district.upper(), BADGE_FONT)
        badge_w, badge_h = tw + pad_x * 2, 30 + pad_y
        bx1, by1 = W - 50, 50
        bx0, by0 = bx1 - badge_w, by1
        _solid_band(base, (bx0, by0, bx1, by0 + badge_h), (10, 10, 8), 190)
        draw.text((bx0 + pad_x, by0 + pad_y - 4), district.upper(), font=BADGE_FONT, fill=GOLD)

    # Stacked rows rather than a strict left/right grid -- the stats line's real width
    # varies a lot with listing data, so row 1 (title + price) is the only row that
    # shares left/right space; stats and the agent strip each get their own full row.
    bar_h = 320
    bar_top = H - bar_h
    _solid_band(base, (0, bar_top, W, H), (10, 10, 8), 175)

    x = 56
    y = bar_top + 36
    draw.text((x, y), _mixed(property_type), font=DISTRICT_FONT, fill=WHITE)
    _right_text(base, draw, W - 56, y + 6, price_text, BOLD_PRICE_FONT, GOLD, shadow=False)

    y += 76
    draw.text((x, y), _stats_line(stats), font=STATS_FONT, fill=(214, 210, 198, 255))

    y += 60
    _agent_strip(base, draw, 0, y, W, 110, agent_name, agent_contact_line, agent_photo,
                 translucent=False, align="right", edge_pad=56)

    return base.convert("RGB")


# ================================
# 10. ASYMMETRIC COLUMN
# ================================
def _render_asymmetric_column(property_type, district, price_text, stats, agent_name, agent_contact_line,
                               property_photo, agent_photo):
    base = Image.new("RGBA", (W, H), CREAM_BG)
    draw = ImageDraw.Draw(base)

    photo_w = int(W * 0.68)
    col_x = photo_w
    col_w = W - photo_w

    if property_photo:
        photo = _fit(property_photo, photo_w, H)
        base.paste(photo, (0, 0))

    pad = 46
    tx = col_x + pad
    max_w = col_w - pad * 2
    y = 90

    draw.text((tx, y), district.upper(), font=EYEBROW_FONT_SM, fill=(184, 145, 46, 255))
    y += 44

    lines = _wrap_text(draw, _mixed(property_type), COLUMN_TITLE_FONT, max_w)
    for line in lines[:3]:
        draw.text((tx, y), line, font=COLUMN_TITLE_FONT, fill=INK)
        y += 62
    y += 10

    draw.text((tx, y), price_text, font=COLUMN_PRICE_FONT, fill=INK)
    y += 70

    for label, value in _stat_pairs(stats):
        draw.text((tx, y), label, font=LABEL_FONT, fill=MUTED)
        y += 28
        for line in _wrap_text(draw, value, VALUE_FONT, max_w):
            draw.text((tx, y), line, font=VALUE_FONT, fill=INK)
            y += 40
        y += 18

    # Column is too narrow for photo-beside-text, so the photo sits above the name/contact
    # instead of beside it -- avoids needing ~250px of horizontal room this column doesn't have.
    block_top = H - 190
    draw.line((tx, block_top - 20, tx + max_w, block_top - 20), fill=(226, 220, 204, 255), width=1)

    photo_size = 70
    if agent_photo:
        _square_photo(base, agent_photo, tx + photo_size / 2, block_top + photo_size / 2, photo_size)
        name_y = block_top + photo_size + 14
    else:
        name_y = block_top

    draw.text((tx, name_y), agent_name.upper(), font=EYEBROW_FONT_SM, fill=INK)
    if agent_contact_line:
        draw.text((tx, name_y + 30), agent_contact_line, font=EYEBROW_FONT_SM, fill=(184, 145, 46, 255))

    return base.convert("RGB")


# ==========================================================================
# BOLD -- the new "clean and bright" family (four candidate layouts)
# ==========================================================================
# Light ground, one saturated accent, big confident type.
#
# The rule that holds the whole family together: every piece of TEXT sits on an
# OPAQUE ground, never on the property photo. That is not a style choice, it is the
# robustness choice. These posters are generated for whatever photo the agent happened
# to upload -- backlit, dark, blown out, busy -- and text floated over an unknown photo
# is exactly where generated posters start looking cheap. The photo gets its own clean
# rectangle; the words get paper.
#
# Every type block is fitted to a BOX (width x height) rather than given a fixed point
# size, so a 12-character headline and a 60-character one both land inside the same
# space and nothing below them ever moves.

PAPER = (250, 249, 246, 255)
BOLD_INK = (22, 23, 27, 255)
BOLD_MUTED = (124, 126, 134, 255)
BOLD_HAIRLINE = (222, 220, 212, 255)

# Deliberately not red -- that is the reference poster's signature and it stays his.
# Deliberately not GOLD or forest green either: those are NestList's colours, and a
# poster belongs to the agent and the property, not to the app.
BOLD_ACCENTS = {
    "cobalt": (21, 74, 214, 255),
    "teal": (0, 126, 122, 255),
    "violet": (93, 48, 196, 255),
    "magenta": (170, 26, 110, 255),
}

# Kept clear at the foot of every Bold layout so that when _add_compliance_band()
# appears (condo only) its 46px strip reads as a margin rather than crowding the design.
BOLD_BOTTOM_SAFE = 56


def _bold_color(value, default):
    """Accepts an accent by name ("cobalt") or as a raw RGB/RGBA tuple."""
    if value is None:
        return default
    if isinstance(value, str):
        return BOLD_ACCENTS.get(value.strip().lower(), default)
    if isinstance(value, (tuple, list)) and len(value) >= 3:
        c = tuple(int(v) for v in value[:4])
        return c if len(c) == 4 else (c[0], c[1], c[2], 255)
    return default


def _tint(color, amount):
    """A pale wash of `color` over paper -- the no-photo fallback ground."""
    pr, pg, pb = PAPER[:3]
    r, g, b = color[:3]
    return (round(pr + (r - pr) * amount), round(pg + (g - pg) * amount),
            round(pb + (b - pb) * amount), 255)


def _cap_metrics(draw, font):
    """(top inset, cap height) for this font -- lets a block be positioned by the top of
    its capitals instead of the font's invisible ascender box, which is what makes tight
    display leading look deliberate rather than accidental."""
    box = draw.textbbox((0, 0), "HOMEX", font=font)
    return box[1], max(box[3] - box[1], 1)


def _tracked_width(draw, text, font, tracking=0.0):
    if not text:
        return 0
    return draw.textlength(text, font=font) + tracking * max(len(text) - 1, 0)


def _draw_tracked(draw, xy, text, font, fill, tracking=0.0):
    """Draws `text` with letter-spacing Pillow has no setting for. Each glyph is placed
    at the measured width of everything before it, so the font's own kerning pairs still
    apply -- drawing character by character with a fixed advance would throw them away."""
    if not text:
        return
    x, y = xy
    if not tracking:
        draw.text((x, y), text, font=font, fill=fill)
        return
    for i, ch in enumerate(text):
        draw.text((x + draw.textlength(text[:i], font=font) + tracking * i, y), ch, font=font, fill=fill)


def _wrap_tracked(draw, text, font, max_width, tracking=0.0):
    """Wraps to `max_width`, honouring any explicit newlines. Three levels of fallback so
    nothing can ever run off the edge: spaces, then hyphens (SEMI-DETACHED), then bare
    characters -- the last one catches a single unbroken token longer than the column,
    which is what an unusually long street or project name actually is."""
    out = []
    for para in str(text).split("\n"):
        words = []
        for word in para.split():
            if _tracked_width(draw, word, font, tracking) <= max_width:
                words.append(word)
            elif "-" in word:
                parts = word.split("-")
                for i, part in enumerate(parts):
                    words.append(part + "-" if i < len(parts) - 1 else part)
            else:
                chunk = ""
                for ch in word:
                    if _tracked_width(draw, chunk + ch, font, tracking) <= max_width or not chunk:
                        chunk += ch
                    else:
                        words.append(chunk)
                        chunk = ch
                if chunk:
                    words.append(chunk)

        cur = ""
        for w in words:
            sep = "" if cur.endswith("-") else " "
            trial = (cur + sep + w) if cur else w
            if _tracked_width(draw, trial, font, tracking) <= max_width or not cur:
                cur = trial
            else:
                out.append(cur)
                cur = w
        if cur:
            out.append(cur)
    return out or [""]


def _bold_fit_lines(draw, text, max_width, max_height, size_hi, size_lo,
                    weight=900, opsz=32, track_ratio=-0.022, line_gap=1.16, font_path=INTER):
    """Largest size at which `text` fits inside a max_width x max_height box.

    Returns (font, lines, size, tracking, total_height). Because the caller gives a box
    rather than a line count, a long headline simply sets smaller over more lines instead
    of shoving the facts row off the poster. If even size_lo overflows the box the text is
    cut to the lines that fit and the last one gets an ellipsis -- a visible, honest
    truncation beats silently drawing over the row below."""
    text = (text or "").strip()
    if not text:
        return None, [], 0, 0.0, 0
    size = int(size_hi)
    while size >= size_lo:
        font = _load_font(font_path, size, weight=weight, opsz=opsz)
        tracking = track_ratio * size
        lines = _wrap_tracked(draw, text, font, max_width, tracking)
        _, cap_h = _cap_metrics(draw, font)
        step = round(cap_h * line_gap)
        total = (len(lines) - 1) * step + cap_h
        widest = max(_tracked_width(draw, l, font, tracking) for l in lines)
        if total <= max_height and widest <= max_width:
            return font, lines, size, tracking, total
        size -= 2

    font = _load_font(font_path, int(size_lo), weight=weight, opsz=opsz)
    tracking = track_ratio * size_lo
    lines = _wrap_tracked(draw, text, font, max_width, tracking)
    _, cap_h = _cap_metrics(draw, font)
    step = round(cap_h * line_gap)
    keep = max(1, int((max_height - cap_h) // step) + 1)
    if len(lines) > keep:
        lines = lines[:keep]
        lines[-1] = lines[-1].rstrip(" ,.;:-") + "…"
    return font, lines, int(size_lo), tracking, (len(lines) - 1) * step + cap_h


def _bold_block(base, draw, x, y, width, lines, font, fill, tracking, line_gap=1.16, align="left"):
    """Draws fitted lines with `y` meaning the top of the capitals. Returns the y of the
    bottom of the last line, so layouts stack by measurement, never by guessed constants."""
    if not lines or font is None:
        return y
    top_off, cap_h = _cap_metrics(draw, font)
    step = round(cap_h * line_gap)
    for i, line in enumerate(lines):
        lw = _tracked_width(draw, line, font, tracking)
        if align == "right":
            lx = x + width - lw
        elif align == "center":
            lx = x + (width - lw) / 2
        else:
            lx = x
        _draw_tracked(draw, (lx, y + i * step - top_off), line, font, fill, tracking)
    return y + (len(lines) - 1) * step + cap_h


# ---- geometric line icons -------------------------------------------------
# There is no icon set in this project and buying one would drag a licence behind every
# poster an agent publishes. These are drawn from primitives instead: nothing to licence,
# nothing to attribute, and a flat geometric line suits "clean" better than a filled set.
# Each takes a square box at (x, y) of side s and draws inside it.

def _ico_house(d, x, y, s, c, w):
    d.line([(x + s * .06, y + s * .46), (x + s * .5, y + s * .1), (x + s * .94, y + s * .46)], fill=c, width=w, joint="curve")
    d.rectangle((x + s * .17, y + s * .44, x + s * .83, y + s * .9), outline=c, width=w)


def _ico_plot(d, x, y, s, c, w):
    d.rectangle((x + s * .06, y + s * .14, x + s * .94, y + s * .86), outline=c, width=w)
    d.line((x + s * .28, y + s * .68, x + s * .72, y + s * .32), fill=c, width=w)
    d.line([(x + s * .72, y + s * .32), (x + s * .72, y + s * .48)], fill=c, width=w)
    d.line([(x + s * .72, y + s * .32), (x + s * .56, y + s * .32)], fill=c, width=w)
    d.line([(x + s * .28, y + s * .68), (x + s * .28, y + s * .52)], fill=c, width=w)
    d.line([(x + s * .28, y + s * .68), (x + s * .44, y + s * .68)], fill=c, width=w)


def _ico_bed(d, x, y, s, c, w):
    d.line((x + s * .05, y + s * .32, x + s * .05, y + s * .86), fill=c, width=w)
    d.line((x + s * .95, y + s * .56, x + s * .95, y + s * .86), fill=c, width=w)
    d.line((x + s * .05, y + s * .56, x + s * .95, y + s * .56), fill=c, width=w)
    d.line((x + s * .05, y + s * .78, x + s * .95, y + s * .78), fill=c, width=w)
    d.rounded_rectangle((x + s * .16, y + s * .38, x + s * .5, y + s * .56), radius=s * .06, outline=c, width=w)


def _ico_train(d, x, y, s, c, w):
    d.rounded_rectangle((x + s * .18, y + s * .1, x + s * .82, y + s * .74), radius=s * .16, outline=c, width=w)
    d.line((x + s * .18, y + s * .44, x + s * .82, y + s * .44), fill=c, width=w)
    d.line((x + s * .3, y + s * .86, x + s * .16, y + s * .96), fill=c, width=w)
    d.line((x + s * .7, y + s * .86, x + s * .84, y + s * .96), fill=c, width=w)
    d.ellipse((x + s * .3, y + s * .54, x + s * .4, y + s * .64), fill=c)
    d.ellipse((x + s * .6, y + s * .54, x + s * .7, y + s * .64), fill=c)


def _ico_cap(d, x, y, s, c, w):
    d.polygon([(x + s * .5, y + s * .16), (x + s * .96, y + s * .4), (x + s * .5, y + s * .64), (x + s * .04, y + s * .4)], outline=c, width=w)
    d.line([(x + s * .22, y + s * .49), (x + s * .22, y + s * .76), (x + s * .5, y + s * .88), (x + s * .78, y + s * .76), (x + s * .78, y + s * .49)], fill=c, width=w, joint="curve")


def _ico_key(d, x, y, s, c, w):
    d.ellipse((x + s * .06, y + s * .3, x + s * .46, y + s * .7), outline=c, width=w)
    d.line((x + s * .44, y + s * .5, x + s * .96, y + s * .5), fill=c, width=w)
    d.line((x + s * .78, y + s * .5, x + s * .78, y + s * .72), fill=c, width=w)
    d.line((x + s * .92, y + s * .5, x + s * .92, y + s * .68), fill=c, width=w)


def _ico_car(d, x, y, s, c, w):
    d.line([(x + s * .12, y + s * .56), (x + s * .26, y + s * .28), (x + s * .74, y + s * .28), (x + s * .88, y + s * .56)], fill=c, width=w, joint="curve")
    d.rounded_rectangle((x + s * .06, y + s * .54, x + s * .94, y + s * .78), radius=s * .1, outline=c, width=w)
    d.ellipse((x + s * .18, y + s * .72, x + s * .36, y + s * .9), outline=c, width=w)
    d.ellipse((x + s * .64, y + s * .72, x + s * .82, y + s * .9), outline=c, width=w)


def _ico_layers(d, x, y, s, c, w):
    for i, top in enumerate((.18, .44, .7)):
        d.rounded_rectangle((x + s * (.08 + i * .02), y + s * top, x + s * (.92 - i * .02), y + s * (top + .14)),
                            radius=s * .04, outline=c, width=w)


def _ico_width(d, x, y, s, c, w):
    d.line((x + s * .06, y + s * .5, x + s * .94, y + s * .5), fill=c, width=w)
    d.line((x + s * .06, y + s * .26, x + s * .06, y + s * .74), fill=c, width=w)
    d.line((x + s * .94, y + s * .26, x + s * .94, y + s * .74), fill=c, width=w)
    d.line([(x + s * .2, y + s * .38), (x + s * .06, y + s * .5), (x + s * .2, y + s * .62)], fill=c, width=w, joint="curve")
    d.line([(x + s * .8, y + s * .38), (x + s * .94, y + s * .5), (x + s * .8, y + s * .62)], fill=c, width=w, joint="curve")


def _ico_tag(d, x, y, s, c, w):
    d.polygon([(x + s * .5, y + s * .08), (x + s * .92, y + s * .5), (x + s * .5, y + s * .92), (x + s * .08, y + s * .5)], outline=c, width=w)
    d.ellipse((x + s * .42, y + s * .3, x + s * .58, y + s * .46), outline=c, width=w)


def _ico_pin(d, x, y, s, c, w):
    d.ellipse((x + s * .22, y + s * .1, x + s * .78, y + s * .66), outline=c, width=w)
    d.line([(x + s * .3, y + s * .56), (x + s * .5, y + s * .92), (x + s * .7, y + s * .56)], fill=c, width=w, joint="curve")
    d.ellipse((x + s * .42, y + s * .3, x + s * .58, y + s * .46), fill=c)


_BOLD_ICONS = {
    "house": _ico_house, "built-up": _ico_house,
    "plot": _ico_plot, "land": _ico_plot,
    "bed": _ico_bed, "bedrooms": _ico_bed,
    "train": _ico_train, "mrt": _ico_train,
    "cap": _ico_cap, "school": _ico_cap,
    "key": _ico_key, "tenure": _ico_key,
    "car": _ico_car, "parking": _ico_car,
    "layers": _ico_layers, "storeys": _ico_layers,
    "width": _ico_width, "frontage": _ico_width,
    "tag": _ico_tag, "psf": _ico_tag,
    "pin": _ico_pin, "location": _ico_pin,
}


def _bold_icon(draw, name, x, y, size, color):
    """Unknown icon names draw nothing rather than raising -- a typo in a caption should
    never be the reason an agent's poster fails to render."""
    fn = _BOLD_ICONS.get(str(name or "").strip().lower())
    if not fn:
        return
    try:
        fn(draw, x, y, size, color, max(2, round(size * 0.075)))
    except Exception:
        pass


# ---- facts ----------------------------------------------------------------

def _bold_facts(facts):
    """Normalises label/value/unit facts and drops anything with nothing to say.

    Accepts dicts or (icon, label, value, unit) tuples. A value that is blank, "0" or
    "0 sqft" is dropped: in this data a zero means "the agent never filled it in", and
    the poster must not claim the property has none. That is what keeps the unknown
    bathroom count off the Mugliston Road poster without any caller special-casing it."""
    out = []
    for f in facts or []:
        if isinstance(f, dict):
            icon = f.get("icon") or ""
            label = str(f.get("label") or "").strip()
            value = str(f.get("value") or "").strip()
            unit = str(f.get("unit") or "").strip()
        elif isinstance(f, (tuple, list)):
            seq = list(f) + ["", "", "", ""]
            icon, label, value, unit = seq[0] or "", str(seq[1] or "").strip(), str(seq[2] or "").strip(), str(seq[3] or "").strip()
        else:
            continue
        if not value:
            continue
        if value.split(" ")[0].replace(",", "").strip("-") in ("0", "0.0", ""):
            continue
        out.append({"icon": icon, "label": label.upper(), "value": value, "unit": unit})
    return out


_STAT_ICONS = {"ROOMS": "bed", "BATHS": "house", "BUILT-UP": "house", "PSF": "tag"}


def _bold_facts_from_stats(stats):
    """Fallback for callers still on the old flat `stats` list (every existing caller).
    Turns "5 Rooms" into value 5 / label ROOMS so Bold degrades to something sensible
    instead of rendering one long run-on line."""
    facts = []
    for label, value in _stat_pairs(stats or []):
        unit = ""
        if label == "BUILT-UP" and value.lower().endswith("sqft"):
            value, unit = value[:-4].strip(), "sqft"
        elif label == "PSF":
            unit, label = "psf", "PRICE"
            value = value.replace(" psf", "").strip()
        facts.append({"icon": _STAT_ICONS.get(label, ""), "label": label, "value": value, "unit": unit})
    return facts


def _bold_value_line(draw, value, unit, max_width, size_hi, size_lo=18):
    """One line of 'big number + small unit', shrunk together until the pair fits."""
    size = int(size_hi)
    while size > size_lo:
        vfont = _load_font(INTER, size, weight=800, opsz=20)
        ufont = _load_font(INTER, max(13, round(size * 0.5)), weight=600, opsz=12)
        vw = draw.textlength(value, font=vfont)
        uw = (draw.textlength(unit, font=ufont) + size * 0.16) if unit else 0
        if vw + uw <= max_width:
            return vfont, ufont, vw, uw, size
        size -= 2
    vfont = _load_font(INTER, size_lo, weight=800, opsz=20)
    ufont = _load_font(INTER, max(12, round(size_lo * 0.5)), weight=600, opsz=12)
    return vfont, ufont, draw.textlength(value, font=vfont), (draw.textlength(unit, font=ufont) + size_lo * 0.16) if unit else 0, size_lo


def _bold_fact_row(base, draw, x0, y0, width, facts, accent, icon_size=42, value_hi=46,
                   dividers=False, divider_h=120):
    """Facts as evenly-weighted centred columns: icon, value, label. Column width is
    derived from however many facts survived, so two facts and five facts both fill the
    row rather than leaving a hole or spilling."""
    if not facts:
        return y0
    n = len(facts)
    col_w = width / n
    bottom = y0
    for i, f in enumerate(facts):
        cx = x0 + col_w * (i + 0.5)
        inner = col_w - 22
        y = y0
        if f["icon"]:
            _bold_icon(draw, f["icon"], cx - icon_size / 2, y, icon_size, accent)
            y += icon_size + 16

        vfont, ufont, vw, uw, vsize = _bold_value_line(draw, f["value"], f["unit"], inner, value_hi)
        v_top, v_cap = _cap_metrics(draw, vfont)
        start = cx - (vw + uw) / 2
        draw.text((start, y - v_top), f["value"], font=vfont, fill=BOLD_INK)
        if f["unit"]:
            u_top, u_cap = _cap_metrics(draw, ufont)
            draw.text((start + vw + vsize * 0.16, y + v_cap - u_cap - u_top), f["unit"], font=ufont, fill=BOLD_MUTED)
        y += v_cap + 13

        if f["label"]:
            lfont = _load_font(INTER, 19, weight=600, opsz=12)
            llines = _wrap_tracked(draw, f["label"], lfont, inner, 1.1)[:2]
            y = _bold_block(base, draw, x0 + col_w * i + 11, y, inner, llines, lfont, BOLD_MUTED, 1.1,
                            line_gap=1.42, align="center")
        bottom = max(bottom, y)
        if dividers and i:
            draw.line((round(x0 + col_w * i), y0 - 6, round(x0 + col_w * i), y0 + divider_h), fill=BOLD_HAIRLINE, width=1)
    return bottom


def _bold_fact_grid(base, draw, x0, y0, width, facts, accent, cols=2, row_h=78, icon_size=40):
    """The same facts as horizontal cells (icon on the left) in a grid -- the quieter,
    airier arrangement, used by the inset-card layout."""
    if not facts:
        return y0
    col_w = width / cols
    bottom = y0
    for i, f in enumerate(facts):
        cx0 = x0 + col_w * (i % cols)
        cy0 = y0 + row_h * (i // cols)
        _bold_icon(draw, f["icon"], cx0, cy0 + (row_h - 28 - icon_size) / 2, icon_size, accent)
        tx = cx0 + icon_size + 20
        inner = col_w - (icon_size + 20) - 24

        vfont, ufont, vw, uw, vsize = _bold_value_line(draw, f["value"], f["unit"], inner, 34)
        v_top, v_cap = _cap_metrics(draw, vfont)
        draw.text((tx, cy0 - v_top), f["value"], font=vfont, fill=BOLD_INK)
        if f["unit"]:
            u_top, u_cap = _cap_metrics(draw, ufont)
            draw.text((tx + vw + vsize * 0.16, cy0 + v_cap - u_cap - u_top), f["unit"], font=ufont, fill=BOLD_MUTED)

        lfont = _load_font(INTER, 18, weight=600, opsz=12)
        llines = _wrap_tracked(draw, f["label"], lfont, inner, 1.1)[:1]
        bottom = max(bottom, _bold_block(base, draw, tx, cy0 + v_cap + 11, inner, llines, lfont, BOLD_MUTED, 1.1))
    return bottom


# ---- photo, chrome, agent strip -------------------------------------------

def _bold_photo(img, w, h, accent, centering=(0.5, 0.45)):
    """The photo treatment, built for what actually arrives rather than for a hero shot.

    Sources here are 640x480 and 800x600 -- a 1200-wide panel is a ~1.9x enlargement, so a
    light unsharp pass (scaled to how far it was stretched) puts the edges back instead of
    leaving a soft smear. A dark or flat photo is lifted toward the family's "bright", and
    a missing photo falls back to a pale accent wash so the poster still renders.
    """
    if img is None:
        return Image.new("RGB", (w, h), _tint(accent, 0.1)[:3])
    try:
        photo = ImageOps.fit(img.convert("RGB"), (w, h), method=Image.LANCZOS, centering=centering)
        lum = ImageStat.Stat(photo.convert("L")).mean[0]
        if lum < 120:
            photo = ImageEnhance.Brightness(photo).enhance(min(1.45, 120.0 / max(lum, 1.0)))
            photo = ImageEnhance.Contrast(photo).enhance(1.06)
        photo = ImageEnhance.Color(photo).enhance(1.05)
        stretch = w / max(img.width, 1)
        if stretch > 1.15:
            photo = photo.filter(ImageFilter.UnsharpMask(radius=1.6, percent=int(min(115, 45 * stretch)), threshold=3))
        return photo
    except Exception as e:
        print(f"bold photo treatment skipped: {e}")
        return Image.new("RGB", (w, h), _tint(accent, 0.1)[:3])


def _bold_eyebrow(base, draw, x, y, width, text, color, size=25, align="left"):
    if not text:
        return y
    font = _load_font(INTER, size, weight=700, opsz=13)
    lines = _wrap_tracked(draw, text.upper(), font, width, size * 0.09)[:1]
    return _bold_block(base, draw, x, y, width, lines, font, color, size * 0.09, align=align)


def _bold_pill(base, draw, x, y, text, accent, size=25, align="left", width=None):
    """District marker as a solid accent lozenge with the text knocked out in paper.
    Sizes itself to the text, so "D15" and "DISTRICT 15 (EAST COAST)" both work."""
    if not text:
        return 0
    font = _load_font(INTER, size, weight=700, opsz=13)
    track = size * 0.1
    tw = _tracked_width(draw, text.upper(), font, track)
    pad_x, h = round(size * 0.9), round(size * 1.95)
    pw = round(tw + pad_x * 2)
    px = x + (width - pw) if (align == "right" and width) else x
    draw.rounded_rectangle((px, y, px + pw, y + h), radius=h / 2, fill=accent)
    top_off, cap_h = _cap_metrics(draw, font)
    _draw_tracked(draw, (px + pad_x, y + (h - cap_h) / 2 - top_off), text.upper(), font, PAPER, track)
    return h


def _bold_agent_strip(base, draw, x0, y0, width, height, agent_name, agent_contact_line,
                      agent_photo, accent, align="left"):
    """The agent's signature, on paper. Name and contact are each fitted to the room left
    after the photo, so a long agency name or a two-number contact line shrinks instead of
    running into the edge of the poster."""
    photo_size = max(0, round(min(height - 16, height * 0.84)))
    cy = y0 + height / 2

    text_w = width - (photo_size + 22 if agent_photo else 0)
    name_font, name_lines, _, name_track, name_h = _bold_fit_lines(
        draw, (agent_name or "").upper(), text_w, height * 0.46, 34, 18,
        weight=800, opsz=16, track_ratio=0.01)
    c_font, c_lines, _, c_track, c_h = _bold_fit_lines(
        draw, agent_contact_line or "", text_w, height * 0.36, 29, 15,
        weight=600, opsz=14, track_ratio=0.0)

    gap = 10 if (name_lines and c_lines) else 0
    total_h = name_h + gap + c_h
    block_w = (photo_size + 22 if agent_photo else 0) + text_w
    bx = x0 + (width - block_w) if align == "right" else (x0 + (width - block_w) / 2 if align == "center" else x0)

    if agent_photo and photo_size > 0:
        _square_photo(base, agent_photo, bx + photo_size / 2, cy, photo_size, border_color=accent, border_width=3)
        tx = bx + photo_size + 22
    else:
        tx = bx

    y = cy - total_h / 2
    y = _bold_block(base, draw, tx, y, text_w, name_lines, name_font, BOLD_INK, name_track)
    if c_lines:
        _bold_block(base, draw, tx, y + gap + 4, text_w, c_lines, c_font, accent, c_track)


def _bold_defaults(property_type, district, headline, subhead, facts, stats):
    """Headline, subhead and facts are INPUTS -- written copy belongs upstream. These
    fallbacks only cover a caller that supplies nothing, so the template never renders
    empty while the copy side catches up."""
    head = (headline or "").strip()
    if not head:
        head = " ".join(p for p in [(property_type or "").strip(), ("IN " + district.strip()) if district else ""] if p)
    sub = (subhead or "").strip()
    normalised = _bold_facts(facts) if facts else _bold_facts(_bold_facts_from_stats(stats))
    return head.upper(), sub, normalised


# ---- the four Bold layouts ------------------------------------------------

def _render_bold_base_row(property_type, district, price_text, stats, agent_name, agent_contact_line,
                          property_photo, agent_photo, headline=None, subhead=None, facts=None,
                          accent=None):
    """A -- photo on top, everything else on paper, facts as a measured row along the base.
    The price is the loudest thing on the poster after the headline."""
    acc = _bold_color(accent, BOLD_ACCENTS["cobalt"])
    head, sub, fl = _bold_defaults(property_type, district, headline, subhead, facts, stats)

    base = Image.new("RGBA", (W, H), PAPER)
    draw = ImageDraw.Draw(base)
    photo_h = 680
    base.paste(_bold_photo(property_photo, W, photo_h, acc), (0, 0))

    pad = 64
    inner = W - pad * 2

    # The facts row and the agent strip are placed from the BOTTOM edge up and own that
    # ground outright; the headline block flows down from the photo into what is left.
    # Anchoring both ends is what stops a long headline from walking the facts row into
    # the agent's name -- the two can no longer share vertical space at any input length.
    strip_h = 84
    strip_y = H - BOLD_BOTTOM_SAFE - strip_h
    row_h = 142
    row_y = strip_y - 34 - row_h
    rule_y = row_y - 32

    y = photo_h + 44
    pill_h = _bold_pill(base, draw, pad, y, district, acc)
    _bold_eyebrow(base, draw, pad, y + pill_h / 2 - 11, inner, property_type, BOLD_MUTED, 24, align="right")
    y += pill_h + 30

    f, lines, _, tr, _h = _bold_fit_lines(draw, head, inner, 140, 140, 36)
    y = _bold_block(base, draw, pad, y, inner, lines, f, BOLD_INK, tr) + 22

    if sub:
        f, lines, _, tr, _h = _bold_fit_lines(draw, sub, inner, 62, 29, 19, weight=500, opsz=18,
                                              track_ratio=0.0, line_gap=1.5)
        y = _bold_block(base, draw, pad, y, inner, lines, f, BOLD_MUTED, tr, line_gap=1.5) + 24

    f, lines, _, tr, _h = _bold_fit_lines(draw, price_text, inner, 78, 72, 32, track_ratio=-0.02)
    _bold_block(base, draw, pad, y, inner, lines, f, acc, tr)

    draw.line((pad, rule_y, W - pad, rule_y), fill=BOLD_HAIRLINE, width=1)
    _bold_fact_row(base, draw, pad, row_y, inner, fl[:4], acc)
    _bold_agent_strip(base, draw, pad, strip_y, inner, strip_h, agent_name, agent_contact_line, agent_photo, acc)
    return base.convert("RGB")


def _render_bold_inset_card(property_type, district, price_text, stats, agent_name, agent_contact_line,
                            property_photo, agent_photo, headline=None, subhead=None, facts=None,
                            accent=None):
    """B -- the photo as an inset card with paper all the way round, a heavier accent rule
    under it, smaller type and facts in a 2x2 grid. The calmest of the four."""
    acc = _bold_color(accent, BOLD_ACCENTS["teal"])
    head, sub, fl = _bold_defaults(property_type, district, headline, subhead, facts, stats)

    base = Image.new("RGBA", (W, H), PAPER)
    draw = ImageDraw.Draw(base)

    pad = 70
    inner = W - pad * 2
    photo_h = 740
    base.paste(_bold_photo(property_photo, inner, photo_h, acc), (pad, pad))
    draw.rectangle((pad, pad + photo_h, W - pad, pad + photo_h + 10), fill=acc)

    y = pad + photo_h + 54
    label = "  ·  ".join(p for p in [(district or "").strip(), (property_type or "").strip()] if p)
    _bold_eyebrow(base, draw, pad, y, inner * 0.6, label, acc, 24)

    pf, plines, _, ptr, _h = _bold_fit_lines(draw, price_text, inner * 0.38, 46, 42, 22, weight=800, opsz=20,
                                             track_ratio=-0.012)
    _bold_block(base, draw, pad + inner * 0.62, y - 6, inner * 0.38, plines, pf, BOLD_INK, ptr, align="right")
    y += 54

    f, lines, _, tr, _h = _bold_fit_lines(draw, head, inner, 148, 118, 34)
    y = _bold_block(base, draw, pad, y, inner, lines, f, BOLD_INK, tr) + 22

    if sub:
        f, lines, _, tr, _h = _bold_fit_lines(draw, sub, inner, 62, 28, 19, weight=500, opsz=18,
                                              track_ratio=0.0, line_gap=1.5)
        y = _bold_block(base, draw, pad, y, inner, lines, f, BOLD_MUTED, tr, line_gap=1.5) + 20

    strip_h = 80
    strip_y = H - BOLD_BOTTOM_SAFE - strip_h
    draw.line((pad, strip_y - 26, W - pad, strip_y - 26), fill=BOLD_HAIRLINE, width=1)
    grid_h = 78 * ((len(fl[:4]) + 1) // 2)
    grid_y = max(y + 18, strip_y - 26 - 24 - grid_h)
    _bold_fact_grid(base, draw, pad, grid_y, inner, fl[:4], acc)
    _bold_agent_strip(base, draw, pad, strip_y, inner, strip_h, agent_name, agent_contact_line, agent_photo, acc)
    return base.convert("RGB")


def _render_bold_headline_first(property_type, district, price_text, stats, agent_name, agent_contact_line,
                                property_photo, agent_photo, headline=None, subhead=None, facts=None,
                                accent=None):
    """C -- words first. The headline sits at the top of the paper at the biggest type
    scale of the four and the photo bleeds off the bottom edge. The agent's signature is an
    opaque paper band laid across the foot of the photo, so it reads the same over any shot."""
    acc = _bold_color(accent, BOLD_ACCENTS["violet"])
    head, sub, fl = _bold_defaults(property_type, district, headline, subhead, facts, stats)

    base = Image.new("RGBA", (W, H), PAPER)
    draw = ImageDraw.Draw(base)

    pad = 64
    inner = W - pad * 2
    y = 92
    eyebrow = "  ·  ".join(p for p in [(district or "").strip(), (property_type or "").strip()] if p)
    y = _bold_eyebrow(base, draw, pad, y, inner, eyebrow, acc, 26) + 34

    f, lines, _, tr, _h = _bold_fit_lines(draw, head, inner, 240, 168, 42)
    y = _bold_block(base, draw, pad, y, inner, lines, f, BOLD_INK, tr) + 26

    if sub:
        f, lines, _, tr, _h = _bold_fit_lines(draw, sub, inner, 70, 30, 19, weight=500, opsz=18,
                                              track_ratio=0.0, line_gap=1.5)
        y = _bold_block(base, draw, pad, y, inner, lines, f, BOLD_MUTED, tr, line_gap=1.5) + 28

    f, lines, _, tr, _h = _bold_fit_lines(draw, price_text, inner, 76, 68, 32, track_ratio=-0.02)
    y = _bold_block(base, draw, pad, y, inner, lines, f, acc, tr) + 32

    draw.line((pad, y, W - pad, y), fill=BOLD_HAIRLINE, width=1)
    facts_bottom = _bold_fact_row(base, draw, pad, y + 30, inner, fl[:4], acc, icon_size=38, value_hi=42,
                                  dividers=True, divider_h=116)

    strip_h = 88
    band_top = H - BOLD_BOTTOM_SAFE - strip_h
    photo_top = min(int(facts_bottom) + 48, band_top - 240)
    base.paste(_bold_photo(property_photo, W, H - photo_top, acc, centering=(0.5, 0.4)), (0, photo_top))
    _solid_band(base, (0, band_top, W, H), PAPER[:3], 255)
    draw.line((0, band_top, W, band_top), fill=BOLD_HAIRLINE, width=1)
    _bold_agent_strip(base, draw, pad, band_top, inner, strip_h, agent_name, agent_contact_line, agent_photo, acc)
    return base.convert("RGB")


def _render_bold_accent_band(property_type, district, price_text, stats, agent_name, agent_contact_line,
                             property_photo, agent_photo, headline=None, subhead=None, facts=None,
                             accent=None):
    """D -- the loudest. A solid accent band between photo and paper carries the price and
    the district in knocked-out white, so the colour does the scroll-stopping and the type
    below it stays calm."""
    acc = _bold_color(accent, BOLD_ACCENTS["magenta"])
    head, sub, fl = _bold_defaults(property_type, district, headline, subhead, facts, stats)

    base = Image.new("RGBA", (W, H), PAPER)
    draw = ImageDraw.Draw(base)

    photo_h = 660
    base.paste(_bold_photo(property_photo, W, photo_h, acc), (0, 0))

    band_h = 142
    draw.rectangle((0, photo_h, W, photo_h + band_h), fill=acc)
    pad = 64
    inner = W - pad * 2

    dfont = _load_font(INTER, 27, weight=700, opsz=14)
    dtrack = 2.6
    dtext = (district or "").upper()
    dw = _tracked_width(draw, dtext, dfont, dtrack) if dtext else 0
    price_w = inner - (dw + 46 if dw else 0)

    pf, plines, _, ptr, ph = _bold_fit_lines(draw, price_text, price_w, 72, 66, 26, track_ratio=-0.018)
    _bold_block(base, draw, pad, photo_h + (band_h - ph) / 2, price_w, plines, pf, PAPER, ptr)
    if dtext:
        d_top, d_cap = _cap_metrics(draw, dfont)
        _draw_tracked(draw, (W - pad - dw, photo_h + (band_h - d_cap) / 2 - d_top), dtext, dfont, PAPER, dtrack)

    y = photo_h + band_h + 54
    _bold_eyebrow(base, draw, pad, y, inner, property_type, BOLD_MUTED, 24)
    y += 44

    f, lines, _, tr, _h = _bold_fit_lines(draw, head, inner, 150, 132, 36)
    y = _bold_block(base, draw, pad, y, inner, lines, f, BOLD_INK, tr) + 22

    if sub:
        f, lines, _, tr, _h = _bold_fit_lines(draw, sub, inner, 64, 29, 19, weight=500, opsz=18,
                                              track_ratio=0.0, line_gap=1.5)
        _bold_block(base, draw, pad, y, inner, lines, f, BOLD_MUTED, tr, line_gap=1.5)

    # Same bottom-anchored furniture as layout A. The accent band already separates photo
    # from paper, so the facts row needs no rule above it -- only the one over the strip.
    strip_h = 84
    strip_y = H - BOLD_BOTTOM_SAFE - strip_h
    row_h = 142
    row_y = strip_y - 34 - row_h
    draw.line((pad, strip_y - 28, W - pad, strip_y - 28), fill=BOLD_HAIRLINE, width=1)
    _bold_fact_row(base, draw, pad, row_y, inner, fl[:4], acc, icon_size=40, value_hi=44)
    _bold_agent_strip(base, draw, pad, strip_y, inner, strip_h, agent_name, agent_contact_line, agent_photo, acc)
    return base.convert("RGB")


# Templates that take the richer content inputs (headline / subhead / label-value facts /
# accent) on top of the shared arguments every template gets.
CONTENT_TEMPLATES = {
    "bold-base-row": _render_bold_base_row,
    "bold-inset-card": _render_bold_inset_card,
    "bold-headline-first": _render_bold_headline_first,
    "bold-accent-band": _render_bold_accent_band,
}


TEMPLATES = {
    "editorial": _render_editorial,
    "gallery-frame": _render_gallery_frame,
    "bold-type": _render_bold_type,
    "vignette-frame": _render_vignette_frame,
    "postcard": _render_postcard,
    "gold-frame": _render_gold_frame,
    "top-banner-minimal": _render_top_banner_minimal,
    "numeral-focus": _render_numeral_focus,
    "corner-badge": _render_corner_badge,
    "asymmetric-column": _render_asymmetric_column,
}
TEMPLATES.update(CONTENT_TEMPLATES)


COMPLIANCE_BAND_H = 46
COMPLIANCE_FONT = _load_font(INTER, 22, weight=600, opsz=11)


def _add_compliance_band(poster, text):
    """Fits the finished poster into a W x H frame with a slim band along the bottom that
    carries `text` (the CEA registration + agency licence line, NestList Home only).

    The template is scaled down a few percent and centred rather than overlaid, so no
    part of any template's layout is covered; the poster stays exactly W x H (4:5), the
    ratio Instagram accepts. The band takes the colour of the template's own bottom edge.
    Never raises: on any trouble the poster is returned without the band."""
    try:
        poster = poster.convert("RGB")
        edge = poster.getpixel((2, poster.height - 2))
        scale = (H - COMPLIANCE_BAND_H) / poster.height
        inner = poster.resize((max(1, round(poster.width * scale)), H - COMPLIANCE_BAND_H), Image.LANCZOS)
        framed = Image.new("RGB", (W, H), edge)
        framed.paste(inner, ((W - inner.width) // 2, 0))
        draw = ImageDraw.Draw(framed)
        luminance = 0.299 * edge[0] + 0.587 * edge[1] + 0.114 * edge[2]
        ink = (20, 20, 20) if luminance > 150 else (245, 241, 232)
        font = COMPLIANCE_FONT
        size = 22
        while draw.textlength(text, font=font) > W - 60 and size > 14:
            size -= 1
            font = _load_font(INTER, size, weight=600, opsz=11)
        tw = draw.textlength(text, font=font)
        draw.text(((W - tw) / 2, H - COMPLIANCE_BAND_H + (COMPLIANCE_BAND_H - size) / 2 - 2),
                  text, font=font, fill=ink)
        return framed
    except Exception as e:
        print(f"compliance band skipped (poster returned without it): {e}")
        return poster


# ===========================================================================
# POTENTIAL -- an AI makeover concept shown beside the real photograph
# ===========================================================================
#
# WHAT IT IS FOR. Some listings are killed by their own best photo: a tired house with a
# lorry across the driveway, a tarpaulin on the gate and a washing line in shot. The
# property is worth eleven million dollars and the picture says otherwise. Potential
# shows what the same house could look like restored -- generated FROM that photograph,
# so it is the same house from the same camera position -- with the real photograph
# printed beside it, labelled BEFORE, and a disclosure band saying plainly which is
# which.
#
# THE INVARIANT, WHICH IS THE WHOLE FEATURE AND NOT A POLISH ITEM.
# A generated building may never appear on a poster without BOTH the BEFORE inset and
# the disclosure band. Not by convention -- by construction. There is exactly one
# function in this file that composites generated pixels onto a canvas,
# _potential_concept_poster(), and it checks both before it paints and proves both on
# the finished pixels afterwards. Every failure inside it -- a missing real photo, an
# inset that will not draw, a band whose wording will not set legibly, a band that
# drew blank -- raises _MakeoverUnsafe, and _render_potential() answers that by throwing
# the generated image away and rendering an ordinary poster from the real photograph.
# A poster always comes out; a dishonest one never does.
#
# WHAT GOES IN: THE RAW PHOTO, NOT AN ANONYMISED FRAME. The Old-to-New video generates an
# anonymised exterior first (neighbours altered for privacy) and builds the rebuild from
# that. A poster does not, for two reasons. First, the BEFORE inset on this very poster
# IS the raw photograph, and the other ten templates already publish that same raw photo
# full-bleed today -- so anonymising the hero would hide nothing that the poster does not
# already show, while costing a second four cents. Second, hard rule 4: the neighbours
# must be left alone, and the prompt now says "exactly as photographed, not repainted and
# not tidied" rather than "indistinct", because "indistinct" invites the model to smear a
# neighbour's house on what is meant to be a hero image. Privacy-softening a neighbour
# remains a video-pipeline job, where the house is the only subject on screen.
#
# COST. One generated image, about US$0.04 (fal_images.COST_PER_IMAGE_USD), and it is
# CACHED against the listing's source photo -- so a second press of Generate, a different
# accent colour, a reworded headline or a typo fix all cost nothing. See makeover_image().

# The prompt. A NAMED CONSTANT because content-studio owns the wording and will keep
# tuning it; nothing about the layout depends on a single word of it. Written to hard
# rules 3, 4 and 5: nothing invented, neighbours left alone, the building kept honest.
# Screened through fal_images.screen_prompt() before it can be sent (see makeover_image),
# which is why no word in it is one of fal's refused words in any polarity.
POTENTIAL_MAKEOVER_PROMPT = (
    "Renovate the house in the centre of this photograph and show it freshly restored, "
    "from the same camera position: the same framing, the same angle, the same distance "
    "and the same lens. Keep the building's structure exactly as photographed -- the "
    "footprint, the number of storeys, the roofline and its pitch, and the position and "
    "size of every window, door, balcony and gate opening all stay where they are. "
    "Change only the surfaces and the grounds: repaint the walls in warm ivory and "
    "cream, re-tile the roof in dark slate, repaint the balcony balustrades white, swap "
    "the entrance gate for a clean modern gate in the same opening, plant manicured "
    "shrubs and flowering tropical plants along the front boundary, relay the driveway "
    "in neat pale paving, and carry off the parked vehicles, equipment, tarpaulins, bins "
    "and rubbish from the plot so the front is clear and tidy. Keep the sky, the daylight "
    "and the shadows exactly as photographed. Keep the neighbouring buildings, the road, "
    "the kerb and the trees exactly as photographed, not repainted and not tidied. Leave "
    "every gate pillar, wall and letterbox plain: no house number, no name plate, no sign "
    "and no lettering is added anywhere, and any lettering already in the photograph "
    "stays exactly as it is. No people, pets or vehicles appear. Photographic and "
    "realistic, like estate agency architectural photography."
)

# THE DISCLOSURE. Content-studio's wording, sentence case as written -- not forced to
# caps, not italic, not grey. Two lengths, and which one is used is DECIDED BY
# MEASUREMENT rather than by anyone remembering: _disclosure_spec() takes the longest
# wording that still sets on one line at or above DISCLOSURE_SIZE_LO.
POTENTIAL_DISCLOSURE = ("AI concept: what it could be, not how it stands today. "
                        "Real photo shown as BEFORE.")
POTENTIAL_DISCLOSURE_SHORT = ("Concept only: an AI-imagined makeover. Today's home is "
                              "the BEFORE photo.")

# The default copy. Deliberately says nothing about the picture's CONTENT: naming an
# "ivory facade" or a "slate roof" would describe a generated surface as a fact about
# the house. Both are overridable by the caller, like every other Bold-family input.
POTENTIAL_HEADLINE = "WHAT IT COULD BE"
# The second sentence ("The real photograph is inset.") is gone: "inset" is trade
# language, and the disclosure band already says the real photo is shown as BEFORE. One
# claim, in one place, in the band that is legally load-bearing.
POTENTIAL_SUBHEAD = "A concept of the potential."
POTENTIAL_BEFORE_LABEL = "BEFORE"

# A band, not small print. Taller than the CEA strip because it is a sentence rather
# than a registration number, and it carries the accent colour at full strength: this
# should read as confidence, not as an apology in the gutter.
DISCLOSURE_BAND_H = 70
DISCLOSURE_SIZE_HI = 30
# The floor. Below this a disclosure on a 1200px poster is small print, which is the
# thing this band exists not to be -- so it is a reason to refuse the makeover, not a
# reason to shrink the type further.
DISCLOSURE_SIZE_LO = 23

# The BEFORE inset has a minimum size as well as a minimum existence: "prominent, not a
# thumbnail" is a requirement, so it is measured. The number is the opaque area of the
# tilted card in pixels -- about 1/9th of the poster.
MIN_BEFORE_INSET_AREA = 150_000

# --- the cost ceiling ------------------------------------------------------
# ONE PRESS = ONE IMAGE = about US$0.04, and the cache means a RE-press is free. What
# bites is not the happy path, it is a bad day: an fal incident where every collection
# attempt 502s leaves fal paid and us empty-handed, which is why the tally below counts
# what fal CHARGED for (fal_images.billed_images) and not what arrived.
#
# TWENTY MAKEOVERS AN AGENT A CALENDAR MONTH:
#     20 x US$0.04   = US$0.80 an agent a month
#     50 agents      = US$40 a month, worst case, if every agent spends the lot
# Twenty distinct listings (or twenty re-picked source photos) a month is generous for
# the handful of tired properties this style is for, and it is deliberately an order
# below the Old-to-New image allowance (240 images, US$9.60) because a poster buys one
# picture and a video buys six.
#
# IT IS A SAFETY NET, NOT THE REAL CEILING, and the real ceiling is worth Jane knowing
# about: there is no company-wide cap anywhere in this codebase -- only per-agent ones.
# Per-agent caps across the three features come to roughly US$22 an agent a month, which
# fifty agents could not spend because the balance runs out first.
#
# AND THE BALANCE IS THE HARD WALL, because fal HAS NO SPEND-LIMIT SETTING. Checked in
# Jane's own account on 2026-11-12: the control does not exist, so earlier notes in this
# project recommending one were wrong. What protects her instead is the account being
# PREPAID with auto top-up OFF and NO CARD ATTACHED -- the balance cannot be exceeded and
# cannot silently refill. Running out degrades gracefully (fal refuses, every caller here
# falls back to the real photograph), so it fails safe. The thing to watch is therefore
# the BALANCE, not a setting: it is shared with the video features, and when it empties
# the Dramatic and Elegant films stop too.
MAX_POTENTIAL_MAKEOVERS_PER_AGENT_PER_MONTH = 20

# Bump to regenerate every listing's makeover deliberately -- a changed treatment, a
# model swap -- without touching the prompt text. Part of the cache key, so a bump is a
# cache miss rather than a migration.
POTENTIAL_PROMPT_VERSION = 1

# AND THE PROMPT'S OWN FINGERPRINT, so a reworded prompt is a cache miss WHETHER OR NOT
# anybody remembered to bump the number above. content-studio owns this wording and will
# keep tuning it; a hand-maintained version beside a string that somebody else edits is a
# promise waiting to be broken, and the failure is silent and wrong -- every listing would
# keep serving a concept made by the OLD prompt, including the one listing whose invented
# house number was the reason the prompt was reworded. Derived rather than asserted,
# because an assertion still needs someone to run it.
POTENTIAL_PROMPT_FINGERPRINT = hashlib.sha1(
    POTENTIAL_MAKEOVER_PROMPT.encode("utf-8")).hexdigest()[:8]

# The storage role, passed to main.py's store_image. Distinct from the Old-to-New roles
# so a poster makeover and a video's rebuilt exterior never land on the same path.
POTENTIAL_ROLE = "poster_potential"

# Column names for the per-agent month-to-date tally, declared HERE so main.py's wiring
# and the migration cannot disagree about them. main.py already has the generic
# compare-and-swap that writes a tally of this shape (_add_old_to_new_images takes the
# column names as parameters).
POTENTIAL_USAGE_MONTH_COLUMN = "potential_makeovers_month"
POTENTIAL_USAGE_COUNT_COLUMN = "potential_makeovers_used"

# Wall-clock ceiling on the generation, and it is SHORTER than fal_images' own 90s on
# purpose: this runs inside the agent's poster request, not inside a background render.
# A healthy nano-banana edit lands in 10-25s. Past 45 the agent gets their ordinary
# poster now rather than a spinner, which is the right trade for a press of a button.
POTENTIAL_BUDGET_SECONDS = 45


# --- white type over a photograph, made legible by measurement ------------
# "THE TEXT DREW" IS NOT THE SAME CLAIM AS "THE TEXT CAN BE READ", which is the
# distinction GUARD 4 already makes for the disclosure bands. The headline on this layout
# is knocked out of the hero in white, and the first version of it trusted a fixed
# gradient -- which put "DETACHED IN" straight onto the white gate pillar and the white
# garage door of the Mugliston photograph, where it was close to unreadable. So the scrim
# is now SOLVED from the picture rather than assumed.
#
# WHY THE BRIGHTEST LOCAL PATCH AND NOT AN AVERAGE. Illegibility is local. That
# photograph is mostly mid-tone with one bright white pillar through the middle of it: an
# average says "comfortable" while two words of the headline vanish into the pillar. So
# _bright_level() measures the worst small patch, not the picture as a whole.
#
# AND IT MATTERS MOST ON THE FALLBACK. fal starts an account at two concurrent requests,
# so on a busy day most presses render the real photograph through this same function.
# The fallback is the common case, not the edge case.
#
# 96 is the grey value that gives white type about 5.9:1 contrast (WCAG relative
# luminance) -- past the 4.5:1 normal-text bar with room to spare, and deep enough that
# the scrim reads as a deliberate panel rather than as a slightly dim photograph.
TYPE_SCRIM_TARGET = 96
# A floor, so even a dark photo gets a scrim that reads as deliberate rather than as
# nothing; and a ceiling, so a white building does not become a black wall.
TYPE_SCRIM_MIN = 0.34
TYPE_SCRIM_MAX = 0.90
# The gradient fades in over this many pixels ABOVE the type and then holds at full
# strength underneath it. Fading up into the picture is what makes it read as design.
TYPE_SCRIM_FADE = 200
# The size of the patch the brightness is measured over, in pixels. Roughly the area one
# letter of the headline covers, which is the unit at which a word becomes unreadable.
TYPE_SCRIM_CELL = 48


def _bright_level(image, box, cell=TYPE_SCRIM_CELL):
    """The brightest LOCAL patch inside `box`, as the mean of ~cell-sized tiles.

    LOCAL, AND THAT IS THE WHOLE POINT. The first version of this measured a 90th
    percentile over the whole box, which is a GLOBAL statistic: a bright region smaller
    than a tenth of the box sits ABOVE the percentile and escapes the constraint
    entirely. That is exactly the shape of the failure being fixed -- a white gate pillar
    covering a few per cent of a wide headline box, with two words disappearing into it
    while the box as a whole measures comfortable.

    Tiling and taking the worst tile asks the question that actually matters: is there
    anywhere behind this type where a letter could hide? Image.BOX resize gives each
    tile's mean in one pass."""
    x0, y0, x1, y1 = (int(round(v)) for v in box)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(image.width, x1), min(image.height, y1)
    if x1 <= x0 or y1 <= y0:
        return 0
    crop = image.crop((x0, y0, x1, y1)).convert("L")
    tiles = crop.resize((max(1, crop.width // cell), max(1, crop.height // cell)),
                        Image.BOX)
    return max(tiles.getdata())


def _legible_type_scrim(base, box, fade_top, hold_to):
    """Darkens the hero behind a block of white type until it is measurably legible.

    SOLVED, NOT GUESSED. Compositing the scrim's near-black at alpha a over a pixel of
    value v leaves v*(1-a) + 8a, so the alpha that brings the bright end of `box` down to
    TYPE_SCRIM_TARGET is (v - target) / (v - 8). The result is then RE-MEASURED on the
    actual pixels, and a corrective pass runs if rounding left it short -- the scrim is
    deepened rather than the poster shipped unreadable.

    Returns (alpha, brightness before, brightness after) so a caller or a test can assert
    on the finished image instead of taking the code's word for it."""
    before = _bright_level(base, box)
    need = (0.0 if before <= TYPE_SCRIM_TARGET
            else (before - TYPE_SCRIM_TARGET) / max(before - 8, 1))
    alpha = max(TYPE_SCRIM_MIN, min(TYPE_SCRIM_MAX, need))

    def lay(a):
        a = max(0, min(255, int(round(a * 255))))
        _vertical_gradient_scrim(base, max(0, int(round(fade_top))), int(round(box[1])), 0, a)
        _vertical_gradient_scrim(base, int(round(box[1])), int(round(hold_to)), a, a)

    lay(alpha)
    after = _bright_level(base, box)
    if after > TYPE_SCRIM_TARGET:
        extra = min(TYPE_SCRIM_MAX, (after - TYPE_SCRIM_TARGET) / max(after - 8, 1))
        lay(extra)
        after = _bright_level(base, box)
    return alpha, before, after


class _MakeoverUnsafe(Exception):
    """The makeover image cannot be shown honestly on this poster, so it is not shown.

    Raised by every guard in _potential_concept_poster(). Caught in exactly one place,
    _render_potential(), which answers it by rendering the real photograph instead."""


# --- the BEFORE inset ------------------------------------------------------

def _potential_before_inset(base, photo, accent, right_x, top_y, photo_w=470, photo_h=353,
                            border=16, tilt=-6.0):
    """Draws the REAL photograph as a tilted framed card with a solid BEFORE tab.

    Returns the opaque area of the card in pixels, or 0 if it could not be drawn. The
    area is the return value rather than a bool because the caller enforces a MINIMUM:
    an inset too small to read is not a disclosure of anything, and "prominent, not a
    thumbnail" has to be measurable to be kept."""
    if photo is None:
        return 0
    cw, ch = photo_w + border * 2, photo_h + border * 2
    card = Image.new("RGBA", (cw, ch), PAPER)
    card.paste(_bold_photo(photo, photo_w, photo_h, accent, centering=(0.5, 0.5)),
               (border, border))
    cd = ImageDraw.Draw(card)

    # The label, as a solid tab sitting on the photograph's lower-left corner. Drawn
    # INTO the card before the rotation, so the tab can never come adrift from the
    # picture it labels.
    font = _load_font(INTER, 31, weight=800, opsz=16)
    track = 3.4
    tw = _tracked_width(cd, POTENTIAL_BEFORE_LABEL, font, track)
    tab_w, tab_h = round(tw + 60), 56
    tx, ty = border, ch - border - tab_h
    cd.rectangle((tx, ty, tx + tab_w, ty + tab_h), fill=accent)
    top_off, cap_h = _cap_metrics(cd, font)
    _draw_tracked(cd, (tx + 30, ty + (tab_h - cap_h) / 2 - top_off),
                  POTENTIAL_BEFORE_LABEL, font, PAPER, track)
    cd.rectangle((0, 0, cw - 1, ch - 1), outline=accent, width=3)

    rotated = card.rotate(tilt, resample=Image.BICUBIC, expand=True)
    # A shadow, so a paper card over a photograph reads as laid on top of it rather than
    # cut into it. Same rotation, so it stays square to the card.
    shadow = Image.new("RGBA", (cw, ch), (0, 0, 0, 150)).rotate(
        tilt, resample=Image.BICUBIC, expand=True).filter(ImageFilter.GaussianBlur(14))
    x = round(right_x - rotated.width)
    base.alpha_composite(shadow, dest=(max(0, x + 7), max(0, round(top_y) + 12)))
    base.alpha_composite(rotated, dest=(max(0, x), max(0, round(top_y))))

    alpha = rotated.getchannel("A")
    return int(sum(alpha.point(lambda v: 255 if v > 200 else 0).convert("L").histogram()[255:]))


# --- the stacked disclosure bands ------------------------------------------
# Built the same way _add_compliance_band() is -- the poster is SCALED DOWN and the bands
# sit below it, so nothing in any layout is ever covered -- with two differences that
# matter.
#
#   1. IT STACKS, AND SCALES ONLY ONCE. A Home/condo Potential poster needs two bands:
#      the AI disclosure and the CEA registration + agency licence line. Calling the
#      existing function twice would scale the poster twice and squash the first band
#      inside the second one. So the total band height is worked out first and the
#      artwork is resized once, by (H - total) / H. Neither band can push the other off
#      the poster because neither is laid over anything: they are given their own room.
#   2. IT RAISES instead of returning the poster bandless. _add_compliance_band() is
#      right to swallow -- a landed poster with no CEA line is still a legitimate poster.
#      Here the band is the CONDITION for showing a generated building at all, so there
#      is nothing safe to return and the only correct answer is to fail up to
#      _render_potential() and print the real photograph.
#
# ORDER, top to bottom: the AI disclosure first, immediately under the artwork, because
# it is about the picture; the CEA line last, because it is about the agent.


def _disclosure_spec(texts, fill, ink, size_hi, size_lo, height, weight=600):
    """One band's drawing plan, choosing wording by measurement.

    `texts` is a list of wordings, longest (most informative) first. The first one that
    sets on ONE line at or above `size_lo` wins. Nothing fits -> _MakeoverUnsafe, which
    is why the AI band's floor is a legibility floor and not a styling preference."""
    probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    usable = W - 80
    for text in [t for t in texts if t and str(t).strip()]:
        text = str(text).strip()
        size = int(size_hi)
        while size >= size_lo:
            font = _load_font(INTER, size, weight=weight, opsz=11)
            if probe.textlength(text, font=font) <= usable:
                return {"text": text, "font": font, "size": size, "fill": fill,
                        "ink": ink, "height": height}
            size -= 1
    raise _MakeoverUnsafe("no disclosure wording fits the band at a legible size")


def _potential_band_specs(compliance_line):
    """The bands this poster must carry, in drawing order. Called BEFORE any generated
    pixel is painted, so a wording problem costs nothing but a fallback."""
    specs = [_disclosure_spec([POTENTIAL_DISCLOSURE, POTENTIAL_DISCLOSURE_SHORT],
                              fill=None, ink=PAPER, size_hi=DISCLOSURE_SIZE_HI,
                              size_lo=DISCLOSURE_SIZE_LO, height=DISCLOSURE_BAND_H)]
    cea = (compliance_line or "").strip()
    if cea:
        # The CEA line is a registration number, not a sentence: it may set smaller, down
        # to the same 14px the existing compliance band allows, so a long agency licence
        # line can never be the reason a Home poster loses its disclosure. But it must
        # still draw -- a condo poster that silently dropped its licence line would be a
        # licensing problem, so a failure here also falls back to the real photograph.
        specs.append(_disclosure_spec([cea], fill=BOLD_INK, ink=(236, 233, 226),
                                      size_hi=22, size_lo=14, height=COMPLIANCE_BAND_H))
    return specs


def _add_disclosure_bands(poster, specs, accent):
    """Scales the artwork once and stacks `specs` under it. Raises on any trouble."""
    poster = poster.convert("RGB")
    total = sum(int(s["height"]) for s in specs)
    if total <= 0 or total > H // 4:
        raise _MakeoverUnsafe(f"the disclosure bands do not fit ({total}px)")
    scale = (H - total) / poster.height
    inner = poster.resize((max(1, round(poster.width * scale)), H - total), Image.LANCZOS)
    edge = poster.getpixel((2, poster.height - 2))
    framed = Image.new("RGB", (W, H), edge)
    framed.paste(inner, ((W - inner.width) // 2, 0))

    draw = ImageDraw.Draw(framed)
    y = H - total
    for spec in specs:
        fill = (spec["fill"] or accent)[:3]
        h = int(spec["height"])
        draw.rectangle((0, y, W, y + h), fill=fill)
        top_off, cap_h = _cap_metrics(draw, spec["font"])
        tw = draw.textlength(spec["text"], font=spec["font"])
        draw.text(((W - tw) / 2, y + (h - cap_h) / 2 - top_off),
                  spec["text"], font=spec["font"], fill=spec["ink"][:3])
        spec["drawn_box"] = (y, y + h, fill)
        y += h
    return framed


def _bands_are_legible(image, specs):
    """Proof, on the finished pixels, that each band carries visible text.

    Not a formality. A band is drawn from a fitted font and a measured width, and any one
    of those could in principle come back empty on a font the deploy does not have; a
    solid colour strip with nothing written on it would look deliberate and disclose
    nothing. So each band's own rows are counted: enough pixels must differ from the
    band's fill colour to be words, and they must contrast with it."""
    for spec in specs:
        box = spec.get("drawn_box")
        if not box:
            return False
        y0, y1, fill = box
        band = image.crop((0, y0, W, y1)).convert("RGB")
        fr, fg, fb = fill
        ir, ig, ib = spec["ink"][:3]
        if abs(0.299 * fr + 0.587 * fg + 0.114 * fb
               - (0.299 * ir + 0.587 * ig + 0.114 * ib)) < 60:
            return False
        different = sum(count for value, count in
                        zip(range(256), band.convert("L").histogram())
                        if abs(value - (0.299 * fr + 0.587 * fg + 0.114 * fb)) > 40)
        if different < 600:
            return False
    return True


# --- the layout ------------------------------------------------------------

def _potential_artwork(property_type, district, price_text, agent_name, agent_contact_line,
                       hero_photo, before_photo, agent_photo, head, sub, facts, acc):
    """The Potential layout, at full W x H before the bands are added.

    Returns (image, before_inset_area). The inset area is 0 when `before_photo` is None,
    which is the fallback render -- the hero is then the real photograph and there is
    nothing to label.

    Loud on purpose. The hero is full-bleed to two thirds of the poster with the headline
    knocked out of it at the largest type scale anywhere in this file, the price sits in a
    full-width solid accent band, and only the facts and the agent's signature are on
    paper. The furniture at the foot is bottom-anchored exactly as the Bold layouts do it,
    so a long headline shrinks into its own box instead of walking the facts row into the
    agent's name."""
    base = Image.new("RGBA", (W, H), PAPER)
    draw = ImageDraw.Draw(base)
    pad = 64
    inner = W - pad * 2

    # Bottom-anchored furniture, as every Bold layout does it: the agent strip and the
    # facts row own the foot of the poster outright and the hero takes what is left, so
    # a long headline can never walk one into the other. A listing with NO facts at all
    # gives that room back to the hero rather than leaving a hole in the paper.
    strip_h = 92
    strip_y = H - BOLD_BOTTOM_SAFE - strip_h
    rule_y = strip_y - 30
    row_y = (rule_y - 26 - 138) if facts else None
    band_bottom = (row_y - 30) if facts else (rule_y - 26)
    band_h = 124
    band_top = band_bottom - band_h
    hero_h = band_top

    base.paste(_bold_photo(hero_photo, W, hero_h, acc, centering=(0.5, 0.5)), (0, 0))

    # The type block that sits ON the hero, measured first so the scrim is only as deep
    # as the words need and the house keeps as much clear sky as possible.
    head_f, head_lines, _hs, head_tr, head_h = _bold_fit_lines(draw, head, inner, 380, 190, 54)
    sub_f, sub_lines, _ss, sub_tr, sub_h = _bold_fit_lines(
        draw, sub, inner, 78, 32, 20, weight=500, opsz=18, track_ratio=0.0, line_gap=1.45)
    pill_h = round(27 * 1.95) if (district or "").strip() else 0
    total = (pill_h + (30 if pill_h else 0) + head_h
             + ((24 + sub_h) if sub_lines else 0))
    y = hero_h - 48 - total

    # The box the white type will actually occupy -- the headline and the subhead, not
    # the district pill, which is a solid accent lozenge and legible over anything. The
    # scrim is solved from the hero's brightness inside THIS box, before a word is drawn.
    type_top = y + (pill_h + 30 if pill_h else 0)
    type_bottom = type_top + head_h + ((24 + sub_h) if sub_lines else 0)
    _legible_type_scrim(base, (pad, type_top, pad + inner, type_bottom),
                        fade_top=type_top - TYPE_SCRIM_FADE, hold_to=hero_h)

    if pill_h:
        _bold_pill(base, draw, pad, y, district, acc, size=27)
        y += pill_h + 30
    y = _bold_block(base, draw, pad, y, inner, head_lines, head_f, PAPER, head_tr)
    if sub_lines:
        _bold_block(base, draw, pad, y + 24, inner, sub_lines, sub_f, (232, 230, 223, 255), sub_tr,
                    line_gap=1.45)

    # The BEFORE card, over the top corner of the hero. Last of the hero's layers so
    # nothing can be drawn across the label. Not called at all on the fallback render,
    # where there is no second picture to label -- so the fallback never executes a line
    # of inset code and cannot be brought down by one.
    area = (_potential_before_inset(base, before_photo, acc, right_x=W - 46, top_y=52)
            if before_photo is not None else 0)

    # Price in a full-width accent band, property type knocked out on the right.
    draw.rectangle((0, band_top, W, band_bottom), fill=acc)
    tfont = _load_font(INTER, 27, weight=700, opsz=14)
    ttext = (property_type or "").upper()
    ttrack = 2.6
    tw = _tracked_width(draw, ttext, tfont, ttrack) if ttext else 0
    pf, plines, _ps, ptr, ph = _bold_fit_lines(
        draw, price_text, inner - (tw + 48 if tw else 0), 84, 78, 28, track_ratio=-0.018)
    _bold_block(base, draw, pad, band_top + (band_h - ph) / 2, inner, plines, pf, PAPER, ptr)
    if ttext:
        t_top, t_cap = _cap_metrics(draw, tfont)
        _draw_tracked(draw, (W - pad - tw, band_top + (band_h - t_cap) / 2 - t_top),
                      ttext, tfont, PAPER, ttrack)

    draw.line((pad, rule_y, W - pad, rule_y), fill=BOLD_HAIRLINE, width=1)
    if facts:
        _bold_fact_row(base, draw, pad, row_y, inner, facts[:4], acc, icon_size=40, value_hi=44)
    _bold_agent_strip(base, draw, pad, strip_y, inner, strip_h, agent_name,
                      agent_contact_line, agent_photo, acc)
    return base.convert("RGB"), area


def _guarded_concept_poster(artwork, property_type, district, price_text, agent_name,
                            agent_contact_line, makeover_photo, property_photo,
                            agent_photo, head, sub, facts, acc, compliance_line):
    """THE ONLY FUNCTION IN THIS FILE THAT PUTS GENERATED PIXELS ON A POSTER.

    Every exit is either a poster carrying both the BEFORE inset and the disclosure band,
    or a _MakeoverUnsafe. There is no third outcome.

    GENERIC OVER THE LAYOUT, and that is deliberate rather than tidiness. `artwork` is a
    layout function returning (image, before_inset_area); the four guards below are NOT
    part of it and cannot be re-implemented by it. A second concept style registers its
    ARTWORK (see _register_concept_style) and therefore inherits these guards -- it
    cannot be given a generated image by any other route, because render_poster hands
    the makeover to the registry's entry point and to nothing else. Before this, the
    wiring turned on an id being present in a set of strings two hundred lines away, so
    adding one id would have handed a brand-new renderer a generated building with no
    guarantee it drew either the inset or the band."""
    # GUARD 1 -- before a single generated pixel is drawn. No real photograph means no
    # BEFORE inset is possible, so the makeover is not usable at all.
    if property_photo is None:
        raise _MakeoverUnsafe("there is no real photograph to show as BEFORE")

    # GUARD 2 -- also before drawing. The band's wording is chosen and proved to fit at a
    # legible size now, so a disclosure that cannot be set costs a fallback and not a
    # published poster.
    specs = _potential_band_specs(compliance_line)

    poster, area = artwork(
        property_type, district, price_text, agent_name, agent_contact_line,
        hero_photo=makeover_photo, before_photo=property_photo, agent_photo=agent_photo,
        head=head, sub=sub, facts=facts, acc=acc)

    # GUARD 3 -- the inset is actually on the canvas, and is prominent rather than a
    # thumbnail. Measured in pixels so it is a fact about the image, not about the code
    # that was meant to draw it.
    if area < MIN_BEFORE_INSET_AREA:
        raise _MakeoverUnsafe(f"the BEFORE inset did not draw at a readable size ({area}px)")

    framed = _add_disclosure_bands(poster, specs, acc)

    # GUARD 4 -- proof on the finished pixels that every band carries contrasting,
    # visible text. This is the last thing that happens before a generated building is
    # allowed to leave this function.
    if not _bands_are_legible(framed, specs):
        raise _MakeoverUnsafe("a disclosure band drew blank")
    return framed


def _render_potential_reporting(property_type, district, price_text, stats, agent_name,
                                agent_contact_line, property_photo, agent_photo,
                                headline=None, subhead=None, facts=None, accent=None,
                                makeover_photo=None, compliance_line=""):
    """Potential, returning (poster, concept_used).

    CONCEPT_USED IS MEASURED HERE AND NOWHERE ELSE, because here is the only place that
    knows. A caller cannot infer it from "we had a makeover image": the image may fail to
    download, or any of the four guards may fire, and the poster then correctly falls back
    to the real photograph. An API that answered "this is an AI concept" on the strength
    of having HAD a makeover would be making exactly the false claim the guards exist to
    prevent -- displaced out of the pixels and into the response. So the flag is set on
    the branch that actually returns the concept, and on no other.

    `makeover_photo` None (not configured, refused, timed out, over the allowance, failed
    to download) is the ORDINARY case, not an error: the poster renders from the real
    photograph with no AI claim on it anywhere, which is simply a loud poster.
    """
    acc = _bold_color(accent, BOLD_ACCENTS["cobalt"])
    _h, _s, fl = _bold_defaults(property_type, district, headline, subhead, facts, stats)
    cea = str(compliance_line or "").strip()

    if makeover_photo is not None:
        # The copy defaults are the CONCEPT's copy, and they are only ever applied on
        # this branch: "WHAT IT COULD BE" over a real photograph would promise a concept
        # that is not in the picture.
        head = (headline or "").strip().upper() or POTENTIAL_HEADLINE
        sub = (subhead or "").strip() or POTENTIAL_SUBHEAD
        try:
            return _guarded_concept_poster(
                _potential_artwork,
                property_type, district, price_text, agent_name, agent_contact_line,
                makeover_photo, property_photo, agent_photo, head, sub, fl, acc, cea), True
        except Exception as e:
            # Includes _MakeoverUnsafe and anything unforeseen. One log line and the real
            # photograph; the agent still gets a poster.
            print(f"potential: the makeover image was not used ({e}); "
                  "rendering the real photograph instead")

    # The fallback, and it is DEFENDED IN ITS TURN. This render is the safety net for the
    # whole feature, and a safety net that can throw is not one: if the Potential layout
    # itself fails on the real photograph, the agent gets a Bold poster rather than a 502
    # from main.py's generate-poster endpoint.
    try:
        poster, _area = _potential_artwork(
            property_type, district, price_text, agent_name, agent_contact_line,
            hero_photo=property_photo, before_photo=None, agent_photo=agent_photo,
            head=_h, sub=_s, facts=fl, acc=acc)
    except Exception as e:
        print(f"potential: the layout failed on the real photograph ({e}); "
              "falling back to the Bold poster")
        poster = _render_bold_base_row(
            property_type, district, price_text, stats, agent_name, agent_contact_line,
            property_photo, agent_photo, headline=headline, subhead=subhead,
            facts=facts, accent=accent)
    if cea:
        poster = _add_compliance_band(poster, cea)
    return poster, False


def _render_potential(property_type, district, price_text, stats, agent_name,
                      agent_contact_line, property_photo, agent_photo, headline=None,
                      subhead=None, facts=None, accent=None, makeover_photo=None,
                      compliance_line=""):
    """The TEMPLATES entry. Returns an image, like every other template, so nothing that
    iterates TEMPLATES has to know this style is different."""
    return _render_potential_reporting(
        property_type, district, price_text, stats, agent_name, agent_contact_line,
        property_photo, agent_photo, headline=headline, subhead=subhead, facts=facts,
        accent=accent, makeover_photo=makeover_photo, compliance_line=compliance_line)[0]


# --- the concept registry --------------------------------------------------
# Styles that may be handed a generated image. The ONLY way into this dict is
# _register_concept_style, which writes all three registries in one call, so a style
# cannot be half-registered -- present in TEMPLATES but missing its guards, or given a
# makeover by render_poster while its layout draws neither inset nor band.
#
# The VALUE is the (poster, concept_used) entry point, not the plain template function:
# render_poster hands the makeover image to this and to nothing else.
CONCEPT_STYLES = {}


def _register_concept_style(template_id, template_fn, reporting_fn):
    """Registers a style that owns its own disclosure band and may receive a makeover.

    Registering is what makes render_poster willing to fetch and pass a generated image,
    and it is also what makes render_poster STOP adding the CEA band a second time -- a
    concept style stacks both bands itself. The two used to be separate declarations (a
    dict assignment and an id in a set) and could have disagreed."""
    TEMPLATES[template_id] = template_fn
    CONTENT_TEMPLATES[template_id] = template_fn
    CONCEPT_STYLES[template_id] = reporting_fn


_register_concept_style("potential", _render_potential, _render_potential_reporting)


# --- getting the makeover image -------------------------------------------

def potential_cache_key(source_url):
    """A short, stable key for one listing's makeover. The source url is hashed rather
    than stored, so the cache never becomes a second place that knows a photo's address.
    Same shape as old_to_new.cache_key, deliberately."""
    digest = hashlib.sha1(str(source_url or "").encode("utf-8")).hexdigest()[:20]
    return f"v{POTENTIAL_PROMPT_VERSION}.{POTENTIAL_PROMPT_FINGERPRINT}:{POTENTIAL_ROLE}:{digest}"


def read_makeover_cache(stored):
    """Whatever is on the listing -> {key: url}. Tolerates anything."""
    if not isinstance(stored, dict):
        return {}
    images = stored.get("images")
    if not isinstance(images, dict):
        return {}
    return {str(k): str(v) for k, v in images.items()
            if isinstance(v, str) and v.startswith("http")}


def read_makeover_approvals(stored):
    """The concept keys a human has looked at and chosen. Tolerates anything.

    AN ALLOWLIST, NOT A BLOCKLIST, and the direction is the whole point. Recording which
    concepts were REJECTED would mean anything not explicitly rejected is publishable --
    including a concept nobody has opened. Recording which were APPROVED means nothing
    reaches a buyer until someone said yes to that exact image. A stored blob from before
    approvals existed has no 'approved' key and therefore approves nothing, which is the
    safe direction to be wrong in.
    """
    if not isinstance(stored, dict):
        return set()
    approved = stored.get("approved")
    if not isinstance(approved, (list, tuple, set)):
        return set()
    return {str(k) for k in approved if isinstance(k, str) and k}


def write_makeover_cache(existing, approved=None):
    """{key: url} plus the approved keys -> the value to store on the listing.

    `approved` is intersected with the images actually held, so an approval can never
    outlive the picture it refers to -- a prompt reword changes every key, and yesterday's
    yes must not silently apply to a concept generated from different instructions."""
    images = dict(existing)
    return {"version": POTENTIAL_PROMPT_VERSION,
            "images": images,
            "approved": sorted(k for k in set(approved or ()) if k in images)}


class Makeover:
    """What one attempt to get a makeover image produced.

    url          -- the public url of the makeover, or None. None means "render the
                    ordinary poster", and every caller must treat it as normal.
    images_paid  -- what fal CHARGED FOR, which is not what arrived. Added to the
                    agent's month-to-date tally.
    images_lost  -- the subset that was paid for and produced nothing usable. A caller
                    must never retry a pass with images_lost > 0 without charging for it
                    again; this is what makes that visible.
    added        -- JUST the entries this pass created, for a caller that must MERGE them
                    into whatever is on the listing row now rather than overwrite it.
                    `cache` is a snapshot taken when the request started, and a poster
                    render can take most of a minute: two presses on the same listing
                    with different photos would each write back their own snapshot and
                    the later one would erase the other's entry, so the erased picture
                    gets bought again. Writing only what changed makes that impossible.
    from_cache   -- True when nothing was generated and nothing was spent.
    rate_limited -- fal had no room. The poster degrades; it does not queue and wait.
    note         -- one plain-English sentence for the agent, or "". Never quotes fal.
    cache        -- the cache to write back to the listing.
    """

    def __init__(self):
        self.url = None
        self.images_paid = 0
        self.images_lost = 0
        self.added = {}
        self.from_cache = False
        self.rate_limited = False
        self.note = ""
        self.cache = {}

    @property
    def cost_usd(self):
        return round(self.images_paid * fal_images.COST_PER_IMAGE_USD, 4)

    def __repr__(self):
        return ("Makeover(url=%s, paid=%d, lost=%d, cached=%s, rate_limited=%s)"
                % (bool(self.url), self.images_paid, self.images_lost,
                   self.from_cache, self.rate_limited))


def makeover_image(source_photo_url, cache, generate_image, store_image,
                   makeovers_used_this_month=0, deadline=None):
    """The makeover image for one listing photo, from cache if we already have it.

    NEVER RAISES. Every failure returns a Makeover with url=None and a note, because a
    poster must always come out -- see _render_potential, where url=None is the ordinary
    quiet path rather than an error.

    cache          -- read_makeover_cache(listing["potential_images"]).
    generate_image(prompt, source_urls, deadline) -> bytes. fal_images.generate,
                      injected so this module is testable without a network or a key --
                      the same injection old_to_new.build_plan takes.
    store_image(role, key, data) -> public url. main.py owns storage.
    makeovers_used_this_month -- IMAGES this agent has been billed for this calendar
                      month, from the POTENTIAL_USAGE_* columns. The thing with a price.

    THE CACHE IS THE COST CONTROL, not a nicety. A makeover is expensive and stable, and
    a poster gets re-rendered constantly -- a different accent, a reworded headline, a
    typo in the price. Only a source photo we have never made a makeover from costs
    anything, so pressing Generate again is free.
    """
    result = Makeover()
    result.cache = dict(cache or {})

    if not source_photo_url:
        result.note = "There is no photo to build a concept from."
        return result

    key = potential_cache_key(source_photo_url)
    cached = result.cache.get(key)
    if cached:
        # FREE, and the reason the ceiling is not reached by ordinary use.
        result.url = cached
        result.from_cache = True
        return result

    if not fal_images.is_enabled():
        result.note = "Concept images are not switched on for this deploy."
        return result

    # The ceiling, checked ONCE and before the only billable call. A cached makeover got
    # here already, so an agent at their limit can still re-render every poster they
    # have made.
    used = max(0, int(makeovers_used_this_month or 0))
    if used >= MAX_POTENTIAL_MAKEOVERS_PER_AGENT_PER_MONTH:
        result.note = (
            "This account has used its concept allowance for this month "
            f"({MAX_POTENTIAL_MAKEOVERS_PER_AGENT_PER_MONTH} concepts). Concepts you "
            "have already made are still there to use, and the allowance resets at the "
            "start of next month.")
        return result

    # Screened before anything is sent: a refused word costs nothing here and a 422 we
    # paid for costs four cents. Free, so it is done on every render rather than trusted
    # to have been done when the constant was written.
    refused = fal_images.screen_prompt(POTENTIAL_MAKEOVER_PROMPT)
    if refused:
        result.note = "The concept could not be requested."
        print(f"potential: the makeover prompt contains {refused!r}, which fal refuses; "
              "not sending it")
        return result

    overall = (deadline if deadline is not None
               else time.monotonic() + POTENTIAL_BUDGET_SECONDS)
    try:
        data = generate_image(POTENTIAL_MAKEOVER_PROMPT, [source_photo_url], overall)
    except Exception as e:
        # COUNT WHAT FAL BILLED, NOT WHAT ARRIVED. Five measured failure shapes leave fal
        # paid and nothing in our hands (an incident where every collect attempt 502s, a
        # non-JSON 200, a 200 we cannot read, COMPLETED with no picture, 429 throughout
        # collection). billed_images() is how the money becomes visible; counting
        # downloads instead is how a ceiling reads zero on exactly the days it matters.
        billed = fal_images.billed_images(e)
        result.images_paid += billed
        result.images_lost += billed
        result.rate_limited = isinstance(e, fal_images.FalRateLimited)
        if result.rate_limited:
            # fal starts an account at two concurrent requests. With fifty agents this is
            # an everyday answer, not a rarity -- and a poster is a button press, so the
            # right response is to hand over the ordinary poster now rather than hold the
            # request open waiting for room.
            result.note = ("The concept service is busy right now. Please try again in "
                           "a minute.")
        else:
            result.note = ("The concept could not be made just now. Please try again "
                           "in a moment.")
        print("potential: no makeover image this render (%s: %s); billed %d"
              % (type(e).__name__, e, billed))
        return result

    result.images_paid += 1
    try:
        result.url = store_image(POTENTIAL_ROLE, key, data)
    except Exception as e:
        # Paid for and cannot be kept. Counted as paid, because fal charged, AND as lost,
        # so a caller can see that retrying would buy it a second time.
        result.images_lost += 1
        result.note = ("The concept was made but could not be saved. Please try "
                       "again.")
        print(f"potential: generated a makeover but could not store it ({e})")
        return result
    if result.url:
        result.cache[key] = result.url
        result.added[key] = result.url
    return result


def render_poster(property_type, district, price_text, stats, agent_name, agent_contact_line,
                  property_photo_url=None, agent_photo_url=None, template_id="editorial",
                  compliance_line="", headline=None, subhead=None, facts=None, accent=None,
                  makeover_photo_url=None):
    """Renders one poster image and returns it as a Pillow Image (RGB, JPEG-ready).

    stats: list of strings (e.g. "5 Rooms", "4 Baths", "2,400 sqft", "SGD 1,200 psf").
    Empty/falsy entries are dropped from the joined stats line rather than left blank.
    template_id: one of TEMPLATES' keys; falls back to "editorial" if unknown.
    compliance_line: optional footer text (NestList Home CEA line). Blank -- every landed
    poster -- returns exactly the template's own render.

    headline / subhead / facts / accent are the Bold family's richer inputs and are only
    forwarded to the templates that declare them (CONTENT_TEMPLATES). The original ten
    never see them, so this call is unchanged for every existing caller.

      headline: the hook line, written upstream. Set in large caps, fitted to its box.
      subhead:  one or two supporting lines.
      facts:    label/value/unit items, e.g.
                [{"icon": "plot", "label": "Land size", "value": "5,573", "unit": "sqft"}]
                or ("plot", "Land size", "5,573", "sqft") tuples. Items with a blank or
                zero value are dropped, so an unfilled bathroom count never reaches the
                poster. Omitted entirely -> derived from `stats`.
      accent:   accent colour by name ("cobalt", "teal", "violet", "magenta") or as an
                RGB tuple. Omitted -> each layout's own default.

    makeover_photo_url is the AI makeover concept for the "potential" template, from
    makeover_image(). Only that template is given it, and None is the ordinary case: the
    poster then renders from the real photograph with no AI claim anywhere on it. A URL
    that will not download is the same case -- it is logged, not raised, because a poster
    must always come out.
    """
    return render_poster_report(
        property_type, district, price_text, stats, agent_name, agent_contact_line,
        property_photo_url=property_photo_url, agent_photo_url=agent_photo_url,
        template_id=template_id, compliance_line=compliance_line, headline=headline,
        subhead=subhead, facts=facts, accent=accent,
        makeover_photo_url=makeover_photo_url).image


class PosterResult:
    """A finished poster and the truth about what is in it.

    image        -- the poster, exactly what render_poster returns.
    concept_used -- whether a GENERATED concept is actually in those pixels. False for
                    every ordinary template, and false on a concept style that fell back
                    to the real photograph for any reason. Never inferred from whether a
                    makeover image was available: see _render_potential_reporting.
    """

    def __init__(self, image, concept_used=False):
        self.image = image
        self.concept_used = bool(concept_used)


def render_poster_report(property_type, district, price_text, stats, agent_name,
                         agent_contact_line, property_photo_url=None, agent_photo_url=None,
                         template_id="editorial", compliance_line="", headline=None,
                         subhead=None, facts=None, accent=None, makeover_photo_url=None):
    """render_poster(), plus what actually happened. See PosterResult.

    Separate from render_poster rather than a changed return type, so every existing
    caller of render_poster is untouched."""
    property_photo = _fetch_listing_photo(property_photo_url) if property_photo_url else None
    # The agent portrait is not a listing photo and has no hires twin -- fetched plainly.
    agent_photo = _fetch_image(agent_photo_url) if agent_photo_url else None

    # A makeover image is fetched ONLY for a registered concept style, and is handed to
    # that style's registry entry point and to nothing else.
    concept_entry = CONCEPT_STYLES.get(template_id)
    makeover_photo = None
    if makeover_photo_url and concept_entry is not None:
        try:
            makeover_photo = _fetch_image(makeover_photo_url)
        except Exception as e:
            print(f"potential: the makeover image could not be fetched ({e}); "
                  "the poster falls back to the real photograph")

    kwargs = dict(
        property_type=property_type,
        district=district,
        price_text=price_text,
        stats=stats,
        agent_name=agent_name,
        agent_contact_line=agent_contact_line,
        property_photo=property_photo,
        agent_photo=agent_photo,
    )
    if template_id in CONTENT_TEMPLATES:
        kwargs.update(headline=headline, subhead=subhead, facts=facts, accent=accent)

    if concept_entry is not None:
        # A concept style stacks its own bands and consumes the compliance line itself,
        # so the CEA band must NOT be added again below -- a second pass would scale the
        # poster twice and squash the first band inside the second.
        poster, concept_used = concept_entry(
            makeover_photo=makeover_photo, compliance_line=compliance_line, **kwargs)
        return PosterResult(poster, concept_used)

    poster = TEMPLATES.get(template_id, _render_editorial)(**kwargs)
    if compliance_line and str(compliance_line).strip():
        poster = _add_compliance_band(poster, str(compliance_line).strip())
    return PosterResult(poster, False)
