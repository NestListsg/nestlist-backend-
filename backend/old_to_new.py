"""The "after" photographs for a From Old to New video: planning, prompts, cost, cache.

Read docs/old-to-new-video-spec.md first. That file is the recipe; this one is the half
of it that costs money.

WHY WE GENERATE THE AFTER RATHER THAN ASK FOR IT
Both old-to-new effects need two photographs of each room from the SAME camera position.
Asking agents to upload an "after" photo would make that a thing to CHECK, and a mismatch
is not a small fault: the Dramatic flight's mask becomes nonsense and the render melts.
Generating the after FROM the before makes the two frames share a camera position by
construction. There is deliberately no upload path for after photos.

THE ORDER IS LOAD-BEARING
    1. the exterior, anonymised   -- the agent's real photo with ONLY the neighbours
                                     replaced. Jane's requirement, and it must be the
                                     first frame on screen, not a later correction.
    2. the exterior, rebuilt      -- generated FROM (1), never from the original.
    3. one redesigned image per chosen room.
Everything after (1) inherits its street, sky and neighbours from (1), which is why the
whole film looks like one house on one street. Generating the rebuild from the ORIGINAL
would put the real neighbours back on screen.

WHAT HAPPENS WHEN IT DOES NOT WORK
Nothing here raises into a render. Every image is independently optional:
  * the anonymised exterior fails  -> the film has NO exterior at all. It does NOT fall
    back to the real photograph: that would put the neighbours Jane wants hidden on
    screen, which is the one failure mode worse than a missing shot.
  * a rebuild fails                -> the opening still plays, as a single real shot.
  * a room's after fails           -> that room plays as a plain dissolve of the real
    photo. A refusal (see the prompt-safety note in the spec) lands here.
  * the budget is short            -> the plan is TRIMMED to what fits, cheapest-first by
    value: exterior pair, then rooms in order. Never a half-paid room.
Every one of those is reported in plain language so the agent is told what their video
did not do.

COST
Measured at fal_images.COST_PER_IMAGE_USD an image, so a full six-image listing costs
about 24 US cents. The ceiling is checked ONCE, up front, before the first generation --
not discovered partway through a listing. See "Cost ceiling" below.
"""
import hashlib
import json
import logging
import re
import time

import fal_images

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost ceiling
#
# WHAT ONE LISTING COSTS. Six images -- the exterior pair plus one per room, four rooms
# being the number of treatments the Dramatic renderer rotates through:
#
#     6 images x US$0.04 = US$0.24 per listing, paid ONCE (the cache below)
#
# THE PER-AGENT CEILING, which is the one the brief asked for. Expressed in LISTINGS
# because that is the unit an agent and Jane think in, with the image ceiling derived so
# the two cannot drift:
#
#     40 listings/agent/month  =  240 images  =  about US$9.60 per agent per month
#
# 40 is generous against observed behaviour -- Janel posts a handful of real listings a
# month -- and bounded: fifty agents all hitting the ceiling is about US$480 a month,
# which is a number Jane can decide about rather than discover. It is NOT an accountant,
# and it is not the authoritative cap. The authoritative cap belongs on fal's own billing
# spend limit, which no bug on our side can outrun, exactly as photo_upscale says of
# Replicate.
#
# HOW "ALREADY USED" IS COUNTED, and why it is counted the way it is. The caller supplies
# it from the video_jobs table: the number of DISTINCT listings this agent has had an
# old-to-new job for this calendar month, excluding the listing being rendered now. That
# makes the count durable and fleet-wide (a process-local counter would be four separate
# ceilings on a four-worker Railway instance), and it is an UPPER BOUND on real spend --
# a listing is only ever paid for once, however many times it is regenerated, because of
# the cache. Excluding the current listing matters: a regenerate must never be refused by
# its own earlier render.
# ---------------------------------------------------------------------------
MAX_ROOMS = 4
IMAGES_PER_LISTING = 2 + MAX_ROOMS          # the exterior pair, then the rooms
MAX_LISTINGS_PER_AGENT_PER_MONTH = 40
MAX_IMAGES_PER_AGENT_PER_MONTH = (
    MAX_LISTINGS_PER_AGENT_PER_MONTH * IMAGES_PER_LISTING)

# Bump to regenerate every listing's after images with a changed prompt. Part of every
# cache key, so a bump is a cache miss rather than a migration.
PROMPT_VERSION = 1

