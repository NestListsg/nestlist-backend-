# Classic Video Music Library — Spec

Owner: content-studio (mood palette, naming, licensing position, selection rule).
Implementer: backend-engineer (the selection function in `video_renderer.py` /
`main.py` — this file specifies behaviour, it contains no code changes).

## 1. The problem this fixes

`video_renderer.py` currently hardcodes one track for every Classic video, every
agent, every listing:

```
AUDIO_PATH = os.path.join(_HERE, "audio", "soft_piano.mp3")
```

At one agent this is invisible. At fifty agents it is the first thing a buyer
notices scrolling a feed: every NestList video in Singapore sounds identical.
That reads as mass-produced — the exact impression Prestige exists to avoid.
Jane's own words: *"a little boring if always playing the same music for
everyone and video."*

## 2. Track count: 6

Argued, not picked:

- **Singapore's landed property_type enum has a natural two-way split.**
  The actual values used across the product (`NewListing.js`, the vision
  prompt in `main.py`) are: *Good Class Bungalow (GCB), Detached/Bungalow,
  Semi-Detached, Inter-Terrace, Corner Terrace, Penthouse*. GCB /
  Detached-Bungalow / Penthouse read as one register (grand, spacious,
  aspirational); Semi-Detached / Inter-Terrace / Corner Terrace read as
  another (warm, domestic, still Prestige but not grandiose). Forcing one
  mood palette across both ends up either too grand for a terrace or too
  modest for a GCB — six tracks is the smallest number that lets the music
  match the property without a manual step (see §4).
- **Three tracks per register is enough to break the "I've heard this
  before" moment.** An agent with a handful of live landed listings at a
  time won't cycle through three options fast enough for a buyer to notice
  the repeat; six across the whole library means even an agent's full
  portfolio feed rarely plays the same bed twice in a row.
- **Six stays curatable and licensable.** Each track needs its own
  individually-verified commercial licence and its own line in
  `LICENSE-music.txt` (see §5) — that is real, non-automatable diligence
  work per track. Twelve or more turns this into an ongoing research
  project and risks padding the library with mediocre filler just to hit a
  number; six is what a careful pass can actually vet properly, and matches
  a boutique, curated feel rather than a stock-library one.

## 3. The mood palette

All six share one sonic identity: **piano-led, mostly solo or piano-plus-one,
no percussion, no synth pads that read as corporate, no "real estate stock
music" swell-and-release arc.** Restrained, warm, unhurried — the brand
brief, not just this feature's brief.

### Tier A — "Grand Prestige" (GCB, Detached/Bungalow, Penthouse)

More spacious and cinematic, but still never bombastic — think a large,
quiet room, not a trailer.

| Slot | Evokes | Instrumentation | Best suited to |
|---|---|---|---|
| **Long Driveway** | Arrival, gravitas, old-money restraint | Solo cello, low register, over sparse piano chords | GCB, Detached/Bungalow |
| **Manor Light** | Early morning light through tall windows, lots of space between notes | Solo piano, very sparse, distant string pad underneath | GCB, Detached/Bungalow |
| **Quiet Altitude** | Height, calm confidence, a view rather than a room | Piano + soft strings, a gentle rise-and-settle arc, still no percussion | Penthouse |

### Tier B — "Warm Home" (Semi-Detached, Inter-Terrace, Corner Terrace)

More intimate and melodic, family-warm without turning sentimental.

| Slot | Evokes | Instrumentation | Best suited to |
|---|---|---|---|
| **Front Porch** | Welcoming, late-afternoon light, a lived-in family home | Warm solo piano, a simple repeating motif | Semi-Detached, Inter-Terrace, Corner Terrace (default) |
| **Corner Window** | Character, a bit of personality, slightly brighter | Piano + a single soft acoustic guitar or warm pad, unhurried tempo, major key | Corner Terrace, Semi-Detached |
| **Garden Study** | Reflective, a quiet view out to a garden | Piano with a very light ambient texture, no drums, slower than Front Porch | Inter-Terrace, Semi-Detached |

