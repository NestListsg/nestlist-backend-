"""NestList poster renderer -- Pillow-based replacement for the Placid.app integration.

Ten templates, selected by template_id (matches agent.poster_template_id / POSTER_TEMPLATES
in main.py). Each _render_* function takes the same normalized inputs and returns an RGB
Pillow Image ready to save as JPEG. Shared helpers (fonts, scrims, text-with-shadow, frames)
live at the top; template-specific layout code lives in each _render_* function.
"""
import io
import os

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

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


def _fetch_image(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content))


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
    photo = ImageOps.fit(agent_photo.convert("RGB"), (size, size), centering=(0.5, 0.3))
    dest = (round(cx - half), round(cy - half))
    base.paste(photo, dest)
    x0, y0 = dest
    ImageDraw.Draw(base).rectangle((x0, y0, x0 + size, y0 + size), outline=border_color, width=border_width)


def _photo_beside_text(base, draw, cx, y, text, font, fill, agent_photo, size=56, gap=18, shadow=True, blur=6, offset=(0, 2)):
    """Centered row: square agent photo + text, the pair centered together at cx.
    Falls back to plain centered text when there's no agent photo."""
    if not agent_photo:
        _centered_text(base, draw, cx, y, text, font, fill, blur=blur, offset=offset, shadow=shadow)
        return
    text_w = _text_width(draw, text, font)
    bbox = draw.textbbox((0, 0), text, font=font) if text else (0, 0, 0, 0)
    text_h = bbox[3] - bbox[1]
    x0 = cx - (size + gap + text_w) / 2
    _square_photo(base, agent_photo, x0 + size / 2, y + text_h / 2, size)
    text_x = x0 + size + gap
    if shadow:
        _text_with_shadow(base, draw, (text_x, y), text, font, fill, blur=blur, offset=offset)
    else:
        draw.text((text_x, y), text, font=font, fill=fill)


def _left_photo_text_row(base, draw, x, y, text, font, fill, agent_photo, size=56, gap=18, shadow=True, blur=6, offset=(0, 2)):
    """Left-aligned row: square agent photo, then text starting after it."""
    text_x = x
    if agent_photo:
        bbox = draw.textbbox((0, 0), text, font=font) if text else (0, 0, 0, 0)
        text_h = bbox[3] - bbox[1]
        _square_photo(base, agent_photo, x + size / 2, y + text_h / 2, size)
        text_x = x + size + gap
    if shadow:
        _text_with_shadow(base, draw, (text_x, y), text, font, fill, blur=blur, offset=offset)
    else:
        draw.text((text_x, y), text, font=font, fill=fill)


def _right_photo_text_row(base, draw, right_x, y, text, font, fill, agent_photo, size=48, gap=16, shadow=True, blur=6, offset=(0, 2)):
    """Right-aligned row: text ending at right_x, square agent photo to its left."""
    text_w = _text_width(draw, text, font)
    text_x = right_x - text_w
    if agent_photo:
        bbox = draw.textbbox((0, 0), text, font=font) if text else (0, 0, 0, 0)
        text_h = bbox[3] - bbox[1]
        _square_photo(base, agent_photo, text_x - gap - size / 2, y + text_h / 2, size)
    if shadow:
        _text_with_shadow(base, draw, (text_x, y), text, font, fill, blur=blur, offset=offset)
    else:
        draw.text((text_x, y), text, font=font, fill=fill)


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
    y += 60
    contact = f"{agent_name.upper()}   ·   {agent_contact_line}" if agent_contact_line else agent_name.upper()
    _photo_beside_text(base, draw, cx, y, contact, GALLERY_CONTACT_FONT, INK, agent_photo, size=78, gap=20, shadow=False)

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
    contact = f"{agent_name.upper()}   ·   {agent_contact_line}" if agent_contact_line else agent_name.upper()
    _right_photo_text_row(base, draw, W - 44, 24, contact, EYEBROW_FONT_SM, WHITE, agent_photo, size=54, gap=14, shadow=False)

    _vertical_gradient_scrim(base, H - 460, H, 0, 210)

    x = 48
    y = H - 340
    lines = _wrap_text(draw, property_type.upper(), HUGE_TITLE_FONT, W - 96)
    for line in lines[:2]:
        _text_with_shadow(base, draw, (x, y), line, HUGE_TITLE_FONT, WHITE, blur=10)
        y += 100

    y += 6
    _text_with_shadow(base, draw, (x, y), price_text, BOLD_PRICE_FONT, GOLD, blur=6)
    y += 60
    _text_with_shadow(base, draw, (x, y), _stats_line(stats), STATS_FONT, WHITE, blur=5)

    return base.convert("RGB")


