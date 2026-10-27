# "From Old to New" video — the two variations

Two ways to show a property's potential, both agent-selectable. Decided with Jane on
2026-10-22 after building and rejecting several alternatives. This file is the recipe:
the prompts are the valuable part and they are reproduced verbatim.

---

## Names, and what is built (2026-10-22)

Agents choose between **three** videos. Jane named them, and the ids below are a
contract with the frontend — `classic` is written onto listing rows going back months,
and the frontend keys its option list on all three, so none of them may be renamed.

| Agent sees | id | What it is | State |
|---|---|---|---|
| Classic | `classic` | today's slideshow, for a property that is already modern | shipped, **unchanged** |
| Dramatic | `dramatic` | Variation A below — the old furniture flying out | **phase one, built** |
| Elegant | `elegant` | real generated motion, via Kling. **Two treatments** — see below | **phase two, built 2026-10-27, switched OFF** |

---

## The Elegant is a TABLE of treatments, not one effect (2026-10-27)

This section supersedes the original reading of Variation B as "the Elegant video".

Jane watched a competitor (Andrew Goh, `drew11luxhome`) and said *"these are the kinds of
bold videos i am talking about"*. His film is continuous AI video — no scene cuts in 56
seconds: the old house buckles, folds and throws dust, the modern build rises out of it
with no cut, and then room by room, camera locked off, the old interior comes apart and
the new one assembles in the same frame. **That is the headline effect, and it is not
Variation B.**

So the Elegant takes a **treatment** — one row of `old_to_new.ELEGANT_TREATMENTS` — and
nothing downstream cares which. Two rows to start:

| Treatment id | What it is | Clips | Cost |
|---|---|---|---|
| `tear-down-and-rebuild` | **the headline.** Exterior comes down and the new house rises, with no cut; then each interior comes apart and reassembles | 2 × 5s exterior + **5s walk-in** + 1 × 10s per room | 55s ≈ **US$3.85** + 7 stills ≈ US$0.28 |
| `gentle-makeover` | Variation B below, verbatim — furniture carried out piece by piece, camera locked off | **5s walk-in** + 1 × 10s per room | 45s ≈ **US$3.15** + 6 stills ≈ US$0.24 |

Jane picks per listing; the id is remembered on the listing because the clip cache is
keyed on it.

### The walk-in is a hard requirement of both (2026-10-27)

*"i want the zoom walk in from driveway living room."* — the single effect Jane has
praised most across the whole project, raised again unprompted weeks after she first saw
it. It is the shot "Shared: the exterior and the tour" below calls the thing that turns a
set of room clips into a house tour, built in as **standard, not a flag**, with two
changes to the spec's own version:

1. **It starts at the DRIVEWAY**, not at the facade. The camera begins outside at the
   gate, low over the drive, goes up the drive, through the front entrance, and comes to
   rest looking across the living room. One continuous move, no cut.
2. **It lands on the LIVING ROOM specifically**, chosen by the Classic's own space labels
   (`SPACE_RANKING` — the living room opens the film) rather than by photo order. The two
   styles now answer "where does a film begin" the same way, from the same labels. With no
   living room, it falls back to the best-ranked space that is *allowed* to open a film;
   a landing, a bathroom or a staircase is never walked into.

Its **end frame is the living room as it is TODAY**, not redesigned — that is the first
frame of the shot after it, so the two meet exactly and the join is invisible. Landing on
the redesigned room would cut straight from the new living room to the old one coming
apart, which reads as a mistake. The one oddity left is a tear-down film walking from a
conceptual new facade into today's interior: that is the honest version of the arc, and it
is the creative call in this feature most worth a human's eyes on a real film.

**Five seconds, not ten**, which is a money call on the most-praised shot: ten would give
the travel more room and take a tear-down listing to US$4.48, the very top of the band
this feature was costed against. `WALKIN_CLIP_SECONDS` is one constant to change.

With no rebuilt exterior there is **no walk-in** and the film falls back to the Dramatic's
own hard cut through the door. That is deliberate: starting on the real photograph would
make the clip's first frame a real picture with altered neighbours, needing a fourth
disclosure string, for a case that only arises when the exterior has already failed.

**The per-agent ceiling is 165 seconds a calendar month** — US$11.55, exactly three
tear-down films. It was 150 (three films at 50s) before the walk-in made a tear-down 55
seconds.

**The tear-down needs one extra still: the CLEARED PLOT.** That is not a nicety, it is
"The end frame decides what happens" at the bottom of this file applied to the hardest
shot in the product. Anchored on the finished house the model washes one picture into the
other — the dissolve Jane's verdict on was *"it is a joke"*. Anchored on a cleared plot it
has no way there except by bringing the house down. So the tear-down is **two clips that
meet on that one frame**, which is also what makes them cut together with no visible join.

