"""The Dramatic template: the two geometry mistakes, the disclaimers, the walk-through.

THE FIRST THING THIS FILE DOES is prove the Classic has not moved -- not by re-running
its golden (test_classic_render.py owns that), but by proving the piece of the Classic
the Dramatic had to COPY still agrees with the original. _dramatic_backdrop is a
deliberate second copy of _contain_over_blur's composition, because the Classic's
composite is pinned command-for-command and must not be refactored underneath it. Two
copies that nobody compares are two copies that drift.

Everything else here is about the things the spec says were got wrong once each, and
about the disclosure rules, which are not negotiable.

No ffmpeg, no network, no model, no key: the ffmpeg calls are captured and inspected.
(A real end-to-end encode of every treatment was run separately; see the report.)

Run: python3 tests/test_dramatic_render.py
"""
import ast, io, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logging
logging.disable(logging.CRITICAL)
from PIL import Image, ImageDraw
import video_renderer as vr
import old_to_new

P = F = 0
def check(label, cond, extra=""):
    global P, F
    if cond: P += 1; print("  ok   %s" % label)
    else:    F += 1; print("  FAIL %s %s" % (label, extra))


BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def room(shade, furniture, w=1600, h=1067):
    """A synthetic room whose STRUCTURE is fixed and whose furniture is not -- which is
    the property the real pipeline gets by generating the after FROM the before."""
    img = Image.new("RGB", (w, h), (shade, shade - 6, shade - 14))
    d = ImageDraw.Draw(img)
    d.rectangle([0, int(h * .72), w, h], fill=(120, 104, 86))
    d.rectangle([int(w * .62), int(h * .14), int(w * .92), int(h * .60)],
                fill=(214, 226, 236), outline=(70, 70, 70), width=6)
    for (x0, y0, x1, y1, fill) in furniture:
        d.rectangle([int(w * x0), int(h * y0), int(w * x1), int(h * y1)], fill=fill)
    for x in range(0, w, 53):
        for y in range(0, h, 37):
            img.putpixel((x, y), (30, 36, 44))
    return img


OLD_F = [(.05, .50, .33, .78, (62, 30, 24)), (.36, .62, .52, .74, (80, 48, 30)),
         (.08, .14, .26, .38, (120, 40, 40)), (.56, .60, .72, .80, (58, 36, 26))]
NEW_F = [(.07, .52, .35, .76, (226, 218, 204)), (.38, .64, .54, .72, (205, 205, 200)),
         (.10, .16, .24, .36, (236, 232, 222)), (.58, .58, .70, .78, (198, 190, 176))]
BEFORE, AFTER = room(176, OLD_F), room(182, NEW_F)


