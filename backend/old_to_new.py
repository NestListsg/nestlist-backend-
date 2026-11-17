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
import concurrent.futures
import hashlib
import json
import logging
import re
import time

import fal_images
import fal_video

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Cost ceiling
#
# THE UNIT IS AN IMAGE, AND THAT IS THE WHOLE POINT OF THIS BLOCK.
#
# It used to be a listing: forty listings an agent a month, six images each, US$9.60.
# That number was wrong in the direction that costs money, because the bill is per image
# per press and a listing is neither. An audit measured it on 2026-10-22:
#
#   * re-picking which photo is the hero costs THREE more images (a new exterior pair and
#     a new room), because the cache is keyed on the source photo.
#   * one ten-photo listing, hero re-picked ten times, reached 25 images -- US$1.00 --
#     while still counting as ONE of the forty.
#   * with the cache column absent (the state this feature ships in until Jane runs
#     docs/old-to-new-images-migration.sql), five presses of the same listing paid five
#     times: US$1.20, still counted as one.
#
# Forty of those is about US$40 an agent a month, not US$9.60 -- and the count was
# re-opening roughly weekly on top of that (see "HOW ALREADY USED IS COUNTED"), so the
# true worst case was nearer US$160 an agent and US$8,000 across fifty agents, against a
# quoted US$480. So we count what is billed.
#
#     ONE IMAGE                     = US$0.04
#     ONE FULL LISTING, first press = 6 images (the exterior pair + one per room) = $0.24
#     THE CEILING                   = 240 images per agent per calendar month = US$9.60
#
# 240 images is about forty listings IF every press is a first press, which is why the
# money ceiling is unchanged and the headline number Jane has already decided about
# (fifty agents x US$9.60 = US$480 a month) still holds. The difference is that it now
# holds whatever agents actually do: an agent who re-picks photos on one listing spends
# the same allowance faster rather than spending it invisibly.
#
# It is NOT an accountant, and it is not the authoritative cap. The authoritative cap
# belongs on fal's own billing spend limit, which no bug on our side can outrun, exactly
# as photo_upscale says of Replicate.
#
# HOW "ALREADY USED" IS COUNTED, and why it changed. The caller supplies a month-to-date
# IMAGE TOTAL kept on the AGENT row (main.py's _old_to_new_images_used). It used to count
# rows in video_jobs, which was durable-looking and was not: prune_finished() deletes
# finished rows after PRUNE_AFTER_DAYS = 7, so from the 8th of the month the counter
# could only ever see a week, and the ceiling quietly reset about weekly -- roughly four
# times the quoted allowance. A total on the agent row cannot be pruned, counts the
# in-process fallback renderer (which writes no job row at all) just as happily, and
# counts IMAGES, which is the thing with a price.
#
# A regenerate still cannot be refused by its own earlier render, and now for a better
# reason than an exclusion: a full cache hit generates nothing, so it never asks for
# budget at all (see build_plan -- reserve_budget is only called when something is
# missing).
#
# AND WHAT IS COUNTED IS WHAT FAL BILLED, which is not the same as what we received. A
# second audit on 2026-10-22 measured the gap: the tally was exact whenever fal behaved
# and read ZERO whenever it did not. Five everyday shapes -- every collect attempt 502s, a
# non-JSON 200, a 200 shape we cannot read, COMPLETED with no picture, 429 throughout
# collection -- all leave fal paid and the old counter untouched, because the counter rose
# only after bytes arrived:
#
#   * fal incident, 502s on collection:   5 pictures billed per press, US$0.20, read as $0
#   * fal rate-limiting collection:       the SAME picture re-submitted once per deferral
#                                         cycle, 17 cycles in the 600s window, US$0.68 for
#                                         one press, read as $0
#
# So Plan.images_paid now rises on the CHARGE (fal_images.billed_images), and
# Plan.images_lost records the subset we got nothing for -- which is what stops the
# deferral loop above from buying the same picture seventeen times.
# ---------------------------------------------------------------------------
MAX_ROOMS = 4
IMAGES_PER_LISTING = 2 + MAX_ROOMS          # the exterior pair, then the rooms
# The Elegant tear-down treatment needs ONE MORE still: the cleared plot, which is the
# end frame of the clip that brings the house down and the first frame of the clip that
# raises the new one. See CLEARED_IS_LOAD_BEARING at cleared_prompt().
IMAGES_PER_ELEGANT_TEARDOWN_LISTING = 3 + MAX_ROOMS
# The real ceiling, in the unit that is billed.
MAX_IMAGES_PER_AGENT_PER_MONTH = 240
# What that is worth in listings, for talking to agents. DERIVED, and only ever
# approximate -- a listing whose photos get re-picked costs more than one listing's worth
# of images. Never used as a limit.
TYPICAL_LISTINGS_PER_AGENT_PER_MONTH = (
    MAX_IMAGES_PER_AGENT_PER_MONTH // IMAGES_PER_LISTING)

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


# ---------------------------------------------------------------------------
# Cost ceiling, part two: the Elegant film's GENERATED VIDEO
#
# THE UNIT IS A SECOND OF VIDEO, for exactly the reason the unit above is an image: it
# is what fal bills. Measured from Jane's own fal bill and confirmed against fal's
# published price for the endpoint we use with audio off: US$0.07 a second.
#
#     ONE SECOND OF VIDEO                     = US$0.07
#     ONE ELEGANT LISTING, tear-down, 4 rooms = 2 x 5s exterior + 5s walk-in
#                                               + 4 x 10s rooms = 55s
#                                             = US$3.85 of video
#                                             + 7 stills at US$0.0398 = US$0.28
#                                             = about US$4.13
#     ONE ELEGANT LISTING, gentle makeover    = 5s walk-in + 4 x 10s rooms = 45s
#                                             = US$3.15
#                                             + 6 stills = US$0.24 = about US$3.39
#
# So ONE Elegant video costs about as much as FIFTEEN Dramatic ones. That single fact is
# why this ceiling exists as its own number in its own unit rather than being folded into
# the image allowance: a mistake that costs four cents and a mistake that costs seventy
# are not the same mistake, and an allowance expressed in images cannot see the second
# one at all.
#
# THE CAP, AND WHAT IT PERMITS. 165 seconds an agent a calendar month:
#
#     165s x US$0.07  = US$11.55 an agent a month
#     50 agents       = US$578 a month, worst case, if every agent spends the whole
#                       allowance every month
#     in Elegant videos: EXACTLY THREE tear-down films a month per agent (55s each), or
#                        three gentle makeovers (45s each) with 30s left over
#
# IT WAS 150, AND THE WALK-IN MOVED IT. 150 was three films when a tear-down was 50
# seconds; the driveway-to-living-room walk-in (WALKIN_CLIP_SECONDS) made a tear-down 55,
# which would have left the cap permitting two and a bit films and a number nobody could
# explain. Raised to exactly three films, which is what the cap was always meant to mean.
#
# WHY THAT NUMBER. It is deliberately the same ORDER as the image allowance Jane has
# already decided about (240 images = US$9.60 an agent, US$480 across fifty), so the two
# ceilings together roughly double a known figure rather than introducing an unknown one.
# Jane has not set pricing for the Elegant tier, so this is A SAFETY NET AND NOT A
# BUSINESS RULE: when she prices it, this number should be re-derived from that price,
# and it is one constant so that is a one-line change.
#
# IT IS NOT THE AUTHORITATIVE CAP, and at this price that matters more than it did for
# images. The authoritative cap belongs on fal's own billing spend limit, which no bug on
# our side can outrun. Jane's fal balance when this was written was US$14.90 -- about
# three Elegant videos -- so in practice fal's balance is the real ceiling until she tops
# it up, and a render that runs out of balance degrades through the ordinary refusal path
# rather than failing.
#
# HOW "ALREADY USED" IS COUNTED: a month-to-date SECONDS total on the AGENT row, written
# by the same compare-and-swap the image tally uses, for the same three reasons (nothing
# prunes it, it is in the unit that is billed, and it counts every path including the one
# that writes no job row). COUNTED WHEN THE SUBMIT IS ACCEPTED, NOT WHEN THE BYTES
# ARRIVE -- that distinction was the whole of the second old-to-new audit, and at seventy
# cents a clip getting it wrong is fifteen times more expensive here than it was there.
# See fal_video.billed_seconds.
# ---------------------------------------------------------------------------
MAX_VIDEO_SECONDS_PER_AGENT_PER_MONTH = 165

# Bump to regenerate every listing's clips with a changed prompt or treatment. Part of
# every clip cache key, so a bump is a cache miss rather than a migration. SEPARATE from
# PROMPT_VERSION: changing a room's still-image prompt should not throw away fifty cents
# of video, and changing a clip prompt should not throw away the stills.
CLIP_PROMPT_VERSION = 1

# Wall-clock ceiling on the WHOLE clip-generation pass for one listing, shared across
# every clip.
#
# FIFTEEN MINUTES, against the still images' four. A Kling clip takes two to six minutes
# and a tear-down listing wants six of them, so this is the number that decides whether
# an agent waits a quarter of an hour or an hour. Past it the plan is TRIMMED (the film
# keeps the clips it has and the rest of the rooms play as stills) rather than failed --
# every clip already bought is cached, so nothing paid for is thrown away.
TOTAL_CLIP_BUDGET_SECONDS = 900

# How many clips we ask fal for AT ONCE.
#
# TWO, and the number is fal's rather than ours: fal starts a new account at two
# concurrent requests, rising with credits spent. Sequential generation would make a
# six-clip tear-down listing take six times three minutes -- past TOTAL_CLIP_BUDGET_SECONDS,
# so the last rooms would be trimmed after the agent had already waited a quarter of an
# hour. Two at a time fits the same six clips into three waves and is the difference
# between "10-15 minutes" and "over half an hour".
#
# IT IS ALSO WHY THE ELEGANT JOB TYPE IS CAPPED AT ONE RENDER ACROSS THE WHOLE FLEET
# (video_jobs.JOB_TYPES): one render at two requests is exactly fal's limit, so two
# renders would 429 each other, and a 429 costs a deferral cycle rather than a clip.
# Jane's own experiments share the key, so there is no headroom to spend.
#
# Drop this to 1 if 429s turn out to be common in live renders; nothing else has to
# change, the pass just takes longer. Every clip's keyframes are STILLS that already
# exist, so clips have no ordering dependency on each other and this is safe to vary.
ELEGANT_CLIP_CONCURRENCY = 2


class BudgetExhausted(Exception):
    """This agent has no generation allowance left this month.

    The one failure here that is not a degradation: there is no Dramatic video without
    an after image, so the caller turns this into a plain-language permanent failure and
    points the agent at the Classic."""


class VideoBudgetExhausted(Exception):
    """This agent has no GENERATED VIDEO allowance left this month.

    Separate from BudgetExhausted because the remedies differ and the agent should be
    told the true one: a used-up picture allowance leaves the Classic, while a used-up
    video allowance leaves both the Classic AND the Dramatic -- which is a much better
    thing to be offered, and is a different sentence."""


class RateLimited(Exception):
    """fal had no room for this account, so part of this plan was not generated.

    NOT raised from build_plan -- that would throw away the cache for the images this
    pass already paid for, which is the opposite of what we want. build_plan sets
    `plan.rate_limited` and returns the plan intact; the caller stores the cache and
    THEN decides, and the decision it wants is to put the whole job back for a few
    seconds (video_jobs.DeferJob) rather than hand the agent a film with a room missing
    its transformation. See fal_images.FalRateLimited for why a 429 is an everyday
    answer on a fresh fal account rather than a rarity.

    A DEFERRAL IS ONLY FREE WHEN THE PASS LOST NO MONEY, and that qualifier is the second
    audit's other expensive finding. The deferral was designed for the 429 that arrives on
    the SUBMIT: nothing was queued, nothing was charged, and waiting costs a wait. A 429
    that arrives while COLLECTING a picture fal already accepted is a different animal --
    that picture is billed, uncollected and therefore uncached, so the next cycle submits
    it again and pays again. Measured on 2026-10-22: 17 deferral cycles fit inside the
    600-second patience window, US$0.68 for one press of Generate, every cent of it for
    one picture. So `plan.images_lost` counts the pictures this pass was charged for and
    did not get, and the caller defers only when that number is zero."""


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


def cleared_prompt():
    """The plot with nothing standing on it, generated FROM the anonymised frame.

    CLEARED_IS_LOAD_BEARING, and it is the reason the tear-down treatment costs one more
    still than anything else here. docs/old-to-new-video-spec.md, "The end frame decides
    what happens": anchoring a transformation on the FINISHED house let the model wash one
    picture into the other -- the smeary dissolve Jane's verdict on was "it is a joke" --
    and anchoring the same prompt on a cleared plot forced a real structural failure,
    because there is no way from a standing house to an open plot except by bringing the
    house down. So the tear-down is TWO clips with this image between them: it is the last
    frame of the one that brings the house down and the first frame of the one that raises
    the new one, which is what makes the two cut together with no visible join.

    Everything outside the plot is pinned, because this image, the frame it came from and
    the rebuild are all cut together: the street has to be the same street. It is
    generated from the ANONYMISED frame, never the original, so the neighbours stay
    altered.

    Written entirely in words fal will accept. fal's safety layer refuses "demolish",
    "rubble", "debris" and their relatives even as exclusions, so there are none of them
    here -- the state is described as an empty plot with broken concrete on it, which is
    the same picture in words that get generated rather than refused.
    """
    return (
        "Replace the house in the centre of this photograph with an empty building plot: "
        "flat bare ground, patches of sand and gravel, and a few low heaps of broken "
        "concrete and timber lying on it. Nothing is standing on the plot and the ground "
        "is level and open. Keep the road, the kerb, the pavement, the neighbouring "
        "buildings, the trees, the sky and the daylight exactly as they are in this "
        "photograph. Same camera position, same framing, same angle, same distance. "
        "Photographic and realistic, like a photograph of a cleared building site."
    )


# ---------------------------------------------------------------------------
# THE TREATMENT TABLE -- how the transformation is shown, as DATA rather than as a
# hardcoded prompt.
#
# WHY A TABLE. The spec was written with one Elegant effect in mind: Variation B, the
# furniture carried out piece by piece. Then Jane watched a competitor and said "these are
# the kinds of bold videos i am talking about" about something else entirely -- a
# continuous film in which the old house comes down and the new one rises with no cut, and
# then each interior comes apart and reassembles in the same frame. That is the headline
# effect now, and it is not Variation B.
#
# Rather than replace one hardcoded prompt with another, the two are rows in this table
# and everything downstream -- the clip plan, the cache keys, the shot table, the queue,
# the ceiling -- takes the treatment as a parameter and does not care which it is. Jane
# picks per listing later; adding a third is a row here.
#
# WHAT EVERY ROW MUST OBEY, and neither of these is negotiable:
#
#   1. THE NEIGHBOURS ARE ANONYMISED FIRST. Every image and every clip in both rows is
#      derived from the anonymised frame, never from the agent's original -- see "THE
#      ORDER IS LOAD-BEARING" at the top of this file. The competitor's film leaves the
#      adjacent houses fully recognisable. Ours does not, and the ordering that guarantees
#      it is tested rather than remembered.
#   2. NOTHING IS FABRICATED. The competitor added a red supercar and a yellow one to the
#      driveway. A generated car is an invented fact about a driveway exactly as an
#      invented bedroom would be, and not inventing property features is the hardest
#      standing rule there is. So every prompt below PINS what stays ("the driveway and
#      the street stay empty the whole time", "the garden keeps exactly the planting in
#      the photograph", "nothing appears that is not named here") and
#      fal_video.NO_FABRICATION_NEGATIVE excludes the specific things a video model likes
#      to add. Both, because the positive pin is what the model follows and the negative
#      is the backstop for when it does not.
#
# AND THE PROMPT-SAFETY RULE THAT COST TWO PAID REFUSALS. fal refuses a prompt containing
# explosion, blast, fire, debris, rubble, demolish, collapse and their relatives EVEN AS
# EXCLUSIONS. So none of those words appears anywhere below, in any polarity. What should
# HAPPEN is described; what should not is left to the negative-prompt field, which carries
# no such word either. Every prompt here is screened through fal_images.screen_prompt
# before it can be sent, and the tests screen all of them so a reworded prompt cannot
# quietly become a refusal an agent waits for.
# ---------------------------------------------------------------------------
TREATMENT_TEARDOWN = "tear-down-and-rebuild"
TREATMENT_MAKEOVER = "gentle-makeover"

# The notice each shot carries, as a KIND rather than as text. The text itself lives in
# video_renderer beside the code that draws it, so there is one copy of Jane's exact
# wording and the table decides only WHICH line applies where.
NOTICE_PRIVACY = "privacy"           # a real photograph with the neighbours altered
NOTICE_AI_CONCEPT = "ai_concept"     # a tear-down or a rebuild: what COULD be built
NOTICE_AI_INTERIOR = "ai_interior"   # a redesigned interior
NOTICE_NONE = None                   # a plain real photograph: no notice at all

# The roles a generated STILL can play. Declared here rather than beside the cache below
# because the treatment table names them, and a table that refers to strings a hundred
# lines further down is a table nobody can check.
ROLE_EXTERIOR_ANON = "exterior_anon"     # the agent's real photo, neighbours altered
ROLE_EXTERIOR_NEW = "exterior_new"       # the rebuilt house
ROLE_EXTERIOR_CLEARED = "exterior_cleared"   # the plot with nothing standing on it
ROLE_ROOM_NEW = "room_new"               # one room redesigned

# Exterior clip stages. Five seconds each, not ten: a house coming down and a house going
# up are each one simple continuous move, they read clearly in five seconds, and the pair
# costs US$0.70 instead of US$1.40.
EXTERIOR_CLIP_SECONDS = 5
# THE WALK-IN. Five seconds, and this one is a judgement about money against the single
# effect Jane has praised most across the whole project -- she asked for it again
# unprompted weeks after first seeing it ("i want the zoom walk in from driveway living
# room"). Ten seconds would give the travel more room to breathe and would cost US$0.70
# instead of US$0.35, taking a tear-down listing to US$4.48 -- the very top of the band
# this feature was costed against. Five keeps the headroom and matches the spec's own
# estimate for this shot ("about US$0.50"). It is one constant: if a human watches a real
# film and finds five seconds rushed, this is the line to change, and the only cost is
# money.
WALKIN_CLIP_SECONDS = 5
# Room clips. TEN, and this is the spec's own measurement rather than a preference: "5 is
# too short for everything to move out and back in."
ROOM_CLIP_SECONDS = 10


class ClipStage:
    """One generated clip in the film, as data.

    stage       -- a short stable id. Part of the cache key, so renaming one regenerates.
    start_role  -- the cache role whose image is this clip's FIRST frame.
    end_role    -- the cache role whose image is its LAST frame, or None to let the model
                   choose (which no real stage does -- see fal_video.generate_clip).
    seconds     -- 5 or 10. What we are billed for.
    prompt      -- a callable returning the prompt. A callable rather than a string so a
                   room's prompt can take the description of that room's contents.
    notice      -- which disclaimer this shot carries.
    """

    def __init__(self, stage, start_role, end_role, seconds, prompt, notice):
        self.stage = stage
        self.start_role = start_role
        self.end_role = end_role
        self.seconds = seconds
        self.prompt = prompt
        self.notice = notice


def teardown_exterior_prompt():
    """The old house comes down. The headline shot of the headline treatment."""
    return (
        "Locked-off static camera, no camera movement. The house in the centre of the "
        "frame comes down: its walls lean inward and sink, the roof folds down onto the "
        "plot, the window glass drops out of its frames, and thick grey dust rolls up "
        "and across the garden and then thins away, leaving the plot flat and open with "
        "low heaps of broken concrete and timber on it. The road, the kerb, the "
        "pavement, the neighbouring buildings, the trees and the sky stay exactly as "
        "they are, untouched and still. The driveway and the street stay empty the whole "
        "time, and the garden keeps exactly the planting in the photograph. Heavy, "
        "weighty, realistic motion, one continuous move."
    )


def teardown_rebuild_prompt():
    """The new house rises on the cleared plot. Cut straight onto the shot above, and
    because the clip above ENDS on the same image this one BEGINS on, the join is
    invisible -- which is the "no scene cuts" quality of the film Jane was shown."""
    return (
        "Locked-off static camera, no camera movement. A brand new modern house rises on "
        "the open plot in the centre of the frame: the ground floor slab is laid, white "
        "rendered walls lift up into place, full-height glazing slides into the "
        "openings, the flat roof lowers onto the walls, a slatted timber entrance swings "
        "into position, and a low planted front wall grows along the boundary, until the "
        "finished house stands complete. The road, the kerb, the pavement, the "
        "neighbouring buildings, the trees and the sky stay exactly as they are, "
        "untouched and still. The driveway and the street stay empty the whole time. "
        "Smooth, weighty, realistic construction motion, one continuous move."
    )


def teardown_room_prompt(old_contents=None, new_contents=None):
    """One interior comes apart and the new one assembles in the SAME frame.

    The camera is locked off and the structure is pinned hard, for the same reason the
    still-image room prompt pins it: this is a before-and-after of one room, and a room
    redrawn from a nicer angle is not a better after, it is a different room.
    """
    return (
        "Locked-off static camera, no camera movement. The old room comes apart and the "
        "new one assembles in the same frame. First "
        f"{old_contents or DEFAULT_OLD_CONTENTS} break into pieces, drop to the floor "
        "and are swept out of frame, and grey dust drifts through the air and clears, "
        "leaving the room bare. Then the new room builds itself in place: "
        f"{new_contents or DEFAULT_NEW_CONTENTS} rise and settle into position one after "
        "another, and the surfaces finish clean and modern. The walls, the ceiling and "
        "its height, the floor and its level, the doorways, and the windows with the "
        "same view through them stay exactly where they are the whole time. Nothing "
        "appears that is not named here, and no people or pets appear. Weighty, "
        "continuous, realistic motion."
    )


def walkin_prompt():
    """From the DRIVEWAY, up the drive, through the front door, into the LIVING ROOM.

    THE EFFECT JANE HAS PRAISED MOST, and a standard part of every Elegant film rather
    than an option: she asked for it again, unprompted, weeks after first seeing it --
    "i want the zoom walk in from driveway living room". The spec has it under "Shared:
    the exterior and the tour", where it is called the thing that turns a set of room
    clips into a house tour, and its prompt is reproduced here with two changes Jane
    asked for:

      1. IT STARTS AT THE DRIVEWAY, not at the facade. The spec's version opens with the
         camera already "approaching the front entrance"; this one begins outside at the
         gate, low over the drive, so the driveway is genuinely in the opening frames.
      2. IT LANDS ON THE LIVING ROOM, specifically -- which is also the Classic's own
         rule (video_renderer.SPACE_RANKING: the living room opens the film). The two
         styles now agree about where a film begins, and the end frame is chosen by the
         same space labels rather than by photo order. See video_renderer.plan_elegant.

    The end frame is the living room AS IT IS TODAY, not redesigned, and that is
    deliberate: it is the first frame of the shot that follows, so the two meet exactly
    and the join is invisible. Landing on the redesigned room instead would cut straight
    from the new living room to the old one coming apart, which reads as a mistake. The
    one oddity it leaves is a tear-down film walking from a conceptual new facade into
    today's interior; it is the honest version of that arc, and it is the creative call
    in this feature most worth a human's eyes on a real film.
    """
    return (
        "The camera starts outside at the gate, low over the driveway, and glides "
        "smoothly forward: up the driveway, past the front wall, straight at the front "
        "entrance of the house, in through the front entrance, and on into the living "
        "room inside, coming to rest looking across the room. One continuous flowing "
        "forward move from the driveway to inside the house, like a drone easing up the "
        "drive and through the door. The house, the gate, the driveway, the planting, "
        "the neighbouring buildings and the sky stay exactly as they are, and the room "
        "inside keeps exactly the walls, floor, windows and furniture it has in the "
        "photograph. The driveway and the street stay empty the whole time. Nothing "
        "appears that is not in these two photographs, and no people or pets appear. "
        "Smooth, steady, cinematic."
    )


def makeover_room_prompt(old_contents=None, new_contents=None):
    """The spec's Variation B: the old furniture carried out piece by piece.

    THE FOUR FIXED SENTENCES ARE THE SPEC'S, VERBATIM -- the opening "Locked-off static
    camera, no camera movement, fast-paced home makeover timelapse" that stops the camera
    drifting, the two phase headers, and the closing "Smooth, satisfying, realistic
    motion". They are quoted rather than paraphrased because the spec says the prompts are
    the valuable part of that document and these were arrived at by elimination.

    WHAT DIFFERS FROM THE SPEC'S WORKED EXAMPLE, and it is worth knowing. In the example a
    human had looked at the photograph and gave every single object its own exit ("the
    carved wooden coffee table and its lace cloth lift off and fly out"). Here the objects
    come from the vision pass, which names them but cannot be relied on to choreograph
    them, so the exits and entries are applied to the named objects as a GROUP in the
    spec's own verb vocabulary. The trick the spec says is "the whole trick" -- naming each
    real object alongside a physical verb, so the furniture MOVES instead of melting from
    one room into the other -- survives intact; the per-object assignment does not.
    """
    return (
        "Locked-off static camera, no camera movement, fast-paced home makeover "
        "timelapse. First, the old furniture is pulled out of the room one piece at a "
        f"time: {old_contents or DEFAULT_OLD_CONTENTS} slide out of frame to the left "
        "and right, lift off and fly out, peel off the walls and fly away, and rise up "
        "into the ceiling and disappear, leaving an empty room. Then the new design "
        "shifts in piece by piece: the lighting switches on and the curtains unroll from "
        f"the top, then {new_contents or DEFAULT_NEW_CONTENTS} glide in from the edges "
        "and settle, slide into place and lower down, and the small decor pops onto the "
        "surfaces. The walls, the ceiling and its height, the floor and its level, the "
        "doorways, and the windows with the same view through them stay exactly where "
        "they are. Nothing appears that is not named here, and no people or pets appear. "
        "Smooth, satisfying, realistic motion."
    )


class ElegantTreatment:
    """One way of showing the transformation. See THE TREATMENT TABLE above.

    label             -- what an agent would see. Not a contract with anything.
    needs_cleared     -- whether the still-image pass must also generate the cleared plot.
    exterior_stages   -- the generated clips that carry the exterior, in cut order. Empty
                         means the exterior plays as the Dramatic's two stills.
    room_stage        -- the ClipStage template for a room. Its start/end roles are unused
                         (a room's keyframes are that room's own two urls); its seconds,
                         prompt and notice are what matter.
    walkin            -- the ClipStage for the driveway-to-living-room walk-in, or None.
                         Both treatments have one: it is a HARD REQUIREMENT rather than an
                         option (see walkin_prompt). Its end frame is not a role either --
                         it is the first room's own photograph -- so build_clip_plan fills
                         that in.
    exterior_notice   -- the disclaimer on the rebuilt exterior STILL, for a treatment
                         whose exterior is not a clip.
    """

    def __init__(self, id, label, needs_cleared, exterior_stages, room_stage,
                 exterior_notice, walkin=None):
        self.id = id
        self.label = label
        self.needs_cleared = needs_cleared
        self.exterior_stages = tuple(exterior_stages)
        self.room_stage = room_stage
        self.walkin = walkin
        self.exterior_notice = exterior_notice

    @property
    def exterior_clip_seconds(self):
        return sum(s.seconds for s in self.exterior_stages)

    def listing_seconds(self, rooms):
        """What a cold render of `rooms` transformable rooms would BUY, in seconds. The
        number the ceiling is reserved against, so it is computed from the same table the
        clips are generated from and cannot drift from it."""
        rooms = max(0, int(rooms))
        walkin = self.walkin.seconds if (self.walkin and rooms) else 0
        return (self.exterior_clip_seconds + walkin
                + rooms * self.room_stage.seconds)


# ONE WALK-IN STAGE, SHARED BY BOTH TREATMENTS, because it is the same shot in both: the
# camera comes up the drive, through the door, and rests looking across the living room.
# It is a field on the treatment rather than a constant in the renderer so a future
# treatment could legitimately have a different one -- but neither of these does, and a
# treatment with `walkin=None` is one that has dropped the effect Jane asked for by name.
#
# ITS START FRAME IS THE REBUILT EXTERIOR, and there is no fallback to the real
# photograph. That is a deliberate bound rather than an oversight: starting on the real
# photograph would make the clip's first frame a real picture with altered neighbours, so
# it would need the privacy line as well as the AI one, and a fourth disclosure string is
# not worth inventing for a case that only arises when the exterior has already failed and
# the whole outside arc is degraded anyway. With no rebuilt exterior there is no walk-in,
# and the film falls back to the Dramatic's own hard cut through the door.
WALKIN_STAGE = ClipStage("walkin", ROLE_EXTERIOR_NEW, None, WALKIN_CLIP_SECONDS,
                         walkin_prompt, NOTICE_AI_CONCEPT)

ELEGANT_TREATMENTS = {
    # THE HEADLINE. Exterior collapse into the rebuild, then every interior demolished and
    # reassembled in the same frame. This is what Jane pointed at.
    TREATMENT_TEARDOWN: ElegantTreatment(
        id=TREATMENT_TEARDOWN,
        label="Tear down and rebuild",
        needs_cleared=True,
        exterior_stages=(
            # The anonymised REAL photograph is the first frame of the first clip, so
            # the film's generated motion starts from the house as it actually is, with
            # the neighbours already altered. Nothing is ever derived from the original.
            ClipStage("teardown", ROLE_EXTERIOR_ANON, ROLE_EXTERIOR_CLEARED,
                      EXTERIOR_CLIP_SECONDS, teardown_exterior_prompt,
                      NOTICE_AI_CONCEPT),
            ClipStage("rebuild", ROLE_EXTERIOR_CLEARED, ROLE_EXTERIOR_NEW,
                      EXTERIOR_CLIP_SECONDS, teardown_rebuild_prompt,
                      NOTICE_AI_CONCEPT),
        ),
        room_stage=ClipStage("room_teardown", None, None, ROOM_CLIP_SECONDS,
                             teardown_room_prompt, NOTICE_AI_INTERIOR),
        walkin=WALKIN_STAGE,
        exterior_notice=NOTICE_AI_CONCEPT,
    ),
    # THE SPEC'S VARIATION B. No exterior clip: the opening real photograph cuts to the
    # rebuilt still exactly as the Dramatic's does, and the transformation lives in the
    # rooms. Cheaper by two clips, and the gentler film of the two.
    TREATMENT_MAKEOVER: ElegantTreatment(
        id=TREATMENT_MAKEOVER,
        label="Gentle makeover",
        needs_cleared=False,
        exterior_stages=(),
        room_stage=ClipStage("room_makeover", None, None, ROOM_CLIP_SECONDS,
                             makeover_room_prompt, NOTICE_AI_INTERIOR),
        walkin=WALKIN_STAGE,
        exterior_notice=NOTICE_AI_CONCEPT,
    ),
}

# What an Elegant render uses when nothing says otherwise. The headline effect, because
# it is the one Jane asked for.
DEFAULT_ELEGANT_TREATMENT = TREATMENT_TEARDOWN


def resolve_treatment(treatment_id):
    """An ElegantTreatment for whatever came in off a request or a listing row.

    Falls back to the default rather than raising, for the same reason
    main.py's _resolve_video_template_id falls back: an id stored on a listing before a
    treatment was renamed, or held in a browser tab across a deploy, must still render a
    film rather than erroring on an agent who asked for nothing in particular.
    """
    return ELEGANT_TREATMENTS.get(treatment_id) \
        or ELEGANT_TREATMENTS[DEFAULT_ELEGANT_TREATMENT]


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
# The ROLE_* names are declared up beside the treatment table, which names them.


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
# The CLIP cache. Same idea as the image cache, and much more valuable: one cached clip
# is seventy cents, where one cached image is four.
#
# KEYED ON THE TREATMENT, THE STAGE, THE DURATION AND BOTH KEYFRAME URLS -- and
# deliberately not on the prompt text, for the same reason the image cache is not: the
# room descriptions are model-written and differ slightly every run, so a prompt-keyed
# cache would miss on every regenerate and pay again, which is the one thing a cache this
# expensive exists to prevent. CLIP_PROMPT_VERSION is in the key instead, so a deliberate
# change to a clip prompt regenerates and an incidental change of wording does not.
#
# THE TREATMENT IS IN THE KEY AND THAT IS NOT OPTIONAL. The same room, the same two
# keyframes and the same ten seconds produce a completely different clip under the two
# treatments -- one comes apart, the other is tidied out piece by piece. Without the
# treatment in the key, an agent who tried both would be served the first one's film with
# the second one's name on it.
#
# A SEPARATE COLUMN FROM THE IMAGES, and separate versions, so that changing one prompt
# cannot throw away the other's spending.
# ---------------------------------------------------------------------------
def clip_cache_key(treatment_id, stage, seconds, source_urls):
    """A short, stable key for one generated clip. The urls are hashed rather than
    stored, so the cache cannot become a second place that knows a photo's address."""
    joined = "|".join(str(u or "") for u in source_urls)
    digest = hashlib.sha1(joined.encode("utf-8")).hexdigest()[:20]
    return (f"c{CLIP_PROMPT_VERSION}:{treatment_id}:{stage}:{int(seconds)}s:{digest}")


def read_clip_cache(stored):
    """Whatever is on the listing -> {key: url}. Tolerates anything."""
    if not isinstance(stored, dict):
        return {}
    clips = stored.get("clips")
    if not isinstance(clips, dict):
        return {}
    return {str(k): str(v) for k, v in clips.items()
            if isinstance(v, str) and v.startswith("http")}


def write_clip_cache(existing):
    """{key: url} -> the value to store on the listing."""
    return {"version": CLIP_PROMPT_VERSION, "clips": dict(existing)}


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
    images_paid   -- how many images FAL CHARGED FOR in this pass (0 on a full cache
                     hit). The caller logs what the render cost AND adds it to the
                     agent's month-to-date total, which is the ceiling's only source of
                     truth, so this number must stay honest about anything billed --
                     an image we paid for and then could not store, and (since the
                     2026-10-22 second audit) an image fal accepted, charged for and then
                     never handed over. See fal_images.billed_images.
    images_lost   -- of those, how many we did NOT get a usable picture for. Money spent
                     with nothing to show, and nothing in the cache either, so a RETRY OF
                     THIS PASS WOULD SPEND IT AGAIN. That is why the caller consults this
                     before deferring; see RateLimited.
    rate_limited  -- fal had no room, so at least one image was not even attempted. On
                     its own that is the caller's cue to defer; see RateLimited.

    THE ELEGANT HALF, all of it empty for a Dramatic render and all of it optional:

    cleared_url   -- the plot with nothing standing on it, or None. Only the tear-down
                     treatment asks for it, and without it that treatment has no
                     tear-down (see cleared_prompt) and falls back to a still rebuild.
    treatment     -- the ElegantTreatment id this plan's clips were generated for. In
                     every clip cache key, so one treatment can never be served the
                     other's film.
    exterior_clips-- [{"stage", "url", "seconds", "notice", "backdrop_url"}] in cut
                     order, or [] when the exterior plays as stills.
    walkin_clip   -- the driveway-to-living-room walk-in, or None. Same shape. It is a
                     hard requirement of the Elegant rather than an option, so None here
                     means something was missing (no rebuilt exterior, no rooms, or the
                     clip could not be bought) and not that it was switched off.
    room_clips    -- {room index: {"url", "seconds", "notice"}} for the rooms that got
                     one. A room missing from here plays as the Dramatic would play it.
    clip_cache    -- the full {key: url} clip map to store back on the listing.
    clip_seconds_paid / clip_seconds_lost / clips_rate_limited
                  -- the exact three facts images_paid / images_lost / rate_limited carry,
                     in seconds of video. Separate counters because they are separate
                     ceilings in separate units, and folding them together would hide a
                     seventy-cent loss inside a four-cent one.
    """

    def __init__(self):
        self.opening_url = None
        self.exterior_url = None
        self.rooms = []
        self.degradations = []
        self.cache = {}
        self.images_paid = 0
        self.images_lost = 0
        self.rate_limited = False
        # --- the Elegant half ---------------------------------------------------------
        self.cleared_url = None
        self.treatment = None
        self.exterior_clips = []
        self.walkin_clip = None
        self.room_clips = {}
        self.clip_cache = {}
        self.clip_seconds_paid = 0
        self.clip_seconds_lost = 0
        self.clips_rate_limited = False
        # What the vision pass said is in each room now and what should go there
        # instead, [(old, new)] in room order. Kept on the plan because the CLIP prompts
        # need it and they are written one step later, by build_clip_plan.
        self.room_contents = []

    @property
    def transformable_rooms(self):
        return [r for r in self.rooms if r.get("after_url")]

    def __repr__(self):
        return ("Plan(opening=%s, exterior=%s, rooms=%d of which %d transformable, "
                "paid=%d, lost=%d, clips=%d+%d, clip_seconds=%d/%d)"
                % (bool(self.opening_url), bool(self.exterior_url),
                   len(self.rooms), len(self.transformable_rooms),
                   self.images_paid, self.images_lost,
                   len(self.exterior_clips), len(self.room_clips),
                   self.clip_seconds_paid, self.clip_seconds_lost))


def reserve_budget(wanted, images_used_this_month):
    """How many of `wanted` images this agent may generate. Checked ONCE, up front.

    `images_used_this_month` is a count of IMAGES, month to date -- the thing that is
    billed -- not a count of listings. Returns a number between 1 and `wanted`, or raises
    BudgetExhausted when there is no allowance at all. Trimming rather than refusing is
    deliberate: an agent who is three images short of a full plan should get a shorter
    film, not an error.
    """
    allowance = MAX_IMAGES_PER_AGENT_PER_MONTH - max(0, int(images_used_this_month or 0))
    if allowance <= 0:
        raise BudgetExhausted(
            "This account has used its whole Old-to-New picture allowance for this "
            f"month (enough for about {TYPICAL_LISTINGS_PER_AGENT_PER_MONTH} listings, "
            "and faster than that if the same listing's photos are changed and "
            "regenerated). The Classic video is still available, and the allowance "
            "resets at the start of next month.")
    return max(1, min(wanted, allowance))


def reserve_video_budget(wanted_seconds, seconds_used_this_month):
    """How many SECONDS of video this agent may generate. Checked ONCE, up front.

    `seconds_used_this_month` is a count of SECONDS, month to date -- the thing that is
    billed. Returns a number between 1 and `wanted_seconds`, or raises
    VideoBudgetExhausted when there is no allowance at all.

    IT RETURNS SECONDS AND NOT CLIPS, and the caller packs whole clips into them (see
    build_clip_plan), because a clip is five or ten seconds and all-or-nothing: there is
    no such thing as paying for three seconds of a ten-second generation. Trimming by
    whole clips rather than refusing is the same choice reserve_budget makes for images --
    an agent two clips short of a full film should get a shorter film, not an error.

    Unlike the image version there is no `max(1, ...)` floor that would let a used-up
    allowance through: at seventy cents a clip, one clip over the line is not a rounding
    courtesy. The floor is applied to the TRIM, not to the refusal.
    """
    allowance = (MAX_VIDEO_SECONDS_PER_AGENT_PER_MONTH
                 - max(0, int(seconds_used_this_month or 0)))
    if allowance <= 0:
        raise VideoBudgetExhausted(
            "This account has used its whole Elegant video allowance for this month "
            f"({MAX_VIDEO_SECONDS_PER_AGENT_PER_MONTH} seconds of generated video, "
            "which is about three full Elegant videos). The Classic and Dramatic videos "
            "are both still available, and the allowance resets at the start of next "
            "month.")
    return min(int(wanted_seconds), allowance)


def build_plan(exterior_url, room_urls, cache, generate_image, store_image,
               describe=None, images_used_this_month=0, deadline=None,
               want_cleared=False, always_describe=False):
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
    images_used_this_month -- IMAGES this agent has already been billed for this calendar
                     month. See the cost-ceiling block: images, not listings.
    want_cleared  -- also generate the CLEARED PLOT still, from the anonymised frame.
                     Only the Elegant tear-down treatment wants it, and without it that
                     treatment has no tear-down at all (see cleared_prompt). Asked for by
                     the caller rather than inferred, so the Dramatic's six-image plan is
                     byte-for-byte what it was.
    always_describe -- run the description pass even when every image is cached and
                     nothing has to be generated. The Elegant needs this: its CLIP
                     prompts name the real objects in each room, and a listing whose
                     stills were all made by an earlier Dramatic render would otherwise
                     buy fifty cents of video a room described only by the house default.
                     Off by default, so a cached Dramatic regenerate still pays for no
                     model call at all.

    Never raises except BudgetExhausted. Everything else is a degradation on the plan --
    except a rate limit, which sets plan.rate_limited for the caller to act on and is
    not a degradation either.
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
        if want_cleared:
            # AFTER the rebuild in the trim order, BEFORE the rooms. It is only ever
            # wanted by the tear-down treatment, where it is the shot's whole mechanism,
            # so it should outlive a room; and the rebuild is the shot the film rests on,
            # so it should not outlive that. Generated FROM the anonymised frame, like
            # the rebuild, so all three exteriors share a street.
            wanted.append((ROLE_EXTERIOR_CLEARED, exterior_url))
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
    if (ROLE_EXTERIOR_ANON, exterior_url) not in budgeted and \
            cache_key(ROLE_EXTERIOR_ANON, exterior_url) not in plan.cache:
        # Both the rebuild and the cleared plot are generated FROM the anonymised frame,
        # so neither is a cheaper plan without it -- it is an impossible one.
        budgeted.discard((ROLE_EXTERIOR_NEW, exterior_url))
        budgeted.discard((ROLE_EXTERIOR_CLEARED, exterior_url))

    # --- descriptions, only if we are actually generating -----------------------------
    neighbours = None
    contents = [(None, None)] * len(room_urls)
    if (budgeted or always_describe) and describe is not None:
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
                budgeted = {b for b in budgeted
                            if b[0] not in (ROLE_EXTERIOR_ANON, ROLE_EXTERIOR_NEW,
                                            ROLE_EXTERIOR_CLEARED)}
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
    # Whatever we learned, kept for the clip prompts one step later. Empty or full of
    # Nones is a perfectly good answer -- it means the house defaults.
    plan.room_contents = list(contents)

    def _make(role, source_urls, prompt, key):
        """Generate one image, store it, cache it. Returns the url or None."""
        if plan.rate_limited:
            # fal has already told us this account is at its limit. Asking again, five
            # more times, for the same answer only makes the agent wait for it.
            return None
        try:
            data = generate_image(prompt, source_urls, overall)
        except Exception as e:
            # COUNT WHAT FAL BILLED, NOT WHAT ARRIVED. This single line is the second
            # audit's most expensive finding. `images_paid` used to rise only after bytes
            # came back, so five measured everyday shapes -- every collect attempt 502s, a
            # non-JSON 200, a 200 shape we do not understand, COMPLETED with no picture,
            # 429 throughout collection -- spent Jane's money and left the per-agent
            # ceiling reading zero. fal_images.generate now reports the charge on the way
            # out and billed_images reads it; anything injected here that has never heard
            # of it reports 0, which is what it meant before.
            billed = fal_images.billed_images(e)
            plan.images_paid += billed
            plan.images_lost += billed
            if isinstance(e, fal_images.FalNotConfigured):
                logger.warning("old-to-new: %s", e)
            elif isinstance(e, fal_images.FalRateLimited):
                # NOT a degradation, and the one failure here the caller must act on
                # rather than report: the picture is makeable, there is simply no room
                # right now. Flagged and returned, so the images this pass DID pay for
                # still reach the cache before the job goes back in the queue.
                #
                # The flag now means "stop asking fal for more this pass" and nothing
                # more. Whether to DEFER is the caller's decision and it turns on
                # images_lost, because a 429 that arrives AFTER an accepted submit has
                # already been paid for and a deferral would pay for it again.
                logger.warning("old-to-new: fal is at this account's concurrency limit "
                               "(%s); not asking it for more pictures in this pass", e)
                plan.rate_limited = True
            elif isinstance(e, fal_images.FalRefused):
                # The expected, designed-for failure. A refusal is not a fault -- though
                # fal's COMPLETED-with-no-picture decline is a refusal we were charged
                # for, which is why the count above happens before this branching.
                logger.info("old-to-new: fal declined the %s image (%s)", role, e)
            else:
                logger.warning("old-to-new: could not generate the %s image (%s: %s)",
                               role, type(e).__name__, e)
            return None
        plan.images_paid += 1
        try:
            url = store_image(role, key, data)
        except Exception as e:
            # We paid for this one and cannot keep it. Counted as paid above, so the
            # accounting is honest even though we got nothing -- and counted as LOST, so
            # the caller knows a retry of this pass would buy it a second time.
            plan.images_lost += 1
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
            if want_cleared:
                # ALSO from the anonymised frame, never from the rebuild: the cleared
                # plot has to be the same street as both the shot before it and the shot
                # after it, and deriving it from a third generation would compound two
                # models' drift instead of one's.
                cleared_key = cache_key(ROLE_EXTERIOR_CLEARED, exterior_url)
                cleared_url = plan.cache.get(cleared_key)
                if not cleared_url and \
                        (ROLE_EXTERIOR_CLEARED, exterior_url) in budgeted:
                    cleared_url = _make(ROLE_EXTERIOR_CLEARED, [anon_url],
                                        cleared_prompt(), cleared_key)
                plan.cleared_url = cleared_url
                if not cleared_url:
                    # Reported here rather than in the renderer because the renderer
                    # cannot tell a cleared plot that was never asked for from one that
                    # failed, and the agent should be told which film they got.
                    plan.degradations.append(
                        "the cleared-site view could not be prepared, so the video "
                        "cuts from the house as it is today to the new design instead "
                        "of showing it being rebuilt")
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

    logger.info("old-to-new plan: %r, %d image(s) billed (about US$%.2f)%s",
                plan, plan.images_paid,
                plan.images_paid * fal_images.COST_PER_IMAGE_USD,
                ("; %d of them BILLED AND LOST (about US$%.2f spent with nothing to "
                 "cache, so a retry of this pass would buy them again)"
                 % (plan.images_lost,
                    plan.images_lost * fal_images.COST_PER_IMAGE_USD))
                if plan.images_lost else "")
    return plan


# ---------------------------------------------------------------------------
# The CLIP plan -- the Elegant film's generated video. THE EXPENSIVE PASS.
#
# This runs AFTER build_plan, on the plan it produced, and buys nothing that pass has not
# already made: every clip's two keyframes are stills that exist and are publicly
# reachable. That ordering is not an implementation detail -- it is what guarantees the
# neighbours are anonymised in every generated frame of the film, because the first frame
# of the first clip IS the anonymised photograph and every other keyframe was derived from
# it.
#
# WHAT IT COSTS TO GET THIS WRONG. One ten-second room clip is US$0.70 and one tear-down
# listing is about US$3.50. The still-image pass's two audits both turned on the same
# distinction and it matters fifteen times more here:
#
#   * COUNT WHAT FAL BILLED, NOT WHAT ARRIVED. A submit fal accepted is billed for its
#     whole duration whether or not we ever collect the file. clip_seconds_paid rises on
#     the CHARGE (fal_video.billed_seconds), not on the bytes.
#   * A DEFERRAL IS ONLY FREE WHEN THE PASS LOST NO MONEY. clip_seconds_lost counts
#     seconds we were charged for and have nothing cached for, so a retry would buy them
#     again. The caller consults it before deferring, exactly as it does for images.
#
# AND ONE THING THE IMAGE PASS DID NOT HAVE TO THINK ABOUT: THIS PASS TAKES A QUARTER OF
# AN HOUR. Over a quarter of an hour, Railway will redeploy, a worker will be killed, and
# the stale sweep will hand the job to somebody else. So the cache and the tally are
# persisted AFTER EVERY CLIP rather than at the end of the pass (the on_cached and
# on_paid hooks): a worker that dies eleven minutes in must leave the four clips it
# bought behind it, or the next worker buys them again and the agent is charged twice for
# one film. That is one small database write per clip, six at most, against a US$3.50
# exposure.
# ---------------------------------------------------------------------------
def build_clip_plan(plan, treatment, generate_clip=None, store_clip=None,
                    clip_cache=None, seconds_used_this_month=0, deadline=None,
                    on_paid=None, on_cached=None, concurrency=None,
                    still_ours=None):
    """Generate (or recover from cache) every clip one Elegant listing needs.

    plan          -- the old_to_new.Plan build_plan returned. Mutated in place: its
                     exterior_clips, room_clips, clip_cache and the three clip counters
                     are filled in, and any degradation is appended to its own list so
                     ONE list of plain-language notes reaches the agent.
    treatment     -- an ElegantTreatment, or its id.
    generate_clip(prompt, start_url, end_url, seconds, *, deadline) -> mp4 bytes.
                     fal_video.generate_clip, injected so this module is testable without
                     a network, without a key and without spending money. DEADLINE IS
                     KEYWORD-ONLY and this line is a contract: it once read as a fifth
                     POSITIONAL argument, the real function's fifth parameter was
                     `negative_prompt`, and every Elegant clip failed in production while
                     every stub built to this line passed. A stub must now match the real
                     signature, and a test checks that it does.
    store_clip(stage, key, data) -> public url. main.py owns storage.
    clip_cache    -- {key: url} already on the listing (read_clip_cache).
    seconds_used_this_month -- SECONDS of video this agent has already been billed for
                     this calendar month. See the second cost-ceiling block.
    on_paid(seconds)  -- called with the seconds fal has just been charged for, as soon as
                     it is charged. This is how the per-agent tally survives a worker
                     dying mid-pass.
    on_cached(cache)  -- called with the full {key: url} map after every clip that lands,
                     for the same reason.
    still_ours()  -> False when this render has been handed to another worker. Checked
                     BEFORE EVERY WAVE, and this one is about money rather than
                     correctness. A job row is fenced against a late write, so a
                     superseded worker's result is already discarded harmlessly -- but
                     for a quarter-hour pass that means it could spend another US$3.15 on
                     clips nobody will ever use. Three extra database reads buy that
                     back. Optional: omitted, the pass behaves as it did.

    Raises VideoBudgetExhausted when this agent has no video allowance left at all.
    Everything else degrades onto the plan.
    """
    treatment = treatment if isinstance(treatment, ElegantTreatment) \
        else resolve_treatment(treatment)
    plan.treatment = treatment.id
    plan.clip_cache = dict(clip_cache or {})
    generate_clip = generate_clip or fal_video.generate_clip
    overall = (deadline if deadline is not None
               else time.monotonic() + TOTAL_CLIP_BUDGET_SECONDS)
    lanes = max(1, int(concurrency or ELEGANT_CLIP_CONCURRENCY))

    # --- which clips this film wants, in cut order and in PRIORITY order -------------
    # They are the same order, which is convenient and not a coincidence: the exterior is
    # both the first shot and the shot the film is about, so trimming from the end of this
    # list trims the least important thing left.
    by_role = {
        ROLE_EXTERIOR_ANON: plan.opening_url,
        ROLE_EXTERIOR_CLEARED: plan.cleared_url,
        ROLE_EXTERIOR_NEW: plan.exterior_url,
    }
    stages = []
    for stage in treatment.exterior_stages:
        start_url = by_role.get(stage.start_role)
        end_url = by_role.get(stage.end_role)
        if not start_url or not end_url:
            # One of the two stills is missing -- build_plan has already told the agent
            # why, and said what the film does instead. Nothing to add here, and nothing
            # to buy: a clip with one keyframe would be the model inventing the other end,
            # which is the failure the spec's "end frame decides what happens" note is
            # about.
            logger.info("elegant: no %s clip (%s -> %s: a still is missing)",
                        stage.stage, stage.start_role, stage.end_role)
            continue
        stages.append({
            "stage": stage.stage, "kind": "exterior", "index": None,
            "start_url": start_url, "end_url": end_url, "seconds": stage.seconds,
            "prompt": stage.prompt(), "notice": stage.notice,
            "backdrop_url": end_url,
        })

    # THE WALK-IN, between the outside and the rooms. Not optional: see walkin_prompt.
    #
    # Its end frame is the FIRST ROOM'S PHOTOGRAPH AS IT IS TODAY -- which, after
    # video_renderer.plan_elegant has ordered the rooms by the Classic's own space
    # labels, is the living room. Landing on today's picture rather than the redesigned
    # one is what makes this clip and the shot after it meet on the same frame, so the
    # join is invisible.
    first_room = plan.rooms[0] if plan.rooms else None
    if treatment.walkin and first_room and first_room.get("before_url") \
            and plan.exterior_url:
        stage = treatment.walkin
        stages.append({
            "stage": stage.stage, "kind": "walkin", "index": None,
            "start_url": plan.exterior_url, "end_url": first_room["before_url"],
            "seconds": stage.seconds, "prompt": stage.prompt(),
            "notice": stage.notice,
            # The shot comes to rest looking across this room, so this is the picture it
            # rests on.
            "backdrop_url": first_room["before_url"],
        })
    elif treatment.walkin:
        # No rebuilt exterior, or no rooms. Nothing is bought and nothing is said to the
        # agent beyond what build_plan has already said about the missing exterior: the
        # film falls back to the Dramatic's own hard cut through the door.
        logger.info("elegant: no walk-in clip (exterior=%s, rooms=%d)",
                    bool(plan.exterior_url), len(plan.rooms))

    room_stage = treatment.room_stage
    contents = list(plan.room_contents or [])
    for index, room in enumerate(plan.rooms):
        after_url = room.get("after_url")
        if not after_url:
            # No redesigned still, so there is no "after" to move towards. The room plays
            # exactly as the Dramatic plays it, as a real photograph, and build_plan has
            # already counted and reported it.
            continue
        old_contents, new_contents = contents[index] if index < len(contents) \
            else (None, None)
        stages.append({
            "stage": f"{room_stage.stage}:{index}", "kind": "room", "index": index,
            "start_url": room["before_url"], "end_url": after_url,
            "seconds": room_stage.seconds,
            "prompt": room_stage.prompt(old_contents, new_contents),
            "notice": room_stage.notice,
            "backdrop_url": after_url,
        })

    # --- screen every prompt BEFORE anything waits or is reserved --------------------
    # Knowing a clip cannot be generated is worth much more before the agent waits a
    # quarter of an hour than after. A room prompt is part model-written, so this is not
    # a theoretical check: the vision pass is told not to use the words fal refuses, and
    # _clean_contents screens its answer, but the prompt as ASSEMBLED is what gets sent
    # and this is the only place that sees it.
    safe = []
    for item in stages:
        refused = fal_images.screen_prompt(item["prompt"])
        if refused:
            logger.warning("elegant: the %s prompt contains %r, which fal refuses; "
                           "that shot is left as a still", item["stage"], refused)
            plan.degradations.append(
                "one shot could not be animated, so it is shown as a still picture")
            continue
        safe.append(item)
    stages = safe

    # --- the cache, then the ceiling, then the plan ----------------------------------
    for item in stages:
        item["key"] = clip_cache_key(treatment.id, item["stage"], item["seconds"],
                                     (item["start_url"], item["end_url"]))
        item["url"] = plan.clip_cache.get(item["key"])

    missing = [item for item in stages if not item["url"]]
    wanted_seconds = sum(item["seconds"] for item in missing)
    budgeted = []
    if missing:
        allowed_seconds = reserve_video_budget(wanted_seconds, seconds_used_this_month)
        # THE EXTERIOR STAGES ARE ONE PURCHASE, NOT TWO, and that is a money rule rather
        # than a tidy one. They are a chain that meets on a single frame: the renderer
        # plays both or neither, because a new house rising out of a plot the film never
        # cleared is nonsense. So an allowance that covers the tear-down but not the
        # rebuild must buy NEITHER -- otherwise we would pay US$0.35 for a clip that is
        # then dropped on the floor. The same reasoning build_plan applies to the
        # exterior still PAIR.
        #
        # Everything else is its own group, so the order below is the priority order:
        # the exterior chain, then the walk-in (the effect Jane asked for by name, and
        # the last thing that should ever be cut), then the rooms in cut order.
        groups = []
        chain = [item for item in missing if item["kind"] == "exterior"]
        if chain:
            groups.append(chain)
        groups += [[item] for item in missing if item["kind"] != "exterior"]
        spent = 0
        for group in groups:
            # WHOLE CLIPS ONLY. There is no part of a ten-second generation that costs
            # seven seconds, so a group that does not fit is not bought at all.
            cost = sum(item["seconds"] for item in group)
            if spent + cost > allowed_seconds:
                continue
            budgeted.extend(group)
            spent += cost
        if len(budgeted) < len(missing):
            logger.warning("elegant: the monthly video allowance covers %d of %d clips "
                           "(%ds of %ds) for this listing",
                           len(budgeted), len(missing), spent, wanted_seconds)
            plan.degradations.append(
                "this account is near its monthly limit for Elegant videos, so part of "
                "this one is shown as still pictures")

    # --- generate, in waves, with everything persisted as it lands -------------------
    #
    # NO LOCK, AND THAT IS THE DESIGN RATHER THAN AN OVERSIGHT. The only thing that runs
    # off this thread is the fal call itself: _work returns bytes or raises and touches
    # nothing shared, and concurrent.futures.as_completed yields back HERE, so every
    # counter, every store, every cache write and every persist happens on one thread in
    # a defined order. The waiting is concurrent; the money is not. A lock would suggest
    # the opposite, which is worse than no lock at all.
    def _finish(item, data):
        """Store one clip, cache it, and tell the caller to persist both. Main thread."""
        try:
            url = store_clip(item["stage"], item["key"], data)
        except Exception as e:
            # We paid for this one and cannot keep it. Already counted as paid; counted
            # as LOST too, so the caller knows a retry of this pass would buy it again.
            plan.clip_seconds_lost += item["seconds"]
            logger.warning("elegant: generated the %s clip but could not store it (%s)",
                           item["stage"], e)
            return
        if not url:
            plan.clip_seconds_lost += item["seconds"]
            return
        item["url"] = url
        plan.clip_cache[item["key"]] = url
        if on_cached is not None:
            try:
                on_cached(dict(plan.clip_cache))
            except Exception as e:
                # A cache we cannot persist costs money on the NEXT render, not this one.
                logger.warning("elegant: could not persist the clip cache after %s "
                               "(%s) -- a regenerate may pay for it again",
                               item["stage"], e)

    def _work(item):
        """Ask fal for one clip. Runs in a worker thread and touches NOTHING shared --
        it returns bytes or raises, and every counter, every store and every persist
        happens back on the main thread. That is what keeps the money accounting
        single-threaded while the waiting is not."""
        # `deadline` BY NAME. Passed positionally it landed on fal_video.generate_clip's
        # `negative_prompt` and killed every clip before it was sent; see that function.
        return generate_clip(item["prompt"], item["start_url"], item["end_url"],
                             item["seconds"], deadline=overall)

    index = 0
    while index < len(budgeted):
        if plan.clips_rate_limited:
            # fal has already told us this account is at its limit. Asking again, four
            # more times, for the same answer only makes the agent wait for it.
            break
        if time.monotonic() >= overall:
            logger.warning("elegant: out of time after %d clip(s); the rest of the film "
                           "is stills", index)
            plan.degradations.append(
                "the video took too long to prepare, so the later shots are shown as "
                "still pictures")
            break
        if still_ours is not None:
            try:
                ours = still_ours()
            except Exception as e:
                # Cannot tell. Carry on: wrongly stopping a render that IS ours costs
                # the agent their video, which is worse than the spending this guard
                # saves.
                logger.warning("elegant: could not confirm we still own this render "
                               "(%s); continuing", e)
                ours = True
            if not ours:
                # Another worker owns this render now, so anything we buy from here is
                # money spent on a film that will be discarded. The clips already bought
                # are cached and the new owner will use them.
                logger.warning("elegant: this render was taken over by another worker "
                               "after %d clip(s); not buying any more", index)
                break
        wave = budgeted[index:index + lanes]
        index += len(wave)
        # ONE CHARGE PER SUBMIT, COUNTED WHEN IT IS ACCEPTED. The submit happens inside
        # the worker, so the count happens here the instant the worker comes back -- on
        # success from the duration we asked for, and on failure from what the error says
        # fal charged. Either way it is written through on_paid before the next wave, so
        # a worker killed mid-pass has already recorded what it spent.
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=len(wave), thread_name_prefix="elegant-clip") as pool:
            futures = {pool.submit(_work, item): item for item in wave}
            for future in concurrent.futures.as_completed(futures):
                item = futures[future]
                try:
                    data = future.result()
                except Exception as e:
                    billed = fal_video.billed_seconds(e)
                    plan.clip_seconds_paid += billed
                    plan.clip_seconds_lost += billed
                    if billed and on_paid is not None:
                        try:
                            on_paid(billed)
                        except Exception as inner:
                            logger.warning("elegant: could not record %ds billed (%s)",
                                           billed, inner)
                    if isinstance(e, fal_video.FalNotConfigured):
                        logger.warning("elegant: %s", e)
                    elif isinstance(e, fal_video.FalRateLimited):
                        # NOT a degradation, and the one failure the caller must act on
                        # rather than report: the clip is makeable, there is simply no
                        # room right now. Flagged so this pass stops asking; whether to
                        # DEFER is the caller's decision and turns on clip_seconds_lost,
                        # because a 429 after an accepted submit has already been paid
                        # for and a deferral would pay again.
                        logger.warning("elegant: fal is at this account's concurrency "
                                       "limit (%s); not asking it for more clips in "
                                       "this pass", e)
                        plan.clips_rate_limited = True
                    elif isinstance(e, fal_video.FalRefused):
                        logger.info("elegant: fal declined the %s clip (%s)",
                                    item["stage"], e)
                    else:
                        logger.warning("elegant: could not generate the %s clip (%s: %s)",
                                       item["stage"], type(e).__name__, e)
                    continue
                plan.clip_seconds_paid += item["seconds"]
                if on_paid is not None:
                    try:
                        on_paid(item["seconds"])
                    except Exception as inner:
                        logger.warning("elegant: could not record %ds billed (%s)",
                                       item["seconds"], inner)
                _finish(item, data)

    # --- what the film actually got ---------------------------------------------------
    missing_shots = 0
    for item in stages:
        if not item["url"]:
            missing_shots += 1
            continue
        entry = {"stage": item["stage"], "url": item["url"],
                 "seconds": item["seconds"], "notice": item["notice"],
                 "backdrop_url": item["backdrop_url"]}
        if item["kind"] == "exterior":
            plan.exterior_clips.append(entry)
        elif item["kind"] == "walkin":
            plan.walkin_clip = entry
        else:
            plan.room_clips[item["index"]] = entry
    if missing_shots and not any(
            "still pictures" in note or "could not be animated" in note
            for note in plan.degradations):
        # Only said once, and only when nothing above has already said it: a film with
        # two of its six shots as stills has one explanation, not two.
        plan.degradations.append(
            f"{missing_shots} shot(s) could not be animated, so they are shown as still "
            "pictures")

    logger.info(
        "elegant clip plan (%s): %d exterior clip(s) + %s + %d room clip(s), %ds billed "
        "(about US$%.2f)%s", treatment.id, len(plan.exterior_clips),
        "a walk-in" if plan.walkin_clip else "NO WALK-IN",
        len(plan.room_clips), plan.clip_seconds_paid,
        fal_video.clip_cost_usd(plan.clip_seconds_paid),
        ("; %ds of it BILLED AND LOST (about US$%.2f spent with nothing to cache, so a "
         "retry of this pass would buy it again)"
         % (plan.clip_seconds_lost,
            fal_video.clip_cost_usd(plan.clip_seconds_lost)))
        if plan.clip_seconds_lost else "")
    return plan
