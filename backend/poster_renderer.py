"""NestList poster renderer -- Pillow-based replacement for the Placid.app integration.

Produces the approved v1 template: full-bleed property photo, title/district/price/stats
top-left, and a bottom agent panel with a large crisp headshot bleeding to the edge and a
translucent name/phone panel over the property photo.
"""
import io
import os

import requests
from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageOps

W, H = 1200, 1500

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


TITLE_FONT = _load_font(PLAYFAIR, 82, weight=700)
DISTRICT_FONT = _load_font(PLAYFAIR, 46, weight=500)
PRICE_FONT = _load_font(INTER, 68, weight=700, opsz=32)
STATS_FONT = _load_font(INTER, 38, weight=500, opsz=18)
NAME_FONT = _load_font(INTER, 52, weight=700, opsz=16)
CONTACT_FONT = _load_font(INTER, 76, weight=700, opsz=32)


def _fetch_image(url):
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    return Image.open(io.BytesIO(response.content))


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


def _text_with_shadow(base, draw, pos, text, font, fill, shadow_color=(0, 0, 0, 160), blur=6, offset=(0, 2)):
    x, y = pos
    shadow_layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    ImageDraw.Draw(shadow_layer).text((x + offset[0], y + offset[1]), text, font=font, fill=shadow_color)
    shadow_layer = shadow_layer.filter(ImageFilter.GaussianBlur(blur))
    base.alpha_composite(shadow_layer)
    draw.text((x, y), text, font=font, fill=fill)


def render_poster(property_type, district, price_text, stats, agent_name, agent_contact_line,
                   property_photo_url=None, agent_photo_url=None):
    """Renders one poster image and returns it as a Pillow Image (RGB, JPEG-ready).

    stats: list of strings (e.g. "5 Rooms", "4 Baths", "2,400 sqft", "SGD 1,200 psf").
    Empty/falsy entries are dropped from the joined stats line rather than left blank --
    this is what fixes the "gap" bug the Placid template had for missing fields.
    """
    property_photo = _fetch_image(property_photo_url) if property_photo_url else None
    agent_photo = _fetch_image(agent_photo_url) if agent_photo_url else None

    if property_photo:
        base = ImageOps.fit(property_photo.convert("RGB"), (W, H), centering=(0.5, 0.42)).convert("RGBA")
    else:
        base = Image.new("RGBA", (W, H), (40, 40, 36, 255))

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

    stats_line = "   ·   ".join(s for s in stats if s)
    sy = py + 92
    _text_with_shadow(base, draw, (x, sy), stats_line, STATS_FONT, WHITE, blur=5)

    photo_w = 380
    right_overlay = Image.new("RGBA", (W - photo_w, bar_h), (10, 10, 8, 150))
    base.alpha_composite(right_overlay, dest=(photo_w, bar_top))

    if agent_photo:
        photo = ImageOps.fit(agent_photo.convert("RGB"), (photo_w, bar_h), centering=(0.5, 0.25))
        base.paste(photo, (0, bar_top))
    else:
        draw.rectangle((0, bar_top, photo_w, H), fill=(40, 40, 36, 255))

    tx = photo_w + 48
    ty = bar_top + 84
    _text_with_shadow(base, draw, (tx, ty), agent_name.upper(), NAME_FONT, PALE, blur=3, offset=(0, 1))
    ty += 78
    _text_with_shadow(base, draw, (tx, ty), agent_contact_line, CONTACT_FONT, PALE, blur=3, offset=(0, 1))

    return base.convert("RGB")