# Wall-clock ceiling on the WHOLE generation pass for one listing, shared across every
# image. One shared deadline rather than one per image, deliberately: a slow first image
# then eats into the plan instead of extending the agent's wait six times over. Sized
# against fal's healthy 10-25s an image with room for a queue, and well inside
# video_renderer's own RENDER_BUDGET_SECONDS so the film still gets rendered.
TOTAL_BUDGET_SECONDS = 240

# Wall-clock ceiling on the one vision call that reads the photos. It only buys STYLE --
# what to clear out of each room and what to put in. Past it we use the house default
# below, which makes a perfectly good video.
DESCRIBE_TIMEOUT_SECONDS = 60
DESCRIBE_BUDGET_SECONDS = 90


class BudgetExhausted(Exception):
    """This agent has no generation allowance left this month.

    The one failure here that is not a degradation: there is no Dramatic video without
    an after image, so the caller turns this into a plain-language permanent failure and
    points the agent at the Classic."""


# ---------------------------------------------------------------------------
# The prompts.
#
# THE RULES THESE OBEY, all of them paid for in rework (docs/old-to-new-video-spec.md):
#   * Say what should HAPPEN, never what should not. fal's safety layer refuses words
#     like "explosion" or "debris" EVEN AS EXCLUSIONS, so there are no exclusions here
#     at all -- only positive instructions about what stays and what changes.
#   * Name each neighbouring building INDIVIDUALLY. A general "vary the neighbours"
#     leaves the recognisable one untouched, which defeats the whole point.
#   * Pin the camera explicitly in every prompt. The two frames sharing a camera
#     position is what makes the flight work; an editor left to itself will happily
#     redraw the room from a nicer angle and lose the curtains with it.
# ---------------------------------------------------------------------------

# Where a neighbouring building sits, when the vision pass could not look. Naming a
# position that has no building is harmless -- there is nothing there to change -- so
# this is a safe default rather than a guess that can go wrong.
DEFAULT_NEIGHBOUR_POSITIONS = ("to the left", "to the right", "behind it")

# What to clear and what to put in, when the vision pass could not look. A calm
# contemporary scheme described in real objects, which is what makes an editor move
# furniture rather than wash one picture into another.
DEFAULT_OLD_CONTENTS = ("the existing furniture, rugs, wall decoration and window "
                        "dressing")
DEFAULT_NEW_CONTENTS = ("a low cream sofa, a slim stone coffee table, a soft pale rug, "
                        "floor-length linen curtains, a tall potted plant in the corner "
                        "and one large framed artwork on the wall")


def anonymise_prompt(neighbour_positions=None):
    """The agent's real exterior photograph with ONLY the neighbours replaced.

    The subject house is pinned feature by feature, because this frame is a REAL
    PHOTOGRAPH of the property and carries the "Neighbouring homes altered for privacy"
    line rather than the AI notice. If the editor touched the house itself, that line
    would be a false statement about the image.
    """
    positions = list(neighbour_positions or DEFAULT_NEIGHBOUR_POSITIONS)
    each = " ".join(
        f"Replace the building {position} with a different house of similar height, "
        f"roof shape and distance from the road."
        for position in positions)
    return (
        "Keep the house in the centre of this photograph exactly as it is: every window, "
        "door, roofline, wall, gate, driveway, plant and parked car of it stays in the "
        "same place, the same colour and the same light, pixel for pixel. "
        + each +
        " Keep the road, the kerb, the pavement, the trees, the sky and the daylight "
        "exactly as photographed. Same camera position, same framing, same angle, same "
        "distance. Photographic and realistic, like the original photograph."
    )


def rebuild_prompt():
    """The rebuilt house, generated FROM the anonymised frame.

    Everything outside the plot is pinned, because this image and the one it came from
    are cut together in the film: the street has to be the same street.
    """
    return (
        "Rebuild the house in the centre of this photograph as a brand new modern home "
        "on the same plot, standing in the same place and the same footprint. Give it "
        "clean white rendered walls, full-height glazing, a flat roofline, a slatted "
        "timber entrance, a low planted front wall and soft architectural lighting. "
        "Keep the road, the kerb, the pavement, the neighbouring buildings, the trees, "
        "the sky and the daylight exactly as they are in this photograph. Same camera "
        "position, same framing, same angle, same distance. Photographic and realistic, "
        "like estate agency architectural photography."
    )


