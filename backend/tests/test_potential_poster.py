"""The Potential poster style. Run:  python3 tests/test_potential_poster.py  (exit 1 on failure)

THE INVARIANT IS THE FEATURE. A generated building may never appear on a poster without
BOTH the real photograph inset and labelled BEFORE, and the disclosure band. Section [1]
proves that by SABOTAGE: it feeds an unmistakable flat-magenta "makeover" and breaks each
guard in turn, then looks at the HERO PIXELS to decide whether the generated image was
used. "The code logged a fallback" is not the claim being tested; "no generated pixels
reached the poster" is.

Section [2] measures headline legibility on the finished hero, because "the text drew" is
not the same claim as "the text can be read". Section [3] checks the two bands compose.
Section [4] checks the cost ceiling, the cache and the billing count with fal injected.

This file deliberately does NOT re-check that the other templates are pixel-identical --
test_home_decisions.py already does that against a pushed commit, which is the stronger
mechanism.
"""
import os
import sys

from PIL import Image

BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)

import poster_renderer as PR  # noqa: E402

PASSED = FAILED = 0


def check(label, ok, detail=None):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {label}" + (f"  -- {detail}" if detail else ""))
    else:
        FAILED += 1
        print(f"  FAIL  {label}" + (f"  -- {detail}" if detail else ""))


# An unmistakable stand-in for the generated image: nothing in a photograph looks like
# this, so "did the makeover get used?" is answered by counting pixels.
FAKE = Image.new("RGB", (640, 480), (255, 0, 190))
REAL = Image.new("RGB", (640, 480), (118, 126, 112))
# A bright white block in the middle of REAL, standing in for the white gate pillar that
# made the first version of the headline unreadable.
REAL.paste(Image.new("RGB", (190, 300), (248, 248, 246)), (230, 150))

CEA = "Janel Chee  CEA Reg. No. R123456H  |  Agency Licence No. L3008899K"
LISTING = dict(
    property_type="SEMI-DETACHED", district="DISTRICT 15", price_text="S$11,000,000",
    stats=["5 Rooms", "", "5,868 sqft", "SGD 1,875 psf"],
    agent_name="Janel Chee", agent_contact_line="8123 0889",
    facts=[{"icon": "plot", "label": "Land size", "value": "5,573", "unit": "sqft"}])


def generated_used(img):
    small = img.convert("RGB").resize((120, 150))
    return sum(1 for p in small.getdata()
               if p[0] > 200 and p[1] < 90 and p[2] > 140) > 40


def has_band(img):
    """A full-width flat strip at the foot that is not paper."""
    for y in range(img.height - 1, img.height - 120, -1):
        px = [img.getpixel((x, y)) for x in range(0, img.width, 97)]
        if max(max(abs(p[c] - px[0][c]) for c in range(3)) for p in px) < 12 \
                and sum(px[0]) < 690:
            return True
    return False


def render(cea="", photo=REAL, makeover=FAKE, patch=None, **over):
    saved = {}
    for name, value in (patch or {}).items():
        saved[name] = getattr(PR, name)
        setattr(PR, name, value)
    data = dict(LISTING)
    data.update(over)
    try:
        return PR.TEMPLATES["potential"](
            property_photo=photo, agent_photo=None, makeover_photo=makeover,
            accent="cobalt", compliance_line=cea, **data)
    finally:
        for name, value in saved.items():
            setattr(PR, name, value)


def boom(*a, **k):
    raise RuntimeError("sabotage")


# ---------------------------------------------------------------------------
print("\n[1] the invariant: no generated building without BOTH the inset and the band")

