# "From Old to New" video — the two variations

Two ways to show a property's potential, both agent-selectable. Decided with Jane on
2026-10-22 after building and rejecting several alternatives. This file is the recipe:
the prompts are the valuable part and they are reproduced verbatim.

Both variations need the **same two photographs of each room**: the room as it is, and
the room redesigned. **The two shots must share a camera position.** That single
requirement is what makes either effect work — see "Why the angle match matters".

---

## Variation A — "Transformation" (dramatic)

The old furniture flies out of the room towards the camera while the new room stands
behind it. Built entirely in `video_renderer`; **no AI generation, no per-listing cost.**

**How it works.** The two shots are the same room from the same spot, so the pixels that
differ between them are almost exactly the furniture. That difference is a mask. The old
frame is lifted through it, split into pieces, and each piece is rushed at the camera on
its own path with its own spin and motion blur, while the new room sits underneath the
whole time. Nothing is generated, erased or inpainted.

**Room treatments, rotated so the film has rhythm:**

| Room | Treatment |
|---|---|
| 1 | old furniture **scatters out**, each group on its own path |
| 2 | new furniture **arrives**, flying in from off-frame and settling (the same flight run backwards) |
| 3 | the new room **builds up** from the floor |
| 4 | a **quick zoom** punches into the old room and pulls back out of the new, through a white flash |

**Two things that are load-bearing and were each got wrong once:**

- Every frame is composed in the ORIGINAL photograph's space, then put through the same
  contain-over-blur as the rest of the film, then zoomed. Composing at output size — or
  cropping the upscaled composite without scaling it down first — makes the flight sit at
  a different scale from the shots either side, and the cut reads as a jump.
- On the "arrives" treatment the room underneath must cross-dissolve from old to new and
  finish slightly early, or the new furniture lands on the old room's walls and the next
  shot cuts to the new walls.

---

## Variation B — "Makeover" (gentle)

The old furniture is carried out piece by piece and the new design moves in, camera
locked off. Generated with **Kling** (via fal, `fal-ai/kling-video/v1.6/pro/image-to-video`).
**About US$1 per room**, so roughly US$4 for a four-room listing.

**Settings.** Keyframes mode — BEFORE as the first frame, AFTER as the last frame, middle
empty. 10 seconds (5 is too short for everything to move out and back in). 1080p for the
version that gets posted.

**The prompt shape. Naming each real object with a physical verb is the whole trick** —
it is what makes the furniture move instead of melting from one room into the other.

1. Open with: *"Locked-off static camera, no camera movement, fast-paced home makeover
   timelapse."* — this stops the camera drifting.
2. Phase one, out: *"First, the old furniture is pulled out of the room one piece at a
   time:"* then name each old item with a specific exit — slides out of frame to the
   left, rolls up and is dragged away, lifts off and flies out, rises into the ceiling,
   drops down and vanishes. End with *"leaving an empty [floor type] room."*
3. Phase two, in: *"Then the new design shifts in piece by piece:"* then each new item in
   order — lighting and curtains first, then large furniture, then small decor. Give each
   an entry: glides in from the edges and settles, slides into place, lowers down,
   unrolls from the top, pops onto the table.
4. Close with: *"Smooth, satisfying, realistic motion."*

**Worked example** (the living room of the Tembeling demo listing):

> Locked-off static camera, no camera movement, fast-paced home makeover timelapse. First,
> the old furniture is pulled out of the room one piece at a time: the dark carved rosewood
> sofas with red cushions slide out of frame to the left and right, the carved wooden coffee
> table and its lace cloth lift off and fly out, the red and gold wall scrolls and the framed
> floral painting peel off the walls and fly away, the dark display cabinets slide out, and
> the ceiling pendant lamps rise up into the ceiling and disappear, leaving an empty white
> marble room. Then the new design shifts in piece by piece: warm cove lighting switches on
> in the ceiling, slim drum pendants lower down, the cream sofas glide in from the edges and
> settle, the marble coffee table and round ottoman slide into place, a soft rug unrolls
> beneath them, potted olive trees walk into the corners, and artwork pops onto the wall.
> Smooth, satisfying, realistic motion.

---

## Shared: the exterior and the tour

**The opening.** The agent's real photograph with **only the neighbouring buildings
replaced**, then the rebuilt house. Anonymising the neighbours is Jane's requirement and
it must be the first frame on screen, not something applied later.

Generate the anonymised frame FIRST and then generate everything else FROM it, so the
street, sky and neighbours stay identical across every beat. Name each neighbouring
building individually — a general instruction to vary them leaves the recognisable one
untouched.

**In through the front door.** A generated clip from the finished exterior to the first
interior — about US$0.50, and it is what turns a set of room clips into a house tour:

> The camera glides smoothly forward, approaches the front entrance of the modern house,
> passes through the doorway and continues into the bright open living room inside, coming
> to rest looking across the room. One continuous flowing forward move from outside to
> inside, like a drone easing through the door. Smooth, steady, cinematic.

**Walking between rooms.** A forward push out of the finished room into the next, done
locally for nothing. A hard cut between two rooms breaks the feeling of one house.

**Night closes the film.** Blue hour, warm light through the glazing.

---

## Disclaimers

- The AI notice goes on **every generated frame** and never on a real photograph.
- The opening carries its own, separate line — *"Neighbouring homes altered for privacy"* —
  because the house in that shot is a real photograph and calling it a visualisation would
  be wrong, but the surroundings have been changed and that should be said.

---

## Why the angle match matters

If the before and after are shot from different positions, Variation A's mask becomes
nonsense and Variation B morphs the room instead of moving the furniture. **Check the
alignment before an agent waits several minutes for a render**, and fall back to a plain
dissolve when it fails.

---

## Things that did not work — do not re-try these

- **Cross-dissolving stills to fake a demolition.** The smeary dissolution in the
  reference videos is a video model interpolating between two states; stacking more
  stills never gets there. Jane's verdict on the attempt: *"it is a joke."*
- **A flat edge-detect "blueprint".** Deriving a wireframe from the finished image gives
  a pencil sketch, not a structure that assembles.
- **Generating an "empty room" in-between frame.** Image editors redraw the room from a
  new angle and lose the curtains and lighting. The two-phase keyframe prompt does the
  job on its own.
- **Asking one clip to carry a whole arc.** More detail in one phase steals time from the
  others — a detailed demolition left the wireframe with a second and a half.

## Prompt-safety note

The generator refuses prompts containing words like *explosion*, *blast*, *fire* or
*debris* **even when they appear as exclusions** ("no explosion"). Describe what should
happen, never what should not. Two generations were refused before this was spotted.

## The end frame decides what happens

Anchoring a transformation on the finished house let the model wash one picture into the
other. Anchoring the same prompt on a rubble image forced a real collapse — there is no
way from a standing house to a pile of rubble except by bringing the house down. **Pick
the end frame to be the state you want reached**, and split long arcs into stages that
each have their own end frame.