def room_prompt(old_contents=None, new_contents=None):
    """One room redesigned from the same camera position.

    The structure of the room is pinned hard. That is not a style preference: the
    Dramatic flight works by taking the pixels that DIFFER between the two frames as its
    mask, so a redrawn wall or a moved window is not a worse-looking after, it is a
    broken effect.
    """
    return (
        "Redesign this room from the same camera position: the same framing, the same "
        "angle, the same distance and the same lens. Keep the room's structure exactly "
        "as photographed -- the walls, the ceiling and its height, the floor and its "
        "level, the doorways, and the windows with the same view through them all stay "
        "where they are. Change the furnishings: in place of "
        f"{old_contents or DEFAULT_OLD_CONTENTS}, furnish the room with "
        f"{new_contents or DEFAULT_NEW_CONTENTS}. Light it warmly and evenly. "
        "Photographic and realistic, like estate agency interior photography."
    )


# ---------------------------------------------------------------------------
# The vision pass: what is in each room now, and what should go in instead.
#
# This is STYLE ONLY. Every failure here -- no key, a timeout, an unparseable reply, a
# description carrying a word fal refuses -- falls back to the house default above, which
# makes a perfectly good video. It is never allowed to cost the feature.
# ---------------------------------------------------------------------------
_DESCRIBE_PROMPT = """You are preparing a before-and-after renovation video of a
Singapore property. You have been shown {n} photos of it, in order.

For each photo, write two short phrases:

  "old" -- the furniture and decoration actually visible in that photo, named as real
           objects: "the dark carved rosewood sofas with red cushions, the glass display
           cabinet and the lace table runner". Name only what you can see. If the room
           is empty, say "the bare floor and walls".
  "new" -- a calm contemporary scheme to put there instead, also named as real objects
           in the same way: "a low cream sofa, a marble coffee table, a soft wool rug,
           floor-length linen curtains and a potted olive tree".

Photo 1 is MEANT to be the EXTERIOR of the property, seen from the street. For that one,
instead of "old" and "new", give two things:

  "exterior" -- true if photo 1 really does show the outside of a building from
                outside it, false if it shows an interior room or anything else.
                Answer honestly: a wrong "true" here puts a rebuilt house in the middle
                of somebody's living room.
  "neighbours" -- a list of short phrases saying where each OTHER building in the photo
                sits relative to the property, for example ["to the left",
                "immediately to the right", "behind the roofline on the right"]. One
                entry per neighbouring building you can actually see, named
                individually. If you can see none, give an empty list.

Describe only what is in the photographs. Do not use the words explosion, blast, fire,
flames, burning, debris, rubble, demolish or destroy, in any form, for any reason.

Reply with JSON only, no other text, in exactly this shape:
{{"photos": [{{"n": 1, "exterior": true, "neighbours": ["...", "..."]}}, {{"n": 2, "old": "...", "new": "..."}}]}}
with one entry per photo, in order, for all {n} photos."""

# Long enough that a redesign reads as a real scheme, short enough that it cannot smuggle
# a paragraph into a generation prompt.
MAX_CONTENTS_CHARS = 320


def _clean_contents(value):
    """One model-written phrase -> something safe to put in a generation prompt, or None.

    Three things are checked, in order of how much they cost if missed: a word fal
    refuses (which would waste a paid request and a round trip), newlines and quotes
    (which make the prompt hard to read in logs), and length.
    """
    try:
        text = str(value or "").strip()
    except Exception:
        return None
    if not text:
        return None
    text = re.sub(r"\s+", " ", text).replace('"', "'")
    if len(text) > MAX_CONTENTS_CHARS:
        return None
    if fal_images.screen_prompt(text):
        # The model was told not to; it did anyway. Cheaper to notice here than to pay
        # fal to refuse it.
        logger.warning("a model-written room description used a word fal refuses; "
                       "using the house default for that room")
        return None
    return text


