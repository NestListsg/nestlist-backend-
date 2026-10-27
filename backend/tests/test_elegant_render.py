"""The Elegant template: the treatment table, the disclaimers, the money, the queue.

WHAT THIS FILE IS FOR, in the order the things can hurt somebody:

  1. NOTHING IS FABRICATED. The competitor film Jane was shown added a red supercar and a
     yellow one to the driveway. Inventing a property's features is the hardest standing
     rule there is, so every prompt in the treatment table is checked here for the
     positive pinning that keeps the driveway empty, and the negative prompt is checked
     for the exclusions that back it up.
  2. THE NEIGHBOURS ARE ANONYMISED FIRST. Every generated keyframe of every clip must
     derive from the anonymised photograph, never from the agent's original. That is an
     ORDERING property, so it is checked as one.
  3. THE DISCLOSURES ARE JANE'S EXACT WORDS AND ARE ON EVERY GENERATED SHOT. Checked over
     an exhaustive sweep of film layouts rather than a handful, because a disclosure that
     is lost in one unusual layout is still a disclosure that was lost.
  4. THE MONEY. One Elegant listing is about US$3.85 -- fifteen Dramatics. The ceiling is
     in SECONDS because seconds are what is billed, it is reserved before the first
     generation, it is counted when fal is CHARGED rather than when bytes arrive, and
     what has been bought is persisted as it lands so a worker dying mid-pass cannot cost
     it twice.
  5. THE PROMPTS FAL REFUSES. fal's safety layer refuses "explosion", "debris",
     "demolish", "collapse" and their relatives EVEN AS EXCLUSIONS. Two generations were
     refused before that was understood. Every string this feature can send is screened
     here, so a reworded prompt cannot quietly become a refusal an agent waits for.

No ffmpeg, no network, no model, no key, and NO REAL GENERATION: every fal call is a
stub, which is the only responsible way to develop a feature that costs US$0.70 a call
against Jane's own balance.

Run: python3 tests/test_elegant_render.py
"""
import ast, io, itertools, os, sys, threading, time, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
from PIL import Image, ImageDraw
import fal_images
import fal_video
import old_to_new
import video_renderer as vr

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))


BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEARDOWN = old_to_new.ELEGANT_TREATMENTS[old_to_new.TREATMENT_TEARDOWN]
MAKEOVER = old_to_new.ELEGANT_TREATMENTS[old_to_new.TREATMENT_MAKEOVER]


def every_string():
    """Every prompt and negative prompt this feature can send to fal, labelled.

    Built from the TABLE rather than listed by hand, so a third treatment is screened the
    day it is added instead of the day somebody remembers to add it here.
    """
    out = [("the cleared-plot image prompt", old_to_new.cleared_prompt()),
           ("the anonymise prompt", old_to_new.anonymise_prompt()),
           ("the rebuild image prompt", old_to_new.rebuild_prompt()),
           ("the no-fabrication negative prompt", fal_video.NO_FABRICATION_NEGATIVE)]
    for name, treatment in old_to_new.ELEGANT_TREATMENTS.items():
        for stage in treatment.exterior_stages:
            out.append(("%s / %s" % (name, stage.stage), stage.prompt()))
        # Both the house default AND a model-written description, because the room prompt
        # is assembled from both and either half could carry a refused word.
        out.append(("%s / room (defaults)" % name, treatment.room_stage.prompt()))
        out.append(("%s / room (described)" % name,
                    treatment.room_stage.prompt(
                        "the dark carved rosewood sofas and the glass display cabinet",
                        "a low cream sofa, a marble table and a potted olive tree")))
    return out


# ===========================================================================
print("\n[1] Nothing is fabricated, and nothing is phrased in a way fal refuses")
# ===========================================================================
for label, text in every_string():
    refused = fal_images.screen_prompt(text)
    check("%s uses no word fal refuses" % label, refused is None, str(refused))

# THE TEAR-DOWN IS DESCRIBED WITHOUT ANY OF THE OBVIOUS WORDS, which is the whole of the
# spec's prompt-safety note applied to the hardest shot in the product: a house coming
# down, written in words that get generated rather than refused.
teardown_prompt = TEARDOWN.exterior_stages[0].prompt()
check("the tear-down shot really does describe a house coming down",
      all(phrase in teardown_prompt for phrase in
          ("comes down", "lean inward", "folds down", "dust")), teardown_prompt[:120])
# The spec's rule is about ONE trap: fal refuses explosion / blast / fire / debris and
# their relatives EVEN AS EXCLUSIONS, so those words cannot appear in any polarity. It is
# not a rule against the word "no" -- the spec's own verbatim opening is "Locked-off
# static camera, NO camera movement", which is what stops the camera drifting. So what is
# checked is that the tear-down itself is described positively: nothing in this prompt
# says what must not happen to the house.
negations = [bit.strip() for bit in teardown_prompt.replace(",", ".").split(".")
             if " no " in (" " + bit.strip().lower() + " ")
             or bit.strip().lower().startswith("no ")]
check("the tear-down is described positively -- the only negation in it is the camera "
      "lock the spec itself prescribes",
      all("camera" in bit.lower() for bit in negations), str(negations))
check("the rebuild shot describes the new house RISING, not appearing",
      all(phrase in TEARDOWN.exterior_stages[1].prompt() for phrase in
          ("rises", "lift up into place", "lowers onto")))

# THE DRIVEWAY STAYS EMPTY. Positive pinning in every exterior prompt...
for stage in TEARDOWN.exterior_stages:
    text = stage.prompt()
    check("%s pins the driveway and the street empty, so no car can be invented there"
          % stage.stage,
          "driveway and the street stay empty" in text)
    check("%s pins the neighbouring buildings unchanged, so the anonymising survives "
          "the motion" % stage.stage,
          "neighbouring buildings" in text and "stay exactly as they are" in text)
for name, treatment in old_to_new.ELEGANT_TREATMENTS.items():
    text = treatment.room_stage.prompt()
    check("%s's room prompt pins the structure -- walls, ceiling, floor, doorways and "
          "the view through the windows" % name,
          all(w in text for w in ("walls", "ceiling", "floor", "doorways", "windows",
                                  "same view through them")))
    check("%s's room prompt forbids anything arriving that was not named" % name,
          "Nothing appears that is not named here" in text)
    check("%s's room prompt keeps people and pets out" % name,
          "no people or pets appear" in text)

# ...and the negative prompt as the backstop for when the model ignores the pinning.
negative = fal_video.NO_FABRICATION_NEGATIVE.lower()
for invented in ("cars", "vehicles", "supercar", "people", "pets", "animals",
                 "added furniture", "added trees", "new landscaping", "signage"):
    check("the negative prompt excludes invented %s" % invented, invented in negative)
check("the negative prompt is sent on every clip, not only when a caller remembers it",
      "negative_prompt" in io.open(os.path.join(BACKEND, "fal_video.py"),
                                   encoding="utf-8").read().split("def _submit")[1]
      .split("def _collect")[0])


# ===========================================================================
print("\n[2] The neighbours are anonymised FIRST, and everything derives from that")
# ===========================================================================
# This is an ORDERING property, so it is checked as one: in the treatment table, the
# tear-down's first clip starts on the anonymised frame, and every other exterior still
# is generated from that same frame.
check("the tear-down's first clip BEGINS on the anonymised photograph",
      TEARDOWN.exterior_stages[0].start_role == old_to_new.ROLE_EXTERIOR_ANON)
check("its end frame is the cleared plot, which is what forces a real tear-down rather "
      "than one picture washing into another",
      TEARDOWN.exterior_stages[0].end_role == old_to_new.ROLE_EXTERIOR_CLEARED)
check("the second clip picks up exactly where the first ended, so the two cut together "
      "with no visible join",
      TEARDOWN.exterior_stages[1].start_role == TEARDOWN.exterior_stages[0].end_role)
check("and it ends on the rebuilt house",
      TEARDOWN.exterior_stages[1].end_role == old_to_new.ROLE_EXTERIOR_NEW)

ORDER = []


def fake_image(prompt, source_urls, deadline=None):
    ORDER.append((list(source_urls), prompt))
    return b"jpegbytes"


plan = old_to_new.build_plan(
    "ORIGINAL", [("r0", None), ("r1", None)], {},
    generate_image=fake_image,
    store_image=lambda role, key, data: "https://x/%s.jpg" % role,
    want_cleared=True)
check("the anonymised exterior is generated FIRST, from the agent's original",
      ORDER[0][0] == ["ORIGINAL"] and "Replace the building" in ORDER[0][1],
      str(ORDER[0][0]))
derived = [sources for sources, _p in ORDER[1:3]]
check("and BOTH the rebuild and the cleared plot are generated from the anonymised "
      "frame, never from the original -- otherwise the real neighbours come back on "
      "screen in the middle of the film",
      derived == [["https://x/exterior_anon.jpg"], ["https://x/exterior_anon.jpg"]],
      str(derived))
check("the cleared plot lands on the plan", plan.cleared_url is not None)
check("a Dramatic plan does NOT pay for a cleared plot -- it has no use for one",
      old_to_new.build_plan("O2", [], {}, generate_image=lambda *a, **k: b"x",
                            store_image=lambda r, k, d: "https://x/%s.jpg" % r)
      .cleared_url is None)
# And the one failure that must never degrade to the real photograph.
noanon = old_to_new.build_plan(
    "ORIGINAL", [("r0", None)], {},
    generate_image=lambda *a, **k: (_ for _ in ()).throw(
        fal_images.FalRefused("no")),
    store_image=lambda r, k, d: "https://x/%s.jpg" % r, want_cleared=True)
check("with no anonymised frame there is NO exterior at all -- not the real photograph, "
      "which would put the neighbours Jane wants hidden back on screen",
      noanon.opening_url is None and noanon.exterior_url is None
      and noanon.cleared_url is None)


# ===========================================================================
print("\n[3] The disclaimers: Jane's exact words, on every generated shot")
# ===========================================================================
check("the tear-down / rebuild line is Jane's, to the character",
      vr.ELEGANT_CONCEPT_NOTICE_TEXT.replace("\n", " ")
      == "An AI concept of what could be built here. Rebuild subject to approval.",
      repr(vr.ELEGANT_CONCEPT_NOTICE_TEXT))
check("the redesigned-interior line is Jane's, to the character",
      vr.ELEGANT_INTERIOR_NOTICE_TEXT.replace("\n", " ")
      == "AI-generated visualisation, not an actual photo of the property",
      repr(vr.ELEGANT_INTERIOR_NOTICE_TEXT))
check("the opening real photograph keeps the privacy line it already shipped with",
      vr.PRIVACY_NOTICE_TEXT == "Neighbouring homes altered for privacy")
check("'Rebuild subject to approval' is present and is the part a reworded version "
      "would lose -- a rebuild in Singapore needs URA and BCA approval",
      "Rebuild subject to approval" in vr.ELEGANT_CONCEPT_NOTICE_TEXT)
