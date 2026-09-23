"""Tidy stray flyaway hairs in an agent's avatar reference photos.

WHY THIS EXISTS
An agent records themselves once, at home, on a phone, against a plain wall --
exactly what the shooting guide asks for. What the guide cannot ask for is hair
that behaves. Flyaway strands stand out sharply against a pale backdrop, they
carry into every avatar built from those photos, and no agent is going to notice
or ask us to fix it. So it happens on the way in, for everybody.

HOW IT WORKS, AND WHY IT IS SAFE
A stray hair is a THIN DARK line sitting in an area that is otherwise smooth and
light. A median filter erases structures thinner than its window while leaving
broad shapes alone -- so the median of the photo is, in those places, the photo
with the strays already gone. All that is needed is a mask saying where to trust
it.

The mask fires only where BOTH hold:
  * the pixel is meaningfully darker than its own neighbourhood  -> a thin dark mark
  * that neighbourhood is light                                  -> we are on the
    backdrop, not inside her hair, where the same test would fire on every strand

That second condition is what protects the face and the body of the hair. On a
photo taken against a busy or dark background the mask barely fires at all and
the photo comes back essentially untouched, which is the correct failure: doing
nothing is always better than smearing someone's face.

Deliberately Pillow-only. A segmentation model would clean more thoroughly, but
it means onnxruntime plus a ~180MB download on first use, on a web worker that
also has to answer buyer pages. Not worth it for a cosmetic pass.
"""
import logging

from PIL import Image, ImageChops, ImageFilter, ImageOps

logger = logging.getLogger(__name__)

# Tuned on Janel's recording against a pale wall, comparing r9/r13/r15 side by
# side at full size: r9 leaves the long strands, r15 begins to smudge the hair's
# own edge. r13 removes what reads as untidy and keeps the silhouette honest.
STRAY_RADIUS = 13
STRAY_DARKER_THAN = 6      # 0-255; how much darker than its neighbourhood a mark must be
STRAY_MIN_BACKDROP = 100   # 0-255; how light that neighbourhood must be to count as backdrop


def tidy_stray_hairs(img, radius=STRAY_RADIUS, threshold=STRAY_DARKER_THAN,
                     min_backdrop=STRAY_MIN_BACKDROP):
    """Return a copy with flyaway hairs softened, or the original if anything goes wrong.

    Never raises. This is a cosmetic improvement on an upload path -- an agent
    losing their photo because a filter failed would be a far worse outcome than
    an untidied one.
    """
    try:
        rgb = img.convert("RGB")
        median = rgb.filter(ImageFilter.MedianFilter(radius))
        grey = ImageOps.grayscale(rgb)
        grey_median = ImageOps.grayscale(median)

        darker = ImageChops.subtract(grey_median, grey)
        is_thin_dark = darker.point(lambda v: 255 if v >= threshold else 0)
        is_backdrop = grey_median.point(lambda v: 255 if v >= min_backdrop else 0)

        mask = ImageChops.multiply(is_thin_dark, is_backdrop)
        mask = mask.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(2))
        return Image.composite(median, rgb, mask)
    except Exception as e:
        logger.warning("stray-hair tidy skipped (%s); using the photo as uploaded", e)
        return img