for label, kw in [
        ("healthy, landed", {}),
        ("healthy, Home/condo (two bands)", {"cea": CEA}),
        ("no real photograph -> GUARD 1", {"photo": None}),
        ("inset draws nothing -> GUARD 3",
         {"patch": {"_potential_before_inset": lambda *a, **k: 0}}),
        ("inset is only a thumbnail -> GUARD 3",
         {"patch": {"MIN_BEFORE_INSET_AREA": 10_000_000}}),
        ("inset raises -> caught", {"patch": {"_potential_before_inset": boom}}),
        ("no wording fits the band -> GUARD 2", {"patch": {"DISCLOSURE_SIZE_LO": 400}}),
        ("the CEA line will not set -> GUARD 2",
         {"cea": CEA, "patch": {"_disclosure_spec": boom}}),
        ("band drawing raises -> caught", {"patch": {"_add_disclosure_bands": boom}}),
        ("band reports itself blank -> GUARD 4",
         {"patch": {"_bands_are_legible": lambda *a, **k: False}}),
        ("a 3-character disclosure is not one -> GUARD 4",
         {"patch": {"POTENTIAL_DISCLOSURE": "xxx",
                    "POTENTIAL_DISCLOSURE_SHORT": "xxx"}}),
]:
    img = render(**kw)
    used, band = generated_used(img), has_band(img)
    check(f"{label}: " + ("concept + band" if used else "fell back to the real photo"),
          (used and band) or not used, f"used={used} band={band}")

check("every outcome is still a 1200x1500 poster (Instagram 4:5)",
      all(render(**kw).size == (1200, 1500)
          for kw in ({}, {"cea": CEA}, {"photo": None}, {"makeover": None})))
check("a fallback render carries NO AI band and no BEFORE inset",
      not has_band(render(makeover=None)) and not generated_used(render(makeover=None)))
check("the concept copy is used ONLY when there is a concept",
      PR.POTENTIAL_HEADLINE not in PR._bold_defaults(
          "SEMI-DETACHED", "DISTRICT 15", None, None, None, [])[0])

# ---------------------------------------------------------------------------
print("\n[2] headline legibility, measured on the hero rather than assumed")


def contrast_vs_white(level):
    c = level / 255.0
    lum = c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 1.05 / (lum + 0.05)


seen = []
_real_scrim = PR._legible_type_scrim


def spy(base, box, fade_top, hold_to):
    out = _real_scrim(base, box, fade_top, hold_to)
    seen.append(out)
    return out


PR._legible_type_scrim = spy
try:
    long_head = ("A RARE FREEHOLD SEMI-DETACHED HOME ON MUGLISTON ROAD IN THE HEART "
                 "OF KATONG, DISTRICT 15")
    worst = 99.0
    for label, kw in [("concept", {}), ("FALLBACK", {"makeover": None}),
                      ("no photo at all", {"photo": None, "makeover": None}),
                      ("92-char headline", {"headline": long_head}),
                      ("no facts", {"facts": [], "district": ""})]:
        seen.clear()
        render(**kw)
        alpha, before, after = seen[0]
        ratio = contrast_vs_white(after)
        worst = min(worst, ratio)
        check(f"{label}: white headline reads at {ratio:.1f}:1", ratio >= 4.5,
              f"scrim={alpha:.2f} worst patch {before}->{after}")
    check("worst case across all of them clears the 4.5:1 bar", worst >= 4.5,
          f"{worst:.1f}:1")
finally:
    PR._legible_type_scrim = _real_scrim

check("brightness is measured LOCALLY, so a small bright patch cannot hide",
      PR._bright_level(REAL, (0, 0, 640, 480)) > 240,
      "a white block on a mid-grey field reads bright")

# ---------------------------------------------------------------------------
print("\n[3] the two bands compose, and neither pushes the other off")

one = PR._potential_band_specs("")
two = PR._potential_band_specs(CEA)
longcea = PR._potential_band_specs(
    CEA + "  |  Marine Parade Branch, 2 Mugliston Road, Singapore 437722")
check("landed: one band, the AI disclosure", len(one) == 1)
check("Home/condo: two bands, AI disclosure above the CEA line",
      len(two) == 2 and two[0]["text"] == one[0]["text"])
check("the AI band is the SAME size with and without the CEA line beside it",
      two[0]["size"] == one[0]["size"] and two[0]["height"] == one[0]["height"],
      f"{one[0]['size']}px either way")