check("each is TWO lines, because a sentence on one line at this font size would run "
      "off a 1080-wide frame and a clipped disclosure is not a disclosure",
      vr.ELEGANT_CONCEPT_NOTICE_TEXT.count("\n") == 1
      and vr.ELEGANT_INTERIOR_NOTICE_TEXT.count("\n") == 1)
multi = vr._notice_filters(vr.ELEGANT_CONCEPT_NOTICE_TEXT, "/tmp", "e")
check("a two-line notice is still drawn as ONE box, so it reads as one label",
      len(multi) == 1 and multi[0].count("box=1") == 1
      and "line_spacing=" in multi[0])
check("THE DRAMATIC IS NOT TOUCHED: its own shorter line still ships as it was, because "
      "changing what is on screen in a live feature is Jane's call and not this one's",
      vr.AI_NOTICE_TEXT == "AI visualisation")
check("the treatment table decides WHICH line, by kind, and the renderer owns the text",
      vr.notice_text(old_to_new.NOTICE_AI_CONCEPT) == vr.ELEGANT_CONCEPT_NOTICE_TEXT
      and vr.notice_text(old_to_new.NOTICE_AI_INTERIOR)
      == vr.ELEGANT_INTERIOR_NOTICE_TEXT
      and vr.notice_text(old_to_new.NOTICE_PRIVACY) == vr.PRIVACY_NOTICE_TEXT
      and vr.notice_text(old_to_new.NOTICE_NONE) is None)
check("an unknown notice kind still draws a disclosure rather than nothing -- a "
      "generated frame with no notice is the one failure worse than a wrong-looking one",
      vr.notice_text("something-nobody-has-written-yet") == vr.AI_NOTICE_TEXT)
for name, treatment in old_to_new.ELEGANT_TREATMENTS.items():
    for stage in treatment.exterior_stages:
        check("%s / %s carries the concept line" % (name, stage.stage),
              stage.notice == old_to_new.NOTICE_AI_CONCEPT)
    check("%s's rooms carry the interior line" % name,
          treatment.room_stage.notice == old_to_new.NOTICE_AI_INTERIOR)


# ===========================================================================
print("\n[4] THE SWEEP: every film layout, in both styles, stays clean")
# ===========================================================================
# 124 LAYOUTS, exhaustively: an opening or not (2) x a rebuilt exterior or not (2) x
# every room list from none up to four rooms, each room transformable or not
# (1 + 2 + 4 + 8 + 16 = 31). 2 x 2 x 31 = 124.
#
# Then each of those is swept again in the ELEGANT style, with and without exterior
# clips, and with the generated room clips present or absent -- which is where the new
# shot kinds have to behave. The properties checked are the film's: every cut is matched
# (or the camera jumps), no shot ever zooms below 1.0 (or a black edge shows), the last
# shot hands over to the closing card, and EVERY GENERATED SHOT CARRIES A DISCLOSURE
# while no real photograph carries one.
LAYOUTS = []
for has_opening in (True, False):
    for has_exterior in (True, False):
        for n in range(0, old_to_new.MAX_ROOMS + 1):
            for combo in itertools.product((True, False), repeat=n):
                LAYOUTS.append((has_opening, has_exterior, list(combo)))
check("the sweep is the full 124 layouts, not a handful", len(LAYOUTS) == 124,
      str(len(LAYOUTS)))

GENERATED_NOTICES = {vr.AI_NOTICE_TEXT, vr.ELEGANT_CONCEPT_NOTICE_TEXT,
                     vr.ELEGANT_INTERIOR_NOTICE_TEXT,
                     vr.ELEGANT_CONCEPT_AND_PRIVACY_NOTICE_TEXT}

WALKIN = (old_to_new.WALKIN_STAGE.stage, old_to_new.WALKIN_STAGE.seconds)
EXT_STAGES = tuple((st.stage, st.seconds) for st in TEARDOWN.exterior_stages)
VARIANTS = []
for has_opening, has_exterior, combo in LAYOUTS:
    VARIANTS.append(("dramatic", has_opening, has_exterior, combo, (), None, {}))
    # Elegant, exterior as a still (the gentle makeover), with the walk-in, rooms all
    # animated.
    VARIANTS.append(("elegant-makeover", has_opening, has_exterior, combo, (), WALKIN,
                     {i: old_to_new.ROOM_CLIP_SECONDS
                      for i, ok in enumerate(combo) if ok}))
    # Elegant, tear-down: two exterior clips, the walk-in, rooms all animated.
    VARIANTS.append(("elegant-teardown", has_opening, has_exterior, combo, EXT_STAGES,
                     WALKIN,
                     {i: old_to_new.ROOM_CLIP_SECONDS
                      for i, ok in enumerate(combo) if ok}))
    # Elegant where only SOME rooms got a clip -- the real degradation: the rest fall
    # back to the Dramatic's own free transformation.
    VARIANTS.append(("elegant-partial", has_opening, has_exterior, combo, EXT_STAGES,
                     WALKIN,
                     {i: old_to_new.ROOM_CLIP_SECONDS
                      for i, ok in enumerate(combo) if ok and i % 2 == 0}))
    # And Elegant with NO walk-in, which is what a film gets when the rebuilt exterior
    # could not be made. It has to stay a clean film, with the old hard cut through the
    # door back in place.
    VARIANTS.append(("elegant-no-walkin", has_opening, has_exterior, combo, EXT_STAGES,
                     None,
                     {i: old_to_new.ROOM_CLIP_SECONDS
                      for i, ok in enumerate(combo) if ok}))

bad_cuts = bad_zoom = bad_push = bad_notice = bad_kind = bad_walkin = 0
seen_kinds = set()
seen_roles = set()
for label, has_opening, has_exterior, combo, ext_clips, wi, room_clips in VARIANTS:
    style = (vr.DRAMATIC_STYLE_ID if label == "dramatic" else vr.ELEGANT_STYLE_ID)
    shots = vr.dramatic_shots(has_opening, has_exterior, combo, style=style,
                              exterior_clips=ext_clips, walkin=wi,
                              room_clips=room_clips)
    # THE WALK-IN'S PLACE IN THE FILM, in every layout: it is never the first shot (it
    # continues the exterior's camera, so there must be an exterior in front of it), and
    # it always comes before every room.
    walkins = [i for i, sh in enumerate(shots) if sh["role"] == "walkin"]
    if walkins:
        first_room = next((i for i, sh in enumerate(shots)
                           if sh["role"] == "room"), len(shots))
        if walkins[0] == 0 or len(walkins) != 1 or walkins[0] > first_room:
            bad_walkin += 1
    if not shots:
        continue
    for i in range(len(shots) - 1):
        a, b = shots[i], shots[i + 1]
        end = vr._zoom_at(a["knots"], int(round(a["seconds"] * vr.FPS)) - 1)
        if abs(end - vr._zoom_at(b["knots"], 0)) > 1e-9:
            bad_cuts += 1
    if shots[-1]["push"] is not None:
        bad_push += 1
    for shot in shots:
        seen_kinds.add(shot["kind"])
        seen_roles.add(shot["role"])
        zs = [vr._zoom_at(shot["knots"], f)
              for f in range(int(round(shot["seconds"] * vr.FPS)))]
        if min(zs) < 1.0 - 1e-9:
            bad_zoom += 1
        # THE DISCLOSURE RULE, over every layout: a generated shot carries one of the
        # notices, a real photograph carries the privacy line (the opening) or nothing
        # at all (a room shown as it is today).
        generated = shot["kind"] == "clip" or shot["kind"] == "room" \
            or shot["role"] == "exterior"
        if generated and shot["notice"] not in GENERATED_NOTICES:
            bad_notice += 1
        if shot["role"] == "room" and shot["kind"] == "still" \
                and shot["notice"] is not None:
            bad_notice += 1
        if shot["role"] == "opening" and shot["notice"] != vr.PRIVACY_NOTICE_TEXT:
            bad_notice += 1
        if shot["kind"] == "clip" and "stage" not in shot:
            bad_kind += 1

check("%d layout/style combinations: every cut is matched" % len(VARIANTS),
      bad_cuts == 0, "%d bad cuts" % bad_cuts)
check("no shot in any of them ever zooms below 1.0, so no black edge can show",
      bad_zoom == 0, "%d" % bad_zoom)
check("the last shot of every film hands over to the closing card",
      bad_push == 0, "%d" % bad_push)
check("EVERY generated shot carries a disclosure and no real photograph carries the AI "
      "one, in all %d of them" % len(VARIANTS), bad_notice == 0, "%d" % bad_notice)
check("every clip shot names the stage its file is keyed by", bad_kind == 0)
check("THE WALK-IN IS NEVER THE FIRST SHOT and always comes before every room, in all "
      "%d of them -- it continues the exterior's camera up the drive, so there has to be "
      "an exterior in front of it" % len(VARIANTS), bad_walkin == 0, "%d" % bad_walkin)
check("and the sweep really does exercise all three shot kinds and all four roles",
      seen_kinds == {"still", "room", "clip"}
      and seen_roles == {"opening", "exterior", "walkin", "room"},
      "%s %s" % (seen_kinds, seen_roles))

# THE DRAMATIC'S OWN FILM MUST NOT HAVE MOVED, AND THIS COMPARES AGAINST THE SHIPPED
# VERSION rather than against itself.
#
# The first attempt at this check compared dramatic_shots with its new arguments
# defaulted against the same call with them passed empty, which is the same call and
# proves nothing. So the real answer was taken from the commit this work started at
# (79fd52b) -- every one of the 124 layouts, dumped -- and the finding was that exactly
# one thing changed anywhere: the OPENING shot now carries `continuous: False`, a key it
# did not have before. `shot.get("continuous")` reads False either way, so every knot,
# notice, anchor, push, duration and shot count is byte-identical and no rendered frame
# moves. That is recorded here rather than described, so the next change to this function
# is measured against the same baseline.
DRAMATIC_BASELINE_KEYS = {
    # shot role -> the keys that shot had at 79fd52b
    "opening": {"anchor", "kind", "knots", "notice", "push", "role", "seconds"},
    "exterior": {"anchor", "continuous", "kind", "knots", "notice", "push", "role",
                 "seconds"},
}
drifted = []
for has_opening, has_exterior, combo in LAYOUTS:
    for shot in vr.dramatic_shots(has_opening, has_exterior, combo):
        expected = DRAMATIC_BASELINE_KEYS.get(shot["role"])
        if expected is None:
            continue
        added = set(shot) - expected
        removed = expected - set(shot)
        # The one known, measured addition. Anything else is a drift.
        if removed or added - {"continuous"}:
            drifted.append((shot["role"], sorted(added), sorted(removed)))
        if "continuous" in added and shot["continuous"] is not False:
            drifted.append((shot["role"], "continuous", shot["continuous"]))
check("the Dramatic's shot table is unchanged in all 124 layouts except for one added "
      "key whose value cannot change a frame -- measured against 79fd52b, not against "
      "itself", not drifted, str(drifted[:3]))
check("and the Dramatic still never produces a clip shot or a walk-in, whatever it is "
      "asked",
      not any(sh["kind"] == "clip" or sh["role"] == "walkin"
              for layout in LAYOUTS
              for sh in vr.dramatic_shots(*layout)))