def describe_photos(photos, vision_call):
    """(neighbour positions, [(old, new) per photo], exterior is really an exterior).

    Never raises. `photos` are PIL images, photo 0 being the one we intend to use as the
    exterior. `vision_call(prompt, timeout, max_tokens, budget) -> raw text or None` is
    injected by video_renderer, so there is exactly one place in this codebase that
    knows about Anthropic keys, timeouts and retry policy.

    The third return value is a GUARD, not a nicety. The first chosen photo is used as
    the exterior because that is the agent's own hero photo and for landed property it
    essentially always is one -- but when it is not, anonymising "the neighbours" in a
    living room and then rebuilding "the house in the centre" of it produces something
    nonsensical that would go out carrying an AI notice. Defaults to True whenever we
    could not look, because then the heuristic is all we have.
    """
    blank = [(None, None)] * len(photos)
    raw = vision_call(_DESCRIBE_PROMPT.format(n=len(photos)),
                      DESCRIBE_TIMEOUT_SECONDS,
                      256 + 160 * len(photos),
                      DESCRIBE_BUDGET_SECONDS)
    if not raw:
        logger.info("old-to-new: no room descriptions available; using the house "
                    "default scheme")
        return None, blank, True

    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        entries = json.loads(match.group(0) if match else raw)["photos"]
    except Exception as e:
        logger.warning("old-to-new: could not parse the description reply (%s); using "
                       "the house default scheme", e)
        return None, blank, True

    neighbours = None
    exterior_ok = True
    out = list(blank)
    for entry in entries:
        try:
            index = int(entry["n"]) - 1
        except Exception:
            continue
        if not 0 <= index < len(photos):
            continue
        if index == 0:
            if isinstance(entry.get("neighbours"), list):
                cleaned = [_clean_contents(x) for x in entry["neighbours"][:6]]
                cleaned = [x for x in cleaned if x]
                if cleaned:
                    neighbours = cleaned
            # Only an explicit false counts. A missing or unparseable answer leaves the
            # heuristic in place rather than silently dropping the opening.
            if entry.get("exterior") is False:
                exterior_ok = False
        out[index] = (_clean_contents(entry.get("old")),
                      _clean_contents(entry.get("new")))
    return neighbours, out, exterior_ok


# ---------------------------------------------------------------------------
# The cache.
#
# Keyed on the SOURCE PHOTO's url and the role, and deliberately NOT on the prompt text.
# The room descriptions are model-written and differ slightly every run, so a
# prompt-keyed cache would miss on every regenerate and pay again -- which is the one
# thing this cache exists to prevent. PROMPT_VERSION is in the key instead, so a
# deliberate change to the prompts regenerates and an incidental change of wording does
# not.
#
# A hit also means the vision pass can be SKIPPED entirely, which is most of the wait as
# well as most of the cost.
# ---------------------------------------------------------------------------
ROLE_EXTERIOR_ANON = "exterior_anon"
ROLE_EXTERIOR_NEW = "exterior_new"
ROLE_ROOM_NEW = "room_new"


def cache_key(role, source_url):
    """A short, stable key for one generated image. The source url is hashed rather than
    stored, so the cache cannot become a second place that knows a photo's address."""
    digest = hashlib.sha1(str(source_url or "").encode("utf-8")).hexdigest()[:20]
    return f"v{PROMPT_VERSION}:{role}:{digest}"


def read_cache(stored):
    """Whatever is on the listing -> {key: url}. Tolerates anything."""
    if not isinstance(stored, dict):
        return {}
    images = stored.get("images")
    if not isinstance(images, dict):
        return {}
    return {str(k): str(v) for k, v in images.items()
            if isinstance(v, str) and v.startswith("http")}


def write_cache(existing):
    """{key: url} -> the value to store on the listing."""
    return {"version": PROMPT_VERSION, "images": dict(existing)}


# ---------------------------------------------------------------------------
# The plan
# ---------------------------------------------------------------------------
class Plan:
    """What the Dramatic renderer should cut together.

    opening_url   -- the anonymised REAL photograph, or None. Carries the privacy line
                     and no AI notice. None means the film has no exterior at all.
    exterior_url  -- the rebuilt house, or None. Generated, so it carries the AI notice.
    rooms         -- [{"before_url", "after_url" or None, "space"}], in cut order.
    degradations  -- plain-language notes for the agent.
    cache         -- the full {key: url} map to store back on the listing.
    images_paid   -- how many images this pass actually generated (0 on a full cache
                     hit), so the caller can log what the render cost.
    """

    def __init__(self):
        self.opening_url = None
        self.exterior_url = None
        self.rooms = []
        self.degradations = []
        self.cache = {}
        self.images_paid = 0

    @property
    def transformable_rooms(self):
        return [r for r in self.rooms if r.get("after_url")]

    def __repr__(self):
        return ("Plan(opening=%s, exterior=%s, rooms=%d of which %d transformable, "
                "paid=%d)" % (bool(self.opening_url), bool(self.exterior_url),
                              len(self.rooms), len(self.transformable_rooms),
                              self.images_paid))


