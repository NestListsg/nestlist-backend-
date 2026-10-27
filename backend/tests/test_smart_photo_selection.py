"""Smart photo selection: choosing a VARIED set of listing photos for the Classic.

The problem being solved: the old automatic selection was "hero photo, then everything
else in upload order, truncate". It had no idea what was in any photo, so on the demo
listing it spent two of its six slots on the swimming pool and never showed the dining
room.

Three things are protected here, in order of how much they would cost if they broke.

1. IT CAN NEVER FAIL A RENDER. Every way the vision call can go wrong -- no API key, a
   timeout, an exception, unparseable JSON, nonsense labels, unreadable photos -- has
   to end in the plain upload-order selection, silently, with nothing reported to the
   agent. A worse-chosen video is a small loss; a failed render is a large one.
2. THE AGENT'S OWN PICK STILL WINS. choose_video_photos is a shipped feature. Smart
   selection may only ever replace the AUTO path.
3. CAPTIONS STAY ON THE RIGHT PHOTO. The survey writes captions for the photos it
   chose; the renderer then re-fetches those photos and may drop one. Captions are
   keyed by url for exactly that reason -- anything keyed by position would slide a
   caption onto the next photo along, which is a false claim about the property.
4. THE OPENING SHOT IS CHOSEN (added 2026-10-27). The living room opens the Classic,
   falling back to the facade and then the next most impressive living space; stairs,
   bathrooms, the entrance lobby and the carpark never open one and play last. The
   rule applies to AUTO only -- (2) above still comes first -- and the pecking order
   lives in ONE table, video_renderer.SPACE_RANKING.

Runs without ffmpeg, without a network and without an API key.
"""
import json, logging, os, random, sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
logging.disable(logging.CRITICAL)
from PIL import Image  # noqa: E402
import video_renderer as vr  # noqa: E402

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("  PASS  " if cond else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


def photo(w=1600, h=1067):
    return Image.new("RGB", (w, h), (160, 140, 120))


URLS = ["https://x/%d.jpg" % i for i in range(12)]


# --------------------------------------------------------------- label normalising
print("\n[labels] free-text room names collapse onto the fixed vocabulary")
for raw, want in [("living hall", "living"), ("Living Room", "living"),
                  ("master bedroom", "bedroom"), ("swimming pool", "pool"),
                  ("en-suite bathroom", "bathroom"), ("dining area", "dining"),
                  ("BALCONY", "balcony"), ("  kitchen  ", "kitchen")]:
    check("%r -> %r" % (raw, want), vr._normalise_space(raw) == want,
          vr._normalise_space(raw))
for raw in ("", "junk", None, 7, {"a": 1}, "bath", "roof deck"):
    check("unrecognised %r -> %s" % (raw, vr.SPACE_UNKNOWN),
          vr._normalise_space(raw) == vr.SPACE_UNKNOWN)
check("'bathroom' never collapses to 'bath' by prefix",
      vr._normalise_space("bathroom") == "bathroom")


# ------------------------------------------------------------------ the selection
print("\n[selection] one photo per space, hero first, agent's order kept")
V = vr._varied_order
LIMIT, MIN = vr.MAX_VIDEO_PHOTOS, vr.MIN_VIDEO_PHOTOS

janes_bug = ["living", "pool", "pool", "bedroom", "kitchen", "bathroom", "dining"]
got = [janes_bug[i] for i in V(janes_bug, LIMIT, MIN)]
check("the demo listing's two pool shots become one", got.count("pool") == 1, str(got))
check("...and the dining room that was dropped is now in", "dining" in got, str(got))

many = ["living", "living", "pool", "pool", "dining", "kitchen",
        "bedroom", "bedroom", "bathroom", "pool", "living", "garden"]
pick = V(many, LIMIT, MIN)
# CHANGED 2026-10-27 with the opening-shot rule: the bathroom used to sit between the
# bedroom and the garden, because everything after the hero was in pure upload order.
# It now plays LAST -- see SPACE_RANKING. The SET of photos is the same seven.
check("12 photos over 7 spaces -> 7 shots, one each",
      [many[i] for i in pick] == ["living", "pool", "dining", "kitchen", "bedroom",
                                  "garden", "bathroom"], str([many[i] for i in pick]))
check("the hero is still in the film", 0 in pick, str(pick))
check("the rooms after the opener stay in the agent's upload order",
      [i for i in pick[1:] if vr._space_opens(many[i])]
      == sorted(i for i in pick[1:] if vr._space_opens(many[i])), str(pick))

print("\n[selection] the top-up, when there is not enough variety to fill the film")
thin = ["living"] * 4 + ["pool"] * 3 + ["kitchen"] * 2
got = [thin[i] for i in V(thin, LIMIT, MIN)]
check("a 3-space listing still yields MIN_VIDEO_PHOTOS shots", len(got) == MIN, str(got))
check("the top-up is spread across spaces, not stacked on the first one",
      got.count("living") == 2 and got.count("pool") == 2 and got.count("kitchen") == 2,
      str(got))
only_one = ["living"] * 9
check("a listing with one space falls back to MIN shots of it",
      len(V(only_one, LIMIT, MIN)) == MIN)
check("fewer photos than MIN cannot invent any",
      V(["living", "dining"], LIMIT, MIN) == [0, 1])

print("\n[selection] 'other' is never treated as a duplicate")
unknown = [vr.SPACE_UNKNOWN] * 15
check("15 unlabelled photos fill the film rather than collapsing to one",
      len(V(unknown, LIMIT, MIN)) == LIMIT)
mixed = ["living", vr.SPACE_UNKNOWN, vr.SPACE_UNKNOWN, "living", vr.SPACE_UNKNOWN]
check("unknowns survive next to a real duplicate",
      [mixed[i] for i in V(mixed, LIMIT, MIN)].count(vr.SPACE_UNKNOWN) == 3,
      str([mixed[i] for i in V(mixed, LIMIT, MIN)]))

print("\n[selection] invariants over 4000 random listings")
random.seed(11)
bad = []
for _ in range(4000):
    n = random.randint(1, 15)
    spaces = [random.choice(list(vr._SPACE_VOCABULARY)) for _ in range(n)]
    r = V(spaces, LIMIT, MIN)
    if 0 not in r:
        bad.append(("hero dropped from the film", spaces, r))
    if len(set(r)) != len(r):
        bad.append(("a photo used twice", spaces, r))
    if len(r) > LIMIT or any(not 0 <= i < n for i in r):
        bad.append(("out of bounds", spaces, r))
    # The opening shot: a space that may open, whenever the chosen set holds one at
    # all, and the best-ranked such space in it.
    openable = [i for i in r if vr._space_opens(spaces[i])]
    if openable and not vr._space_opens(spaces[r[0]]):
        bad.append(("opened on a space that may not open", spaces, r))
    if openable and vr._space_rank(spaces[r[0]]) != min(
            vr._space_rank(spaces[i]) for i in openable):
        bad.append(("a better-ranked opener was available", spaces, r))
    # After the opener: rooms in upload order, then circulation in upload order.
    rooms = [i for i in r[1:] if vr._space_opens(spaces[i])]
    circ = [i for i in r[1:] if not vr._space_opens(spaces[i])]
    if rooms != sorted(rooms) or circ != sorted(circ):
        bad.append(("upload order broken within a band", spaces, r))
    if r[1:] != rooms + circ:
        bad.append(("circulation not pushed to the end", spaces, r))
    if len(r) < min(n, MIN):
        bad.append(("shorter than MIN with photos to spare", spaces, r))
    # THE LENGTH GUARANTEE. Deprioritising a space must never cost a shot: the film
    # is as long as the old rule's film, which was min(distinct spaces, LIMIT) topped
    # up to min(LIMIT, MIN).
    distinct = len({s for s in spaces if s != vr.SPACE_UNKNOWN}) + \
        sum(1 for s in spaces if s == vr.SPACE_UNKNOWN)
    if len(r) < min(n, max(min(distinct, LIMIT), min(LIMIT, MIN))):
        bad.append(("shorter than the old rule's film", spaces, r))
    kept = [spaces[i] for i in r if spaces[i] != vr.SPACE_UNKNOWN]
    if len(r) <= len({s for s in spaces if s != vr.SPACE_UNKNOWN}) and \
            len(kept) != len(set(kept)):
        bad.append(("repeated a space while variety was still available", spaces, r))
check("no invariant violated in 4000 listings", not bad, str(bad[:2]))


# ------------------------------------------------------------ the opening shot
# ADDED 2026-10-27. Jane, on the comparison films: "i don't want the video to start
# with the stairs. start it with the living room", and then "the stairs is not an
# important feature in the house". Before this, NOTHING chose the opening shot --
# whatever the photo ordering produced landed first, so real films opened on a dining
# hall, on a bungalow facade and on a stairwell, and one gave a stairwell the SECOND
# slot and a hero's caption. The first second of a Reel decides whether anyone watches.
print("\n[opening] the living room opens the Classic, and circulation never does")


def opens(spaces):
    """The space the film would open on."""
    return spaces[V(spaces, LIMIT, MIN)[0]]


def film(spaces):
    return [spaces[i] for i in V(spaces, LIMIT, MIN)]


# The three real openings Jane's own listings had, each with the hero photo FIRST in
# the candidate list -- which is how each of them came to lead its film.
stairwell = ["stairs", "living", "dining", "kitchen", "bedroom", "bathroom", "pool"]
check("the local render's STAIRWELL opening becomes the living room",
      opens(stairwell) == "living", str(film(stairwell)))
dining_first = ["dining", "living", "stairs", "kitchen", "bedroom", "garden", "bathroom"]
check("the production film that opened on the DINING HALL now opens on the living room",
      opens(dining_first) == "living", str(film(dining_first)))
facade_first = ["exterior", "living", "stairs", "bathroom", "kitchen", "bedroom", "dining"]
check("the production film that opened on the BUNGALOW FACADE now opens on the "
      "living room", opens(facade_first) == "living", str(film(facade_first)))

print("  the fallback chain")
check("no living room -> the exterior/facade opens",
      opens(["stairs", "dining", "kitchen", "bedroom", "bathroom", "exterior",
             "garden"]) == "exterior")
check("no living room and no exterior -> the next most impressive living space",
      opens(["stairs", "bathroom", "bedroom", "kitchen", "dining", "study",
             "garden"]) == "dining")
check("...and below that, rank order decides, not upload order",
      opens(["study", "bedroom", "garden", "pool"]) == "pool")
check("a listing of nothing but unlabelled photos still opens on one of them",
      opens([vr.SPACE_UNKNOWN] * 8) == vr.SPACE_UNKNOWN)
check("...and is left in exactly today's order, hero leading",
      V([vr.SPACE_UNKNOWN] * 8, LIMIT, MIN) == list(range(8)))

print("  never the opening shot: stairs, bathroom, entrance lobby, carpark")
for bad_opener in ("stairs", "bathroom", "entrance", "carpark"):
    spaces = [bad_opener, "living"]
    check("a %s first in the order does not open the film" % bad_opener,
          opens(spaces) == "living", str(film(spaces)))
    # ...and not even when it is the only thing before the weakest room there is.
    spaces = [bad_opener, vr.SPACE_UNKNOWN]
    check("...nor ahead of a photo nobody could label", opens(spaces) == vr.SPACE_UNKNOWN,
          str(film(spaces)))

print("  the listing whose ONLY candidate is a staircase")
only_stairs = ["stairs"] * 7
r = V(only_stairs, LIMIT, MIN)
check("it still gets a film rather than nothing", len(r) == MIN, str(r))
check("...in exactly today's order, hero leading (nothing better exists)",
      r == list(range(MIN)), str(r))
stairs_and_baths = ["stairs", "bathroom", "stairs", "entrance", "carpark", "bathroom",
                    "stairs"]
r = V(stairs_and_baths, LIMIT, MIN)
check("a listing of nothing but circulation and bathrooms degrades the same way",
      r == list(range(MIN)), str(r))


# ------------------------------------------- circulation is low value, not banned
print("\n[low value] stairs lose slots to real rooms, but never shrink a film")
rich = ["living", "stairs", "bathroom", "entrance", "carpark", "dining", "kitchen",
        "pool", "garden", "view", "bedroom", "study", "terrace", "balcony", "gym"]
got = film(rich)
check("15 photos over 15 spaces -> 10 shots and NOT ONE of them circulation",
      len(got) == LIMIT and not any(s in ("stairs", "bathroom", "entrance", "carpark")
                                    for s in got), str(got))
check("...the ten it kept are the ten best-ranked spaces this listing HAS (the gym "
      "goes too, 11th of the fifteen -- the cut is rank and nothing else)",
      set(got) == set(sorted(rich, key=vr._space_rank)[:LIMIT]), str(sorted(got)))
check("...and they play in the listing's own upload order behind the living room",
      got == ["living", "dining", "kitchen", "pool", "garden", "view", "bedroom",
              "study", "terrace", "balcony"], str(got))

short = ["living", "stairs", "dining", "kitchen", "bedroom", "bathroom", "garden"]
got = film(short)
check("a listing SHORT of photos keeps its staircase rather than losing a shot",
      len(got) == len(short) and "stairs" in got, str(got))
check("...but plays it at the end, not in the second slot it used to get",
      got.index("stairs") >= len(got) - 2, str(got))
check("...and the bathroom is at the end too", got[-2:] == ["stairs", "bathroom"]
      or got[-2:] == ["bathroom", "stairs"], str(got))

# The guarantee Jane asked for in so many words: this rule may never make a film
# shorter than the one it replaces. Checked against the OLD rule's length directly.
random.seed(77)
shrunk = []
for _ in range(4000):
    n = random.randint(1, 15)
    spaces = [random.choice(list(vr._SPACE_VOCABULARY)) for _ in range(n)]
    # What the old rule produced: one per distinct space in upload order, truncated at
    # LIMIT, topped up to min(LIMIT, MIN).
    seen, distinct = set(), 0
    for i, s in enumerate(spaces):
        if i == 0 or s == vr.SPACE_UNKNOWN or s not in seen:
            distinct += 1
            seen.add(s)
    was = min(n, max(min(distinct, LIMIT), min(LIMIT, MIN)))
    if len(V(spaces, LIMIT, MIN)) < was:
        shrunk.append((spaces, was, V(spaces, LIMIT, MIN)))
check("over 4000 listings, not one film is shorter than the old rule's",
      not shrunk, str(shrunk[:2]))

print("\n[table] the pecking order is one table, and it covers the vocabulary")
check("every space the vision pass can write is ranked",
      {s for s, _o in vr.SPACE_RANKING} == set(vr._SPACE_VOCABULARY),
      str({s for s, _o in vr.SPACE_RANKING} ^ set(vr._SPACE_VOCABULARY)))
check("no space is ranked twice",
      len({s for s, _o in vr.SPACE_RANKING}) == len(vr.SPACE_RANKING))
check("the living room is ranked first", vr.SPACE_RANKING[0][0] == "living")
check("the exterior/facade is the fallback opener", vr.SPACE_RANKING[1][0] == "exterior")
check("stairs are ranked last", vr.SPACE_RANKING[-1][0] == "stairs")
check("circulation, bathrooms and the carpark may not open a film",
      not any(vr._space_opens(s) for s in ("stairs", "bathroom", "entrance", "carpark")))
check("unlabelled photos MAY open a film -- a listing of them must still get one",
      vr._space_opens(vr.SPACE_UNKNOWN))
check("...and are ranked below every real room, above circulation",
      vr._space_rank(vr.SPACE_UNKNOWN) > vr._space_rank("bedroom")
      and vr._space_rank(vr.SPACE_UNKNOWN) < vr._space_rank("stairs"))
check("a label the table forgot ranks last and may not open the film",
      vr._space_rank("penthouse-helipad") == len(vr.SPACE_RANKING)
      and not vr._space_opens("penthouse-helipad"))


# ------------------------------------------------------------- the survey parsing
print("\n[survey] the model's reply is parsed, guarded, and never trusted")


def with_reply(text, n=3, copy_guard=None):
    """Run _survey_photos against a canned model reply."""
    saved_call, saved_keys = vr._vision_call, vr._anthropic_keys
    vr._anthropic_keys = lambda: ["test-key-not-a-real-credential"]
    vr._vision_call = lambda content, timeout, max_tokens=1024, budget=None: (text, None)
    try:
        return vr._survey_photos([photo()] * n, copy_guard=copy_guard)
    finally:
        vr._vision_call, vr._anthropic_keys = saved_call, saved_keys


good = json.dumps({"photos": [
    {"n": 1, "space": "living hall", "caption": "the living hall ... where laughter lingers"},
    {"n": 2, "space": "kitchen", "caption": "the kitchen ... where the day begins"},
    {"n": 3, "space": "pool", "caption": "the lap pool ... where mornings feel slow"}]})
out, note = with_reply(good)
check("a clean reply gives a space and a caption per photo",
      [s for s, _c in out] == ["living", "kitchen", "pool"] and all(c for _s, c in out),
      str(out))
check("a clean reply raises no note", note is None, str(note))

out, _ = with_reply("here you go!\n" + good + "\nhope that helps")
check("prose around the JSON is tolerated",
      [s for s, _c in out] == ["living", "kitchen", "pool"])

for broken, label in [("not json at all", "not JSON"),
                      ('{"photos": ', "truncated mid-reply"),
                      ('{"wrong_key": []}', "right JSON, wrong shape"),
                      ("", "empty reply")]:
    out, note = with_reply(broken)
    check("%s -> every photo unlabelled, nothing raised" % label,
          out == [(vr.SPACE_UNKNOWN, None)] * 3 and note, str(note))

out, _ = with_reply(json.dumps({"photos": [
    {"n": 99, "space": "living", "caption": "the living hall ... where laughter lingers"},
    {"n": "x", "space": "pool", "caption": "the pool ... where days feel slow"},
    {"n": 2, "space": "kitchen", "caption": "the kitchen ... where the day begins"}]}))
check("an out-of-range or non-numeric photo number is dropped, not applied",
      out[0][0] == vr.SPACE_UNKNOWN and out[1] == ("kitchen", "the kitchen ... where the day begins")
      and out[2][0] == vr.SPACE_UNKNOWN, str(out))

unsafe = json.dumps({"photos": [
    {"n": 1, "space": "living", "caption": "the stunning living hall ... where luxury lives"},
    {"n": 2, "space": "kitchen", "caption": "unit 12-04 ... where the day begins"},
    {"n": 3, "space": "pool", "caption": "the lap pool ... where mornings feel slow"}]})
out, note = with_reply(unsafe)
check("a superlative caption is dropped but its LABEL still counts for selection",
      out[0] == ("living", None), str(out[0]))
check("a caption with a digit is dropped too", out[1] == ("kitchen", None), str(out[1]))
check("the safe caption survives", out[2][1] is not None)
check("the agent is told captions were rejected", note and "rejected" in note, str(note))

out, _ = with_reply(good, copy_guard=lambda t, context=None: (_ for _ in ()).throw(ValueError("boom")))
check("a copy guard that raises costs the captions, not the run",
      all(c is None for _s, c in out) and [s for s, _c in out] == ["living", "kitchen", "pool"])

saved_keys = vr._anthropic_keys
vr._anthropic_keys = lambda: []
try:
    out, note = vr._survey_photos([photo()] * 3)
    check("no API key -> unlabelled, with a note, no exception",
          out == [(vr.SPACE_UNKNOWN, None)] * 3 and note == "no API key")
finally:
    vr._anthropic_keys = saved_keys


# ---------------------------------------------------- the whole pass, and failure
print("\n[fallback] every failure mode ends in plain upload order, silently")


def run_selection(n=12, fetch=None, survey=None, keys=("k",)):
    saved = {k: getattr(vr, k) for k in
             ("_fetch_image", "_survey_photos", "_anthropic_keys")}
    vr._anthropic_keys = lambda: list(keys)
    vr._fetch_image = fetch or (lambda url, attempts=2, deadline=None: photo())
    if survey is not None:
        vr._survey_photos = survey
    try:
        return vr._smart_auto_selection(URLS[:n], 0)
    finally:
        for k, v in saved.items():
            setattr(vr, k, v)


AUTO = (None, None, None)
check("no API key -> upload order",
      run_selection(survey=lambda p, copy_guard=None: ([(vr.SPACE_UNKNOWN, None)] * len(p),
                                                       "no API key")) == AUTO)
check("the survey raising -> upload order",
      run_selection(survey=lambda p, copy_guard=None: (_ for _ in ()).throw(RuntimeError("timeout"))) == AUTO)
check("every photo unlabelled -> upload order (nothing to select on)",
      run_selection(survey=lambda p, copy_guard=None: ([(vr.SPACE_UNKNOWN, None)] * len(p), None)) == AUTO)


def fetch_fails(url, attempts=2, deadline=None):
    raise IOError("cdn down")


check("every photo unreadable -> upload order", run_selection(fetch=fetch_fails) == AUTO)


def hero_fails(url, attempts=2, deadline=None):
    if url == URLS[0]:
        raise IOError("gone")
    return photo()


check("the HERO unreadable -> upload order, never a film without the star photo",
      run_selection(fetch=hero_fails,
                    survey=lambda p, copy_guard=None: ([("living", None)] * len(p), None)) == AUTO)


def mostly_fail(url, attempts=2, deadline=None):
    if url in URLS[2:]:
        raise IOError("gone")
    return photo()


check("fewer than SURVEY_MIN_CANDIDATES readable -> upload order",
      run_selection(fetch=mostly_fail,
                    survey=lambda p, copy_guard=None: ([("living", None)] * len(p), None)) == AUTO)
check("a listing at or under MIN_VIDEO_PHOTOS never spends a vision call",
      run_selection(n=vr.MIN_VIDEO_PHOTOS) == AUTO)
check("...nor does a listing of one photo", run_selection(n=1) == AUTO)


def good_survey(photos, copy_guard=None):
    spaces = ["living", "living", "pool", "pool", "dining", "kitchen",
              "bedroom", "bedroom", "bathroom", "pool", "living", "garden"]
    return ([(spaces[i], "the %s ... where the day begins" % spaces[i])
             for i in range(len(photos))], None)


urls, captions, note = run_selection(survey=good_survey)
check("the happy path returns a varied set", urls is not None and len(urls) == 7, str(urls))
# good_survey labels the hero "living", so the hero opens -- because the LIVING ROOM
# opens, not because it is the hero. The run below, where the hero is a stairwell,
# is the one that tells those two apart.
check("...the living room opens the film (here, that is also the hero)",
      urls and urls[0] == URLS[0])


def hero_is_a_stairwell(photos, copy_guard=None):
    spaces = ["stairs", "bathroom", "living", "kitchen", "dining", "bedroom",
              "pool", "garden", "entrance", "view", "carpark", "terrace"]
    return ([(spaces[i], "the %s ... where the day begins" % spaces[i])
             for i in range(len(photos))], None)


urls, captions, _n = run_selection(survey=hero_is_a_stairwell)
check("a listing whose STAR photo is a stairwell opens on the living room instead",
      urls and urls[0] == URLS[2], str(urls))
check("...the star photo is still in the film, just not leading it",
      urls and URLS[0] in urls, str(urls))
check("...and its caption travelled with it, still keyed to its own url",
      captions.get(URLS[0], "").startswith("the stairs"), str(captions.get(URLS[0])))
check("...no space twice", len({u for u in urls}) == len(urls))
check("...captions keyed by url, one per chosen photo",
      set(captions) == set(urls), str(set(captions) ^ set(urls)))
check("...and no caption for a photo that was not chosen",
      all(u in urls for u in captions))
check("...and the survey's note comes back with the result, not dropped",
      note is None, str(note))


def noisy_survey(photos, copy_guard=None):
    out, _ = good_survey(photos)
    return out, "2 caption(s) rejected by the copy guards"


_u, _c, note = run_selection(survey=noisy_survey)
check("a note from the survey is carried out of _smart_auto_selection",
      note == "2 caption(s) rejected by the copy guards", str(note))


# ------------------------------------------------- precedence and caption keying
print("\n[precedence] the agent's explicit pick is never second-guessed")


CAPTION_PASS_CALLS = []


def install_stubs(mod, unreadable=(), survey=None):
    calls = []
    del CAPTION_PASS_CALLS[:]
    mod._fetch_listing_photo = lambda u: (_ for _ in ()).throw(IOError("x")) if u in unreadable else "P:" + u
    mod._fetch_image = lambda u, attempts=2, deadline=None: photo()
    def caption_pass(photos, copy_guard=None):
        # Recorded, because "did the caption pass run at all?" is the question behind
        # the one-model-call guarantee on the AUTO path.
        CAPTION_PASS_CALLS.append(len(photos))
        return ["generated(%s)" % p for p in photos], None
    mod._generate_room_captions = caption_pass
    mod._smart_auto_selection = survey or (lambda u, i, copy_guard=None: (None, None, None))
    mod._caption_font_size = lambda captions: 1
    def contain(p, clip_path, workdir, i, **kw):
        calls.append((p, kw.get("caption")))
        open(clip_path, "w").write("[%s]" % p)
    mod._contain_clip = contain
    def join(paths, out):
        open(out, "w").write("".join(open(x).read() for x in paths))
    mod._dissolve_segments = lambda paths, out, wd, t: (join(paths, out), 5.0)[1]
    mod._concat = lambda paths, out, wd, name, t: join(paths, out)
    mod._probe_duration = lambda path, est: est
    mod._render_contact_card = lambda *a, **k: "card"
    mod._build_card_clip = lambda b, c, out, wd: open(out, "w").write("<card>")
    mod._select_music_track = lambda seed, pt: (None, None)
    mod._run_ffmpeg = lambda cmd, t: open(cmd[-1], "w").write(
        "F:" + open(cmd[cmd.index("-i") + 1]).read())
    return calls


def render(unreadable=(), survey=None, **kw):
    saved = {k: getattr(vr, k) for k in
             ("_fetch_listing_photo", "_fetch_image", "_generate_room_captions",
              "_smart_auto_selection", "_caption_font_size", "_contain_clip",
              "_dissolve_segments", "_concat", "_probe_duration",
              "_render_contact_card", "_build_card_clip", "_select_music_track",
              "_run_ffmpeg")}
    calls = install_stubs(vr, unreadable, survey)
    try:
        out = vr.render_property_video(URLS, agent_name="A", agent_contact_line="c", **kw)
        return list(out[1]), dict(out[2]), calls
    finally:
        for k, v in saved.items():
            setattr(vr, k, v)


SMART = ([URLS[0], URLS[4], URLS[7]],
         {URLS[0]: "the living hall ... where laughter lingers",
          URLS[4]: "the kitchen ... where the day begins",
          URLS[7]: "the terrace ... where golden hours drift by"})
smart_called = []


def spy_survey(urls, photo_index, copy_guard=None):
    smart_called.append(urls)
    return (list(SMART[0]), dict(SMART[1]), None)


del smart_called[:]
_degs, caps, calls = render(survey=spy_survey, video_photos=[URLS[5], URLS[2]])
check("an explicit pick does not even consult smart selection", smart_called == [])
check("...and renders exactly the chosen photos, in order",
      [c[0] for c in calls] == ["P:" + URLS[5], "P:" + URLS[2]], str([c[0] for c in calls]))
check("...with captions from the normal caption pass",
      all(c[1] and c[1].startswith("generated") for c in calls), str(calls))

del smart_called[:]
_degs, caps, calls = render(survey=spy_survey)
check("AUTO does consult smart selection", len(smart_called) == 1)
check("...and renders the photos it chose",
      [c[0] for c in calls] == ["P:" + u for u in SMART[0]], str([c[0] for c in calls]))
check("...reusing the survey's captions instead of a second model call",
      [c[1] for c in calls] == [SMART[1][u] for u in SMART[0]], str([c[1] for c in calls]))
check("...and reports no degradation for having chosen well",
      [d for d in _degs if "music" not in d] == [], str(_degs))

print("\n[captions] stay on their own photo when one is dropped mid-render")
_degs, caps, calls = render(survey=spy_survey, unreadable=(SMART[0][1],))
rendered = [(c[0], c[1]) for c in calls]
check("the unreadable photo is gone", len(rendered) == 2, str(rendered))
check("the photo AFTER it keeps its own caption, not the dropped one's",
      rendered == [("P:" + SMART[0][0], SMART[1][SMART[0][0]]),
                   ("P:" + SMART[0][2], SMART[1][SMART[0][2]])], str(rendered))
check("the returned caption map names the photos actually used",
      set(caps) == {SMART[0][0], SMART[0][2]}, str(set(caps)))

print("\n[captions] a survey that loses captions says so, and does not re-call the model")
# F2/F3, found in audit. A survey can choose a good set of photos and still have most
# or all of its captions thrown out by the copy guards. Two things went wrong there:
# the note was dropped on the floor, so the film shipped with bare photos and an EMPTY
# degradation list (nothing to diagnose a complaint with), and an empty caption dict
# read as "no survey ran", sending the render into a SECOND model call -- inside the
# render slot and against the render budget, unlike the survey itself.
GUARD_NOTE = "2 caption(s) rejected by the copy guards"


def survey_mostly_rejected(urls, photo_index, copy_guard=None):
    """Chose three photos; the guards allowed only the first one's caption."""
    return list(SMART[0]), {SMART[0][0]: SMART[1][SMART[0][0]]}, GUARD_NOTE


def survey_all_rejected(urls, photo_index, copy_guard=None):
    return list(SMART[0]), {}, "every caption was rejected"


_degs, caps, calls = render(survey=survey_mostly_rejected)
check("a partly-rejected survey reports what was lost",
      any(GUARD_NOTE in d for d in _degs), str(_degs))
check("...the photo that kept its caption keeps it",
      calls[0][1] == SMART[1][SMART[0][0]], str(calls[0][1]))
check("...the others render bare rather than borrowing someone else's",
      [c[1] for c in calls[1:]] == [None, None], str([c[1] for c in calls]))
check("...and no second model call was made",
      CAPTION_PASS_CALLS == [], str(CAPTION_PASS_CALLS))

_degs, caps, calls = render(survey=survey_all_rejected)
check("a survey whose captions were ALL rejected makes no second model call",
      CAPTION_PASS_CALLS == [], "caption pass ran with %s" % CAPTION_PASS_CALLS)
check("...and still reports the loss to the agent",
      any("rejected" in d for d in _degs), str(_degs))
check("...and still renders the photos the survey chose",
      [c[0] for c in calls] == ["P:" + u for u in SMART[0]], str([c[0] for c in calls]))
check("...with every caption bare, not borrowed",
      all(c[1] is None for c in calls), str([c[1] for c in calls]))

_degs, caps, calls = render()          # no survey -> upload order
check("when the survey does NOT run, the caption pass still does",
      len(CAPTION_PASS_CALLS) == 1, str(CAPTION_PASS_CALLS))

_degs, caps, calls = render(survey=spy_survey)
check("a fully successful survey also makes exactly one model call",
      CAPTION_PASS_CALLS == [], str(CAPTION_PASS_CALLS))

print("\n[captions] an agent's edited caption still beats the survey's")
edited = "my own words ... exactly as i typed them"
_degs, caps, calls = render(survey=spy_survey, caption_overrides={SMART[0][1]: edited})
check("the override wins on its own photo",
      [c[1] for c in calls] == [SMART[1][SMART[0][0]], edited, SMART[1][SMART[0][2]]],
      str([c[1] for c in calls]))


print("\n[truncated films] what is reported must describe the film that exists")
# F7, found in audit. captions_used was computed over the PLANNED photos before the
# clip loop, so a render that ran out of budget -- or lost a clip to an encode failure
# -- still reported and STORED captions for photos not in the film. Those stored
# captions are what the caption editor shows the agent next time.


def render_slow(fail_on=None, budget=None, n=8, **kw):
    """A render where each clip encode takes real time, so the budget can run out."""
    saved = {k: getattr(vr, k) for k in
             ("_fetch_listing_photo", "_fetch_image", "_generate_room_captions",
              "_smart_auto_selection", "_caption_font_size", "_contain_clip",
              "_dissolve_segments", "_concat", "_probe_duration",
              "_render_contact_card", "_build_card_clip", "_select_music_track",
              "_run_ffmpeg", "RENDER_BUDGET_SECONDS")}
    urls = ["https://x/%d.jpg" % i for i in range(n)]
    calls = install_stubs(vr)
    plain = vr._contain_clip

    def slow_clip(photo, clip_path, workdir, i, **kwargs):
        if fail_on is not None and i == fail_on:
            raise RuntimeError("encode failed")
        time.sleep(0.05)
        calls.append((photo, kwargs.get("caption"), kwargs.get("last")))
        open(clip_path, "w").write("[%s]" % photo)
    vr._contain_clip = slow_clip
    vr._generate_room_captions = lambda photos, copy_guard=None: (
        ["cap-%d" % i for i in range(len(photos))], None)
    if budget is not None:
        vr.RENDER_BUDGET_SECONDS = budget
    try:
        out = vr.render_property_video(urls, agent_name="A", agent_contact_line="c", **kw)
        return list(out[1]), dict(out[2]), calls, urls
    finally:
        for k, v in saved.items():
            setattr(vr, k, v)
        vr._contain_clip = plain


import time  # noqa: E402

degs, caps, calls, urls = render_slow(budget=0.12, n=8)
shots = [c for c in calls if len(c) == 3]
check("the budget really did cut the film short", len(shots) < 8, "%d shots" % len(shots))
check("captions are reported only for the photos in the film",
      len(caps) == len(shots), "%d captions for %d shots" % (len(caps), len(shots)))
check("...and they are the FIRST photos, the ones that rendered",
      set(caps) == set(urls[:len(shots)]), str(sorted(caps)))
check("the agent is told the film was cut short",
      any("took too long" in d for d in degs), str(degs))
check("the final shot of a truncated film is built as an ending, so its caption "
      "does not vanish early", shots[-1][2] is True, str([c[2] for c in shots]))
check("...and only the final one is", [c[2] for c in shots[:-1]] == [False] * (len(shots) - 1),
      str([c[2] for c in shots]))

degs, caps, calls, urls = render_slow(fail_on=3, n=6)
shots = [c for c in calls if len(c) == 3]
check("a clip that fails to encode drops out of the film", len(shots) == 5, str(len(shots)))
check("...and out of the reported captions too",
      urls[3] not in caps and len(caps) == 5, str(sorted(caps)))
check("...while every other photo keeps its own caption",
      set(caps) == set(urls[:3]) | set(urls[4:6]), str(sorted(caps)))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