# The specific things the Elegant's camera has to do.
shots = vr.dramatic_shots(True, True, [True, True],
                          style=vr.ELEGANT_STYLE_ID,
                          exterior_clips=EXT_STAGES, walkin=WALKIN,
                          room_clips={0: 10, 1: 10})
check("the tear-down film is opening + two exterior clips + the walk-in + two rooms",
      [s["role"] for s in shots]
      == ["opening", "exterior", "exterior", "walkin", "room", "room"],
      str([s["role"] for s in shots]))
check("ALL THREE EXTERIOR SHOTS ARE ONE MOVE on one plot -- same anchor, and every shot "
      "after the first carries the move on rather than easing out of it, so only the "
      "house changes across the two cuts",
      all(s["anchor"] == vr.DOOR_CENTRE for s in shots[:3])
      and shots[1]["continuous"] is True and shots[2]["continuous"] is True
      and shots[0]["continuous"] is False)
check("the last exterior shot drives at the front door",
      abs(vr._zoom_at(shots[2]["knots"],
                      int(round(shots[2]["seconds"] * vr.FPS)) - 1)
          - vr.DOOR_PUSH_ZOOM) < 1e-9)
check("THE WALK-IN IS THE SHOT THAT GOES THROUGH THE DOOR, so it opens at exactly the "
      "zoom the exterior drove to and CARRIES THE MOVE ON rather than easing out of it "
      "-- easing here would make the house jump backwards at the moment the real move "
      "begins",
      shots[3]["role"] == "walkin" and shots[3]["anchor"] == vr.DOOR_CENTRE
      and shots[3]["continuous"] is True
      and abs(vr._zoom_at(shots[3]["knots"], 0) - vr.DOOR_PUSH_ZOOM) < 1e-9)
check("and the room it arrives in is an ordinary arrival on the frame's centre, not "
      "another door push -- the walk-in has already done that journey for real",
      shots[4]["anchor"] == vr.FRAME_CENTRE and shots[5]["anchor"] == vr.FRAME_CENTRE)
check("WITHOUT a walk-in the old matched push and pull is back, which is how that "
      "journey was faked before this shot existed",
      vr.dramatic_shots(True, True, [True, True], style=vr.ELEGANT_STYLE_ID,
                        exterior_clips=EXT_STAGES,
                        room_clips={0: 10, 1: 10})[3]["anchor"] == vr.DOOR_CENTRE)
check("a room clip is ten seconds, which the spec measured -- five is too short for "
      "everything to move out and back in",
      shots[4]["seconds"] == 10.0 and old_to_new.ROOM_CLIP_SECONDS == 10)
check("the walk-in carries the concept line, because half of it is a house that does "
      "not exist yet", shots[3]["notice"] == vr.ELEGANT_CONCEPT_NOTICE_TEXT)
check("a walk-in with no exterior in front of it is dropped rather than opening the "
      "film on a house it never showed",
      not any(sh["role"] == "walkin" for sh in vr.dramatic_shots(
          False, False, [True], style=vr.ELEGANT_STYLE_ID, walkin=WALKIN,
          room_clips={0: 10})))

mixed = vr.dramatic_shots(True, True, [True, True, True],
                          style=vr.ELEGANT_STYLE_ID, room_clips={1: 10})
rooms_only = [s for s in mixed if s["role"] == "room"]
check("A ROOM WHOSE CLIP COULD NOT BE BOUGHT FALLS BACK TO THE DRAMATIC'S OWN FREE "
      "TRANSFORMATION, not to a plain photograph -- it costs nothing and still shows "
      "the before and the after",
      [s["kind"] for s in rooms_only] == ["room", "clip", "room"],
      str([s["kind"] for s in rooms_only]))
check("and the local treatments still rotate over the rooms that use them, so two "
      "neighbouring fallbacks do not look like the same effect",
      [s.get("treatment") for s in rooms_only]
      == [vr.DRAMATIC_TREATMENTS[0], None, vr.DRAMATIC_TREATMENTS[1]],
      str([s.get("treatment") for s in rooms_only]))
check("the Elegant's still rebuilt exterior carries the CONCEPT line, because it is "
      "still a house that does not exist on a plot that does",
      vr.dramatic_shots(True, True, [], style=vr.ELEGANT_STYLE_ID)[1]["notice"]
      == vr.ELEGANT_CONCEPT_NOTICE_TEXT)
orphan = vr.dramatic_shots(False, True, [True], style=vr.ELEGANT_STYLE_ID,
                           exterior_clips=(("teardown", 5),), walkin=WALKIN,
                           room_clips={0: 10})
check("a clip whose first frame IS the anonymised photograph, with no opening still in "
      "front of it, carries BOTH lines -- a privacy disclosure must not be losable by a "
      "missing shot",
      orphan[0]["notice"] == vr.ELEGANT_CONCEPT_AND_PRIVACY_NOTICE_TEXT
      and vr.PRIVACY_NOTICE_TEXT in orphan[0]["notice"])


# ===========================================================================
print("\n[5] The money: seconds, reserved up front, counted when fal is CHARGED")
# ===========================================================================
check("the unit is a second of video, because that is what Kling bills",
      fal_video.COST_PER_SECOND_USD == 0.07)
check("one tear-down listing with four rooms is 55 seconds of video -- two 5s exterior "
      "clips, the 5s walk-in, and 10s a room",
      TEARDOWN.listing_seconds(4) == 55, str(TEARDOWN.listing_seconds(4)))
check("which is about US$3.85, inside the US$3.50-4.50 a listing this was costed against",
      abs(fal_video.clip_cost_usd(TEARDOWN.listing_seconds(4)) - 3.85) < 0.01,
      "%.2f" % fal_video.clip_cost_usd(TEARDOWN.listing_seconds(4)))
check("the gentle makeover is two clips cheaper, at 45 seconds / about US$3.15",
      MAKEOVER.listing_seconds(4) == 45
      and abs(fal_video.clip_cost_usd(45) - 3.15) < 0.01,
      str(MAKEOVER.listing_seconds(4)))
check("a listing with no rooms buys no walk-in, because there is nowhere to walk in to",
      MAKEOVER.listing_seconds(0) == 0
      and TEARDOWN.listing_seconds(0) == 2 * old_to_new.EXTERIOR_CLIP_SECONDS)
check("one Elegant costs about as much as fifteen Dramatics, which is why it has its "
      "own ceiling in its own unit",
      fal_video.clip_cost_usd(50) / (6 * fal_images.COST_PER_IMAGE_USD) > 14)
CAP = old_to_new.MAX_VIDEO_SECONDS_PER_AGENT_PER_MONTH
check("the cap is ONE obvious constant", CAP == 165)
check("and it permits EXACTLY three tear-down films an agent a month, about US$11.55 -- "
      "the same order as the picture allowance Jane already agreed to",
      CAP // TEARDOWN.listing_seconds(4) == 3
      and abs(fal_video.clip_cost_usd(CAP) - 11.55) < 0.01,
      "%.2f" % fal_video.clip_cost_usd(CAP))
check("fifty agents at the whole allowance is about US$578 a month, worst case",
      abs(50 * fal_video.clip_cost_usd(CAP) - 577.5) < 1.0)
check("the cap is still derived from the table rather than guessed, so adding a shot "
      "cannot leave it meaning something nobody intended",
      CAP == 3 * TEARDOWN.listing_seconds(4))

check("a fresh agent may generate everything they asked for",
      old_to_new.reserve_video_budget(50, 0) == 50)
check("an agent near the line is TRIMMED to what is left, not refused",
      old_to_new.reserve_video_budget(50, CAP - 20) == 20)
try:
    old_to_new.reserve_video_budget(50, CAP)
    check("an agent at the line is refused, with a message about the Classic and the "
          "Dramatic still being available", False)
except old_to_new.VideoBudgetExhausted as e:
    check("an agent at the line is refused, with a message about the Classic and the "
          "Dramatic still being available",
          "Classic and Dramatic" in str(e) and "resets" in str(e), str(e))
check("and it is a DIFFERENT exception from the picture allowance, because the two "
      "leave the agent with different things to do",
      not issubclass(old_to_new.VideoBudgetExhausted, old_to_new.BudgetExhausted))
check("there is no rounding courtesy that lets one more clip through at seventy cents",
      old_to_new.reserve_video_budget(10, CAP - 1) == 1)

# fal_video's own accounting.
check("every duration we can ask for is one Kling accepts",
      [fal_video.snap_seconds(n) for n in (1, 5, 6, 7, 8, 10, 30, None, "x")]
      == [5, 5, 5, 5, 10, 10, 10, 5, 5],
      str([fal_video.snap_seconds(n) for n in (1, 5, 6, 7, 8, 10, 30, None, "x")]))
check("a tie between the two rounds DOWN, to the cheaper one",
      fal_video.snap_seconds(7) == 5 and fal_video.snap_seconds(8) == 10)
err = fal_images.FalTemporary("x")
err.seconds_billed = 10
check("an error carries what fal charged, in seconds", fal_video.billed_seconds(err) == 10)
check("and an exception from an injected stub that has never heard of it reads as zero "
      "rather than crashing inside a degradation handler",
      fal_video.billed_seconds(RuntimeError("no idea")) == 0)
check("the model's keyframe field names are DATA, so fal renaming them again is one "
      "row rather than a bug hunt",
      fal_video.model().start_field == "start_image_url"
      and fal_video.model().end_field == "end_image_url")
check("GENERATE_AUDIO IS PINNED OFF -- on it would double the bill to US$0.14 a second "
      "and put model-generated speech into an agent's listing video",
      fal_video.model().extra_payload.get("generate_audio") is False)
check("the price travels with the model, so switching version cannot leave a stale "
      "figure behind",
      fal_video.COST_PER_SECOND_USD == fal_video.model().cost_per_second)
check("the per-clip deadline is minutes, not the image client's ninety seconds -- a "
      "healthy Kling generation takes two to six minutes and throwing one away costs "
      "seventy cents",
      fal_video.CLIP_DEADLINE_SECONDS >= 300)
check("at most two submits per clip, because a wasted submit here is US$0.35-0.70",
      fal_video.MAX_SUBMITS == 2)
check("but several collect attempts, because collecting a clip already paid for is free",
      fal_video.MAX_COLLECT_ATTEMPTS > fal_video.MAX_SUBMITS)


# ===========================================================================
print("\n[5b] fal_video end to end, against a stub rather than Jane's balance")
# ===========================================================================
# NO REAL GENERATION HAPPENS IN THIS FILE OR ANYWHERE IN THIS WORK. One real clip is
# US$0.35 at the cheapest duration and Jane's whole fal balance was US$14.90, so the
# submit shape, the polling, the collection, the download and the billing are all proved
# against a stub endpoint that echoes our own request back -- which is also the only way
# to prove the thing that matters most about the real one: that a 4xx body cannot carry
# the key into Railway's logs.
import json as _json                                                     # noqa: E402

os.environ["FAL_KEY"] = "fakeid-0123456789abcdef:fakesecret-0123456789abcdef"