def reserve_budget(wanted, images_used_this_month):
    """How many of `wanted` images this agent may generate. Checked ONCE, up front.

    Returns a number between 1 and `wanted`, or raises BudgetExhausted when there is no
    allowance at all. Trimming rather than refusing is deliberate: an agent who is three
    images short of a full plan should get a shorter film, not an error.
    """
    allowance = MAX_IMAGES_PER_AGENT_PER_MONTH - max(0, int(images_used_this_month or 0))
    if allowance <= 0:
        raise BudgetExhausted(
            "You have made the most Old-to-New videos this account can make this month "
            f"({MAX_LISTINGS_PER_AGENT_PER_MONTH} listings). The Classic video is still "
            "available, and the limit resets at the start of next month.")
    return max(1, min(wanted, allowance))


def build_plan(exterior_url, room_urls, cache, generate_image, store_image,
               describe=None, images_used_this_month=0, deadline=None):
    """Generate (or recover from cache) every after image one listing needs.

    exterior_url  -- the listing's exterior photo, or None if it has none.
    room_urls     -- [(url, space label)] for the rooms to transform, in cut order.
    cache         -- {key: url} already on the listing (read_cache).
    generate_image(prompt, source_urls, deadline) -> bytes. fal_images.generate, injected
                     so this module is testable without a network or a key.
    store_image(role, key, data) -> public url. main.py owns storage; this module must
                     not import it.
    describe()    -> describe_photos' three-tuple. Called ONLY when something actually
                     has to be generated -- a full cache hit must not pay for a vision
                     call either, and that is most of the WAIT as well as most of the
                     cost. None means "use the house defaults".
    deadline      -- absolute time.monotonic(); defaults to TOTAL_BUDGET_SECONDS from now
                     and is SHARED by every generation in the pass.

    Never raises except BudgetExhausted. Everything else is a degradation on the plan.
    """
    plan = Plan()
    plan.cache = dict(cache or {})
    overall = deadline if deadline is not None else time.monotonic() + TOTAL_BUDGET_SECONDS

    room_urls = list(room_urls or [])[:MAX_ROOMS]

    # --- what is missing, in the order the spec requires -----------------------------
    # The exterior pair comes first and is not negotiable in ORDER: the rebuild is
    # generated from the anonymised frame, so the anonymised frame has to exist and be
    # publicly reachable before the rebuild can be asked for.
    wanted = []
    if exterior_url:
        wanted.append((ROLE_EXTERIOR_ANON, exterior_url))
        wanted.append((ROLE_EXTERIOR_NEW, exterior_url))
    for url, _space in room_urls:
        wanted.append((ROLE_ROOM_NEW, url))
    missing = [(role, url) for role, url in wanted
               if cache_key(role, url) not in plan.cache]

    # --- the ceiling, before the first generation -------------------------------------
    allowed = len(missing)
    if missing:
        allowed = reserve_budget(len(missing), images_used_this_month)
        if allowed < len(missing):
            logger.warning("old-to-new: budget allows %d of %d images for this listing",
                           allowed, len(missing))
            plan.degradations.append(
                "this account is near its monthly limit for Old-to-New videos, so part "
                "of this one was left as plain photos")

    # Trim from the END, so the exterior pair and the earliest rooms survive. The
    # exterior pair is trimmed as a PAIR: a rebuild with no anonymised frame to come
    # from is not a cheaper plan, it is an impossible one.
    budgeted = set()
    for role, url in missing[:allowed]:
        budgeted.add((role, url))
    if (ROLE_EXTERIOR_NEW, exterior_url) in budgeted and \
            (ROLE_EXTERIOR_ANON, exterior_url) not in budgeted and \
            cache_key(ROLE_EXTERIOR_ANON, exterior_url) not in plan.cache:
        budgeted.discard((ROLE_EXTERIOR_NEW, exterior_url))

    # --- descriptions, only if we are actually generating -----------------------------
    neighbours = None
    contents = [(None, None)] * len(room_urls)
    if budgeted and describe is not None:
        try:
            neighbours, described, exterior_ok = describe()
            if not exterior_ok and exterior_url:
                # The guard in describe_photos. Rebuilding "the house in the centre" of
                # a photo that is not a house produces something nonsensical, and it
                # would go out carrying an AI notice.
                logger.warning("old-to-new: the first chosen photo is not an exterior; "
                               "skipping the opening and treating it as a room")
                plan.degradations.append(
                    "the first photo is not an outside view, so the video has no "
                    "opening exterior -- put an exterior photo first to get one")
                budgeted = {b for b in budgeted if b[0] not in (ROLE_EXTERIOR_ANON,
                                                                ROLE_EXTERIOR_NEW)}
                exterior_url = None
            # described covers [exterior] + rooms; the rooms are what we need.
            if described:
                for i in range(len(room_urls)):
                    if i + 1 < len(described):
                        contents[i] = described[i + 1]
        except Exception as e:
            # Style only. Never the feature.
            logger.warning("old-to-new: the description pass failed (%s); using the "
                           "house default scheme", e)

    def _make(role, source_urls, prompt, key):
        """Generate one image, store it, cache it. Returns the url or None."""
        try:
            data = generate_image(prompt, source_urls, overall)
        except fal_images.FalNotConfigured as e:
            logger.warning("old-to-new: %s", e)
            return None
        except fal_images.FalRefused as e:
            # The expected, designed-for failure. A refusal is not a fault.
            logger.info("old-to-new: fal declined the %s image (%s)", role, e)
            return None
        except Exception as e:
            logger.warning("old-to-new: could not generate the %s image (%s: %s)",
                           role, type(e).__name__, e)
            return None
        plan.images_paid += 1
        try:
            url = store_image(role, key, data)
        except Exception as e:
            # We paid for this one and cannot keep it. Counted as paid above, so the
            # accounting is honest even though we got nothing.
            logger.warning("old-to-new: generated the %s image but could not store it "
                           "(%s)", role, e)
            return None
        if url:
            plan.cache[key] = url
        return url

    # --- the exterior pair ------------------------------------------------------------
    if exterior_url:
        anon_key = cache_key(ROLE_EXTERIOR_ANON, exterior_url)
        anon_url = plan.cache.get(anon_key)
        if not anon_url and (ROLE_EXTERIOR_ANON, exterior_url) in budgeted:
            anon_url = _make(ROLE_EXTERIOR_ANON, [exterior_url],
                             anonymise_prompt(neighbours), anon_key)
        if anon_url:
            plan.opening_url = anon_url
            new_key = cache_key(ROLE_EXTERIOR_NEW, exterior_url)
            new_url = plan.cache.get(new_key)
            if not new_url and (ROLE_EXTERIOR_NEW, exterior_url) in budgeted:
                # FROM the anonymised frame, never from the original. This is the line
                # that keeps the street identical across every beat of the film.
                new_url = _make(ROLE_EXTERIOR_NEW, [anon_url], rebuild_prompt(), new_key)
            plan.exterior_url = new_url
            if not new_url:
                plan.degradations.append(
                    "the new-build exterior could not be made, so the video opens on "
                    "the property as it is today")
        else:
            # NO FALLBACK TO THE REAL PHOTOGRAPH. Showing it would put the neighbours
            # Jane requires hidden back on screen, which is worse than a missing shot.
            plan.degradations.append(
                "the opening exterior shot could not be prepared with the neighbouring "
                "homes altered, so the video starts inside the property")

    # --- the rooms --------------------------------------------------------------------
    plain_rooms = 0
    for i, (url, space) in enumerate(room_urls):
        key = cache_key(ROLE_ROOM_NEW, url)
        after = plan.cache.get(key)
        if not after and (ROLE_ROOM_NEW, url) in budgeted:
            old_contents, new_contents = contents[i] if i < len(contents) else (None, None)
            after = _make(ROLE_ROOM_NEW, [url],
                          room_prompt(old_contents, new_contents), key)
        if not after:
            plain_rooms += 1
        plan.rooms.append({"before_url": url, "after_url": after, "space": space})
    if plain_rooms:
        plan.degradations.append(
            f"{plain_rooms} room(s) could not be redesigned, so they are shown as they "
            "are today")

    logger.info("old-to-new plan: %r, %d image(s) generated (about US$%.2f)",
                plan, plan.images_paid,
                plan.images_paid * fal_images.COST_PER_IMAGE_USD)
    return plan