**Reuse note:** the current `audio/soft_piano.mp3` ("Piano Soft Gentle
Morning Keys") already fits **Front Porch** exactly and is already licensed
and vetted (see §5). Keep the file and its filename as-is and assign it that
role — no reason to re-source or re-verify a track that already works and is
already documented.

## 4. Selection rule

**Recommendation: deterministic by listing id, scoped to the property-type
tier.** Not property-type-matching alone, not agent-picks.

### Why not agent-picks

Fails the autonomy test outright — it's a new decision on every listing,
for every agent, forever, and it doesn't scale to fifty agents cleanly:
most won't bother and will leave whatever the default is, silently
recreating the exact "everyone sounds the same" problem this spec exists to
fix. Zero of this should be a manual step.

### Why not property-type-matching alone

This looks right at first but reintroduces the same boredom bug **within
one agent's own portfolio**: most agents' landed listings cluster in one or
two types (a lot of Semi-Detached and Inter-Terrace in the SG landed
market). Pure type-matching means every Semi-Detached listing that agent
ever posts gets the *same* track — the identical failure mode Jane flagged,
just narrowed to one agent's feed instead of the whole platform.

### The rule

1. Bucket the listing into a tier from its `property_type`:
   - GCB, Detached/Bungalow, Penthouse → **Tier A**
   - Semi-Detached, Inter-Terrace, Corner Terrace → **Tier B**
   - Blank or anything unrecognized → **Tier B** (the safe default — never
     assign the grander tier to a property whose type hasn't been
     confirmed; per the existing vision-prompt policy, a blank
     `property_type` is an expected, non-rare state — agents are
     deliberately told to leave it blank rather than guess — so this isn't
     a rare edge case to shrug off).
2. Within that tier, pick the track by a **stable hash of the listing id**
   (e.g. `sha256(listing_id) % 3`) — not a property-type hash, not
   randomness at render time.

This means: two neighbouring listings of the same type almost always get
different tracks (id-based), but a GCB never gets a "Front Porch"-register
track and a corner terrace never gets "Long Driveway" (tier-scoped).

### Stability constraint (must hold)

Re-rendering the same listing with the same data must produce the same
track, every time — an agent who re-renders shouldn't get a different film.
A pure function of `(listing_id, property_type)`, recomputed fresh at
render time with no new database column, satisfies this automatically and
needs zero storage or migration.

**One implementation trap to flag explicitly:** the hash must be a fixed,
cross-run deterministic hash (e.g. Python's `hashlib.sha256`/`md5`, or
`zlib.crc32`) — **not** Python's built-in `hash()`, which is randomized
per-process (`PYTHONHASHSEED`) and would silently violate the "same listing,
same track" guarantee across app restarts or redeploys. This is exactly the
kind of thing that looks fine in one test run and breaks in production.

**One acceptable edge case:** if an agent later corrects `property_type`
(e.g. blank → "Semi-Detached", or a Corner/Inter-Terrace correction) and
regenerates the video, the tier — and so possibly the track — can change.
That's fine: the render was already going to change because the underlying
listing data changed. This is not a violation of the stability constraint,
which only covers identical data.

## 5. Practical specs per track

- **Target length: 60–90 seconds.** The renderer loops the track
  (`-stream_loop -1`) to cover whatever the video's actual length turns out
  to be, and Classic videos vary with photo count — roughly 15–60s across
  the observed range (5s/photo, up to the 15-photo cap, minus crossfade
  overlap). A 60–90s track means the overwhelming majority of renders play
  it once, straight through, with no loop seam at all; only an unusually
  long video would ever reach the loop point.
- **No dynamic arc — stay flat.** Because the fade-out is a fixed 4.5
  seconds counted back from *wherever the video happens to end*, and photo
  count (and so video length) varies, the fade-out can land at any point in
  the track. A track with a clear intro → build → climax → outro shape will
  sometimes get cut off mid-swell, which sounds like a mistake. Pick or
  commission tracks that sit at one steady, gently-breathing dynamic level
  throughout — no big swells to interrupt.
- **No slow intro.** The renderer's own fade-in is only 1.5 seconds
  (`AUDIO_FADE_IN_SECONDS`), so the track needs audible, characterful
  material from 0:00. A track with a 15–20 second ambient build before the
  "real" material starts is unusable here — most of a short Classic video
  would play out entirely inside that build.
- **No drums/percussion, no vocal, no obvious loop click** at the seam
  (matters less at 60–90s length, but still worth checking before final
  selection, since ffmpeg's `-stream_loop` does a hard cut, not a
  crossfade).

## 6. Sourcing and licensing — the part not to skip

**Recommendation: stay on Pixabay for this library. Do not move to Artlist
or Suno for this batch.**

- **Artlist is ruled out for this use as things stand.** Jane's records
  note the personal plan does not cover platform use — this product is not
  "Jane posts a video," it's a SaaS product generating videos on behalf of
  paying third-party agents who then post them commercially under their own
  names. That needs a business/sync licence covering redistribution by
  unrelated third parties, which is what Artlist's *business or API* tier
  is for, not the personal plan. Do not source from Artlist until that
  tier is actually purchased and confirmed to cover this exact use — treat
  the personal-plan catalog as off-limits regardless of how it looks in the
  UI.
- **Suno stays parked**, per Jane's existing note, pending a licensed
  model — no change to that position from this spec.
- **Pixabay is the safe answer, but per-track, not blanket.** The existing
  `audio/LICENSE-music.txt` documents exactly the standard this project
  needs: Pixabay's Content License, in the version recorded there, permits
  free commercial use with no attribution required, explicitly covering
  "videos NestList produces for paying agents, and in videos those agents
  post to Facebook, Instagram, TikTok, LinkedIn, YouTube or a property
  portal" — which is precisely the third-party-commercial-redistribution
  case that has caught this project out before (Artlist). That is the bar
  every new track must clear, confirmed individually:
  1. Check the **licence terms on that specific track's Pixabay page** at
     the time of download — don't assume the site-wide default covers
     every track without looking, since individual tracks can carry extra
     usage notes.
  2. Confirm it's commercial use, no attribution required, no restriction
     against use in a product whose output third parties then post
     commercially.
  3. Record it exactly the way `soft_piano.mp3` is recorded today: track
     name, artist, Pixabay track id/URL, plain-English summary of what's
     permitted and what isn't, and the file's MD5 — one entry per track in
     an updated `LICENSE-music.txt`.
  4. If a track's page shows anything narrower than that (e.g. editorial-use
     only, an attribution requirement, a platform restriction), reject it
     and pick another — don't rely on the general Pixabay Content License
     page overriding a specific track's own notice.