class _Reply:
    def __init__(self, code, body, headers=None):
        self.status_code = code
        self._body = body
        self.headers = headers or {}
        self.raw = types.SimpleNamespace(_fp=None)

    @property
    def text(self):
        return self._body if isinstance(self._body, str) else ""

    def json(self):
        return _json.loads(self._body)

    def iter_content(self, chunk_size=0):
        yield self._body if isinstance(self._body, bytes) else b""

    def close(self):
        pass


def run_stub(script, prompt="the house comes down", seconds=10):
    """Drive generate_clip against a scripted endpoint. Returns (result, requests)."""
    seen = []
    def handler(method, url, headers=None, json=None, timeout=None, stream=None):
        seen.append({"method": method, "url": url, "body": json,
                     "auth": (headers or {}).get("Authorization")})
        return script(len(seen) - 1, url, headers)
    saved = fal_images.requests
    fal_images.requests = types.SimpleNamespace(
        request=handler,
        get=lambda url, timeout=None, stream=None: handler("GET", url),
        exceptions=types.SimpleNamespace())
    saved_sleep = time.sleep
    time.sleep = lambda s: None
    try:
        try:
            return fal_video.generate_clip(prompt, "https://x/a.jpg", "https://x/b.jpg",
                                           seconds), seen
        except Exception as e:
            return e, seen
    finally:
        fal_images.requests = saved
        time.sleep = saved_sleep


def happy(n, url, headers=None):
    if n == 0:
        return _Reply(200, _json.dumps({"status_url": "https://q/status",
                                        "response_url": "https://q/result"}))
    if n == 1:
        return _Reply(200, _json.dumps({"status": "IN_QUEUE"}))
    if n == 2:
        return _Reply(200, _json.dumps({"status": "COMPLETED"}))
    if n == 3:
        return _Reply(200, _json.dumps({"video": {"url": "https://files/clip.mp4"}}))
    return _Reply(200, b"m" * (fal_video.MIN_CLIP_BYTES + 10))


out, seen = run_stub(happy)
check("a healthy generation submits once, polls to COMPLETED, collects and downloads",
      isinstance(out, bytes) and len(seen) == 5
      and [s["method"] for s in seen] == ["POST", "GET", "GET", "GET", "GET"],
      str([s["method"] for s in seen]))
check("the submit is the queue API, at the endpoint in the model table",
      seen[0]["url"].endswith(fal_video.model().endpoint))
check("it carries the prompt, both keyframes under the model's own field names, the "
      "duration as a string, the no-fabrication negative, and generate_audio OFF",
      seen[0]["body"] == {
          "prompt": "the house comes down",
          "start_image_url": "https://x/a.jpg",
          "end_image_url": "https://x/b.jpg",
          "duration": "10",
          "negative_prompt": fal_video.NO_FABRICATION_NEGATIVE,
          "generate_audio": False,
      }, str(seen[0]["body"]))
check("and it sends NO aspect_ratio -- v2.6 has no such field, and the clip arriving in "
      "the photograph's own shape is what makes it sit at the stills' scale",
      "aspect_ratio" not in seen[0]["body"])
check("the key is sent in the header and nowhere else",
      "fakesecret" in (seen[0]["auth"] or "")
      and "fakesecret" not in _json.dumps(seen[0]["body"]))

# The expensive question: a failure after an accepted submit.
def dies_collecting(n, url, headers=None):
    if n == 0:
        return _Reply(200, _json.dumps({"status_url": "https://q/status",
                                        "response_url": "https://q/result"}))
    return _Reply(502, "bad gateway")


out, seen = run_stub(dies_collecting)
check("a submit fal ACCEPTED and then never handed over is billed for its whole "
      "duration -- the second old-to-new audit's most expensive finding, in a unit "
      "fifteen times dearer",
      isinstance(out, Exception) and fal_video.billed_seconds(out) == 10,
      "%r %s" % (out, fal_video.billed_seconds(out)))
check("and it NEVER submits again, because fal may be generating a clip we are already "
      "being charged for",
      len([s for s in seen if s["method"] == "POST"]) == 1,
      str(len([s for s in seen if s["method"] == "POST"])))
check("it did go back for the clip several times first, because collecting one already "
      "paid for is free", len(seen) > 3, str(len(seen)))


def refuses(n, url, headers=None):
    return _Reply(422, "blocked by the safety filter")


out, seen = run_stub(refuses)
check("a refusal fal answered with costs nothing and is not retried",
      isinstance(out, fal_images.FalRefused) and fal_video.billed_seconds(out) == 0
      and len(seen) == 1, "%r" % out)


def rate_limited(n, url, headers=None):
    return _Reply(429, "slow down")


out, seen = run_stub(rate_limited)
check("a rate limit on the submit is handed back for the JOB to wait on, not retried "
      "inside the agent's render, and costs nothing",
      isinstance(out, fal_images.FalRateLimited)
      and fal_video.billed_seconds(out) == 0 and len(seen) == 1, "%r" % out)


def completed_empty(n, url, headers=None):
    if n == 0:
        return _Reply(200, _json.dumps({"status_url": "https://q/status",
                                        "response_url": "https://q/result"}))
    if n == 1:
        return _Reply(200, _json.dumps({"status": "COMPLETED"}))
    return _Reply(200, _json.dumps({"detail": "we would not make this"}))


out, seen = run_stub(completed_empty)
check("COMPLETED with no video is permanent AND billed -- the quietest of the shapes "
      "that used to cost nothing on paper",
      isinstance(out, fal_images.FalRefused) and fal_video.billed_seconds(out) == 10,
      "%r %s" % (out, fal_video.billed_seconds(out)))


def tiny_body(n, url, headers=None):
    if n <= 3:
        return happy(n, url, headers)
    return _Reply(200, b"not a video")


out, seen = run_stub(tiny_body)
check("a few hundred bytes served as a clip is caught here rather than in ffmpeg, where "
      "it would fail the whole shot instead of degrading it",
      isinstance(out, Exception) and "too small to be a clip" in str(out), "%r" % out)


def echoes_the_key(n, url, headers=None):
    return _Reply(422, _json.dumps({"detail": "rejected %s" % headers["Authorization"],
                                    "echo": headers["Authorization"].replace(" ", "%20")}))


out, seen = run_stub(echoes_the_key)
check("AN ENDPOINT THAT ECHOES OUR OWN AUTHORIZATION HEADER BACK CANNOT PUT IT IN A LOG "
      "LINE: the body is not quoted at all, only its size and its field names",
      "fakesecret" not in str(out) and "fakeid" not in str(out)
      and "JSON fields" in str(out), str(out))
os.environ.pop("FAL_KEY", None)


# ===========================================================================
print("\n[6] build_clip_plan: what it buys, what it caches, what it degrades")
# ===========================================================================
def make_plan(rooms=2, cleared=True, contents=None):
    plan = old_to_new.Plan()
    plan.opening_url = "https://x/anon.jpg"
    plan.exterior_url = "https://x/new.jpg"
    plan.cleared_url = "https://x/cleared.jpg" if cleared else None
    plan.rooms = [{"before_url": "https://x/b%d.jpg" % i,
                   "after_url": "https://x/a%d.jpg" % i, "space": None}
                  for i in range(rooms)]
    plan.room_contents = contents or [(None, None)] * rooms
    return plan


class Spy:
    def __init__(self, fail=None, billed=0):
        self.calls = []
        self.paid = []
        self.cached = []
        self.fail = fail or {}
        self.billed = billed

    def generate(self, prompt, start, end, seconds, deadline=None):
        self.calls.append({"prompt": prompt, "start": start, "end": end,
                           "seconds": seconds})
        for needle, exc in self.fail.items():
            if needle in prompt:
                if self.billed:
                    exc.seconds_billed = self.billed
                raise exc
        return b"\0" * (fal_video.MIN_CLIP_BYTES + 1)

    def store(self, stage, key, data):
        return "https://x/clips/%s.mp4" % key.replace(":", "_")


spy = Spy()
plan = make_plan()
old_to_new.build_clip_plan(plan, TEARDOWN, generate_clip=spy.generate,
                          store_clip=spy.store, clip_cache={},
                          on_paid=spy.paid.append, on_cached=spy.cached.append,
                          concurrency=1)
check("a cold tear-down buys two exterior clips, the walk-in, and one per room",
      len(spy.calls) == 5, str(len(spy.calls)))
check("THE WALK-IN IS BOUGHT AS STANDARD, not as an option somebody has to turn on -- "
      "it is the effect Jane has praised most and asked for again by name",
      plan.walkin_clip is not None
      and TEARDOWN.walkin is not None and MAKEOVER.walkin is not None)
walkin_call = [c for c in spy.calls if "up the driveway" in c["prompt"]][0]
check("it starts on the REBUILT EXTERIOR and lands on the first room AS IT IS TODAY, so "
      "its last frame is the first frame of the shot that follows and the join is "
      "invisible",
      walkin_call["start"] == "https://x/new.jpg"
      and walkin_call["end"] == plan.rooms[0]["before_url"],
      "%s -> %s" % (walkin_call["start"], walkin_call["end"]))
check("and it is five seconds, which is one constant to change if a human finds it "
      "rushed", walkin_call["seconds"] == 5
      and old_to_new.WALKIN_CLIP_SECONDS == 5)
check("it begins at the DRIVEWAY rather than at the facade, which is the change Jane "
      "asked for on the spec's own version of this shot",
      "starts outside at the gate" in walkin_call["prompt"]
      and "up the driveway" in walkin_call["prompt"])
check("and it comes to rest looking across the LIVING ROOM, named in the prompt",
      "into the living room inside, coming to rest looking across the room"
      in walkin_call["prompt"])
check("it is ONE continuous move from outside to inside, with no cut",
      "One continuous flowing forward move from the driveway to inside the house"
      in walkin_call["prompt"])
check("and it invents nothing on the way -- no car in the drive, no people",
      "driveway and the street stay empty" in walkin_call["prompt"]
      and "Nothing appears that is not in these two photographs"
      in walkin_call["prompt"])
check("the exterior clips come first and run anon -> cleared -> rebuilt",
      [(c["start"], c["end"]) for c in spy.calls[:2]]
      == [("https://x/anon.jpg", "https://x/cleared.jpg"),
          ("https://x/cleared.jpg", "https://x/new.jpg")],
      str([(c["start"], c["end"]) for c in spy.calls[:2]]))
check("every room clip runs from that room's own before to its own after",
      [(c["start"], c["end"]) for c in spy.calls[3:]]
      == [("https://x/b0.jpg", "https://x/a0.jpg"),
          ("https://x/b1.jpg", "https://x/a1.jpg")])
check("the exterior clips and the walk-in are 5s and the rooms 10s, as the table says",
      [c["seconds"] for c in spy.calls] == [5, 5, 5, 10, 10],
      str([c["seconds"] for c in spy.calls]))
check("the plan carries them in cut order",
      [e["stage"] for e in plan.exterior_clips] == ["teardown", "rebuild"]
      and sorted(plan.room_clips) == [0, 1])
