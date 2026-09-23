"""The stray-hair pass must improve plain-backdrop photos and refuse to damage anything else."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from PIL import Image, ImageDraw, ImageStat
import photo_tidy

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))

def diff(a, b):
    return ImageStat.Stat(Image.blend(a.convert("RGB"), b.convert("RGB"), 0.0)).mean  # placeholder

print("\n[1] a thin dark line on a light backdrop is softened")
img = Image.new("RGB", (300, 300), (232, 230, 226))
d = ImageDraw.Draw(img)
d.line([(150, 20), (158, 120)], fill=(40, 30, 25), width=2)     # a 'hair'
BOX = (140, 30, 175, 110)          # a band containing the whole line
before = ImageStat.Stat(img.crop(BOX)).mean
out = photo_tidy.tidy_stray_hairs(img)
after = ImageStat.Stat(out.crop(BOX)).mean
check("the band containing the line gets lighter",
      sum(after) > sum(before) + 6, "before=%.1f after=%.1f" % (sum(before)/3, sum(after)/3))
check("the line is largely gone, not merely dimmed",
      min(ImageStat.Stat(out.crop(BOX)).extrema[0]) > min(ImageStat.Stat(img.crop(BOX)).extrema[0]) + 60,
      "darkest before=%s after=%s" % (ImageStat.Stat(img.crop(BOX)).extrema[0][0],
                                      ImageStat.Stat(out.crop(BOX)).extrema[0][0]))

print("\n[2] broad dark shapes are NOT touched -- that would be her hair or her face")
img2 = Image.new("RGB", (300, 300), (232, 230, 226))
ImageDraw.Draw(img2).ellipse([80, 80, 220, 220], fill=(45, 35, 30))   # a head-sized mass
out2 = photo_tidy.tidy_stray_hairs(img2)
c_before, c_after = img2.getpixel((150, 150)), out2.getpixel((150, 150))
check("the centre of a broad shape is unchanged", abs(sum(c_before) - sum(c_after)) < 12,
      "before=%s after=%s" % (c_before, c_after))

print("\n[3] a dark or busy background leaves the photo essentially alone")
img3 = Image.new("RGB", (300, 300), (28, 26, 24))
ImageDraw.Draw(img3).line([(150, 20), (158, 120)], fill=(12, 10, 9), width=2)
out3 = photo_tidy.tidy_stray_hairs(img3)
same = ImageStat.Stat(Image.eval(Image.blend(img3, out3, 0.5), lambda v: v)).mean
d_before = ImageStat.Stat(img3).mean
check("a dark backdrop is left as-is", all(abs(a-b) < 4 for a, b in zip(same, d_before)),
      "%s vs %s" % (same, d_before))

print("\n[4] it never raises, whatever it is handed")
ok = True
for bad in [Image.new("L", (3, 3)), Image.new("RGB", (1, 1)),
            Image.new("RGBA", (40, 40), (0, 0, 0, 0)), Image.new("P", (20, 20))]:
    try:
        photo_tidy.tidy_stray_hairs(bad)
    except Exception as e:
        ok = False; print("    raised on %s: %s" % (bad.mode, e))
check("survives tiny, 1px, alpha and palette images", ok)

print("\n[5] the tuned constants are the ones that were reviewed")
check("radius is 13", photo_tidy.STRAY_RADIUS == 13, str(photo_tidy.STRAY_RADIUS))
check("output is the same size as the input",
      photo_tidy.tidy_stray_hairs(Image.new("RGB", (640, 480), (230,230,230))).size == (640, 480))

print("\n" + "="*58)
print("PASSED: %d    FAILED: %d" % (P, F))
print("="*58)
sys.exit(1 if F else 0)