# ================================
# 4. VIGNETTE FRAME
# ================================
def _render_vignette_frame(property_type, district, price_text, stats, agent_name, agent_contact_line,
                            property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))
    draw = ImageDraw.Draw(base)

    _vertical_gradient_scrim(base, 0, 240, 150, 0)
    _vertical_gradient_scrim(base, H - 340, H, 0, 190)

    cx = W // 2
    _centered_text(base, draw, cx, 70, property_type.upper(), EYEBROW_FONT, WHITE, blur=5)
    _centered_text(base, draw, cx, 112, district.upper(), DISTRICT_FONT, GOLD, blur=5)

    y = H - 260
    _centered_text(base, draw, cx, y, price_text, PRICE_FONT, GOLD, blur=6)
    y += 92
    _centered_text(base, draw, cx, y, _stats_line(stats), STATS_FONT, WHITE, blur=5)
    y += 66
    contact = f"{agent_name.upper()}   ·   {agent_contact_line}" if agent_contact_line else agent_name.upper()
    _photo_beside_text(base, draw, cx, y, contact, EYEBROW_FONT_SM, WHITE, agent_photo, size=68, gap=18, blur=4)

    _double_gold_frame(base, margin=40, gap=10)

    return base.convert("RGB")


# ================================
# 5. POSTCARD
# ================================
def _render_postcard(property_type, district, price_text, stats, agent_name, agent_contact_line,
                      property_photo, agent_photo):
    base = Image.new("RGBA", (W, H), CREAM_BG)
    draw = ImageDraw.Draw(base)

    margin_x, margin_top, photo_h = 60, 60, 900
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
    y += 50
    contact = f"{agent_name}   ·   {agent_contact_line}" if agent_contact_line else agent_name
    _left_photo_text_row(base, draw, x, y, contact, GALLERY_CONTACT_FONT, INK, agent_photo, size=84, gap=20, shadow=False)

    return base.convert("RGB")