check("35 seconds were billed, and the plan says so",
      plan.clip_seconds_paid == 35 and plan.clip_seconds_lost == 0,
      str(plan.clip_seconds_paid))

# THE INCREMENTAL PERSISTENCE. This is the whole answer to "a worker dies eleven minutes
# into a fifteen-minute pass".
check("the tally is written once per clip AS IT IS CHARGED, not once at the end -- a "
      "worker killed mid-pass must still have recorded what it spent",
      spy.paid == [5, 5, 5, 10, 10], str(spy.paid))
check("and the clip cache is persisted after every clip, so a worker killed mid-pass "
      "leaves behind what it bought instead of the next worker buying it again",
      len(spy.cached) == 5 and len(spy.cached[0]) == 1 and len(spy.cached[-1]) == 5,
      str([len(c) for c in spy.cached]))

# The cache.
again = Spy()
plan2 = make_plan()
old_to_new.build_clip_plan(plan2, TEARDOWN, generate_clip=again.generate,
                          store_clip=again.store, clip_cache=plan.clip_cache,
                          concurrency=1)
check("A REGENERATE WITH THE SAME INPUTS BUYS NOTHING", again.calls == []
      and plan2.clip_seconds_paid == 0)
check("and the film is complete all the same",
      len(plan2.exterior_clips) == 2 and len(plan2.room_clips) == 2)
other = Spy()
plan3 = make_plan(cleared=False)
old_to_new.build_clip_plan(plan3, MAKEOVER, generate_clip=other.generate,
                          store_clip=other.store, clip_cache=plan.clip_cache,
                          concurrency=1)
check("THE TREATMENT IS IN THE CACHE KEY: the gentle makeover does not get served the "
      "tear-down's room clips, which are a completely different film from the same two "
      "pictures", len(other.calls) == 3
      and sorted(c["seconds"] for c in other.calls) == [5, 10, 10],
      str(sorted(c["seconds"] for c in other.calls)))
check("and the makeover buys no exterior clip at all -- its exterior is the rebuilt "
      "still, exactly as the Dramatic's is", plan3.exterior_clips == [])

# Degradations, none of which is a failed video.
plan4 = make_plan(cleared=False)
spy4 = Spy()
old_to_new.build_clip_plan(plan4, TEARDOWN, generate_clip=spy4.generate,
                          store_clip=spy4.store, clip_cache={}, concurrency=1)
check("with no cleared plot there is no tear-down clip -- and no money spent trying, "
      "because a clip with one keyframe is the model inventing the other end",
      plan4.exterior_clips == [] and len(spy4.calls) == 3,
      str(len(spy4.calls)))
check("but the WALK-IN still happens, because it needs only the rebuilt exterior and a "
      "room -- it is the last thing that should be lost",
      plan4.walkin_clip is not None)

refuse = Spy(fail={"comes apart": fal_images.FalRefused("declined")})
plan5 = make_plan()
old_to_new.build_clip_plan(plan5, TEARDOWN, generate_clip=refuse.generate,
                          store_clip=refuse.store, clip_cache={}, concurrency=1)
check("a refused room clip costs that room its animation and nothing else",
      len(plan5.room_clips) == 0 and len(plan5.exterior_clips) == 2)
check("and it is reported to the agent in plain words",
      any("still picture" in n for n in plan5.degradations), str(plan5.degradations))
check("a refusal is not billed", plan5.clip_seconds_paid == 15,
      str(plan5.clip_seconds_paid))

billed_loss = Spy(fail={"comes apart": fal_images.FalTemporary("502 on collection")},
                  billed=10)
plan6 = make_plan()
old_to_new.build_clip_plan(plan6, TEARDOWN, generate_clip=billed_loss.generate,
                          store_clip=billed_loss.store, clip_cache={}, concurrency=1)
check("A CLIP FAL CHARGED FOR AND NEVER HANDED OVER IS COUNTED AS PAID -- the whole "
      "lesson of the second old-to-new audit, in a unit fifteen times more expensive",
      # two exterior clips and the walk-in that arrived (5 + 5 + 5) plus two room clips
      # fal charged ten seconds for and never handed over (10 + 10).
      plan6.clip_seconds_paid == 5 + 5 + 5 + 10 + 10, "%d" % plan6.clip_seconds_paid)
check("and counted as LOST as well, so the caller knows a retry would buy it again",
      plan6.clip_seconds_lost == 20, "%d" % plan6.clip_seconds_lost)

rate = Spy(fail={"comes apart": fal_images.FalRateLimited("429")})
plan7 = make_plan(rooms=4)
old_to_new.build_clip_plan(plan7, TEARDOWN, generate_clip=rate.generate,
                          store_clip=rate.store, clip_cache={}, concurrency=1)
check("a rate limit stops the pass asking for more rather than asking four more times "
      "for the same answer", plan7.clips_rate_limited is True
      and len([c for c in rate.calls if "comes apart" in c["prompt"]]) == 1,
      str(len(rate.calls)))
check("and it is signalled SEPARATELY from the degradations, as a flag the caller acts "
      "on rather than a note the agent reads -- the clip is makeable, there is simply "
      "no room right now, so the right answer is to wait and not to apologise",
      plan7.clips_rate_limited is True
      and not any("limit" in n and "monthly" in n for n in plan7.degradations),
      str(plan7.degradations))

cannot_store = Spy()
cannot_store.store = lambda stage, key, data: (_ for _ in ()).throw(
    RuntimeError("bucket down"))
plan8 = make_plan(rooms=1)
old_to_new.build_clip_plan(plan8, MAKEOVER, generate_clip=cannot_store.generate,
                          store_clip=cannot_store.store, clip_cache={}, concurrency=1)
check("a clip we paid for and cannot store is counted as paid AND as lost, so the "
      "accounting stays honest even though we got nothing",
      plan8.clip_seconds_paid == 15 and plan8.clip_seconds_lost == 15,
      "%d/%d" % (plan8.clip_seconds_paid, plan8.clip_seconds_lost))

# A RENDER THAT IS TAKEN OVER MID-PASS STOPS SPENDING. The job row is fenced against a
# late write, so a superseded worker's film is discarded harmlessly -- but over a quarter
# of an hour that discarded film could have cost another US$3.15.
owned = [True]
spyo = Spy()
plan_o = make_plan(rooms=4)
old_to_new.build_clip_plan(plan_o, TEARDOWN, generate_clip=spyo.generate,
                          store_clip=spyo.store, clip_cache={}, concurrency=1,
                          still_ours=lambda: owned.pop(0) if owned else False)
check("the pass stops buying clips the moment the job belongs to another worker",
      len(spyo.calls) == 1, str(len(spyo.calls)))
check("and what it had already bought is still cached for the new owner",
      len(plan_o.clip_cache) == 1)
check("an ownership check that itself fails lets the render carry on -- wrongly stopping "
      "a render that IS ours costs the agent their video, which is worse than the "
      "spending this guard saves",
      len(Spy().calls) == 0)
spyf = Spy()
old_to_new.build_clip_plan(make_plan(rooms=1), MAKEOVER, generate_clip=spyf.generate,
                          store_clip=spyf.store, clip_cache={}, concurrency=1,
                          still_ours=lambda: (_ for _ in ()).throw(RuntimeError("db")))
check("  ...checked", len(spyf.calls) == 2, str(len(spyf.calls)))

# The budget, and the fact that it trims whole clips.
plan9 = make_plan(rooms=4)
spy9 = Spy()
old_to_new.build_clip_plan(plan9, TEARDOWN, generate_clip=spy9.generate,
                          store_clip=spy9.store, clip_cache={},
                          seconds_used_this_month=CAP - 20, concurrency=1)
check("twenty seconds of allowance buys the two 5s exterior clips and the 5s walk-in, "
      "and no room -- which is the priority order: the exterior chain, then the shot "
      "Jane asked for by name, then the rooms",
      [c["seconds"] for c in spy9.calls] == [5, 5, 5],
      str([c["seconds"] for c in spy9.calls]))
check("no clip is part-bought -- there is no seven seconds of a ten-second generation",
      plan9.clip_seconds_paid == 15, str(plan9.clip_seconds_paid))
# THE EXTERIOR CHAIN IS ONE PURCHASE. The renderer plays both stages or neither, so an
# allowance that covers the tear-down but not the rebuild must buy NEITHER -- otherwise
# US$0.35 is spent on a clip that is then dropped on the floor.
spy9b = Spy()
plan9b = make_plan(rooms=1)
old_to_new.build_clip_plan(plan9b, TEARDOWN, generate_clip=spy9b.generate,
                          store_clip=spy9b.store, clip_cache={},
                          seconds_used_this_month=CAP - 6, concurrency=1)
check("an allowance that covers ONE exterior stage buys neither of them, because the "
      "renderer would drop the pair and the money would be gone",
      not any(c["seconds"] == 5 and "comes down" in c["prompt"]
              for c in spy9b.calls), str([c["prompt"][:30] for c in spy9b.calls]))
check("  ...and it spends what is left on the walk-in instead",
      len(spy9b.calls) == 1 and "up the driveway" in spy9b.calls[0]["prompt"],
      str(len(spy9b.calls)))
check("and the agent is told part of the film is stills",
      any("monthly limit" in n for n in plan9.degradations), str(plan9.degradations))
try:
    old_to_new.build_clip_plan(make_plan(), TEARDOWN,
                              generate_clip=lambda *a, **k: b"never",
                              store_clip=lambda *a, **k: "x", clip_cache={},
                              seconds_used_this_month=CAP, concurrency=1)
    check("an agent with no allowance left generates nothing at all", False)
except old_to_new.VideoBudgetExhausted:
    check("an agent with no allowance left generates nothing at all", True)

# The deadline.
plan10 = make_plan(rooms=4)
spy10 = Spy()
old_to_new.build_clip_plan(plan10, TEARDOWN, generate_clip=spy10.generate,
                          store_clip=spy10.store, clip_cache={},
                          deadline=time.monotonic() - 1, concurrency=1)
check("a pass that is already out of time buys nothing and says so",
      spy10.calls == [] and any("took too long" in n for n in plan10.degradations),
      str(plan10.degradations))

# A model-written description that smuggles in a refused word.
plan11 = make_plan(rooms=1, contents=[("the fire-damaged sofa", "a cream sofa")])
spy11 = Spy()
old_to_new.build_clip_plan(plan11, MAKEOVER, generate_clip=spy11.generate,
                          store_clip=spy11.store, clip_cache={}, concurrency=1)
check("A ROOM DESCRIPTION CARRYING A WORD FAL REFUSES IS CAUGHT BEFORE THE AGENT WAITS, "
      "not after fal is paid to refuse it",
      not any("fire-damaged" in c["prompt"] for c in spy11.calls)
      and any("still picture" in n for n in plan11.degradations),
      str(plan11.degradations))

# Concurrency: two at a time, which is what fal allows a new account.
check("clips are asked for two at a time by default, which is fal's own limit for a new "
      "account -- sequential would push a six-clip film past its budget",
      old_to_new.ELEGANT_CLIP_CONCURRENCY == 2)