- **What I have not done:** sourced or downloaded any actual audio files.
  That's deliberately left to Jane/whoever acquires the tracks, per the
  instruction not to acquire anything before she's seen the recommendation.
  What I've specified above (mood, instrumentation, length, dynamic shape)
  should be enough to search Pixabay's catalog directly against each named
  slot.

**Before this ships, Jane should verify:** (a) Pixabay's Content License
terms haven't changed since the `soft_piano.mp3` entry was written — the
plain-English summary in that file should be re-checked against Pixabay's
current license page, not assumed still accurate; (b) each of the five new
tracks gets its own individually-checked license entry, not a blanket "same
as track 1" assumption; (c) no track is acquired from Artlist's personal
plan under the assumption that "it's just background music" — that's the
exact mistake the existing note warns against.

## 7. Naming

Filenames follow the existing convention (`snake_case`, `audio/<name>.mp3`)
so they work as both a filename and, if a picker UI ever exists, a
display-ready label (title-case the words).

| Slot (display name) | Filename | Tier |
|---|---|---|
| Long Driveway | `audio/long_driveway.mp3` | A — Grand Prestige |
| Manor Light | `audio/manor_light.mp3` | A — Grand Prestige |
| Quiet Altitude | `audio/quiet_altitude.mp3` | A — Grand Prestige |
| Front Porch | `audio/soft_piano.mp3` *(existing file, kept as-is)* | B — Warm Home |
| Corner Window | `audio/corner_window.mp3` | B — Warm Home |
| Garden Study | `audio/garden_study.mp3` | B — Warm Home |

## 8. Handoff to backend-engineer

Everything above is spec, not code — the following is what backend-engineer
needs to implement against:

- A selection function taking `(listing_id: str, property_type: str) -> str`
  (path to an audio file), replacing the single hardcoded `AUDIO_PATH`.
- Tier lookup table from §4 step 1 (case-insensitive match against the six
  known `property_type` strings; anything else, including `""`, falls to
  Tier B).
- Deterministic hash from §4 step 2 — explicitly not Python's built-in
  `hash()`.
- If the selection function fails for any reason (missing file, bad
  listing id, whatever), fall back to the current default track
  (`soft_piano.mp3` / Front Porch) rather than failing the render — this
  matches the file's existing degradation philosophy ("music fails →
  silent video"; here, "selection fails → known-good default" one step
  before that).
- No new database column needed — this is a pure function computed fresh
  at render time.