check("a very long CEA line shrinks ITSELF and never the disclosure",
      longcea[0]["size"] == one[0]["size"] and longcea[1]["size"] < two[1]["size"],
      f"cea {two[1]['size']}px -> {longcea[1]['size']}px")
check("the disclosure never sets below its legibility floor",
      all(s[0]["size"] >= PR.DISCLOSURE_SIZE_LO for s in (one, two, longcea)))
check("the longest wording that fits is the one chosen",
      one[0]["text"] == PR.POTENTIAL_DISCLOSURE)
check("both bands fit inside the poster with the artwork scaled, not covered",
      sum(s["height"] for s in two) < PR.H // 4)

# ---------------------------------------------------------------------------
print("\n[4] the cost ceiling, the cache and what fal CHARGED for")


def store(role, key, data):
    return f"https://example.invalid/{role}/{key}.jpg"


def ok(prompt, urls, deadline):
    return b"\xff\xd8jpeg"


class Billed(PR.fal_images.FalTemporary):
    images_billed = 2


def incident(prompt, urls, deadline):
    raise Billed("every collection attempt failed")


def busy(prompt, urls, deadline):
    raise PR.fal_images.FalRateLimited("at this account's concurrency limit")


SRC = "https://example.invalid/listing/exterior.jpg"
os.environ["FAL_KEY"] = "local-injection-not-a-real-key"
try:
    first = PR.makeover_image(SRC, {}, ok, store)
    check("one press buys exactly one image", first.url and first.images_paid == 1
          and round(first.cost_usd, 4) == round(PR.fal_images.COST_PER_IMAGE_USD, 4),
          f"${first.cost_usd}")
    again = PR.makeover_image(SRC, first.cache, ok, store)
    check("a re-render is FREE -- the cache is the cost control",
          again.url == first.url and again.images_paid == 0 and again.from_cache)
    inc = PR.makeover_image(SRC, {}, incident, store)
    check("an fal incident counts what was BILLED, not what arrived",
          inc.url is None and inc.images_paid == 2 and inc.images_lost == 2,
          f"${inc.cost_usd} for nothing")
    lim = PR.makeover_image(SRC, {}, busy, store)
    check("a rate limit bills nothing and degrades rather than waiting",
          lim.url is None and lim.images_paid == 0 and lim.rate_limited and lim.note)
    over = PR.makeover_image(SRC, {}, ok, store,
                             makeovers_used_this_month=PR.MAX_POTENTIAL_MAKEOVERS_PER_AGENT_PER_MONTH)
    check("the per-agent monthly ceiling stops the spend", over.url is None
          and over.images_paid == 0 and "allowance" in over.note)
    kept = PR.makeover_image(SRC, first.cache, ok, store, makeovers_used_this_month=999)
    check("a cached concept is never refused by the ceiling",
          kept.url == first.url and kept.images_paid == 0)

    def bad_store(role, key, data):
        raise RuntimeError("upload failed")

    lost = PR.makeover_image(SRC, {}, bad_store and ok, bad_store)
    check("paid for and unstorable is counted as paid AND as lost",
          lost.url is None and lost.images_paid == 1 and lost.images_lost == 1)
    check("makeover_image never raises, whatever happens",
          PR.makeover_image(None, {}, boom, store).url is None)
finally:
    os.environ.pop("FAL_KEY", None)

check("the prompt is screened before it can be sent, and passes",
      PR.fal_images.screen_prompt(PR.POTENTIAL_MAKEOVER_PROMPT) is None)
check("the prompt forbids inventing a house number or any lettering",
      "no house number" in PR.POTENTIAL_MAKEOVER_PROMPT
      and "sign or lettering anywhere" in PR.POTENTIAL_MAKEOVER_PROMPT
      and "stays exactly as it is" in PR.POTENTIAL_MAKEOVER_PROMPT)
# The neighbours' BUILDINGS are the guarantee, not the clutter in front of them.
# Rewritten 2026-11-17 and the loosening is deliberate: the first real generation
# changed almost nothing, because "keep the road and the kerb exactly as
# photographed, not tidied" contradicted the instruction to clear the lorry and the
# tarpaulin, and the model resolved the contradiction by doing nothing. Transient
# objects may now be cleared wherever they stand; a neighbour's HOUSE still may not
# be rebuilt, restyled or given a new roof, which is the line Jane actually drew.
check("the prompt leaves the neighbouring buildings alone",
      "neighbouring buildings" in PR.POTENTIAL_MAKEOVER_PROMPT
      and "neither rebuilt nor restyled" in PR.POTENTIAL_MAKEOVER_PROMPT
      and "same shape and the same height" in PR.POTENTIAL_MAKEOVER_PROMPT)
check("the prompt still forbids adding anything that is not there",
      "ADD NOTHING" in PR.POTENTIAL_MAKEOVER_PROMPT
      and "no cars" in PR.POTENTIAL_MAKEOVER_PROMPT)
check("the prompt keeps the real sky, per the no-sky-replacement rule",
      "Keep the sky" in PR.POTENTIAL_MAKEOVER_PROMPT)
check("the prompt keeps the building honest (same structure and camera)",
      "same camera position" in PR.POTENTIAL_MAKEOVER_PROMPT
      and "footprint" in PR.POTENTIAL_MAKEOVER_PROMPT)
check("the ceiling is declared in the unit fal bills, with the columns beside it",
      PR.POTENTIAL_USAGE_MONTH_COLUMN == "potential_makeovers_month"
      and PR.POTENTIAL_USAGE_COUNT_COLUMN == "potential_makeovers_used")

# ---------------------------------------------------------------------------
print("\n[5] the registry: a style cannot be given a concept without the guards")

check("every registered concept style is also a template",
      all(tid in PR.TEMPLATES and tid in PR.CONTENT_TEMPLATES for tid in PR.CONCEPT_STYLES),
      sorted(PR.CONCEPT_STYLES))
check("the old id-matching set is gone, so nothing turns on a string two files away",
      not hasattr(PR, "DISCLOSURE_TEMPLATES"))

# THE TEST THAT CATCHES THE FAILURE F12 DESCRIBES. If somebody registers a second style
# whose layout draws no inset or no band, this fails -- it renders every registered style
# with a generated image and looks at the finished pixels.
for tid, entry in PR.CONCEPT_STYLES.items():
    img, used = entry(property_type="SEMI-DETACHED", district="D15",
                      price_text="S$11,000,000", stats=[], agent_name="Janel Chee",
                      agent_contact_line="8123 0889", property_photo=REAL,
                      agent_photo=None, facts=LISTING["facts"], accent="cobalt",
                      makeover_photo=FAKE, compliance_line="")
    check(f"registered style {tid!r}: reports concept_used and really carries a band",
          used is True and has_band(img) and generated_used(img),
          f"used={used} band={has_band(img)}")
    img2, used2 = entry(property_type="SEMI-DETACHED", district="D15",
                        price_text="S$11,000,000", stats=[], agent_name="Janel Chee",
                        agent_contact_line="8123 0889", property_photo=REAL,
                        agent_photo=None, facts=LISTING["facts"], accent="cobalt",
                        makeover_photo=None, compliance_line="")
    check(f"registered style {tid!r}: reports FALSE when it fell back",
          used2 is False and not generated_used(img2))

check("a template that is NOT registered is never handed a makeover",
      "editorial" not in PR.CONCEPT_STYLES and "bold-base-row" not in PR.CONCEPT_STYLES)

# ---------------------------------------------------------------------------
print("\n[6] the API's claim matches the pixels, and the cache key tracks the prompt")

res = PR.render_poster_report(
    "SEMI-DETACHED", "D15", "S$11,000,000", [], "Janel Chee", "8123 0889",
    template_id="editorial")
check("an ordinary template never claims a concept",
      isinstance(res, PR.PosterResult) and res.concept_used is False
      and res.image.size == (PR.W, PR.H))
check("render_poster still returns a bare image, so existing callers are unchanged",
      PR.render_poster("SEMI-DETACHED", "D15", "S$11,000,000", [], "Janel Chee",
                       "8123 0889", template_id="editorial").size == (PR.W, PR.H))

check("the cache key carries a fingerprint of the prompt itself",
      PR.POTENTIAL_PROMPT_FINGERPRINT in PR.potential_cache_key("https://x/y.jpg"))
_was = PR.POTENTIAL_MAKEOVER_PROMPT
_key_before = PR.potential_cache_key("https://x/y.jpg")
try:
    PR.POTENTIAL_MAKEOVER_PROMPT = _was + " And paint the gate blue."
    PR.POTENTIAL_PROMPT_FINGERPRINT = PR.hashlib.sha1(
        PR.POTENTIAL_MAKEOVER_PROMPT.encode("utf-8")).hexdigest()[:8]
    check("rewording the prompt changes the key, with no version bump to remember",
          PR.potential_cache_key("https://x/y.jpg") != _key_before)
finally:
    PR.POTENTIAL_MAKEOVER_PROMPT = _was
    PR.POTENTIAL_PROMPT_FINGERPRINT = PR.hashlib.sha1(
        _was.encode("utf-8")).hexdigest()[:8]
check("the fingerprint in the module really is this prompt's",
      PR.POTENTIAL_PROMPT_FINGERPRINT == PR.hashlib.sha1(
          PR.POTENTIAL_MAKEOVER_PROMPT.encode("utf-8")).hexdigest()[:8])

# ---------------------------------------------------------------------------
print("\n[7] approvals: an allowlist, so nothing publishes unreviewed")

K1, K2 = "key-one", "key-two"
blob = PR.write_makeover_cache({K1: "https://a", K2: "https://b"}, [K1])
check("an approved key round-trips; an unapproved one stays unapproved",
      PR.read_makeover_approvals(blob) == {K1}
      and set(PR.read_makeover_cache(blob)) == {K1, K2})
check("making a concept does NOT approve it -- approval is a separate act",
      PR.read_makeover_approvals(PR.write_makeover_cache({K1: "https://a"})) == set())
check("a blob from before approvals existed approves NOTHING (fail safe)",
      PR.read_makeover_approvals({"version": 1, "images": {K1: "https://a"}}) == set())
check("junk in the approved slot approves nothing",
      all(PR.read_makeover_approvals({"images": {}, "approved": junk}) == set()
          for junk in ("yes", 7, None, {"a": 1})))
check("an approval cannot outlive the picture it refers to",
      PR.read_makeover_approvals(PR.write_makeover_cache({K2: "https://b"}, [K1])) == set())
check("a reworded prompt changes every key, so yesterday's yes cannot carry over",
      PR.potential_cache_key("https://x/y.jpg").startswith(
          f"v{PR.POTENTIAL_PROMPT_VERSION}.{PR.POTENTIAL_PROMPT_FINGERPRINT}"
          f".{PR.POTENTIAL_CROP_FINGERPRINT}:"))
check("the key still fits the 128 characters generate-poster accepts it back in",
      len(PR.potential_cache_key("https://x/" + "y" * 400 + ".jpg")) <= 128)

# ---------------------------------------------------------------------------
print("\n[8] the crop: a framing problem is fixed by framing, not by the model")

# Two photographs built so the framing question has a known right answer. "Busy" is
# coarse blocks rather than fine lines, because the tightness probe measures a 160x120
# reduction and anything finer would average away into flat grey.
def photo(w, h, centre=True, edges=False, shade=(150, 150, 145)):
    img = Image.new("RGB", (w, h), shade)
    d = PR.ImageDraw.Draw(img)
    step, bar = max(8, w // 16), max(4, w // 32)
    def bars(x0, x1):
        for x in range(int(x0), int(x1), step):
            d.rectangle((x, 0, x + bar, h), fill=(30, 30, 30))
    if centre:
        bars(w * 0.2, w * 0.8)
    if edges:
        bars(0, w * 0.15)
        bars(w * 0.85, w)
    return img


# The shape of Jane's Mugliston exterior: 4:3, the house through the middle, a plain
# lorry flank and a tarpaulin at the edges.
WIDE = photo(640, 480)
box, why = PR.crop_plan(WIDE)
check("a wide shot with plain edges is cropped to the middle", box is not None, why)
if box:
    x0, y0, x1, y1 = box
    check("the crop lands near the one Jane made by hand (420x330 of 640x480)",
          abs((x1 - x0) - 420) <= 40 and abs((y1 - y0) - 330) <= 40,
          f"{x1 - x0}x{y1 - y0}")
    check("it is horizontally centred and keeps more of the top than the bottom",
          abs(x0 - (640 - (x1 - x0)) / 2) <= 1 and y0 < (480 - (y1 - y0)) / 2,
          f"x0={x0} y0={y0}")
    check("it never reaches outside the photograph",
          0 <= x0 < x1 <= 640 and 0 <= y0 < y1 <= 480)

check("a frame whose subject runs to the edges is LEFT ALONE",
      PR.crop_plan(photo(640, 480, edges=True))[0] is None,
      PR.crop_plan(photo(640, 480, edges=True))[1])
check("a portrait or square frame has no side room, so it is left alone",
      PR.crop_plan(photo(480, 640))[0] is None and PR.crop_plan(photo(600, 600))[0] is None,
      PR.crop_plan(photo(480, 640))[1])
check("a photo too small to spare the pixels is left alone",
      PR.crop_plan(photo(400, 300))[0] is None, PR.crop_plan(photo(400, 300))[1])
check("no photo at all is not a crash", PR.crop_plan(None)[0] is None)
check("an unmeasurable photo is left alone, not guessed at",
      PR._frame_is_tight("not an image") is True)
check("the verdict does not depend on the source's resolution",
      (PR.crop_plan(photo(640, 480))[0] is not None)
      == (PR.crop_plan(photo(1280, 960))[0] is not None)
      == (PR.crop_plan(photo(2560, 1920))[0] is not None))

big = photo(2560, 1920)
data, size = PR.cropped_jpeg(big, PR.crop_plan(big)[0])
check("the uploaded crop is capped, and still far bigger than the 1200px hero",
      max(size) <= PR.CROP_MAX_EDGE and min(size) >= 1200 and data[:2] == b"\xff\xd8",
      f"{size[0]}x{size[1]}, {len(data) // 1024}KB")

# --- the crop's failure modes all land on "send the original" --------------
ROLES = []


def store_roles(role, key, data):
    ROLES.append(role)
    return f"https://example.invalid/{role}/{key}.jpg"


def boom_fetch(url):
    raise RuntimeError("the CDN is having a day")


for label, fetch in [("the photo cannot be fetched", boom_fetch),
                     ("there is no photo to crop", lambda u: None),
                     ("the frame is already tight",
                      lambda u: photo(640, 480, edges=True))]:
    url, why = PR._concept_source_url(SRC, "k", fetch, store_roles)
    check(f"{label}: fal is sent the ORIGINAL photograph", url == SRC, why)

url, why = PR._concept_source_url(SRC, "k", lambda u: WIDE, boom)
check("the crop cannot be stored: fal is sent the original photograph", url == SRC, why)
ROLES.clear()
url, why = PR._concept_source_url(SRC, "k", lambda u: WIDE, store_roles)
check("a crop that worked is uploaded under its own role, never the concept's",
      url != SRC and ROLES == [PR.POTENTIAL_SOURCE_ROLE]
      and PR.POTENTIAL_SOURCE_ROLE != PR.POTENTIAL_ROLE, (url, ROLES))

# --- and what fal is actually asked to work from ---------------------------
SENT = []


def watch(prompt, urls, deadline):
    SENT.append(list(urls))
    return b"\xff\xd8jpeg"


os.environ["FAL_KEY"] = "local-injection-not-a-real-key"
try:
    SENT.clear()
    res = PR.makeover_image(SRC, {}, watch, store_roles, fetch_photo=lambda u: WIDE)
    check("the CROPPED copy is what fal is given, not the original",
          res.url and SENT and SENT[-1] != [SRC] and len(SENT[-1]) == 1, SENT)
    SENT.clear()
    PR.makeover_image(SRC, {}, watch, store_roles)
    check("no fetcher injected = no crop: the original url, exactly as before",
          SENT[-1] == [SRC], SENT)

    # The crop shares the generation's budget rather than extending it.
    SENT.clear()
    cropped = PR.makeover_image(SRC, {}, watch, store_roles, fetch_photo=lambda u: WIDE,
                                deadline=PR.time.monotonic() + 20)
    check("too little budget left to crop: the original goes, and a poster still comes out",
          cropped.url and SENT[-1] == [SRC], SENT)

    SENT.clear()
    broke = PR.makeover_image(SRC, {}, watch, store_roles, fetch_photo=lambda u: WIDE,
                              deadline=PR.time.monotonic() + 2)
    check("past the floor: NOTHING is submitted, so nothing is billed",
          broke.url is None and broke.images_paid == 0 and not SENT
          and "allowance" in broke.note, (broke, SENT))
finally:
    os.environ.pop("FAL_KEY", None)

# --- and the key, which is what stops an old yes publishing a new picture ---
check("the cache key carries the crop rule's fingerprint",
      PR.POTENTIAL_CROP_FINGERPRINT in PR.potential_cache_key(SRC))
_was_keep = PR.CROP_KEEP_W
_key_before = PR.potential_cache_key(SRC)
_approved_before = PR.write_makeover_cache({_key_before: "https://cdn/c.jpg"},
                                           [_key_before])
try:
    PR.CROP_KEEP_W = 0.5
    PR.POTENTIAL_CROP_FINGERPRINT = "deadbeef"
    check("retuning the crop changes every key, so a differently framed concept is a miss",
          PR.potential_cache_key(SRC) != _key_before)
    check("and yesterday's approval cannot carry over to the new framing",
          PR.potential_cache_key(SRC)
          not in PR.read_makeover_approvals(_approved_before))
finally:
    PR.CROP_KEEP_W = _was_keep
    PR.POTENTIAL_CROP_FINGERPRINT = PR._crop_fingerprint()
check("the fingerprint in the module really is this crop rule's",
      PR.POTENTIAL_CROP_FINGERPRINT == PR._crop_fingerprint()
      and PR.potential_cache_key(SRC) == _key_before)

# ---------------------------------------------------------------------------
print("\n[9] the facts: land size is on the poster, and nothing is invented")

LANDED = [{"icon": "plot", "label": "Land size", "value": "5,573", "unit": "sqft"},
          {"icon": "house", "label": "Built-up", "value": "5,868", "unit": "sqft"},
          {"icon": "bed", "label": "Rooms", "value": "5", "unit": ""},
          {"icon": "tag", "label": "Price", "value": "1,875", "unit": "psf"}]

labels = [f["label"] for f in PR._bold_facts(LANDED)]
check("all four facts survive, land size first", labels == ["LAND SIZE", "BUILT-UP", "ROOMS", "PRICE"],
      labels)
no_land = [dict(LANDED[0], value=""), *LANDED[1:]]
check("no land area recorded: three facts, not an empty slot",
      [f["label"] for f in PR._bold_facts(no_land)] == ["BUILT-UP", "ROOMS", "PRICE"])
check("a zero land area is 'never filled in', not a claim of no land",
      [f["label"] for f in PR._bold_facts([dict(LANDED[0], value="0"), *LANDED[1:]])]
      == ["BUILT-UP", "ROOMS", "PRICE"])
check("the land size really reaches the pixels (the poster differs without it)",
      render(facts=LANDED).tobytes() != render(facts=no_land).tobytes())
check("every fact the poster can draw has an icon that exists",
      all(f["icon"] in PR._BOLD_ICONS for f in LANDED))

print(f"\n{PASSED} passed, {FAILED} failed")
sys.exit(1 if FAILED else 0)