threads = []
_tlock = threading.Lock()
class Lanes(Spy):
    def generate(self, prompt, start, end, seconds, deadline=None):
        with _tlock:
            threads.append(threading.current_thread().name)
        time.sleep(0.05)
        return Spy.generate(self, prompt, start, end, seconds, deadline)
lanes = Lanes()
old_to_new.build_clip_plan(make_plan(rooms=4), TEARDOWN,
                          generate_clip=lanes.generate, store_clip=lanes.store,
                          clip_cache={})
check("and they really do run in parallel lanes", len(set(threads)) > 1,
      str(set(threads)))
check("while every counter is still written on ONE thread, so the money accounting is "
      "not concurrent even though the waiting is",
      lanes.calls and len(lanes.calls) == 7, str(len(lanes.calls)))


# ===========================================================================
print("\n[6b] The walk-in lands on the LIVING ROOM, by the Classic's own labels")
# ===========================================================================
# Jane's requirement, and the reason it is the Classic's labels rather than a second idea
# of what a good opening is: "where does a NestList film begin" is now one rule, settled
# once in SPACE_RANKING, and both styles answer it the same way.
check("the Classic's ruling is that the living room opens the film, and it still is",
      vr.SPACE_RANKING[0] == ("living", True))

_saved_survey, _saved_thumbs = vr._survey_photos, vr._describe_thumbnails
vr._describe_thumbnails = lambda urls: [Image.new("RGB", (8, 8))] * len(urls)
try:
    def labels(*spaces):
        vr._survey_photos = lambda photos, guard=None: (
            [(sp, None) for sp in spaces], None)

    rooms = [("https://x/r%d.jpg" % i, None) for i in range(4)]
    labels("entrance", "bedroom", "living", "dining")
    out = vr._living_room_first("https://x/ext.jpg", rooms)
    check("a landing first and a living room third: the LIVING ROOM is moved to the "
          "front, which is where the walk-in comes to rest",
          [u for u, _sp in out] == ["https://x/r2.jpg", "https://x/r0.jpg",
                                    "https://x/r1.jpg", "https://x/r3.jpg"],
          str([u for u, _sp in out]))
    check("and every room carries its label onward, so the plan knows what each room is",
          [sp for _u, sp in out] == ["living", "entrance", "bedroom", "dining"],
          str([sp for _u, sp in out]))
    check("everything after the opening keeps the agent's own order -- beyond the first "
          "shot, the order they see in My Listings is the order they get",
          [u for u, _sp in out][1:] == ["https://x/r0.jpg", "https://x/r1.jpg",
                                        "https://x/r3.jpg"])

    labels("living", "bedroom", "dining", "kitchen")
    out = vr._living_room_first("https://x/ext.jpg", rooms)
    check("a living room already first is left exactly where it is",
          [u for u, _sp in out] == [u for u, _sp in rooms])

    labels("bathroom", "stairs", "dining", "entrance")
    out = vr._living_room_first("https://x/ext.jpg", rooms)
    check("WITH NO LIVING ROOM AT ALL the walk-in lands on the best space that is "
          "ALLOWED to open a film -- a bathroom, a staircase or a landing is never "
          "walked into, which is the Classic's own second column",
          out[0][0] == "https://x/r2.jpg" and out[0][1] == "dining",
          str(out[0]))

    labels("bathroom", "stairs", "carpark", "entrance")
    out = vr._living_room_first("https://x/ext.jpg", rooms)
    check("and when NOTHING may open, the agent's order stands rather than this "
          "inventing an answer",
          [u for u, _sp in out] == [u for u, _sp in rooms])

    labels("living")                       # fewer labels than rooms
    out = vr._living_room_first("https://x/ext.jpg", rooms)
    check("a reply shorter than the photo list cannot shuffle a room onto the wrong "
          "label", len(out) == len(rooms)
          and sorted(u for u, _sp in out) == sorted(u for u, _sp in rooms),
          str(out))
    check("a single room is returned untouched, with no model call paid for at all",
          vr._living_room_first("https://x/ext.jpg", rooms[:1]) == rooms[:1])
finally:
    vr._survey_photos, vr._describe_thumbnails = _saved_survey, _saved_thumbs

# A SURVEY THAT FAILS OUTRIGHT MUST NOT COST THE FEATURE, and the guard is one level up:
# plan_old_to_new wraps the callback, so a missing key, a slow model or an unparseable
# reply all leave the agent's own order in place -- which is what the Dramatic has always
# used.
_pon = ast.get_source_segment(
    io.open(os.path.join(BACKEND, "video_renderer.py"), encoding="utf-8").read(),
    {n.name: n for n in ast.walk(ast.parse(io.open(
        os.path.join(BACKEND, "video_renderer.py"), encoding="utf-8").read()))
     if isinstance(n, ast.FunctionDef)}["plan_old_to_new"])
check("the room ordering is wrapped, so a failed survey leaves the agent's own order",
      "room_urls = list(order_rooms(exterior_url, room_urls)) or room_urls" in _pon
      and "except Exception as e:" in _pon.split("order_rooms(")[1][:400]
      and "using the" in _pon.split("order_rooms(")[1][:600])

# THE ORDERING RUNS ON EVERY RENDER, INCLUDING A FULLY CACHED ONE, and the tempting
# optimisation of skipping it when nothing has to be generated is a MONEY BUG. The room
# order is part of what the walk-in's cache key is built from -- it is keyed on the first
# room's own photograph, because that photograph is the clip's last frame. Skip the
# ordering on a regenerate and the agent's first photo goes back in front, which both
# misses the cached walk-in (US$0.35 to buy again) and lands the shot on a landing
# instead of the living room.
plan_src = ast.get_source_segment(
    io.open(os.path.join(BACKEND, "video_renderer.py"), encoding="utf-8").read(),
    {n.name: n for n in ast.walk(ast.parse(io.open(
        os.path.join(BACKEND, "video_renderer.py"), encoding="utf-8").read()))
     if isinstance(n, ast.FunctionDef)}["plan_elegant"])
check("the room ordering is NOT made conditional on anything being generated, because "
      "the walk-in's cache key is built from the first room's own photograph",
      "order_rooms=_living_room_first," in plan_src
      and "if have_clips < expected_clips else None" not in plan_src)
check("and the WALK-IN is counted among the clips a cache has to hold before the "
      "description pass is skipped -- otherwise a cache with every exterior and every "
      "room would read as complete while a clip still had to be bought",
      "+ (1 if treatment.walkin and room_count else 0))" in plan_src)


# ===========================================================================
print("\n[6c] Small source photos are a warning, never a refusal")
# ===========================================================================
# Jane's own demo photos for Mugliston Road are 640x480 and 800x600, which is about a
# tenth of the pixels this renderer is built for. At US$0.70 a clip an agent should be
# told their film will be soft BEFORE they wonder why.
tiny = {"https://x/small.jpg": Image.new("RGB", (800, 600)),
        "https://x/big.jpg": Image.new("RGB", (3200, 1800))}
_saved_fetch = vr._fetch_image
vr._fetch_image = lambda url, attempts=2, deadline=None: tiny[url]
try:
    small_plan = old_to_new.Plan()
    small_plan.opening_url = "https://x/small.jpg"
    small_plan.rooms = [{"before_url": "https://x/small.jpg", "after_url": None,
                         "space": None}]
    vr._warn_about_small_sources(small_plan)
    check("a film built out of thumbnails says so, in words an agent can act on",
          any("small" in n and "upload larger originals" in n
              for n in small_plan.degradations), str(small_plan.degradations))
    big_plan = old_to_new.Plan()
    big_plan.opening_url = "https://x/big.jpg"
    big_plan.rooms = [{"before_url": "https://x/big.jpg", "after_url": None,
                       "space": None}]
    vr._warn_about_small_sources(big_plan)
    check("and a properly sized photograph says nothing", big_plan.degradations == [])
    check("it is a NOTE AND NEVER A REFUSAL -- an agent who only has small photos should "
          "still get a film", len(small_plan.degradations) == 1)
    vr._fetch_image = lambda url, attempts=2, deadline=None: (
        (_ for _ in ()).throw(RuntimeError("404")))
    quiet = old_to_new.Plan()
    quiet.rooms = [{"before_url": "https://x/gone.jpg", "after_url": None,
                    "space": None}]
    vr._warn_about_small_sources(quiet)
    check("and a photo it cannot read is not accused of being small",
          quiet.degradations == [])
finally:
    vr._fetch_image = _saved_fetch
check("the threshold is the film's own geometry, not a round number -- the output is "
      "1080 wide and composed at 2160 before the zoom",
      vr.ELEGANT_SOFT_SOURCE_LONG_EDGE >= vr.VW)


# ===========================================================================
print("\n[7] The render: the clip shot sits at exactly the stills' scale")
# ===========================================================================
def room(shade, furniture=(), w=1600, h=1067):
    img = Image.new("RGB", (w, h), (shade, shade - 6, shade - 14))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(h * .72), w, h], fill=(120, 104, 86))
    for (x0, y0, x1, y1, fill) in furniture:
        d.rectangle([int(w * x0), int(h * y0), int(w * x1), int(h * y1)], fill=fill)
    return img


OLD_F = [(.08, .52, .40, .72, (62, 38, 30)), (.44, .58, .58, .70, (70, 44, 34))]
NEW_F = [(.08, .54, .40, .70, (226, 220, 210)), (.46, .60, .56, .68, (210, 206, 198))]
BEFORE, AFTER = room(170, OLD_F), room(176, NEW_F)
PHOTOS = {"https://x/anon.jpg": room(150), "https://x/new.jpg": room(152),
          "https://x/cleared.jpg": room(148)}
# EACH ROOM'S "AFTER" IS A DISTINGUISHABLE PICTURE, which is what lets the indexing test
# below tell one room's backdrop from another's. The shades stay within
# vr.DIFF_THRESHOLD of each other so the Dramatic's own mask still reads them as "only
# the furniture changed", which is what the fallback rooms depend on.
for i in range(4):
    PHOTOS["https://x/b%d.jpg" % i] = BEFORE
    PHOTOS["https://x/a%d.jpg" % i] = room(176 + 4 * i, NEW_F)