# ===========================================================================
print("\n[1] The Classic's composition and the Dramatic's copy of it still agree")
# ===========================================================================
# If this fails, the flight will sit at a different scale from the shots either side and
# every cut into a room will read as a jump. It is geometry mistake 1, caught at source.
for name, photo in (("landscape 3:2", room(170, OLD_F, 1600, 1067)),
                    ("portrait 9:16", room(170, OLD_F, 900, 1600)),
                    ("square", room(170, OLD_F, 1200, 1200)),
                    ("taller than 9:16", room(170, OLD_F, 700, 1600)),
                    ("very wide", room(170, OLD_F, 3000, 1000))):
    classic, fills = vr._contain_over_blur(photo)
    dramatic = vr._dramatic_composite(photo)
    check("%s: the two composites are byte-identical" % name,
          list(classic.getdata()) == list(dramatic.getdata()))
    backdrop, (fg_w, fg_h), (ox, oy) = vr._dramatic_backdrop(photo)
    check("%s: the foreground box is where the Classic pastes it" % name,
          backdrop.size == (vr.VW * vr.UPSCALE, vr.VH * vr.UPSCALE)
          and ox == (backdrop.width - fg_w) // 2 and oy == (backdrop.height - fg_h) // 2)
    check("%s: and the box agrees about reaching the sides" % name,
          (fg_w >= backdrop.width) == fills)


# ===========================================================================
print("\n[2] The zoom ramp is frame-exact, and the walk-through meets at every cut")
# ===========================================================================
knots = [(0, 1.0), (60, 1.1), (119, 1.55)]
expr = vr._zoom_ramp(knots)
check("the ramp is a function of `on`, never a feedback read of zoompan's `zoom` -- "
      "the trap that made the Classic look static for a month",
      "on" in expr and "zoom" not in expr, expr)
for frame, want in ((0, 1.0), (30, 1.05), (60, 1.1), (119, 1.55), (200, 1.55)):
    check("the ramp reaches %.3f at frame %d" % (want, frame),
          abs(vr._zoom_at(knots, frame) - want) < 1e-9,
          "%.6f" % vr._zoom_at(knots, frame))

for layout in ([True, True, [True, True, True, True]],
               [False, True, [True, True]],
               [True, False, [True]],
               [True, True, [False, False, False]],
               [False, False, [True, True, True, True]]):
    shots = vr.dramatic_shots(*layout)
    name = "opening=%s exterior=%s rooms=%s" % tuple(layout)
    if not shots:
        continue
    gaps = []
    for i in range(len(shots) - 1):
        a, b = shots[i], shots[i + 1]
        end = vr._zoom_at(a["knots"], int(round(a["seconds"] * vr.FPS)) - 1)
        if abs(end - vr._zoom_at(b["knots"], 0)) > 1e-9:
            gaps.append(i)
    check("%s: every cut is matched" % name, not gaps, str(gaps))
    check("%s: the last shot pushes into nothing" % name, shots[-1]["push"] is None)
    for shot in shots:
        zs = [vr._zoom_at(shot["knots"], f)
              for f in range(int(round(shot["seconds"] * vr.FPS)))]
        check("%s: shot %r never zooms below 1.0 (no black edge)" % (name, shot["role"]),
              min(zs) >= 1.0 - 1e-9, "%.4f" % min(zs))

shots = vr.dramatic_shots(True, True, [True, True, True, True])
check("the exterior drives at the front door",
      abs(vr._zoom_at(shots[1]["knots"], int(round(vr.EXTERIOR_SECONDS * vr.FPS)) - 1)
          - vr.DOOR_PUSH_ZOOM) < 1e-9)
check("and the first interior pulls out from the door's own point, not the frame's",
      shots[2]["anchor"] == vr.DOOR_CENTRE and shots[3]["anchor"] == vr.FRAME_CENTRE)
check("THE OPENING AND THE REBUILD ARE ONE MOVE, not two -- same camera, same plot, "
      "so only the house changes across the cut",
      shots[0]["anchor"] == shots[1]["anchor"] == vr.DOOR_CENTRE
      and shots[1].get("continuous") is True)
check("a continuous shot carries the move on rather than easing out of it",
      len(vr._walkthrough_knots(5.0, 1.2, 1.55, continuous=True)) == 3
      and vr._walkthrough_knots(5.0, 1.2, 1.55, continuous=True)[0][1] == 1.2)
check("an arrival DOES ease, because it is a new room opening up",
      vr._walkthrough_knots(5.0, 1.55, 1.22)[1][0]
      == int(round(vr.PULL_HEAD_SECONDS * vr.FPS)))
no_rebuild = vr.dramatic_shots(True, False, [True, True])
check("with no rebuilt exterior the OPENING is the shot we go through the door from",
      no_rebuild[0]["anchor"] == vr.DOOR_CENTRE
      and no_rebuild[0]["push"] == vr.DOOR_PUSH_ZOOM)
check("and the first room pulls out of it from the same point",
      no_rebuild[1]["anchor"] == vr.DOOR_CENTRE)
check("with no exterior shots at all, the film just opens on a room",
      vr.dramatic_shots(False, False, [True, True])[0]["anchor"] == vr.FRAME_CENTRE)
short = vr._walkthrough_knots(0.8, 1.55, 1.22)
check("a shot too short for both moves still MEETS its neighbours at both ends",
      short[0][1] == 1.55 and short[-1][1] == 1.22)


# ===========================================================================
print("\n[3] The disclaimers. Not optional, and not interchangeable.")
# ===========================================================================
shots = vr.dramatic_shots(True, True, [True, False, True, True])
notices = [(s["role"], s.get("treatment"), s["notice"]) for s in shots]
check("the opening carries ONLY the privacy line -- the house in it is a real "
      "photograph, and calling it a visualisation would be a false statement",
      notices[0] == ("opening", None, vr.PRIVACY_NOTICE_TEXT))
check("the rebuilt exterior carries the AI notice",
      notices[1] == ("exterior", None, vr.AI_NOTICE_TEXT))
check("every TRANSFORMED room carries the AI notice from its first frame -- the new "
      "room is underneath the flight the whole time",
      all(n == vr.AI_NOTICE_TEXT for _r, t, n in notices[2:] if t))
check("a room shown as a real photograph carries NEITHER",
      [n for r, t, n in notices[2:] if not t] == [None])
check("no shot ever carries both lines",
      not any(s["notice"] == vr.AI_NOTICE_TEXT and s["role"] == "opening"
              for s in shots))
check("the privacy line is the spec's own words",
      vr.PRIVACY_NOTICE_TEXT == "Neighbouring homes altered for privacy")
filters = vr._notice_filters(vr.AI_NOTICE_TEXT, "/tmp", "x")
check("the notice is drawn boxed, not merely shadowed -- a disclosure has to be "
      "legible over a bright sky, not just usually legible",
      len(filters) == 1 and "box=1" in filters[0] and "boxcolor=black@" in filters[0])
check("and read from a file with expansion off, like every other text in this module",
      "textfile=" in filters[0] and "expansion=none" in filters[0])
check("no notice at all draws nothing", vr._notice_filters(None, "/tmp", "x") == [])


# ===========================================================================
print("\n[4] The treatments, and the two load-bearing facts, in the real commands")
# ===========================================================================
def capture(plan, **kwargs):
    commands = []
    # Names one clip whose ffmpeg call fails while every other call still writes its
    # file -- the shape of "one shot died and the rest of the film is fine".
    fail_clip = kwargs.pop("fail_clip", None)
    def fake_run(cmd, timeout):
        commands.append(list(cmd))
        out = cmd[-1]
        if fail_clip and fail_clip in str(out):
            raise RuntimeError("ffmpeg died on this shot")
        if not out.startswith("-"):
            with open(out, "wb") as f:
                f.write(b"\0" * 2048)
    photos = kwargs.pop("photos")
    saved = (vr._run_ffmpeg, vr._fetch_image, vr._probe_duration, vr._has_ffmpeg_filter,
             vr._render_contact_card)
    vr._run_ffmpeg = fake_run
    vr._fetch_image = lambda url, attempts=2, deadline=None: photos[url]
    vr._probe_duration = lambda path, fallback: fallback
    vr._has_ffmpeg_filter = lambda name: name == "cas"
    # The music bed is NOT stubbed: a stub that reports no track adds a degradation of
    # its own, and this file's whole claim is about which degradations the Dramatic
    # produces. The real library is on disk and the ffmpeg call is captured anyway.
    vr._render_contact_card = lambda *a, **k: Image.new("RGB", (vr.VW, vr.VH))
    vr._clarity_filter_cache = None
    try:
        result = vr.render_dramatic_video(plan, **kwargs)
    finally:
        (vr._run_ffmpeg, vr._fetch_image, vr._probe_duration, vr._has_ffmpeg_filter,
         vr._render_contact_card) = saved
        vr._clarity_filter_cache = None
    return commands, result


def make_plan(rooms, opening="o", exterior="e"):
    plan = old_to_new.Plan()
    plan.opening_url, plan.exterior_url = opening, exterior
    plan.rooms = [{"before_url": b, "after_url": a, "space": None} for b, a in rooms]
    return plan


PHOTOS = {"o": room(150, []), "e": room(152, []), "ap": room(178, OLD_F, 900, 1600),
          "apa": room(180, NEW_F, 900, 1600)}
for i in range(5):
    PHOTOS["b%d" % i], PHOTOS["a%d" % i] = BEFORE, AFTER
PHOTOS["same"] = BEFORE          # an after identical to its before

plan = make_plan([("b%d" % i, "a%d" % i) for i in range(4)])
cmds, (data, degradations) = capture(plan, photos=PHOTOS, agent_name="Janel")
clips = [c for c in cmds if "d_clip_" in c[-1]]
graphs = {}
for c in clips:
    graphs[os.path.basename(c[-1])] = (
        c[c.index("-filter_complex") + 1] if "-filter_complex" in c
        else c[c.index("-vf") + 1], " ".join(str(a) for a in c))
check("six shots: the opening, the rebuild, and four rooms", len(clips) == 6,
      str(len(clips)))
check("a clean plan reports nothing to the agent", degradations == [], str(degradations))
check("the body is CONCATENATED, not dissolved -- the matched push and pull is what "
      "carries one shot into the next, so there is nothing to re-encode",
      any("dissolve_list" not in " ".join(c) and "-f" in c and "concat" in c
          for c in cmds))

bodies = [g for g, _ in graphs.values()]
check("ARRIVES: the base dissolve FINISHES EARLY, or the new furniture lands on the "
      "old room's walls and the next shot cuts to the new walls",
      any("blend=all_expr='A+(B-A)*clip(T/%.3f,0,1)'"
          % (vr.FLIGHT_SECONDS - vr.BASE_DISSOLVE_EARLY_SECONDS) in g for g in bodies))
check("and it finishes early by a real margin, not zero",
      vr.BASE_DISSOLVE_EARLY_SECONDS > 0)
check("SCATTER: the new room is simply underneath -- no base dissolve at all",
      vr._base_blend_expr(vr.TREATMENT_SCATTER, 5.0) is None)
check("BUILDS: the new room rises from the floor with a soft edge",
      any("clip((Y-H*(1-clip(T/" in g for g in bodies))
check("PUNCH: a hard cut inside a white flash, and the zoom peaks in the middle",
      any("if(lt(T,2.500),A,B)" in g and g.count(":color=white") == 2 for g in bodies))
punch = [s for s in vr.dramatic_shots(True, True, [True] * 4)
         if s.get("treatment") == vr.TREATMENT_PUNCH][0]
mid = vr._zoom_at(punch["knots"], int(round(punch["seconds"] * vr.FPS)) // 2)
check("the punch really punches (%.2f at the flash)" % mid, mid >= vr.PUNCH_ZOOM - 0.01)
check("all four treatments were used, each once",
      len({t for t in (vr.TREATMENT_SCATTER, vr.TREATMENT_ARRIVES, vr.TREATMENT_BUILDS,
                       vr.TREATMENT_PUNCH)}) == 4
      and [s["treatment"] for s in vr.dramatic_shots(True, True, [True] * 4)
           if s["kind"] == "room"] == list(vr.DRAMATIC_TREATMENTS))

flights = {n: g for n, (g, full) in graphs.items() if "f_%04d.png" in full}
check("exactly two shots fly furniture", len(flights) == 2, str(sorted(flights)))
for name, g in flights.items():
    comp = vr._dramatic_backdrop(AFTER)
    fg_w, fg_h = comp[1]
    check("%s: EVERY FRAME IS COMPOSED IN THE PHOTOGRAPH'S SPACE -- the flight layer "
          "is scaled back to the contain box before it is overlaid" % name,
          "scale=%d:%d,format=rgba" % (fg_w, fg_h) in g, g[:200])
    check("%s: and the zoom happens AFTER the composition, never before" % name,
          g.index("overlay=%d:%d" % comp[2]) < g.index("zoompan"))
    check("%s: the finished room stays on screen once the flight ends" % name,
          "eof_action=pass" in g)
check("the flight layer is drawn smaller and scaled up -- a resolution economy on "
      "motion-blurred pixels, not a change of space",
      0 < vr.FLIGHT_LAYER_SCALE < 1)


# ===========================================================================
print("\n[5] Every way it degrades, and none of them is a failed video")
# ===========================================================================
# THE MUSIC IS TIMED AGAINST THE SHOTS THAT SURVIVED, not the first few in the plan.
# When ffprobe cannot read the joined body, the fallback estimate used to be
# sum(shot["seconds"] for shot in shots[:len(clip_paths)]) -- the first N shots rather
# than the N that worked. Drop a shot from the middle and those are different lists, so
# the estimate drifts by the difference and the fade is placed against a length the film
# does not have. (With every shot the same length the bug is invisible, which is why the
# room length is changed here: the arithmetic has to be right whatever the durations are.)
def _trim_seconds(commands):
    """The body length the music was timed against, read out of the real filter."""
    for cmd in reversed(commands):
        joined = " ".join(str(a) for a in cmd)
        for token in joined.split():
            if token.startswith("[1:a]atrim=0:"):
                return float(token.split("[1:a]atrim=0:")[1].split(",")[0])
    return None


_saved_room_seconds = vr.ROOM_SECONDS
vr.ROOM_SECONDS = 7.0
try:
    plan = make_plan([("b%d" % i, "a%d" % i) for i in range(4)])
    shots = vr.dramatic_shots(True, True, [True] * 4)
    # The closing card is added on top of the body, so the trim is body + card. Measure
    # that offset from a clean render rather than restating the card's own constants.
    clean_cmds, _clean = capture(plan, photos=PHOTOS, agent_name="J")
    card_offset = _trim_seconds(clean_cmds) - sum(s["seconds"] for s in shots)
    # THE OPENING is the shot to drop: it is the one whose length differs from the rooms',
    # so "the shots that survived" and "the first N shots" give different totals. Dropping
    # a room would not -- every room is the same length, which is exactly why the bug was
    # invisible until the durations were pulled apart.
    survived = sum(s["seconds"] for s in shots[1:])
    first_n = sum(s["seconds"] for s in shots[:len(shots) - 1])
    cmds, (data, degs) = capture(plan, photos=PHOTOS, agent_name="J",
                                 fail_clip="d_clip_00")
    timed = _trim_seconds(cmds) - card_offset
    check("a shot that fails is dropped and reported, not fatal",
          any("one shot failed" in d for d in degs) and data, str(degs))
    check("the music is timed against the %.1fs that survived, not the %.1fs of the "
          "first five shots in the plan" % (survived, first_n),
          timed is not None and abs(timed - survived) < 0.05,
          "timed=%r survived=%.1f firstN=%.1f" % (timed, survived, first_n))
    check("(and the two really do differ, so this test can fail)",
          abs(survived - first_n) > 0.5)
finally:
    vr.ROOM_SECONDS = _saved_room_seconds

plan = make_plan([("b0", "same"), ("b1", "a1")])
cmds, (data, degs) = capture(plan, photos=PHOTOS, agent_name="J")
check("an after that is identical to its before gives an unusable mask",
      any("did not line up" in d for d in degs), str(degs))
check("and that room falls back to a plain dissolve rather than a melted render",
      bool(data) and len([c for c in cmds if "d_clip_" in c[-1]]) == 4)

plan = make_plan([("b0", None), ("b1", None)])
cmds, (data, degs) = capture(plan, photos=PHOTOS, agent_name="J")
check("rooms with no redesign play as real photographs, and the film still ships",
      bool(data))
check("the renderer does NOT duplicate the planner's note about them -- plan-level "
      "problems are reported once, by the planner", degs == [], str(degs))

plan = make_plan([("b0", "a0")], opening=None, exterior=None)
cmds, (data, degs) = capture(plan, photos=PHOTOS, agent_name="J")
check("no exterior at all: the film opens inside and still ships", bool(data))

plan = make_plan([("b0", "a0"), ("missing", "a1")])
cmds, (data, degs) = capture(plan, photos=PHOTOS, agent_name="J")
check("an unreadable room photo is left out, with a note", bool(data)
      and any("could not be read" in d for d in degs), str(degs))

plan = make_plan([("b0", "a0")])
saved_budget = vr.RENDER_BUDGET_SECONDS
vr.RENDER_BUDGET_SECONDS = -1
try:
    cmds, (data, degs) = capture(plan, photos=PHOTOS, agent_name="J")
    check("a render that runs out of budget finishes early rather than running on",
          bool(data) and any("took too long" in d for d in degs), str(degs))
    check("and it still renders the first shot rather than nothing",
          len([c for c in cmds if "d_clip_" in c[-1]]) == 1)
finally:
    vr.RENDER_BUDGET_SECONDS = saved_budget

plan = old_to_new.Plan()
try:
    vr.render_dramatic_video(plan)
    check("an empty plan raises rather than making an empty film", False)
except (ValueError, RuntimeError):
    check("an empty plan raises rather than making an empty film", True)

plan = make_plan([("b0", "a0")])
cmds_a, _ = capture(plan, photos=PHOTOS, agent_name="J", music_seed="L1")
cmds_b, _ = capture(make_plan([("b0", "a0")]), photos=PHOTOS, agent_name="J",
                    music_seed="L1")
import re as _re
def norm(cs):
    out = []
    for c in cs:
        row = []
        for a in c:
            a = os.path.basename(str(a)) if "/" in str(a) else str(a)
            # The delivery file's random suffix is deliberate (see _finish_film), and
            # the Classic golden masks it for the same reason.
            row.append(_re.sub(r"final_[0-9a-f]{8}", "final_X", a))
        out.append(row)
    return out
check("TWO RENDERS OF ONE LISTING ARE THE SAME FILM -- every spin and path is derived "
      "from the piece's index, never from randomness",
      norm(cmds_a) == norm(cmds_b))

pieces_a = vr._flight_pieces(
    BEFORE.resize((400, 267)), vr._difference_mask(
        BEFORE.resize((400, 267)), AFTER.resize((400, 267)))[0], (400, 267))
pieces_b = vr._flight_pieces(
    BEFORE.resize((400, 267)), vr._difference_mask(
        BEFORE.resize((400, 267)), AFTER.resize((400, 267)))[0], (400, 267))
check("the pieces themselves are deterministic",
      [(p["centre"], p["vector"], p["spin"]) for p in pieces_a]
      == [(p["centre"], p["vector"], p["spin"]) for p in pieces_b])
check("a real difference finds furniture to fly", len(pieces_a) > 0)
_mask, coverage = vr._difference_mask(BEFORE.resize((400, 267)),
                                      BEFORE.resize((400, 267)))
check("two identical frames read as no change at all", coverage < vr.MASK_MIN_COVERAGE)
_mask, coverage = vr._difference_mask(
    Image.new("RGB", (400, 267), (0, 0, 0)), Image.new("RGB", (400, 267), (255, 255, 255)))
check("two completely different frames read as outside the usable band",
      coverage > vr.MASK_MAX_COVERAGE)


# ===========================================================================
print("\n[6] main.py wiring: the template, the queue, and the one-job rule")
# ===========================================================================
src = io.open(os.path.join(BACKEND, "main.py"), encoding="utf-8").read()
tree = ast.parse(src)
fns = {n.name: n for n in ast.walk(tree)
       if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
seg = lambda n: ast.get_source_segment(src, fns[n])
import video_jobs

check("the three template ids are classic, dramatic and elegant, spelled exactly",
      vr.DRAMATIC_STYLE_ID == "dramatic" and vr.ELEGANT_STYLE_ID == "elegant")
check("the job type shares the template's spelling, so there is one word per thing",
      video_jobs.JOB_TYPE_DRAMATIC == vr.DRAMATIC_STYLE_ID
      and video_jobs.JOB_TYPE_ELEGANT == vr.ELEGANT_STYLE_ID)
check("/api/video-templates offers Classic and Dramatic, with human names",
      '{"id": DEFAULT_VIDEO_TEMPLATE_ID, "name": "Classic"}' in src
      and '{"id": DRAMATIC_VIDEO_TEMPLATE_ID, "name": "Dramatic"}' in src)
check("ELEGANT IS NAMED BUT NOT OFFERED -- no picker shows it and no job of that type "
      "can be enqueued",
      "ELEGANT_VIDEO_TEMPLATE_ID = video_renderer.ELEGANT_STYLE_ID" in src
      and '"name": "Elegant"' not in src
      and video_jobs.JOB_TYPE_ELEGANT not in video_jobs.JOB_TYPES)
gv = seg("generate_video")
check("asking for Elegant is a clear 'not yet', never a silent Classic",
      "UNRELEASED_VIDEO_TEMPLATE_IDS" in gv and "not available yet" in gv)
check("the request routes onto the template's own queue",
      "job_type = _job_type_for_template(chosen_video_template_id)" in gv
      and "video_jobs.enqueue,\n" in gv and "                job_type," in gv)
check("a Dramatic request with no FAL_KEY fails FAST, before the agent queues",
      "fal_images.is_enabled()" in gv and gv.count("status_code=400") >= 5)
check("ONE VIDEO JOB PER LISTING, whatever its type -- two would race to the same "
      "file and the last one would win the bytes",
      "_find_active_video_job" in gv and "already_in_flight" in gv)
check("and the response names the template actually being made, not the one asked for",
      '"requested_video_template_id": chosen_video_template_id' in gv)
check("the status endpoint looks for a job of either type",
      "_find_active_video_job" in seg("get_video_status"))
check("so do the finished render's notes",
      "_find_done_video_job" in seg("_stored_video_degradations"))
check("the queued Dramatic run re-reads the agent rather than carrying tokens in the "
      "job row", 'db.table("agents").select("*")' in seg("_dramatic_job_run"))
check("and fails PERMANENTLY, not three times over, on things retrying cannot fix",
      seg("_dramatic_job_run").count("PermanentJobError") >= 3)
check("a budget that has run out is permanent too, with the agent's own message",
      "old_to_new.BudgetExhausted" in seg("_dramatic_job_run"))
check("the cache is written BEFORE the render, so a retry never pays twice",
      seg("_render_dramatic_core").index("old_to_new_images")
      < seg("_render_dramatic_core").index("render_dramatic_video"))
check("the cache write is skipped when the column is not there yet",
      '"old_to_new_images" in listing' in seg("_render_dramatic_core"))
check("ownership is confirmed before the upload AND before the listing write",
      seg("_render_dramatic_core").count("ownership_check()") == 2)
# THE COUNT DOES NOT COME OUT OF THE JOBS TABLE ANY MORE. It used to count distinct
# listings with a Dramatic job since the 1st -- and video_jobs.prune_finished() deletes
# finished rows after PRUNE_AFTER_DAYS, so from the 8th the ceiling could only see a week
# and reset itself about weekly.
check("the monthly tally is NOT read from the prunable jobs table",
      "video_jobs.TABLE" not in seg("_old_to_new_images_used")
      and "video_jobs" not in seg("_old_to_new_images_used"))
check("it is read from the agent row, which nothing prunes",
      'db.table("agents")' in seg("_read_old_to_new_usage"))
check("and it is counted in IMAGES, which is what fal bills for",
      'OLD_TO_NEW_IMAGES_COLUMN = "old_to_new_images_used"' in src)
check("a database blip allows the render rather than blocking it",
      "return 0" in seg("_old_to_new_images_used"))
check("the paid images are recorded from the CORE, so the in-process fallback path -- "
      "which writes no job row at all -- is counted the same way",
      "_add_old_to_new_images(agent[\"id\"], plan.images_paid)"
      in seg("_render_dramatic_core"))
check("recording cannot cost an agent their video: it never raises",
      "NEVER RAISES" in seg("_add_old_to_new_images"))
check("there is a migration for both money controls, and it no longer calls itself "
      "optional",
      os.path.exists(os.path.join(BACKEND, "docs", "old-to-new-images-migration.sql")))
_mig = open(os.path.join(BACKEND, "docs", "old-to-new-images-migration.sql"),
            encoding="utf-8").read()
check("the migration header says it IS a blocker, on the money axis",
      "not a blocker" not in _mig and "RUN THIS BEFORE" in _mig)
check("and it creates the tally columns as well as the cache column",
      "old_to_new_images_used" in _mig and "old_to_new_month" in _mig
      and "old_to_new_images jsonb" in _mig)
check("there is a separate migration for the cross-type enqueue race, for Jane to run "
      "on its own",
      os.path.exists(os.path.join(BACKEND, "docs",
                                  "video-jobs-dramatic-migration.sql")))
_vjmig = open(os.path.join(BACKEND, "docs", "video-jobs-dramatic-migration.sql"),
              encoding="utf-8").read()
check("it widens the unique index to the listing alone",
      "on public.video_jobs (listing_id)" in _vjmig
      and "where status in ('queued', 'running')" in _vjmig)
check("the new index is created BEFORE the old one is dropped, so there is never an "
      "instant with no rule in force",
      _vjmig.index("create unique index if not exists "
                   "video_jobs_one_active_per_listing")
      < _vjmig.index("drop index if exists"))
check("and it lets the queue accept 'dramatic' at all -- the live check constraint "
      "allows only classic and signature",
      "video_jobs_type_check" in _vjmig and "'dramatic'" in _vjmig)
check("the Dramatic has its own fleet-wide cap, because fal is the constraint",
      video_jobs.JOB_TYPES[video_jobs.JOB_TYPE_DRAMATIC].account_limit == 1)
check("startup registers both renderers",
      "video_jobs.JOB_TYPE_DRAMATIC,\n" in src and "_dramatic_job_run" in src)


# ===========================================================================
print("\n[7] The shipped generate_video source, executed against stubs")
# ===========================================================================
# Reading the source proves the lines are there; running it proves they do the thing.
import asyncio, datetime as _dt, types                                  # noqa: E402
from typing import Optional, List                                       # noqa: E402
from fastapi import HTTPException, Depends, Query                       # noqa: E402

LISTING = {"id": "l1", "images": ["https://x/%d.jpg" % i for i in range(6)],
           "video_status": None}


def run_endpoint(fal_on=True, active=None, stored=None, ceiling_ok=True,
                 queue_up=True, inprocess_busy=0, **params):
    seen = {}
    listing = dict(LISTING)
    if stored is not None:
        listing["video_template_id"] = stored

    class FakeJobs:
        JOB_TYPE_CLASSIC = "classic"
        JOB_TYPE_DRAMATIC = "dramatic"
        TABLE = "video_jobs"
        # The endpoint falls back to this when describe() fails, so that a
        # response can never say "rendering" with no queued/running truth in it.
        STATUS_QUEUED = "queued"
        @staticmethod
        def available(): return queue_up
        @staticmethod
        def enqueue(job_type, listing_id, agent_id, payload):
            seen["job_type"] = job_type
            seen["payload"] = payload
            return {"id": "j1", "job_type": job_type, "payload": payload}, True
        @staticmethod
        def describe(job): return {"job_status": "queued", "queue_position": 1}
        @staticmethod
        def find_active(job_type, listing_id):
            return active if active and active["job_type"] == job_type else None

    async def db_exec(fn, what=""):
        return types.SimpleNamespace(data=[listing])
    async def noop(*a, **k): return None

    g = dict(asyncio=asyncio, datetime=_dt.datetime, timezone=_dt.timezone,
             timedelta=_dt.timedelta, HTTPException=HTTPException, Depends=Depends,
             Query=Query, Optional=Optional, List=List, logger=logging.getLogger("t"),
             video_renderer=vr, video_jobs=FakeJobs, db_execute_async=db_exec,
             send_telegram_alert_throttled=noop, get_current_agent=lambda: None,
             DEFAULT_VIDEO_TEMPLATE_ID="classic",
             DRAMATIC_VIDEO_TEMPLATE_ID="dramatic",
             UNRELEASED_VIDEO_TEMPLATE_IDS={"elegant"},
             VIDEO_JOB_TYPES=("classic", "dramatic"),
             _resolve_video_template_id=lambda t: t if t in ("classic", "dramatic") else None,
             _job_type_for_template=lambda t: {"dramatic": "dramatic"}.get(t, "classic"),
             fal_images=types.SimpleNamespace(is_enabled=lambda: fal_on),
             _video_render_tasks=set(),
             # The money gate: without the monthly tally columns there is no ceiling and
             # no cache, so the endpoint declines the Dramatic rather than spending with
             # no limit. See main._old_to_new_ceiling_enforceable.
             _old_to_new_ceiling_enforceable=lambda: ceiling_ok,
             # The in-process fallback's own cap. `inprocess_busy` stands for renders this
             # process already has in flight on that path.
             MAX_INPROCESS_DRAMATIC_RENDERS=2,
             _dramatic_inprocess_tasks=set(range(inprocess_busy)),
             _render_video_job=lambda *a: seen.setdefault("legacy", "classic"),
             _render_dramatic_job=lambda *a: seen.setdefault("legacy", "dramatic"))
    for nm in ("_find_active_video_job", "generate_video"):
        node = fns[nm]
        exec(compile("\n".join(src.splitlines()[node.lineno - 1:node.end_lineno]),
                     nm, "exec"), g)
    kw = dict(video_template_id=None, photo_index=0, music_track=None,
              video_photos=None, agent={"id": "a1"})
    kw.update(params)
    try:
        return asyncio.run(g["generate_video"]("l1", **kw)), seen
    except HTTPException as e:
        return ("HTTP", e.status_code, e.detail), seen


resp, seen = run_endpoint(video_template_id="dramatic")
check("a Dramatic request goes on the DRAMATIC queue, not the Classic's",
      seen.get("job_type") == "dramatic", str(seen.get("job_type")))
check("and the payload names the template that will be rendered",
      seen["payload"]["video_template_id"] == "dramatic", str(seen.get("payload")))
check("the response reports it back", resp["video_template_id"] == "dramatic")

resp, seen = run_endpoint()
check("no template asked for still means the Classic, on the Classic queue",
      seen.get("job_type") == "classic")

resp, seen = run_endpoint(fal_on=False, video_template_id="dramatic")
check("WITHOUT FAL_KEY the agent is told NOW, not after waiting in a queue",
      resp[0] == "HTTP" and resp[1] == 400 and "not switched on" in resp[2], str(resp))
check("and nothing was queued", "job_type" not in seen)

# A listing that REMEMBERS the Dramatic style, on a deploy that has lost the key.
# Nobody asked for Dramatic on this press -- the row did -- so refusing would tell
# an agent no about a listing of their own for a reason they cannot see. An explicit
# request still gets the honest 400 (the check above).
resp, seen = run_endpoint(fal_on=False, stored="dramatic")
check("a listing that remembers Dramatic renders the CLASSIC when the key is gone, rather than refusing",
      seen.get("job_type") == "classic"
      and seen["payload"]["video_template_id"] == "classic", str(seen))
check("and the row is healed, so the fallback fires once rather than every render",
      seen["payload"]["persist_video_template_id"] is True, str(seen.get("payload")))
resp, seen = run_endpoint(fal_on=True, stored="dramatic")
check("with the key present a remembered Dramatic is still a Dramatic",
      seen.get("job_type") == "dramatic", str(seen.get("job_type")))

resp, seen = run_endpoint(video_template_id="elegant")
check("Elegant is a clear 'not available yet', never a silent Classic",
      resp[0] == "HTTP" and resp[1] == 400 and "not available yet" in resp[2], str(resp))
check("and nothing was queued for it either", "job_type" not in seen)

resp, seen = run_endpoint(video_template_id="nonsense")
check("an id we have never shipped is still an honest 400",
      resp[0] == "HTTP" and resp[1] == 400 and "Unknown" in resp[2], str(resp))

in_flight = {"id": "j0", "job_type": "classic", "status": "queued",
             "payload": {"video_template_id": "classic", "music_track": "piano"}}
resp, seen = run_endpoint(video_template_id="dramatic", active=in_flight)
check("ONE VIDEO JOB PER LISTING: switching style mid-render does NOT start a second "
      "render racing the first to the same file",
      "job_type" not in seen, str(seen.get("job_type")))
check("the agent is told where the render they already started has got to",
      resp.get("already_in_flight") is True and resp.get("queue_position") == 1,
      str(resp))
check("and told honestly WHICH style is actually being made",
      resp["video_template_id"] == "classic"
      and resp["requested_video_template_id"] == "dramatic", str(resp))

resp, seen = run_endpoint(video_template_id="dramatic",
                          active={"id": "j0", "job_type": "dramatic", "status": "queued",
                                  "payload": {"video_template_id": "dramatic"}})
check("pressing Generate twice on the SAME style still goes through enqueue, where "
      "the database index is the thing that stops a double render",
      seen.get("job_type") == "dramatic")

# ---------------------------------------------------------------------------
# THE MONEY GATE. Without the monthly tally columns there is no ceiling AND no picture
# cache, so every press would pay again and nothing would count it. The second audit
# found the probe that knows this was read by nothing; it is now the gate.
resp, seen = run_endpoint(video_template_id="dramatic", ceiling_ok=False)
check("with no monthly picture tally to count against, a Dramatic request is refused "
      "NOW rather than spending with no ceiling",
      isinstance(resp, tuple) and resp[0] == "HTTP" and resp[1] == 400
      and "not switched on" in resp[2], str(resp))
check("and nothing was queued", "job_type" not in seen)
resp, seen = run_endpoint(stored="dramatic", ceiling_ok=False)
check("a listing that REMEMBERS Dramatic renders the Classic instead, rather than "
      "refusing an agent their own listing for a reason they cannot see",
      seen.get("job_type") == "classic", str(seen.get("job_type")))
resp, seen = run_endpoint(video_template_id="classic", ceiling_ok=False)
check("and the Classic is untouched by any of it", seen.get("job_type") == "classic")

# ---------------------------------------------------------------------------
# THE IN-PROCESS FALLBACK'S OWN CAP. Reached only while the video_jobs table is absent,
# which is every Dramatic render until the queue migration runs. Its only other lock is
# per LISTING, so one agent with ten listings used to get ten concurrent PAID generation
# passes onto one agent row.
resp, seen = run_endpoint(queue_up=False, video_template_id="dramatic")
check("with no queue table, a Dramatic still renders in-process",
      seen.get("legacy") == "dramatic", str(seen))
resp, seen = run_endpoint(queue_up=False, video_template_id="dramatic",
                          inprocess_busy=2)
# A tuple when the endpoint raised an HTTPException, a dict when it went ahead. Spelled
# out rather than indexed, so a version that goes ahead FAILS this check instead of
# raising a KeyError in the test.
check("but the third concurrent one is refused with a plain 409, not started",
      isinstance(resp, tuple) and resp[0] == "HTTP" and resp[1] == 409
      and "already being prepared" in resp[2], str(resp))
check("and no render was kicked off for it", "legacy" not in seen)
resp, seen = run_endpoint(queue_up=False, video_template_id="classic",
                          inprocess_busy=2)
check("the cap is on the DRAMATIC only -- a Classic is not blocked by it",
      seen.get("legacy") == "classic", str(resp))


print("\n%d passed, %d failed" % (P, F))
sys.exit(1 if F else 0)