**The two things that make ours better than his, both non-negotiable:**

1. **The neighbours are anonymised first.** His film leaves the adjacent houses fully
   recognisable. Every image and every clip of ours derives from the anonymised frame —
   the first frame of the first clip IS that photograph — so the ordering at the top of
   `old_to_new.py` is load-bearing and tested, not remembered.
2. **Nothing is fabricated.** He added a red supercar and a yellow one to the driveway. A
   generated car is an invented fact about a driveway exactly as an invented bedroom would
   be. Every prompt pins what stays (*"the driveway and the street stay empty the whole
   time"*, *"nothing appears that is not named here"*) and
   `fal_video.NO_FABRICATION_NEGATIVE` excludes what a video model likes to add. Both:
   the positive pin is what the model follows, the negative is the backstop.

**Written without a single word fal refuses.** A house coming down, described in words
that get generated rather than refused — see the prompt-safety note below, and
`teardown_exterior_prompt()` for how. The negative-prompt field is where exclusions live,
because that is the field for them; the positive prompt carries none, in any polarity.

**The model moved.** The spec names `fal-ai/kling-video/v1.6/pro/image-to-video`, which
fal has since marked deprecated, and the replacement renamed the keyframe fields
(`image_url`/`tail_image_url` → `start_image_url`/`end_image_url`). What ships is
`v2.6/pro`, whose published price with audio **off** is US$0.07 a second — exactly the
figure measured off Jane's own bill. `generate_audio` defaults to TRUE on that endpoint
and is pinned off: on, it doubles the bill and adds generated speech.
`fal_video.MODELS` is one table of endpoint + field names + price, so the next rename is
an edit rather than a bug hunt.

**Disclaimers for the Elegant, Jane's exact wording:** tear-down and rebuild shots carry
*"An AI concept of what could be built here. Rebuild subject to approval."*; redesigned
interiors carry *"AI-generated visualisation, not an actual photo of the property"*. The
Dramatic's own shorter *"AI visualisation"* is deliberately left as it ships.

---

"Variation B — Makeover (gentle)" below is the **`gentle-makeover` treatment**. The word
*gentle* survives in this file because it describes the effect.

**Phase one built** (`fal_images.py`, `old_to_new.py`, the Dramatic half of
`video_renderer.py`, the wiring in `main.py` and `video_jobs.py`):

- We **generate the "after" photographs ourselves** from the agent's own listing photos,
  with `fal-ai/nano-banana/edit`, about 4 US cents each — the exterior pair and one
  redesigned image per room, about 24 cents a listing, cached on the listing so a
  regenerate pays nothing. There is deliberately **no upload path for after photos**:
  generating the after FROM the before is what makes the two share a camera position by
  construction, which is the requirement the whole effect rests on.
- The Dramatic renderer, all four room treatments, and the walk-through (a hard push
  into the front door and a matched pull-out inside, then a forward push room to room).
- The disclaimers, enforced per shot rather than per film.

**Phase two, for Elegant, BUILT 2026-10-27** and switched off until Jane switches it on
(`ELEGANT_VIDEO_ENABLED=1` in Railway, plus `docs/old-to-new-elegant-migration.sql`).
What it turned out to be:

- `fal_video.py`, the Kling client, a **sibling** of `fal_images` rather than a fork of
  it — the whole HTTP discipline (one absolute deadline that frees the socket and the
  thread, a body we never quote, the permanent/retryable/rate-limited split) is imported,
  and only what genuinely differs is new: the unit of money is a SECOND, the time scale is
  minutes, and the model's field names are data.
- `old_to_new.ELEGANT_TREATMENTS`, the treatment table, plus the clip plan, the clip cache
  and a second ceiling in seconds (`MAX_VIDEO_SECONDS_PER_AGENT_PER_MONTH = 150`, about
  US$10.50 an agent a month, three Elegant films).
- a `JOB_TYPES` entry and one `register_renderer` call in `video_jobs`, as predicted —
  but the queue needed more than that, because a quarter-hour job that is mostly asleep
  broke three of its assumptions. See `JobTypeConfig`'s last four fields.
- `kind: "clip"` in `video_renderer.dramatic_shots`, as predicted, and
  `_dramatic_clip_shot` to composite a generated clip at exactly the stills' scale.
- the template appended to the picker **only when switched on** — it is not in
  `VIDEO_TEMPLATES`, so the failure mode of forgetting is "not shown" rather than "shown".

The prompts are below, verbatim, and the tear-down treatment's are in `old_to_new.py`.

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