def capture(plan, **kwargs):
    commands = []
    pairs = {}
    clip_fetch_fails = kwargs.pop("clip_fetch_fails", False)
    def fake_run(cmd, timeout):
        commands.append(list(cmd))
        # WHICH CLIP WAS COMPOSITED OVER WHICH BACKDROP, read off the real command and
        # the real backdrop file, because that pairing is the only place the plan's room
        # numbering and the fetched rooms' numbering meet.
        inputs = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-i"]
        source = [i for i in inputs if "e_src_" in str(i)]
        backdrop = [i for i in inputs if "e_bd_" in str(i)]
        if source and backdrop:
            # BOTH halves: which generated clip went in, and which photograph's backdrop
            # it was laid over. The indexing mistake this catches got the clip wrong, so
            # recording only the backdrop would have missed it -- and did, on the first
            # attempt at this test.
            pairs[os.path.basename(str(source[0]))] = (
                io.open(str(source[0]), "rb").read(),
                io.open(str(backdrop[0]), "rb").read())
        out = cmd[-1]
        if not str(out).startswith("-"):
            with open(out, "wb") as f:
                f.write(b"\0" * 2048)
    saved = (vr._run_ffmpeg, vr._fetch_image, vr._probe_duration,
             vr._has_ffmpeg_filter, vr._render_contact_card, vr._fetch_clip_file)
    vr._run_ffmpeg = fake_run
    vr._fetch_image = lambda url, attempts=2, deadline=None: PHOTOS[url]
    vr._probe_duration = lambda path, fallback: fallback
    vr._has_ffmpeg_filter = lambda name: name == "cas"
    vr._render_contact_card = lambda *a, **k: Image.new("RGB", (vr.VW, vr.VH))
    def fake_clip(url, out_path, deadline=None):
        if clip_fetch_fails:
            raise RuntimeError("404 from the bucket")
        # The url IS the content, so a test can tell which clip was actually used.
        with open(out_path, "wb") as f:
            f.write(str(url).encode("utf-8"))
        return out_path
    vr._fetch_clip_file = fake_clip
    vr._clarity_filter_cache = None
    try:
        result = vr.render_dramatic_video(plan, **kwargs)
    finally:
        (vr._run_ffmpeg, vr._fetch_image, vr._probe_duration, vr._has_ffmpeg_filter,
         vr._render_contact_card, vr._fetch_clip_file) = saved
        vr._clarity_filter_cache = None
    return commands, result, pairs


def backdrop_bytes(img):
    """What _dramatic_clip_shot would write as the backdrop for this photograph."""
    import tempfile as _tf
    path = os.path.join(_tf.mkdtemp(), "bd.bmp")
    vr._dramatic_backdrop(img)[0].save(path, format="BMP")
    return io.open(path, "rb").read()


full = make_plan(rooms=2)
spyr = Spy()
old_to_new.build_clip_plan(full, TEARDOWN, generate_clip=spyr.generate,
                          store_clip=spyr.store, clip_cache={}, concurrency=1)
for entry in full.exterior_clips:
    PHOTOS.setdefault(entry["url"], room(160))
cmds, (data, degs), pairs = capture(full, agent_name="Janel",
                             style=vr.ELEGANT_STYLE_ID)
shot_cmds = [c for c in cmds if "d_clip_" in str(c[-1])]
check("the Elegant film renders one shot per entry in the table",
      len(shot_cmds) == 6, str(len(shot_cmds)))
check("and it produces a video", bool(data))
clip_shots = [c for c in shot_cmds if "e_bd_" in " ".join(str(a) for a in c)]
check("five of them are generated clips composited over a still backdrop -- two "
      "exterior stages, the walk-in, and two rooms",
      len(clip_shots) == 5, str(len(clip_shots)))
graph = clip_shots[0][clip_shots[0].index("-filter_complex") + 1]
check("the clip's frame rate is normalised BEFORE anything else, because every frame "
      "number in the zoom ramp is an output frame index",
      graph.index("fps=%d" % vr.FPS) < graph.index("scale="), graph[:90])
check("THE GEOMETRY IS THE STILLS' GEOMETRY: the clip is scaled into the same "
      "foreground box, at the same offset, that a still of its own last keyframe would "
      "use -- composing at output size is what makes a cut read as a jump",
      True)
backdrop, (fg_w, fg_h), (ox, oy) = vr._dramatic_backdrop(PHOTOS["https://x/cleared.jpg"])
check("  ...checked against _dramatic_backdrop itself, not against a restated number",
      ("scale=%d:%d" % (fg_w, fg_h)) in graph
      and ("overlay=%d:%d" % (ox, oy)) in graph, graph[:200])
check("the zoom happens AFTER the composite, in ffmpeg, exactly as it does for a still",
      graph.index("overlay=") < graph.index("zoompan="))
check("the disclosure is drawn on the clip shot",
      "drawtext" in graph and "box=1" in graph)
check("no audio is taken from the generated clip -- the film's own music is added later "
      "and a second audio stream would fight it",
      "-an" in clip_shots[0])
check("the shot is bounded to the table's length, so a model that does not honour its "
      "own duration cannot desynchronise the film from the music",
      "-frames:v" in clip_shots[0]
      and clip_shots[0][clip_shots[0].index("-frames:v") + 1]
      == str(int(round(5 * vr.FPS))))
check("and a clip that comes back short holds its last frame rather than going black",
      "eof_action=pass" in graph)
check("the Elegant gets a longer local render budget than the Dramatic, because its "
      "shots are twice as long and every frame carries an overlay",
      vr.ELEGANT_RENDER_BUDGET_SECONDS > vr.RENDER_BUDGET_SECONDS)
check("the clip downloads share ONE budget -- they run inside the per-process render "
      "slot, so six clips at a minute each would block every Classic on this worker for "
      "six minutes",
      vr.ELEGANT_CLIP_FETCH_BUDGET_SECONDS <= 180
      and vr.ELEGANT_CLIP_FETCH_BUDGET_SECONDS >= 2 * vr.CLIP_FETCH_SECONDS)
_t = os.path.join("/tmp", "elegant_clip_deadline_probe.mp4")
try:
    vr._fetch_clip_file("https://x/whatever.mp4", _t, time.monotonic() - 1)
    check("a clip fetch with the shared budget already spent does not even open a "
          "connection", False)
except TimeoutError:
    check("a clip fetch with the shared budget already spent does not even open a "
          "connection", True)

# A clip file that cannot be downloaded out of our own bucket.
cmds2, (data2, degs2), _p2 = capture(full, agent_name="Janel",
                                style=vr.ELEGANT_STYLE_ID, clip_fetch_fails=True)
check("if NONE of the clips can be downloaded the film still ships, as the Dramatic "
      "would have made it", bool(data2))
check("and the agent is told", any("another way" in d for d in degs2), str(degs2))
shot_cmds2 = [c for c in cmds2 if "d_clip_" in str(c[-1])]
check("the exterior falls all the way back to the rebuilt still -- a rebuild rising out "
      "of a cleared plot the film never cleared would be nonsense",
      len(shot_cmds2) == 4, str(len(shot_cmds2)))

# THE INDEX BUG, WHICH WAS REAL AND SILENT. A room whose BEFORE photo cannot be read is
# dropped while the film is being assembled, so position N of the fetched list is not
# generally room N of the plan -- and the shot table counts in fetched positions while
# the clip plan is keyed by the plan's own. Before this was fixed, one unreadable photo
# handed room two's clip to room one, which would have put the wrong room's
# transformation on screen with no error anywhere.
gappy = make_plan(rooms=3)
gappy.rooms[0]["before_url"] = "https://x/missing.jpg"      # unreadable
spyg = Spy()
old_to_new.build_clip_plan(gappy, MAKEOVER, generate_clip=spyg.generate,
                          store_clip=spyg.store, clip_cache={}, concurrency=1)
for entry in gappy.room_clips.values():
    PHOTOS.setdefault(entry["url"], room(160))
want = {}
for plan_index, entry in gappy.room_clips.items():
    want[gappy.rooms[plan_index]["after_url"]] = entry["url"]
cmdsg, (datag, degsg), pairsg = capture(gappy, agent_name="J", style=vr.ELEGANT_STYLE_ID)
check("a room whose photo cannot be read is dropped and the film still ships",
      bool(datag) and any("could not be read" in d for d in degsg), str(degsg))
room_pairs = {name: data for name, data in pairsg.items() if "room_" in name}
# The two rooms that survived are the plan's rooms 1 and 2, in that order. So the shot
# written as room_0 must use the plan's room 1 CLIP over the plan's room 1 BACKDROP, and
# room_1 must use room 2's of each.
expected = [(gappy.room_clips[i]["url"].encode("utf-8"),
             backdrop_bytes(PHOTOS[gappy.rooms[i]["after_url"]])) for i in (1, 2)]
check("EACH SURVIVING ROOM USES ITS OWN CLIP OVER ITS OWN BACKDROP even with a gap in "
      "the plan -- the indexing mistake this guards against silently animated a room "
      "that had been dropped and laid it on another room's walls",
      len(room_pairs) == 2
      and room_pairs.get("e_src_room_0.mp4") == expected[0]
      and room_pairs.get("e_src_room_1.mp4") == expected[1],
      str(sorted(room_pairs)))
check("  ...and the two really are different clips over different pictures, so that "
      "check can fail", expected[0] != expected[1]
      and expected[0][0] != expected[1][0] and expected[0][1] != expected[1][1])

# The Dramatic must be untouched by the new argument.
dram = make_plan(rooms=2, cleared=False)
cmds3, (data3, degs3), _p3 = capture(dram, agent_name="Janel")
check("a Dramatic render with the new style argument defaulted makes exactly the film "
      "it made before: opening, exterior, two rooms",
      len([c for c in cmds3 if "d_clip_" in str(c[-1])]) == 4 and bool(data3))
check("and no clip was downloaded for it", not any(
    "e_bd_" in " ".join(str(a) for a in c) for c in cmds3))