# ================================
# 6. GOLD FRAME
# ================================
def _render_gold_frame(property_type, district, price_text, stats, agent_name, agent_contact_line,
                        property_photo, agent_photo):
    base = _fit(property_photo, W, H).convert("RGBA") if property_photo else Image.new("RGBA", (W, H), (40, 40, 36, 255))
    draw = ImageDraw.Draw(base)

    _vertical_gradient_scrim(base, 0, 200, 130, 0)
    _vertical_gradient_scrim(base, H - 320, H, 0, 190)

    x = 70
    draw.text((x, 66), property_type.upper(), font=EYEBROW_FONT_SM, fill=WHITE)
    dw = _text_width(draw, property_type.upper(), EYEBROW_FONT_SM)
    draw.text((x + dw + 26, 66), district, font=EYEBROW_FONT_SM, fill=(220, 216, 206, 255))

    # Single left-aligned column, stacked -- keeps price/stats/contact from ever
    # colliding with each other regardless of how long the real listing's text is.
    y = H - 250
    draw.text((x, y), price_text, font=PRICE_FONT, fill=GOLD)
    y += 84
    draw.text((x, y), _stats_line(stats), font=STATS_FONT, fill=WHITE)
    y += 44
    contact = f"{agent_name}   ·   {agent_contact_line}" if agent_contact_line else agent_name
    _left_photo_text_row(base, draw, x, y, contact, NAME_FONT, PALE, agent_photo, size=80, gap=18, shadow=False)

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

    _vertical_gradient_scrim(base, H - 400, H, 0, 200)

    x = 56
    y = H - 300
    draw_gold = _load_font(PLAYFAIR, 74, weight=700)
    _text_with_shadow(base, draw, (x, y), _mixed(property_type), draw_gold, GOLD, blur=6)
    y += 96
    _text_with_shadow(base, draw, (x, y), _stats_line(stats), STATS_FONT, WHITE, blur=5)
    y += 60
    contact = f"{agent_name}   ·   {agent_contact_line}" if agent_contact_line else agent_name
    _left_photo_text_row(base, draw, x, y, contact, NAME_FONT, WHITE, agent_photo, size=88, gap=20, blur=4)

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

    _vertical_gradient_scrim(base, H - 420, H, 0, 210)

    y = H - 330
    _centered_text(base, draw, cx, y, price_text, NUMERAL_PRICE_FONT, GOLD, blur=8)
    y += 118
    _centered_text(base, draw, cx, y, _stats_line(stats), STATS_FONT, WHITE, blur=5)
    y += 60
    contact = f"{agent_name.upper()}   ·   {agent_contact_line}" if agent_contact_line else agent_name.upper()
    _photo_beside_text(base, draw, cx, y, contact, EYEBROW_FONT_SM, WHITE, agent_photo, size=68, gap=18, blur=4)

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

    # Three stacked rows rather than a strict left/right grid -- the stats line's real
    # width varies a lot with listing data, so row 1 (title + price) is the only row
    # that shares left/right space; stats and contact each get their own full row.
    bar_h = 250
    bar_top = H - bar_h
    _solid_band(base, (0, bar_top, W, H), (10, 10, 8), 175)

    x = 56
    y = bar_top + 36
    draw.text((x, y), _mixed(property_type), font=DISTRICT_FONT, fill=WHITE)
    _right_text(base, draw, W - 56, y + 6, price_text, BOLD_PRICE_FONT, GOLD, shadow=False)

    y += 76
    draw.text((x, y), _stats_line(stats), font=STATS_FONT, fill=(214, 210, 198, 255))

    y += 66
    contact = f"{agent_name}   /   {agent_contact_line}" if agent_contact_line else agent_name
    _right_photo_text_row(base, draw, W - 56, y, contact, EYEBROW_FONT_SM, WHITE, agent_photo, size=68, gap=18, shadow=False)

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

    by = H - 130
    draw.line((tx, by - 34, tx + max_w, by - 34), fill=(226, 220, 204, 255), width=1)
    _left_photo_text_row(base, draw, tx, by, agent_name.upper(), EYEBROW_FONT_SM, INK, agent_photo, size=72, gap=18, shadow=False)
    if agent_contact_line:
        draw.text((tx, by + 56), agent_contact_line, font=EYEBROW_FONT_SM, fill=(184, 145, 46, 255))

    return base.convert("RGB")


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


def render_poster(property_type, district, price_text, stats, agent_name, agent_contact_line,
                   property_photo_url=None, agent_photo_url=None, template_id="editorial"):
    """Renders one poster image and returns it as a Pillow Image (RGB, JPEG-ready).

    stats: list of strings (e.g. "5 Rooms", "4 Baths", "2,400 sqft", "SGD 1,200 psf").
    Empty/falsy entries are dropped from the joined stats line rather than left blank.
    template_id: one of TEMPLATES' keys; falls back to "editorial" if unknown.
    """
    property_photo = _fetch_image(property_photo_url) if property_photo_url else None
    agent_photo = _fetch_image(agent_photo_url) if agent_photo_url else None

    renderer = TEMPLATES.get(template_id, _render_editorial)
    return renderer(
        property_type=property_type,
        district=district,
        price_text=price_text,
        stats=stats,
        agent_name=agent_name,
        agent_contact_line=agent_contact_line,
        property_photo=property_photo,
        agent_photo=agent_photo,
    )