# ===========================================================================
print("\n[8] main.py: unreachable until Jane switches it on")
# ===========================================================================
SRC = io.open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
TREE = ast.parse(SRC)
NODES = {n.name: n for n in ast.walk(TREE)
         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def load(names, extra=None):
    g = {"os": os, "logger": logging.getLogger("t"), "old_to_new": old_to_new,
         "fal_video": fal_video, "fal_images": fal_images,
         "ELEGANT_VIDEO_ENABLED_ENV": "ELEGANT_VIDEO_ENABLED"}
    g.update(extra or {})
    lines = SRC.splitlines()
    for name in names:
        node = NODES[name]
        exec(compile("\n".join(lines[node.lineno - 1:node.end_lineno]), name, "exec"), g)
    return g


conditions = {"switch": True, "key": True, "images": True, "seconds": True}
g = load(["_elegant_switch_on", "_elegant_available"], {
    "fal_video": types.SimpleNamespace(is_enabled=lambda: conditions["key"]),
    "_old_to_new_ceiling_enforceable": lambda: conditions["images"],
    "_elegant_seconds_ceiling_enforceable": lambda: conditions["seconds"],
})
os.environ["ELEGANT_VIDEO_ENABLED"] = "1"
check("with the switch on, a key, and both ceilings countable, the Elegant is available",
      g["_elegant_available"]() is True)
for off, label in (("switch", "the switch is off"), ("key", "there is no FAL_KEY"),
                   ("images", "the picture allowance cannot be counted"),
                   ("seconds", "the video allowance cannot be counted")):
    if off == "switch":
        os.environ.pop("ELEGANT_VIDEO_ENABLED", None)
    else:
        conditions[off] = False
    check("and it is NOT available when %s" % label,
          g["_elegant_available"]() is False)
    if off == "switch":
        os.environ["ELEGANT_VIDEO_ENABLED"] = "1"
    else:
        conditions[off] = True
for value, want in (("1", True), ("true", True), ("yes", True), ("on", True),
                    ("0", False), ("", False), ("maybe", False)):
    os.environ["ELEGANT_VIDEO_ENABLED"] = value
    check("the switch reads %r as %s" % (value, want),
          g["_elegant_switch_on"] is not None
          and g["_elegant_switch_on"]() is want)
os.environ.pop("ELEGANT_VIDEO_ENABLED", None)
check("THE DEFAULT IS OFF: an environment that says nothing leaves the feature off, so "
      "a deploy cannot expose a US$3.85-a-press feature by accident",
      g["_elegant_switch_on"]() is False and g["_elegant_available"]() is False)

check("A KEY IS NOT CONSENT -- the Dramatic's gate is 'is there a FAL_KEY', and the key "
      "is already set in production, so the Elegant needs a switch of its own or "
      "deploying this code would have switched it on for everybody",
      "ELEGANT_VIDEO_ENABLED" in SRC
      and "_elegant_switch_on()" in ast.get_source_segment(
          SRC, NODES["_elegant_available"]))

# The source-level guarantees.
core = ast.get_source_segment(SRC, NODES["_render_elegant_core"])
check("the render core refuses to spend when the feature is not available, as a "
      "backstop for a job queued before it was switched off",
      "if not _elegant_available():" in core
      and "PermanentJobError" in core.split("if not _elegant_available():")[1][:400])
check("the clip cache is persisted per clip, through the hook, rather than once at the "
      "end of a fifteen-minute pass",
      "on_clips_cached=_on_clips_cached" in core and "on_clip_paid=_on_clip_paid" in core)
check("the seconds tally is written per clip for the same reason",
      "_add_elegant_seconds(agent[\"id\"], seconds)" in core)
check("the still cache is stored BEFORE the render, so a failed render and a retry do "
      "not pay for the pictures again",
      core.index("old_to_new.write_cache(plan.cache)")
      < core.index("render_dramatic_video"))
check("A DEFERRAL ONLY HAPPENS WHEN WAITING IS FREE: a pass that has already been "
      "charged for video it did not get renders what it can instead, because deferring "
      "would buy that video again",
      "not plan.clip_seconds_lost" in core and "not plan.images_lost" in core)
check("the treatment is remembered on the listing, because the clip cache is keyed on "
      "it and a silent change of treatment would buy a whole second film",
      "old_to_new_treatment" in core)

run = ast.get_source_segment(SRC, NODES["_elegant_job_run"])
check("the job runner re-reads the agent rather than carrying it in the queue row, "
      "which holds Facebook and Instagram tokens",
      "agents\").select" in run and "payload" in run)
check("its patience with a rate limit comes from the job type rather than a hardcoded "
      "600 seconds, which is shorter than one of its own renders",
      "rate_limit_patience_seconds" in run)
check("a used-up video allowance is a permanent failure with Jane's own message, not a "
      "retry ladder", "VideoBudgetExhausted" in run and "PermanentJobError" in run)
check("and a busy render slot is a DEFERRAL, which is free because every clip is "
      "already cached by then", "RenderSlotBusy" in run and "DeferJob" in run)

endpoint = ast.get_source_segment(SRC, NODES["generate_video"])
check("the endpoint refuses an Elegant request when it is not switched on, so the agent "
      "is told now rather than after waiting in a queue",
      "JOB_TYPE_ELEGANT and not _elegant_available()" in endpoint)
check("THERE IS NO IN-PROCESS ELEGANT PATH: a quarter-hour US$3.85 render with no "
      "durability, no heartbeat and no ownership fencing would lose an agent's paid film "
      "to every deploy",
      "if job_type == video_jobs.JOB_TYPE_ELEGANT:" in endpoint
      and "503" in endpoint)
check("the treatment travels in the job payload only for an Elegant render, so a "
      "Classic or Dramatic job row is byte-for-byte what it was",
      '**({"treatment": chosen_treatment} if chosen_treatment else {})' in endpoint)
check("the response reports the treatment the render IN FLIGHT is using, not the one "
      "this press asked for -- the same honesty rule the template and the music already "
      "follow, and for the same reason: quietly delivering the wrong film is worse than "
      "being told to wait",
      endpoint.count('"video_treatment": ') == 2
      and endpoint.count('"requested_video_treatment": chosen_treatment') == 2)
check("and the treatment is only resolved for an Elegant request, so a Classic "
      "response carries None there and nothing old can be confused by it",
      "if job_type == video_jobs.JOB_TYPE_ELEGANT:\n        chosen_treatment" in endpoint)
check("an unknown treatment name falls back to the headline rather than erroring, the "
      "same way an unknown stored template id does",
      old_to_new.resolve_treatment("something-else").id
      == old_to_new.DEFAULT_ELEGANT_TREATMENT
      and old_to_new.resolve_treatment(None).id == old_to_new.TREATMENT_TEARDOWN)

startup = ast.get_source_segment(SRC, NODES["startup"]) if "startup" in NODES else SRC
check("the Elegant's renderer is registered ONLY when the switch is on -- video_jobs "
      "never claims a type with no renderer, so a row inserted by hand on a switched-off "
      "deploy sits there instead of spending US$3.85",
      "if _elegant_switch_on():" in SRC
      and SRC.index("if _elegant_switch_on():")
      < SRC.index("video_jobs.register_renderer(video_jobs.JOB_TYPE_ELEGANT"))


# ===========================================================================
print("\n[9] The queue: a quarter-hour job that is mostly asleep")
# ===========================================================================
import video_jobs as vj                                                  # noqa: E402

E, C, D = vj.JOB_TYPE_ELEGANT, vj.JOB_TYPE_CLASSIC, vj.JOB_TYPE_DRAMATIC
ORDER = [C, D, E]

check("an idle worker may take any of the three",
      vj.claimable_types(ORDER, {}) == [C, D, E])
check("A WORKER ALREADY RENDERING A CLASSIC MAY STILL START AN ELEGANT -- the Elegant's "
      "quarter hour is spent asleep waiting on fal, and refusing it to protect a cpu "
      "nobody is using is what the old single ceiling did",
      vj.claimable_types(ORDER, {C: 1}) == [E], str(vj.claimable_types(ORDER, {C: 1})))
check("but it may NOT also start a Dramatic, which really is ffmpeg from end to end",
      D not in vj.claimable_types(ORDER, {C: 1}))
check("and a worker WAITING on an Elegant may still render a Classic or a Dramatic, "
      "which is the half of the split that keeps the other two from being taxed",
      vj.claimable_types(ORDER, {E: 1}) == [C, D],
      str(vj.claimable_types(ORDER, {E: 1})))
check("one of each is the most a worker holds: nothing more can be claimed",
      vj.claimable_types(ORDER, {C: 1, E: 1}) == [])
check("two Elegants in one process is refused as well -- 'it is only waiting' is exactly "
      "the reasoning that would let a worker accumulate twenty of them",
      vj.claimable_types(ORDER, {E: 1}) == [C, D]
      and vj.PER_PROCESS_WAITING_LIMIT == 1)
check("a leftover row of a withdrawn type cannot crash the decision",
      vj.claimable_types(ORDER, {"signature": 1}) == [C, D, E])
check("and the memory guarantee the single ceiling used to provide is still there, "
      "enforced where the memory is spent: ONE ffmpeg render per process, held by both "
      "renderers around their encode",
      vr.MAX_CONCURRENT_RENDERS == 1
      and "_RENDER_SLOTS.acquire" in io.open(
          os.path.join(BACKEND, "video_renderer.py"), encoding="utf-8").read())

# The escape window, which a healthy queue of long jobs would otherwise trip.
cfg = vj.JOB_TYPES[E]
check("three agents queued behind one Elegant is 45 minutes of entirely honest waiting, "
      "which is past the global 30-minute escape hatch",
      3 * cfg.default_seconds > vj.QUEUED_ESCAPE_SECONDS)
check("so the Elegant's own window is longer than that -- otherwise the third agent is "
      "failed and told the rendering service is not picking up work, which would be a "
      "lie told to somebody who was simply third in line",
      vj._escape_seconds(E) >= 3 * cfg.default_seconds,
      "%s vs %s" % (vj._escape_seconds(E), 3 * cfg.default_seconds))
check("and the other two types keep the window they had",
      vj._escape_seconds(C) == vj.QUEUED_ESCAPE_SECONDS
      and vj._escape_seconds(D) == vj.QUEUED_ESCAPE_SECONDS)
fresh = {"job_type": E, "updated_at": vj._iso(vj._now())}
check("a row is measured against its OWN type's window, not the shortest one",
      vj._queued_for_seconds(fresh) < vj._escape_seconds(E))
check("an unreadable timestamp falls through to the old behaviour rather than inventing "
      "a new one", vj._queued_for_seconds({"updated_at": "not a date"}) is None)

# The ETA floor.
check("the Elegant is never quoted at less than seven minutes, however the rolling mean "
      "lands -- a cached regenerate takes a minute and a cold render fifteen, so a mean "
      "over a mix of the two describes neither, and under-quoting a long wait is what "
      "makes an agent press Generate again",
      cfg.min_quoted_seconds >= 420)
_saved_avg = vj._average_duration
vj._average_duration = lambda job_type: 60.0
try:
    check("  ...checked: a 60-second mean is quoted as the floor for an Elegant",
          vj._quoted_duration(E) == float(cfg.min_quoted_seconds))
    check("  ...and as 60 seconds for a Classic, which has no floor and is unchanged",
          vj._quoted_duration(C) == 60.0)
    vj._average_duration = lambda job_type: 1500.0
    check("  ...while a mean ABOVE the floor is quoted as it is -- the floor is a floor, "
          "not a cap", vj._quoted_duration(E) == 1500.0)
finally:
    vj._average_duration = _saved_avg

check("its cold-start quote is minutes rather than the Dramatic's five, because a cold "
      "render buys six clips two at a time before ffmpeg even starts",
      cfg.default_seconds >= 900)
check("its patience with a rate limit outlasts one of its own renders, so the first 429 "
      "does not immediately hand the agent the plainer film",
      cfg.rate_limit_patience_seconds > cfg.default_seconds / 2)
check("it is capped at ONE render across the whole fleet: one render already asks fal "
      "for two clips at once, which is all a new account allows",
      cfg.account_limit == 1
      and old_to_new.ELEGANT_CLIP_CONCURRENCY == 2)
check("a worker shutting down hands its Elegant straight back rather than letting the "
      "stale sweep find it two minutes later -- and because every clip is already "
      "cached, the next worker picks up where it left off for nothing",
      "defer(job, \"worker shutting down\"" in io.open(
          os.path.join(BACKEND, "video_jobs.py"), encoding="utf-8").read())
check("the heartbeat is unchanged, and it can be: the beat comes from its own thread, "
      "not from the render, so fifteen minutes of waiting on fal keeps it ticking",
      vj.HEARTBEAT_SECONDS == 20 and vj.STALE_AFTER_SECONDS == 120)
check("the whole clip pass is bounded, so one stuck clip cannot eat the agent's "
      "afternoon", old_to_new.TOTAL_CLIP_BUDGET_SECONDS <= 900
      and fal_video.CLIP_DEADLINE_SECONDS <= old_to_new.TOTAL_CLIP_BUDGET_SECONDS)


print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
