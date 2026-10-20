import asyncio
import json
import os
import re
import httpx
from datetime import datetime, date, timezone, timedelta

TOKEN_URL = "https://eservice.ura.gov.sg/uraDataService/insertNewToken/v1"
TRANSACTION_URL = "https://eservice.ura.gov.sg/uraDataService/invokeUraDS/v1"
SQM_TO_SQFT = 10.7639

# URA's edge WAF serves a JS challenge page (not JSON) to non-browser User-Agents
# like httpx's default -- this isn't flakiness, every httpx request was being
# blocked. A browser-like UA gets a normal JSON response every time.
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}

# The 39 URA-gazetted Good Class Bungalow Areas mapped to their CONSTITUENT
# STREET NAMES (not just the headline area name). This fixes the systematic
# under-count where a sale on a street *inside* a GCBA whose name differs from
# the area name -- e.g. "Coronation Road West" inside Queen Astrid Park, or
# "Andrew Road" inside Caldecott Hill Estate -- was silently missed.
#
# ACCURACY POSTURE (a false positive misrepresents the market on a trust-
# branded panel, so it is worse than a small under-count):
#   * Matching is EXACT on the full street name (normalised), never substring,
#     so "Belmont Road" can't be matched by an unrelated longer street.
#   * A district guard (GCB_DISTRICTS below) additionally requires the txn to
#     be in one of the five districts that contain any GCBA, which neutralises
#     any same-name street elsewhere in Singapore.
#   * The Detached + Land type filter (applied by the callers) already excludes
#     every strata unit (condos/apartments), so an arterial road only risks
#     adding a *detached-on-land* house sitting just outside the gazetted line.
#   * Even so, arterial / mixed roads that plausibly carry non-GCBA landed
#     stock are DELIBERATELY EXCLUDED pending authoritative confirmation
#     (see _GCB_STREETS_EXCLUDED_PENDING_CONFIRMATION) -- they under-count
#     rather than risk a false positive.
#
# Source for constituent streets: goodclassbungalows.com.sg area maps, cross-
# checked against URA's 39-area list (area names match exactly) and against the
# actual URA transaction "street" universe. Confidence is HIGH for streets that
# also appear (correctly) in the live URA data; streets with no recent
# transactions are best-effort and simply won't match until one occurs.
GCBA_STREETS = {
    # ---- District 10 ----
    "Belmont Park": ["Belmont Road", "Morley Road"],
    "Bin Tong Park": ["Bin Tong Park", "Rebecca Road"],
    "Binjai Park": ["Binjai Hill", "Binjai Park", "Binjai Rise", "Binjai Walk"],
    "Brizay Park": ["Brizay Park", "Wilby Road"],  # Holland Road excluded (arterial)
    "Bukit Sedap": ["Bukit Sedap Road"],
    "Chatsworth Park": ["Bishopsgate", "Cable Road", "Chatsworth Avenue",
                        "Chatsworth Park", "Chatsworth Road", "Mount Echo Park",
                        "Rochalie Drive"],
    "Cluny Hill": ["Cluny Hill", "Cluny Park", "Lermit Road"],
    "Cornwall Gardens": ["Belmont Road", "Cornwall Gardens", "Leedon Road"],
    "Dalvey Estate": ["Dalvey Estate", "Dalvey Road", "Lewis Road"],
    "Ewart Park": ["Ewart Park"],
    "First/Third Avenue": ["First Avenue", "Third Avenue", "Namly Avenue",
                           "Namly Close", "Namly Hill"],
    "Ford Avenue": ["Ford Avenue"],
    "Fourth/Sixth Avenue": ["Fourth Avenue", "Fifth Avenue", "Sixth Avenue"],
    "Gallop Road/Woollerton Park": ["Gallop Road", "Gallop Park Road",
                                    "Woollerton Drive", "Woollerton Park"],
    "Garlick Avenue": ["Garlick Avenue", "Old Holland Road"],
    "Holland Park": ["Holland Park"],  # Holland Road excluded (arterial)
    "Holland Rise": ["East Sussex Lane", "Holland Rise"],
    "Leedon Park": ["Leedon Park", "Leedon Road"],
    "Maryland Estate": ["Maryland Drive"],
    "Nassim Road": ["Nassim Road", "Ladyhill Road"],  # Fernhill Road excluded (mixed/arterial)
    "Oei Tiong Ham Park": ["Oei Tiong Ham Park", "Jalan Harum", "Jalan Pelangi",
                           "Jalan Sampurna"],
    "Queen Astrid Park": ["Queen Astrid Park", "Queen Astrid Gardens",
                          "Astrid Hill", "Coronation Road West"],
    "Rebecca Park": ["Rebecca Road"],
    "Ridley Park": ["Pierce Road", "Ridley Park", "Tanglin Hill"],
    "Ridout Park": ["Peel Road", "Ridout Road", "Swettenham Road"],
    "Victoria Park": ["Kingsmead Road", "Victoria Park", "Victoria Park Close"],
    "White House Park": ["Dalvey Road", "Margoliouth Road", "White House Park"],
    # ---- District 11 ----
    "Bukit Tunggal": ["Bukit Tunggal Road"],
    "Caldecott Hill Estate": ["Andrew Road", "Jalan Piala", "John Road",
                              "Olive Road"],  # Lornie Road excluded (arterial)
    "Camden Park": ["Camden Park"],
    "Chee Hoon Avenue": ["Chee Hoon Avenue", "Dunearn Close", "Jalan Asuhan",
                         "Ross Avenue", "University Road"],
    "Eng Neo Avenue": ["Eng Neo Avenue"],
    "Raffles Park": ["Ash Grove", "Cassia Drive", "Linden Drive",
                     "Oriole Crescent", "Pinewalk", "Sunset Avenue"],
    "Swiss Club Road": ["Ascot Rise", "Jalan Kampong Chantek", "Jalan Senandong",
                        "Swiss Club Avenue", "Swiss Club Road"],
    # ---- District 20 ----
    "Windsor Park": ["Windsor Park Road"],
    # ---- District 21 ----
    "Kilburn Estate": ["Denham Close", "Wilmonar Avenue", "Yarwood Avenue"],
    "King Albert Park": ["King Albert Park"],
    # ---- District 23 ----
    "Chestnut Avenue": ["Chestnut Avenue", "Chestnut Close", "Chestnut Crescent",
                        "Chestnut Drive"],
}

# Streets that a source lists under a GCBA but which are arterial / mixed roads
# with non-GCBA landed stock -- excluded to avoid false positives. Revisit if an
# authoritative URA boundary confirms the detached-on-land plots on them are all
# within the gazette. (Recorded so the exclusion is explicit, not accidental.)
_GCB_STREETS_EXCLUDED_PENDING_CONFIRMATION = [
    "Holland Road",   # major arterial, many non-GCB frontages
    "Fernhill Road",  # near Orchard; mixed GCB / non-GCB
    "Lornie Road",    # arterial
]

# The five postal districts that contain any gazetted GCBA. Used as a guard so a
# same-named street in another district can never be counted as GCB.
GCB_DISTRICTS = {"10", "11", "20", "21", "23"}


def _norm_street(s: str) -> str:
    return " ".join((s or "").upper().split())


def _norm_ura_type(value) -> str:
    """URA propertyType compared without case, spaces or hyphens: "Semi-detached",
    "Semi-Detached", "SEMI DETACHED" and "Semidetached" are all "semidetached". URA's own
    spelling has been seen in more than one form, and an exact string compare silently
    returned zero comparables when it differed from ours. Distinct types stay distinct:
    "Detached" -> "detached", "Strata Detached" -> "stratadetached"."""
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


_DETACHED = _norm_ura_type("Detached")

# {normalised street name -> GCBA name}. Exact-match lookup.
_GCB_STREET_TO_AREA = {}
for _area, _streets in GCBA_STREETS.items():
    for _s in _streets:
        _GCB_STREET_TO_AREA.setdefault(_norm_street(_s), _area)

NASSIM_ROAD_TOKEN = "nassim road"

_token_cache = {"token": None, "fetched_on": None}


async def _get_json_with_retry(client, url, *, attempts=3, backoff=2, **kwargs):
    """URA's API intermittently returns an empty 200 body instead of JSON
    (observed in production, not tied to a specific request shape) -- retry
    a few times with a short backoff before giving up. Also decodes leniently:
    some batches contain a stray invalid UTF-8 byte inside an unrelated field
    (e.g. a mangled project name) that would otherwise fail the whole batch."""
    last_error = None
    for attempt in range(attempts):
        resp = await client.get(url, **kwargs)
        resp.raise_for_status()
        try:
            return json.loads(resp.content.decode("utf-8", errors="replace"))
        except ValueError as e:
            last_error = e
            if attempt < attempts - 1:
                await asyncio.sleep(backoff * (attempt + 1))
    raise RuntimeError(f"URA API returned a non-JSON response after {attempts} attempts: {last_error}")


# Singapore time. URA's token day follows the Singapore calendar day, and Railway's
# clock is UTC -- keyed on the UTC date, the cached token would be reused for the first
# eight hours of each Singapore day after URA had already expired it.
SGT = timezone(timedelta(hours=8))


def _sgt_today() -> date:
    return datetime.now(SGT).date()


async def get_token(access_key: str) -> str:
    """URA tokens are valid for the (Singapore) calendar day they were issued. Cache and
    only refetch once the Singapore date rolls over."""
    today = _sgt_today()
    if _token_cache["token"] and _token_cache["fetched_on"] == today:
        return _token_cache["token"]

    async with httpx.AsyncClient(timeout=30) as client:
        data = await _get_json_with_retry(
            client, TOKEN_URL, headers={**BROWSER_HEADERS, "AccessKey": access_key}
        )
        if data.get("Status") != "Success":
            raise RuntimeError(f"URA token request failed: {data.get('Message')}")
        token = data["Result"]
        _token_cache["token"] = token
        _token_cache["fetched_on"] = today
        return token


async def fetch_all_transactions(access_key: str, token: str, batch_timeout: float = None,
                                 on_batch=None):
    """Fetch all 4 batches of PMI_Resi_Transaction (split by postal district
    ranges). GCB areas live in specific districts (D10/D11/D21/D23 etc.) that
    fall in specific batches, so a single dropped batch silently removes whole
    GCB areas and produces an under-count that still *looks* like a valid,
    smaller market.

    Every batch is therefore attempted independently -- one failing batch no
    longer aborts the whole fetch, and instead of vanishing silently each
    batch's outcome is recorded. The caller uses the report to reject a
    partial fetch rather than persist an under-count as if it were complete.

    Returns (all_projects, batch_report), where batch_report is a list of
    {"batch", "ok", "status", "projects", "message"} -- one entry per batch.

    batch_timeout (seconds, optional): a hard ceiling on ONE batch, retries included. A
    batch that runs past it is recorded as failed and the next batch still runs, so the
    batches that did finish are kept. None (the Market Pulse refresh) = no extra ceiling,
    exactly the original behaviour.

    on_batch (optional): called with each successful batch's projects (in a worker
    thread) INSTEAD of collecting them -- the caller turns each batch into its compact form
    and the parsed JSON is freed before the next batch arrives, so peak memory is about
    one batch, not four. The returned project list is then empty."""
    headers = {**BROWSER_HEADERS, "AccessKey": access_key, "Token": token}
    all_projects = []
    batch_report = []
    async with httpx.AsyncClient(timeout=60) as client:
        for batch in (1, 2, 3, 4):
            try:
                call = _get_json_with_retry(
                    client, TRANSACTION_URL,
                    params={"service": "PMI_Resi_Transaction", "batch": batch},
                    headers=headers,
                )
                data = await (asyncio.wait_for(call, timeout=batch_timeout) if batch_timeout else call)
                status = data.get("Status")
                if status == "Success":
                    projects = data.get("Result", []) or []
                    if on_batch is not None:
                        await asyncio.to_thread(on_batch, projects)
                    else:
                        all_projects.extend(projects)
                    batch_report.append({"batch": batch, "ok": True, "status": status,
                                         "projects": len(projects), "message": None})
                    del data, projects
                else:
                    # Valid JSON but not a success (rate-limit, token expiry mid-run,
                    # a URA-side error). Previously this was printed and skipped, which
                    # is exactly how a partial fetch got saved as if it were complete.
                    msg = data.get("Message")
                    print(f"URA batch {batch} did not return Success: {msg}")
                    batch_report.append({"batch": batch, "ok": False, "status": status or "Unknown",
                                         "projects": 0, "message": msg})
            except Exception as e:
                # A raising batch (empty body after retries, HTTP error, timeout) must
                # not abort the other three -- record it and carry on so batch_report
                # reflects exactly which districts we actually received.
                print(f"URA batch {batch} failed: {type(e).__name__}: {e}")
                batch_report.append({"batch": batch, "ok": False, "status": "Exception",
                                     "projects": 0, "message": (str(e) or type(e).__name__)[:300]})
    return all_projects, batch_report


def _gcb_area_for(street: str, district: str = "") -> str:
    """Return the gazetted GCBA name a transaction belongs to, or "" if none.
    Exact (normalised) street match AND, when a district is provided, the txn
    must be in one of the five GCBA districts -- so a same-named street in
    another district is never miscounted."""
    area = _GCB_STREET_TO_AREA.get(_norm_street(street), "")
    if not area:
        return ""
    d = (district or "").strip().lstrip("D").lstrip("d").zfill(2) if district else ""
    if d and d not in GCB_DISTRICTS:
        return ""
    return area

def _street_in_gcb_whitelist(street: str) -> bool:
    """Street-only membership (ignores district) -- for diagnostics display."""
    return _norm_street(street) in _GCB_STREET_TO_AREA


def survey_detached_land_streets(projects: list, window_months: int = 24) -> list:
    """Diagnostics-only: the UNIVERSE of distinct street names that carry a
    Detached + Land transaction in the trailing window, with counts and whether
    the current GCB token list matches them. This is how we broaden the filter
    empirically -- rather than guessing street lists blind, we look at exactly
    which detached-land streets URA actually reports and whitelist the ones
    confirmed to sit inside a gazetted GCBA (a street here is a GCB *candidate*,
    not proof -- detached houses exist outside GCBAs too)."""
    now = datetime.utcnow()

    def within_window(year, month):
        months_ago = (now.year - year) * 12 + (now.month - month)
        return 0 <= months_ago < window_months

    agg = {}
    for project in projects:
        street = (project.get("street", "") or "").strip()
        for txn in project.get("transaction", []):
            if _norm_ura_type(txn.get("propertyType")) != _DETACHED:
                continue
            if txn.get("typeOfArea") != "Land":
                continue
            parsed = _parse_contract_date(txn.get("contractDate", ""))
            if not parsed or not within_window(*parsed):
                continue
            row = agg.setdefault(street, {
                "street": street, "count": 0,
                "district": txn.get("district", ""),
                "gcb_match": _street_in_gcb_whitelist(street),
                "gcb_area": _GCB_STREET_TO_AREA.get(_norm_street(street), ""),
            })
            row["count"] += 1
    return sorted(agg.values(), key=lambda r: (not r["gcb_match"], -r["count"], r["street"]))


def _parse_contract_date(contract_date: str):
    """'mmyy' -> (year, month), e.g. '0715' -> (2015, 7)."""
    try:
        mm = int(contract_date[:2])
        yy = int(contract_date[2:])
        return (2000 + yy, mm)
    except (ValueError, TypeError):
        return None


def _format_sgd(value: float) -> str:
    if value >= 1_000_000_000:
        return f"SGD {value / 1_000_000_000:.2f}B"
    if value >= 1_000_000:
        return f"SGD {value / 1_000_000:.0f}M"
    return f"SGD {value:,.0f}"


def _extract_gcb_transactions(projects: list, window_months: int = 12) -> list:
    """Flatten project->transaction records into individual GCB sale
    records within the trailing window, each carrying its own street/psf."""
    now = datetime.utcnow()

    def within_window(year, month):
        months_ago = (now.year - year) * 12 + (now.month - month)
        return 0 <= months_ago < window_months

    records = []
    seen = set()  # de-dupe identical URA records (same sale reported twice)
    for project in projects:
        street = project.get("street", "")
        # Cheap street-level prefilter; the authoritative check (with the
        # district guard) happens per-transaction below, since district lives
        # on the transaction, not the project.
        if not _street_in_gcb_whitelist(street):
            continue
        for txn in project.get("transaction", []):
            if _norm_ura_type(txn.get("propertyType")) != _DETACHED:
                continue
            if txn.get("typeOfArea") != "Land":
                continue
            area = _gcb_area_for(street, txn.get("district", ""))
            if not area:  # street matched but wrong district -> not this GCBA
                continue
            parsed = _parse_contract_date(txn.get("contractDate", ""))
            if not parsed or not within_window(*parsed):
                continue
            try:
                price = float(txn.get("price", 0))
                area_sqm = float(txn.get("area", 0))
            except (TypeError, ValueError):
                continue
            if price <= 0 or area_sqm <= 0:
                continue
            # URA sometimes carries the same sale twice (observed: identical
            # street+date+price+area pairs). A full-signature key drops exact
            # duplicates without merging genuinely distinct sales.
            sig = (_norm_street(street), txn.get("contractDate", ""), price, round(area_sqm, 2))
            if sig in seen:
                continue
            seen.add(sig)
            psf = price / (area_sqm * SQM_TO_SQFT)
            records.append({"street": street, "gcb_area": area, "price": price, "psf": psf})
    return records


def compute_market_pulse_stats(projects: list) -> dict:
    records = _extract_gcb_transactions(projects, window_months=12)

    if not records:
        return None  # let the caller decide whether to keep prior values

    total_value = sum(r["price"] for r in records)
    avg_psf = sum(r["psf"] for r in records) / len(records)
    largest = max(records, key=lambda r: r["price"])

    nassim_records = [r for r in records if NASSIM_ROAD_TOKEN in r["street"].lower()]
    if nassim_records:
        psf_values = [r["psf"] for r in nassim_records]
        nassim_range = f"SGD {min(psf_values):,.0f}-{max(psf_values):,.0f} psf"
    else:
        nassim_range = "No transactions in the past 12 months"

    return {
        "gcb_transactions": f"{len(records)} units",
        "gcb_total_value": _format_sgd(total_value),
        "gcb_avg_psf": _format_sgd(avg_psf),
        "gcb_largest": _format_sgd(largest["price"]),
        "nassim_range": nassim_range,
        "last_updated": date.today().strftime("%b %Y"),
        # Day-precision UTC timestamp of THIS successful computation. `last_updated`
        # is month-only and so can't tell a fresh pull apart from a weeks-old snapshot
        # within the same month -- `refreshed_at` is what the UI should trust for
        # freshness ("as of 18 Sep 2026", stale-after-N-days badge).
        "refreshed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "ura_api",
    }


# Maps NestList's own property_type labels to URA's PMI_Resi_Transaction
# "propertyType" values, loosely (substring match), since the exact URA
# vocabulary isn't publicly documented in detail and this only needs to
# narrow comparables, not exactly reproduce URA's internal taxonomy.
#
# Matched on the label with spaces/hyphens removed, and SEMI-D IS CHECKED FIRST: the old
# order tested "detached" before "semi-detached", and "semi-detached" contains
# "detached", so every Semi-Detached report was quietly built from Detached sales.
# The URA side is compared with _norm_ura_type, so "Semi-detached" / "Semi-Detached"
# both match whatever spelling URA uses.
_PROPERTY_TYPE_MAP = [
    ("semidetached", "Semi-Detached"),
    ("semid", "Semi-Detached"),
    ("goodclassbungalow", "Detached"),
    ("detached", "Detached"),
    ("bungalow", "Detached"),
    ("terrace", "Terrace"),
]

def _map_property_type(nestlist_type: str) -> str:
    key = re.sub(r"[^a-z]+", "", str(nestlist_type or "").lower())
    if not key:
        return ""
    for token, ura_type in _PROPERTY_TYPE_MAP:
        if token in key:
            return ura_type
    return ""


def _contract_date_key(display_date: str) -> tuple:
    """Sort key for a record's "MM/YYYY" contract_date: (year, month). Sorting the text
    itself ranks "12/2025" above "01/2026", so the newest-20 cut dropped the latest sales
    whenever the window crossed a new year. Unparseable -> sorts last."""
    try:
        mm, yyyy = str(display_date).split("/", 1)
        return (int(yyyy), int(mm))
    except (TypeError, ValueError):
        return (0, 0)


# ---------------------------------------------------------------------------
# NestList Home, Phase 1 -- CONDO (strata) comparables.
#
# The same PMI_Resi_Transaction feed already carries every private condo, EC and
# apartment sale; the landed path above throws them away with its
# typeOfArea == "Land" filter. Strata support is a SEPARATE path, chosen only when the
# NestList type is one of the condo family, so the landed path (and its output) is
# untouched: an unknown, blank or landed type never reaches any code below.
#
# Ordered: EC is checked BEFORE the generic "condo" token, because "Executive
# Condominium" contains "condominium". Condominium and Apartment are pooled: URA's split
# between the two is about the size of the development and its facilities, not a
# different market, and agents use "condo" for both -- keeping them apart would starve a
# street with one of each of comparables. EC is kept on its own: it is a subsidised,
# resale-restricted product that trades at a clearly lower PSF.
# ---------------------------------------------------------------------------
_STRATA_TYPE_MAP = [
    ("executive condominium", ("Executive Condominium",)),
    ("(ec)", ("Executive Condominium",)),
    ("condo", ("Condominium", "Apartment")),
    ("apartment", ("Apartment", "Condominium")),
    ("walk-up", ("Apartment", "Condominium")),
    ("walkup", ("Apartment", "Condominium")),
]
_STRATA_EXACT_TOKENS = {"ec": ("Executive Condominium",)}

# URA typeOfSale codes -> words an agent reads. Anything else -> "" (never a guess).
_SALE_TYPE_LABELS = {"1": "New Sale", "2": "Sub Sale", "3": "Resale"}


def strata_ura_types(nestlist_type: str) -> tuple:
    """URA propertyType values that are valid comparables for a condo-family NestList
    type, or () when the type is not condo-family (=> the landed path). Never raises."""
    key = str(nestlist_type or "").strip().lower()
    if not key:
        return ()
    if key in _STRATA_EXACT_TOKENS:
        return _STRATA_EXACT_TOKENS[key]
    for token, ura_types in _STRATA_TYPE_MAP:
        if token in key:
            return ura_types
    return ()


def is_strata_type(nestlist_type: str) -> bool:
    return bool(strata_ura_types(nestlist_type))


def _norm_project(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(name or "").lower()).strip()


# Penthouses are strata units: URA records their sales as Condominium / Apartment
# (typeOfArea "Strata"), never as Land. Valuing one on LAND PSF compared it with houses.
# The CMA therefore routes Penthouse through the strata path -- but ONLY the CMA: the rest
# of the app (write-up, listing fields, Prestige classification) still treats Penthouse
# exactly as before, which is why this is a separate function from strata_ura_types.
_PENTHOUSE_COMPARABLE_TYPES = ("Condominium", "Apartment")
PENTHOUSE_CAVEAT = ("Penthouses usually sell at a premium to other units in the same "
                    "development (more space, private roof terraces, top-floor views). URA does "
                    "not label penthouse sales separately, so these comparables include ordinary "
                    "units -- treat the PSF range as a floor, not a ceiling.")


def is_penthouse_type(nestlist_type: str) -> bool:
    return "penthouse" in str(nestlist_type or "").lower()


def cma_strata_types(nestlist_type: str) -> tuple:
    """URA types for the CMA's built-up-PSF (strata) path: the condo family, plus
    Penthouse. () => the landed (land-PSF) path. Never raises."""
    types = strata_ura_types(nestlist_type)
    if types:
        return types
    if is_penthouse_type(nestlist_type):
        return _PENTHOUSE_COMPARABLE_TYPES
    return ()


# --- Project-name matching ------------------------------------------------------------
# Agents type "The Sail"; URA records "THE SAIL @ MARINA BAY". Exact matching found
# nothing. Matching is now in two tiers, and deliberately conservative -- valuing a home
# against the WRONG development is worse than falling back to its street:
#   1. exact, after dropping case, punctuation and the words "the" / "at" / "and";
#   2. close: URA's name STARTS WITH what the agent typed ("pearl bank" is in "one pearl
#      bank"; "sail" starts "sail marina bay"), or contains every word of it (2+ words).
#      URA's name is never allowed to be the SHORTER one: "Riviere Tower" never matches
#      RIVIERE. A close match is accepted ONLY when exactly one development fits, and:
#        - the agent typed 2+ significant words (after dropping the/at/and), or
#        - the agent typed ONE word and gave a street, exactly one development on that
#          street fits, AND no other development on that street has the word anywhere
#          in its name. One word with no street ("Dunman": Dunman View? Grand Dunman?),
#          or with a street that has both, is ambiguous -- nothing is chosen and the
#          street is used instead.
#      When a street is given, a close match must also be on it.
# The URA name actually used is reported back (matched_project) so the agent can see it.
_PROJECT_STOPWORDS = {"the", "at", "and"}
_CONDO_FAMILY_TYPES = ("Condominium", "Apartment", "Executive Condominium")


def _project_tokens(name) -> tuple:
    return tuple(t for t in _norm_project(name).split() if t not in _PROJECT_STOPWORDS)


def _is_close_project(wanted: tuple, ura: tuple) -> bool:
    """Is URA's name `ura` a close fit for what the agent typed (`wanted`)? URA's name
    must be at least as long as the agent's -- it never matches by being a prefix of it."""
    if not wanted or not ura or len(ura) < len(wanted):
        return False
    # Too little to go on ("8", "one", "sky") -> never a close match.
    if len("".join(wanted)) < 4 or all(t.isdigit() for t in wanted):
        return False
    if ura[:len(wanted)] == wanted:
        return True
    return len(wanted) >= 2 and set(wanted) <= set(ura)


def _match_projects(projects: list, project_name: str, street_keyword: str) -> tuple:
    """-> (candidate projects, how, URA names matched). how: "exact", "close",
    "ambiguous" (could be more than one development, none chosen) or "" (none)."""
    wanted = _project_tokens(project_name)
    if not wanted:
        return [], "", []
    exact = [p for p in projects if _project_tokens(p.get("project", "")) == wanted]
    if exact:
        return exact, "exact", sorted({str(p.get("project", "")).strip() for p in exact})
    close = [p for p in projects if _is_close_project(wanted, _project_tokens(p.get("project", "")))]
    if street_keyword:
        close = [p for p in close if _matches_street_keyword(p.get("street", ""), street_keyword)]
        if len(wanted) < 2 and close:
            # One word + street: if ANY other development on that street also has the word
            # in its name ("Dunman" on Dunman Road: DUNMAN VIEW and GRAND DUNMAN), the
            # agent could mean either -- choose neither.
            word = wanted[0]
            with_word = {_project_tokens(p.get("project", "")) for p in projects
                         if word in _project_tokens(p.get("project", ""))
                         and _matches_street_keyword(p.get("street", ""), street_keyword)}
            if len(with_word) > 1:
                return [], "ambiguous", []
    elif len(wanted) < 2:
        # One word, no street: too easy to pick the wrong development.
        return [], ("ambiguous" if close else ""), []
    names = {_project_tokens(p.get("project", "")) for p in close}
    if len(names) == 1:
        return close, "close", sorted({str(p.get("project", "")).strip() for p in close})
    if len(names) > 1:
        return [], "ambiguous", []
    return [], "", []


def _strata_records(candidates: list, ura_types: tuple, window_months: int) -> list:
    now = datetime.utcnow()

    def within_window(year, month):
        months_ago = (now.year - year) * 12 + (now.month - month)
        return 0 <= months_ago < window_months

    wanted_types = {_norm_ura_type(t) for t in ura_types}
    records = []
    seen = set()
    for project in candidates:
        street = project.get("street", "")
        project_label = str(project.get("project", "") or "").strip()
        for txn in project.get("transaction", []) or []:
            if txn.get("typeOfArea") != "Strata":
                continue
            if _norm_ura_type(txn.get("propertyType")) not in wanted_types:
                continue
            # A bulk purchase of several units is reported as one record with the
            # combined price and area -- not a comparable for one home.
            try:
                if int(str(txn.get("noOfUnits") or "1").strip() or "1") != 1:
                    continue
            except (TypeError, ValueError):
                continue
            parsed = _parse_contract_date(txn.get("contractDate", ""))
            if not parsed or not within_window(*parsed):
                continue
            try:
                price = float(txn.get("price", 0))
                area_sqm = float(txn.get("area", 0))
            except (TypeError, ValueError):
                continue
            if price <= 0 or area_sqm <= 0:
                continue
            sig = (_norm_street(street), _norm_project(project_label), txn.get("contractDate", ""),
                   price, round(area_sqm, 2), txn.get("propertyType", ""),
                   txn.get("floorRange", ""), txn.get("tenure", ""))
            if sig in seen:
                continue
            seen.add(sig)
            area_sqft = area_sqm * SQM_TO_SQFT
            district_code = txn.get("district", "")
            records.append({
                "street": street,
                "project": project_label,
                "price": price,
                # For a strata sale URA's area is the unit's strata floor area, i.e. the
                # condo's built-up size -- so PSF here is built-up PSF, not land PSF.
                "area_sqft": round(area_sqft),
                "psf": round(price / area_sqft),
                "property_type": txn.get("propertyType", ""),
                "contract_date": f"{parsed[1]:02d}/{parsed[0]}",
                "district": f"D{district_code}" if district_code else "",
                "tenure": txn.get("tenure", ""),
                # A RANGE of floors ("06-10"), never a unit -- and "-" when URA has none.
                "floor_range": str(txn.get("floorRange", "") or "").strip(),
                "sale_type": _SALE_TYPE_LABELS.get(str(txn.get("typeOfSale", "")).strip(), ""),
            })
    # Newest first, by real (year, month) -- see _contract_date_key.
    records.sort(key=lambda r: _contract_date_key(r["contract_date"]), reverse=True)
    return records


def _extract_strata_comparables_detailed(projects: list, street_keyword: str, ura_types: tuple,
                                         window_months: int, project_name: str = "") -> dict:
    """Condo-family / penthouse comparables, with how they were found.

    -> {"records", "matched_on" ("project" | "street" | ""), "project_match" ("exact" |
        "close" | "ambiguous" | ""), "matched_project" (URA name(s) used, "" if none),
        "type_note" ("" or a sentence when the development's sales are of another
        condo-family type than the one selected)}

    Order: the named development (exact, then a unique close match) -> if URA records that
    development under another condo-family type than the agent picked (an EC entered as
    "Condominium"), its own sales are still the right comparison and are used, with a
    note -> otherwise the street, exactly as before. A named development with no sales in
    the window now falls back to the street instead of returning nothing.

    Granularity: project + street + a floor RANGE ("06-10") is the finest URA publishes --
    there are no unit numbers in this feed at all, and none are produced here."""
    out = {"records": [], "matched_on": "", "project_match": "", "matched_project": "",
           "type_note": ""}
    candidates, how, names = _match_projects(projects, project_name, street_keyword)
    out["project_match"] = how
    if candidates:
        records = _strata_records(candidates, ura_types, window_months)
        if not records:
            other = tuple(t for t in _CONDO_FAMILY_TYPES
                          if _norm_ura_type(t) not in {_norm_ura_type(u) for u in ura_types})
            records = _strata_records(candidates, other, window_months)
            if records:
                found = sorted({r["property_type"] for r in records if r["property_type"]})
                out["type_note"] = (f"URA records this development as {' / '.join(found)}, "
                                    "so it is compared with those sales.")
        if records:
            out.update(records=records, matched_on="project", matched_project=" / ".join(names))
            return out
    if street_keyword:
        street_candidates = [p for p in projects
                             if _matches_street_keyword(p.get("street", ""), street_keyword)]
        records = _strata_records(street_candidates, ura_types, window_months)
        if records:
            out.update(records=records, matched_on="street")
    return out


def _extract_strata_comparables(projects: list, street_keyword: str, ura_types: tuple,
                                window_months: int, project_name: str = "") -> tuple:
    """(records, matched_on) -- the original two-value shape, kept for callers/tests."""
    d = _extract_strata_comparables_detailed(projects, street_keyword, ura_types,
                                             window_months, project_name)
    return d["records"], d["matched_on"]

# Generic road-type words agents commonly append/guess (e.g. typing "Road"
# when URA's gazetted name actually uses "Walk" or no suffix at all). These
# are dropped from the fallback word match so a wrong or missing suffix
# doesn't block an otherwise-correct match.
_GENERIC_STREET_SUFFIXES = {
    "road", "street", "st", "avenue", "ave", "walk", "drive", "close",
    "lane", "park", "grove", "hill", "crescent", "terrace", "way", "rise",
    "view", "gardens", "garden", "place", "boulevard", "circle", "loop",
    "green", "walk", "flats", "estate", "jalan", "lorong", "taman",
}

# URA's street field never includes a house/block/unit number -- these
# words plus any leading numeric/unit token ("9", "9A", "#01-01", "9-A")
# are skipped when locating where the actual street name starts, so a
# full address ("Blk 9 #01-01 Minaret Walk", "No. 9, Minaret Walk") reduces
# to the same street name as typing "Minaret Walk" alone.
_ADDRESS_NOISE_WORDS = {"blk", "block", "no", "unit", "level", "lvl", "floor"}


def _strip_address_prefix(keyword: str) -> str:
    tokens = re.findall(r"[A-Za-z]+|[#0-9][#0-9A-Za-z\-]*", keyword.strip())
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.lower() in _ADDRESS_NOISE_WORDS or re.match(r"^[#0-9]", tok):
            i += 1
            continue
        break
    return " ".join(tokens[i:])


def _matches_street_keyword(street: str, keyword: str) -> bool:
    if not keyword:
        return False
    street_lower = (street or "").lower()
    cleaned_keyword = _strip_address_prefix(keyword).lower()
    if not cleaned_keyword:
        return False

    # Fast path: the cleaned input matches verbatim (e.g. "Nassim Road"
    # entered exactly as URA spells it).
    if cleaned_keyword in street_lower:
        return True

    # Fallback: agents often guess the wrong road-type suffix, or URA's
    # gazetted spelling differs from colloquial usage -- match on the
    # significant words only, ignoring generic suffixes on both sides.
    keyword_words = [
        w for w in re.findall(r"[a-z]+", cleaned_keyword)
        if w not in _GENERIC_STREET_SUFFIXES
    ]
    if not keyword_words:
        return False
    return all(w in street_lower for w in keyword_words)

def extract_comparable_transactions(projects: list, street_keyword: str, property_type: str = "", window_months: int = 24) -> list:
    """Landed-property transactions near a given street/area, within a
    trailing window -- the raw comparables list a CMA is built from."""
    now = datetime.utcnow()
    ura_type = _map_property_type(property_type)

    def within_window(year, month):
        months_ago = (now.year - year) * 12 + (now.month - month)
        return 0 <= months_ago < window_months

    records = []
    seen = set()  # de-dupe identical URA records (same sale reported twice)
    for project in projects:
        street = project.get("street", "")
        if not _matches_street_keyword(street, street_keyword):
            continue
        for txn in project.get("transaction", []):
            if txn.get("typeOfArea") != "Land":
                continue
            if ura_type and _norm_ura_type(txn.get("propertyType")) != _norm_ura_type(ura_type):
                continue
            parsed = _parse_contract_date(txn.get("contractDate", ""))
            if not parsed or not within_window(*parsed):
                continue
            try:
                price = float(txn.get("price", 0))
                area_sqm = float(txn.get("area", 0))
            except (TypeError, ValueError):
                continue
            if price <= 0 or area_sqm <= 0:
                continue
            # URA's PMI_Resi_Transaction carries some sales as exact-duplicate
            # records (identical street+date+price+area -- observed and already
            # de-duped in the GCB count path, commit 8627362). Without this the
            # comparables table shows each such sale twice, padding the list and
            # skewing the average/count. A full-signature key drops only records
            # identical on every meaningful field, so genuinely distinct sales
            # (which differ in land area and/or price) are never merged.
            sig = (_norm_street(street), txn.get("contractDate", ""), price,
                   round(area_sqm, 2), txn.get("propertyType", ""), txn.get("tenure", ""))
            if sig in seen:
                continue
            seen.add(sig)
            area_sqft = area_sqm * SQM_TO_SQFT
            district_code = txn.get("district", "")
            records.append({
                "street": street,
                "price": price,
                "area_sqft": round(area_sqft),
                "psf": round(price / area_sqft),
                "property_type": txn.get("propertyType", ""),
                "contract_date": f"{parsed[1]:02d}/{parsed[0]}",
                # URA's public API doesn't expose house/block/unit numbers at all
                # (privacy restriction on their end) -- street is the finest
                # granularity available. District + tenure are the extra fields
                # actually present in the raw data that we weren't surfacing.
                "district": f"D{district_code}" if district_code else "",
                "tenure": txn.get("tenure", ""),
            })
    # Newest first, by real (year, month) -- see _contract_date_key.
    records.sort(key=lambda r: _contract_date_key(r["contract_date"]), reverse=True)
    return records

def compute_cma_stats(records: list, subject_land_size_sqft: float = 0) -> dict:
    if not records:
        return {
            "comparable_count": 0, "avg_psf": 0, "min_psf": 0, "max_psf": 0,
            "estimated_value_low": 0, "estimated_value_high": 0, "comparables": [],
        }
    psf_values = [r["psf"] for r in records]
    avg_psf = sum(psf_values) / len(psf_values)
    min_psf = min(psf_values)
    max_psf = max(psf_values)

    estimated_low = estimated_high = 0
    if subject_land_size_sqft > 0:
        estimated_low = round(subject_land_size_sqft * min_psf)
        estimated_high = round(subject_land_size_sqft * max_psf)

    return {
        "comparable_count": len(records),
        "avg_psf": round(avg_psf),
        "min_psf": round(min_psf),
        "max_psf": round(max_psf),
        "estimated_value_low": estimated_low,
        "estimated_value_high": estimated_high,
        "comparables": records[:20],
    }

# ---------------------------------------------------------------------------
# Source notice. The Singapore Open Data Licence requires the source to be stated
# wherever URA-derived figures are shown (CMA, Market Pulse, printed/exported
# reports). Carried on every response so no screen can render the numbers without
# it. Wording deliberately implies no URA endorsement; no URA logo is ever used.
# Only URA Data Service (PMI_Resi_Transaction) figures flow through this module --
# REALIS data is internal-use-only and must never be merged into these responses.
# ---------------------------------------------------------------------------
SOURCE_ATTRIBUTION = ("Contains information from the URA Data Service, made available "
                      "under the Singapore Open Data Licence.")
SOURCE_LICENCE_URL = "https://data.gov.sg/open-data-licence"
SOURCE_DISCLAIMER = "Indicative only; verify with official records."


def source_notice() -> dict:
    """The three source-notice fields, as added to every CMA / Market Pulse response."""
    return {
        "source_attribution": SOURCE_ATTRIBUTION,
        "source_licence_url": SOURCE_LICENCE_URL,
        "source_disclaimer": SOURCE_DISCLAIMER,
    }


# ---------------------------------------------------------------------------
# CMA transaction cache.
#
# Before this, EVERY CMA click pulled all four URA batches (the whole private-
# residential market) and held them in memory for the length of the request: 50 agents
# generating reports at once meant 50 full copies in RAM and 200 URA calls against one
# daily quota -- and with condo agents using CMA too, that only gets worse. Now each
# worker keeps ONE slimmed copy, refreshed at most every CMA_CACHE_TTL_SECONDS.
#
#   * Once a copy exists, nobody waits on URA again: past 12h it is served at once
#     (marked stale, with its date) while ONE background pull refreshes it -- for up to
#     CMA_MAX_STALE_SECONDS, which also covers a URA outage.
#   * With no copy (a fresh worker), the first request pulls and concurrent requests
#     wait for that one pull (single-flight).
#   * Each of the four batches has its own time budget; batches that finished are kept.
#     A pull with missing batches is served flagged "partial" for the cool-down window
#     only, never kept as the 12-hour copy.
#   * A failed pull is not retried for CMA_FAILURE_COOLDOWN_SECONDS (and Telegram is
#     told), so an outage doesn't make every agent wait out the retry ladder in turn.
#
# The slimmed copy keeps only the fields the comparables code reads, so the extraction
# result is identical to running on the raw feed (tested), at a fraction of the memory.
# ---------------------------------------------------------------------------
CMA_CACHE_TTL_SECONDS = 12 * 3600
CMA_MAX_STALE_SECONDS = 7 * 86400
CMA_FAILURE_COOLDOWN_SECONDS = 120
# Per-BATCH ceiling, retries included. Set above the worst case the batch could take on
# its own before this existed (3 attempts x 60s httpx timeout + 2s + 4s backoff = 186s),
# so it can only ever cut off a batch that would have hung longer than the code allowed
# before. There is deliberately NO overall cap: the old code had none, and a cap on the
# whole pull threw away batches that had already arrived.
CMA_BATCH_BUDGET_SECONDS = 200
# The daily Market Pulse pull reuses a complete CMA download younger than this instead of
# downloading the market again (only ever happens when the two collide on one worker).
PULSE_REUSE_CMA_COPY_SECONDS = 600

_PROJECT_KEEP = ("street", "project")
_TXN_KEEP = ("typeOfArea", "propertyType", "contractDate", "price", "area", "district",
             "tenure", "floorRange", "typeOfSale", "noOfUnits")

_cma_cache = {"projects": None, "fetched_at": 0.0, "as_of": None, "last_failure_at": 0.0,
              "partial_projects": None, "partial_at": 0.0, "partial_as_of": None}
_cma_lock = None          # created lazily, inside the running event loop
_cma_refresh_task = None  # the one background refresh in flight, if any
_failure_alert = None     # optional async callable(message), set by main.py


def set_failure_alert(hook) -> None:
    """main.py hands in its throttled Telegram alert so a URA outage on the CMA path is
    reported, not just logged. Optional: unset = log only."""
    global _failure_alert
    _failure_alert = hook


def _alert_failure(message: str) -> None:
    hook = _failure_alert
    if hook is None:
        return
    try:
        task = asyncio.get_running_loop().create_task(hook(message))
        _BACKGROUND.add(task)
        task.add_done_callback(_BACKGROUND.discard)
    except Exception as e:
        print(f"CMA failure alert not sent (harmless): {e}")


_BACKGROUND = set()  # strong refs so background tasks aren't garbage-collected mid-run


def _slim_projects(projects: list) -> list:
    """Drop every field the comparables code never reads. Strings are interned so the
    thousands of repeats ("Strata", "Condominium", "99 yrs lease commencing from ...")
    are stored once."""
    slim = []
    intern = __import__("sys").intern
    for p in projects or []:
        if not isinstance(p, dict):
            continue
        txns = []
        for t in p.get("transaction", []) or []:
            if not isinstance(t, dict):
                continue
            txns.append({k: (intern(v) if isinstance(v, str) else v)
                         for k, v in t.items() if k in _TXN_KEEP})
        if txns:
            entry = {k: p.get(k, "") for k in _PROJECT_KEEP}
            entry["transaction"] = txns
            slim.append(entry)
    return slim


# ---------------------------------------------------------------------------
# COMPACT MARKET COPY + ONE SHARED SNAPSHOT PER CONTAINER (memory).
#
# Measured on a synthetic 125k-transaction market: the slimmed list-of-dicts copy is
# ~55 MB of objects, but a worker that parsed the raw feed then sits at ~260 MB, because
# the freed JSON objects are not handed back to the OS. With 4 workers each pulling and
# keeping their own copy that is ~1 GB -- the jump seen after the condo launch.
#
# Now:
#   * The copy is COLUMNS (one array per field, strings stored once in a vocabulary):
#     a few MB instead of ~55 MB, with the same answers (materialised back into exactly
#     the dicts _slim_projects produced, for the few projects a report actually touches).
#   * The raw feed is turned into columns ONE BATCH AT A TIME, so the worker that pulls
#     peaks at roughly one batch of parsed JSON instead of all four.
#   * ONE worker per container pulls from URA (a cross-worker file lock); it writes the
#     compact copy to an on-disk snapshot by atomic rename, and the other workers load
#     that snapshot instead of pulling and parsing the feed themselves. A reader always
#     sees either the old file or the new one, never half of one.
#   * The disk is per-deploy and may be empty: no snapshot just means "pull", exactly as
#     before; any disk trouble falls back to the per-worker behaviour, never to an error.
# The serialisation is `marshal` of plain lists/bytes (no code execution on load, unlike
# pickle), in a 0700 directory only this service writes.
# ---------------------------------------------------------------------------
import array as _array
import marshal as _marshal
import math as _math
import sys as _sys

_STR_FIELDS = ("typeOfArea", "propertyType", "contractDate", "district", "tenure", "floorRange",
               "typeOfSale", "noOfUnits")
_SNAPSHOT_VERSION = 1


class CompactMarket:
    """The CMA market copy as columns. Read-only once built (safe to share across the
    worker's threads). candidates() returns, in the ORIGINAL feed order, every project a
    report could possibly use -- so running the unchanged extraction code on that small
    list gives exactly the result it gives on the full market."""

    def __init__(self):
        self.streets, self.names = [], []
        self.starts = _array.array("I", [0])
        self.cols = {f: _array.array("I") for f in _STR_FIELDS}
        self.price, self.area = _array.array("d"), _array.array("d")
        self.vocab, self._vocab_index = [], {}
        self.odd = {}           # txn index -> the original slim dict (rows that don't fit)
        self._finish_indexes()

    # -- building ---------------------------------------------------------------------
    def _v(self, s):
        i = self._vocab_index.get(s)
        if i is None:
            i = self._vocab_index[s] = len(self.vocab)
            self.vocab.append(_sys.intern(s))
        return i

    def add_projects(self, projects) -> None:
        """Append raw (or slim) projects. Same keep/skip rules as _slim_projects."""
        intern = _sys.intern
        for p in projects or []:
            if not isinstance(p, dict):
                continue
            rows = [t for t in (p.get("transaction", []) or []) if isinstance(t, dict)]
            if not rows:
                continue
            street, name = p.get("street", ""), p.get("project", "")
            self.streets.append(intern(street) if isinstance(street, str) else street)
            self.names.append(intern(name) if isinstance(name, str) else name)
            for t in rows:
                idx = len(self.price)
                normal = all(isinstance(t.get(f), str) for f in _STR_FIELDS)
                if normal:
                    try:
                        price, area = float(t["price"]), float(t["area"])
                        normal = (isinstance(t["price"], (str, int, float)) and not isinstance(t["price"], bool)
                                  and isinstance(t["area"], (str, int, float)) and not isinstance(t["area"], bool)
                                  and _math.isfinite(price) and _math.isfinite(area))
                    except (KeyError, TypeError, ValueError):
                        normal = False
                if normal:
                    for f in _STR_FIELDS:
                        self.cols[f].append(self._v(t[f]))
                    self.price.append(price)
                    self.area.append(area)
                else:
                    for f in _STR_FIELDS:
                        self.cols[f].append(0)
                    self.price.append(0.0)
                    self.area.append(0.0)
                    self.odd[idx] = {k: (intern(v) if isinstance(v, str) else v)
                                     for k, v in t.items() if k in _TXN_KEEP}
            self.starts.append(len(self.price))

    def _finish_indexes(self) -> None:
        by_street, by_name = {}, {}
        for i, (s, n) in enumerate(zip(self.streets, self.names)):
            by_street.setdefault(s, []).append(i)
            by_name.setdefault(n, []).append(i)
        self._by_street = by_street
        self._by_name = {n: (_project_tokens(n), idx) for n, idx in by_name.items()}
        self._gcb = [i for i, s in enumerate(self.streets) if _street_in_gcb_whitelist(s or "")]
        self._street_hits = {}

    def finish(self):
        self._finish_indexes()
        return self

    # -- reading ----------------------------------------------------------------------
    @property
    def project_count(self) -> int:
        return len(self.streets)

    @property
    def txn_count(self) -> int:
        return len(self.price)

    def __len__(self):
        return len(self.streets)

    def _project(self, i: int) -> dict:
        txns = []
        vocab, cols = self.vocab, self.cols
        for j in range(self.starts[i], self.starts[i + 1]):
            odd = self.odd.get(j)
            if odd is not None:
                txns.append(dict(odd))
                continue
            t = {f: vocab[cols[f][j]] for f in _STR_FIELDS}
            t["price"] = self.price[j]
            t["area"] = self.area[j]
            txns.append(t)
        return {"street": self.streets[i], "project": self.names[i], "transaction": txns}

    def projects(self, indexes) -> list:
        return [self._project(i) for i in sorted(set(indexes))]

    def materialise_all(self) -> list:
        return [self._project(i) for i in range(len(self.streets))]

    def gcb_projects(self) -> list:
        """Every project on a GCB-whitelisted street -- all Market Pulse stats read."""
        return [self._project(i) for i in self._gcb]

    def _street_indexes(self, street_keyword) -> list:
        if not street_keyword:
            return []
        hit = self._street_hits.get(street_keyword)
        if hit is None:
            match = _street_matcher(street_keyword)
            hit = []
            for s, idx in self._by_street.items():
                if match(s):
                    hit.extend(idx)
            if len(self._street_hits) > 512:
                self._street_hits = {}
            self._street_hits[street_keyword] = hit
        return hit

    def candidates(self, street_keyword: str, project_name: str = "") -> list:
        """Projects (original order) on a street matching street_keyword, plus -- when a
        project name is given -- every project whose name is an exact or close match to
        it. A superset of everything extract_comparable_transactions and
        _extract_strata_comparables_detailed can use, so their output is identical."""
        idx = list(self._street_indexes(street_keyword))
        wanted = _project_tokens(project_name) if project_name else ()
        if wanted:
            for tokens, ids in self._by_name.values():
                if tokens == wanted or _is_close_project(wanted, tokens):
                    idx.extend(ids)
        return self.projects(idx)

    # -- snapshot ---------------------------------------------------------------------
    def to_state(self) -> dict:
        return {"streets": self.streets, "names": self.names, "starts": self.starts.tobytes(),
                "cols": {f: self.cols[f].tobytes() for f in _STR_FIELDS},
                "price": self.price.tobytes(), "area": self.area.tobytes(),
                "vocab": self.vocab, "odd": self.odd}

    @classmethod
    def from_state(cls, state: dict) -> "CompactMarket":
        m = cls.__new__(cls)
        intern = _sys.intern
        m.streets = [intern(s) if isinstance(s, str) else s for s in state["streets"]]
        m.names = [intern(s) if isinstance(s, str) else s for s in state["names"]]
        m.starts = _array.array("I")
        m.starts.frombytes(state["starts"])
        m.cols = {}
        for f in _STR_FIELDS:
            m.cols[f] = _array.array("I")
            m.cols[f].frombytes(state["cols"][f])
        m.price, m.area = _array.array("d"), _array.array("d")
        m.price.frombytes(state["price"])
        m.area.frombytes(state["area"])
        m.vocab = [intern(s) for s in state["vocab"]]
        m._vocab_index = {s: i for i, s in enumerate(m.vocab)}
        m.odd = {int(k): v for k, v in state["odd"].items()}
        n = len(m.price)
        if (len(m.starts) != len(m.streets) + 1 or m.starts[-1] != n or len(m.area) != n
                or any(len(c) != n for c in m.cols.values()) or len(m.names) != len(m.streets)):
            raise ValueError("snapshot columns are inconsistent")
        return m._finish_indexes() or m


def _compact_from_projects(projects) -> CompactMarket:
    m = CompactMarket()
    m.add_projects(projects)
    return m.finish()


def _street_matcher(keyword):
    """A one-keyword version of _matches_street_keyword(street, keyword), with the keyword
    cleaned once instead of once per street. Same answer for every street (tested)."""
    if not keyword:
        return lambda street: False
    cleaned = _strip_address_prefix(keyword).lower()
    if not cleaned:
        return lambda street: False
    words = [w for w in re.findall(r"[a-z]+", cleaned) if w not in _GENERIC_STREET_SUFFIXES]

    def match(street):
        s = (street or "").lower()
        if cleaned in s:
            return True
        return bool(words) and all(w in s for w in words)
    return match


# --- the shared on-disk snapshot ---------------------------------------------------------
URA_SNAPSHOT_DIR = os.environ.get("URA_SNAPSHOT_DIR", "/tmp/nestlist_ura_snapshot")
# A worker waits this long for a sibling's URA pull before pulling itself (cold start: the
# sibling's pull IS this request's pull, exactly like the in-worker single-flight).
CMA_CROSS_WORKER_WAIT_SECONDS = 4 * CMA_BATCH_BUDGET_SECONDS + 60


def _snapshot_dir():
    """The private snapshot directory, or None if it can't be used safely."""
    try:
        os.makedirs(URA_SNAPSHOT_DIR, mode=0o700, exist_ok=True)
        st = os.stat(URA_SNAPSHOT_DIR)
        if not os.path.isdir(URA_SNAPSHOT_DIR) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
            return None
        return URA_SNAPSHOT_DIR
    except OSError:
        return None


def _snapshot_path(kind: str):
    d = _snapshot_dir()
    return os.path.join(d, f"cma_{kind}.snapshot") if d else None


def _write_snapshot(market: CompactMarket, fetched_at: float, as_of: str, kind: str = "complete") -> bool:
    """Atomic: write a temp file in the same directory, fsync, rename over the old one.
    Never raises (a failed write only means siblings pull for themselves)."""
    path = _snapshot_path(kind)
    if not path:
        return False
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        blob = _marshal.dumps({"v": _SNAPSHOT_VERSION, "fetched_at": float(fetched_at), "as_of": as_of,
                               "state": market.to_state()})
        with open(tmp, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:
        print(f"URA snapshot not written (harmless): {type(e).__name__}: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False


def _snapshot_mtime(kind: str = "complete"):
    path = _snapshot_path(kind)
    try:
        return os.stat(path).st_mtime if path else None
    except OSError:
        return None


def _read_snapshot(kind: str = "complete"):
    """-> (market, fetched_at, as_of) or None. Never raises."""
    path = _snapshot_path(kind)
    if not path:
        return None
    try:
        with open(path, "rb") as fh:
            data = _marshal.loads(fh.read())
        if not isinstance(data, dict) or data.get("v") != _SNAPSHOT_VERSION:
            return None
        return CompactMarket.from_state(data["state"]), float(data["fetched_at"]), data.get("as_of")
    except FileNotFoundError:
        return None
    except Exception as e:
        print(f"URA snapshot unreadable, ignored: {type(e).__name__}: {e}")
        return None


def _clear_shared_snapshot() -> None:
    """Remove the on-disk snapshots (tests; also safe to call any time)."""
    for kind in ("complete", "partial", "failed"):
        path = _snapshot_path(kind)
        if path:
            try:
                os.remove(path)
            except OSError:
                pass


def _mark_shared_failure() -> None:
    path = _snapshot_path("failed")
    if path:
        try:
            with open(path, "w") as fh:
                fh.write("1")
        except OSError:
            pass


class _CrossWorkerPullLock:
    """async context manager: hold the container-wide URA pull lock (flock, non-blocking
    polls so the event loop never blocks). .acquired is False if the wait ran out or the
    lock file can't be used -- the caller then pulls anyway (the old behaviour)."""

    def __init__(self, wait_seconds: float):
        self.wait_seconds, self.acquired, self._fh = wait_seconds, False, None
        self.waited = False

    async def __aenter__(self):
        import fcntl
        import time
        d = _snapshot_dir()
        if not d:
            return self
        try:
            self._fh = open(os.path.join(d, "pull.lock"), "a+")
        except OSError:
            return self
        deadline = time.monotonic() + self.wait_seconds
        while True:
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.acquired = True
                return self
            except OSError:
                self.waited = True
                if time.monotonic() >= deadline:
                    return self
                await asyncio.sleep(0.25)

    async def __aexit__(self, *exc):
        if self._fh is not None:
            try:
                self._fh.close()      # releases the flock
            except OSError:
                pass
        return False


def _release_freed_memory() -> None:
    """After a pull: collect, and on Linux ask glibc to hand freed heap back to the OS
    (otherwise the worker keeps the parse's high-water mark). Best effort, never raises."""
    try:
        import gc
        gc.collect()
        if _sys.platform.startswith("linux"):
            import ctypes
            ctypes.CDLL("libc.so.6").malloc_trim(0)
    except Exception:
        pass


def store_cma_projects(projects, already_slim: bool = False, fetched_at: float = None,
                       as_of: str = None) -> None:
    """Seed the CMA cache from a COMPLETE (all four batches) copy. `projects` is a
    CompactMarket or a raw/slim project list (compacted here)."""
    import time
    _cma_cache["projects"] = (projects if isinstance(projects, CompactMarket)
                              else _compact_from_projects(projects))
    _cma_cache["fetched_at"] = time.time() if fetched_at is None else fetched_at
    _cma_cache["as_of"] = as_of or _sgt_today().isoformat()
    _cma_cache["partial_projects"] = None


def _get_cma_lock():
    """The per-worker download lock, created lazily inside the running event loop."""
    global _cma_lock
    if _cma_lock is None:
        _cma_lock = asyncio.Lock()
    return _cma_lock


def _drop_expired_partial() -> None:
    """Free the partial copy once its cool-down window is over (it is never served after)."""
    import time
    if (_cma_cache["partial_projects"] is not None
            and time.time() - _cma_cache["partial_at"] >= CMA_FAILURE_COOLDOWN_SECONDS):
        _cma_cache["partial_projects"] = None
        _cma_cache["partial_as_of"] = None


def _cma_copy_is_fresh() -> bool:
    import time
    return (_cma_cache["projects"] is not None
            and time.time() - _cma_cache["fetched_at"] < CMA_CACHE_TTL_SECONDS)


async def warm_cma_cache(delay_seconds: float = 0.0) -> bool:
    """Startup warm-up: after `delay_seconds`, pull the CMA copy in the background so the
    first pricing report on a freshly started worker doesn't wait on URA. Respects the
    single-flight lock (a request or the Market Pulse pull already downloading -> this
    just waits and then finds the copy fresh) and the failure cool-down. Never raises.
    Returns True if this call stored a fresh copy."""
    import time
    try:
        if delay_seconds > 0:
            await asyncio.sleep(delay_seconds)
        if not os.environ.get("URA_ACCESS_KEY", "") or _cma_copy_is_fresh():
            return False
        if time.time() - _cma_cache["last_failure_at"] < CMA_FAILURE_COOLDOWN_SECONDS:
            return False
        async with _get_cma_lock():
            if _cma_copy_is_fresh():
                return False
            return await _fetch_into_cache()
    except Exception as e:
        print(f"CMA warm-up skipped: {type(e).__name__}: {e}")
        return False


def _cached_meta(stale: bool):
    return _cma_cache["projects"], {"data_as_of": _cma_cache["as_of"], "stale": stale, "partial": False}


def _partial_meta():
    return _cma_cache["partial_projects"], {"data_as_of": _cma_cache["partial_as_of"],
                                            "stale": False, "partial": True}


async def _pull_compact(access_key: str, token: str, batch_timeout):
    """One URA pull, compacted batch by batch. -> (CompactMarket, batch_report)."""
    market = CompactMarket()
    projects, batch_report = await fetch_all_transactions(
        access_key, token, batch_timeout=batch_timeout, on_batch=market.add_projects)
    if projects:      # a fetcher that returned the projects instead of streaming them
        await asyncio.to_thread(market.add_projects, projects)
    del projects
    await asyncio.to_thread(market.finish)
    _release_freed_memory()
    return market, batch_report


async def _adopt_shared_snapshot(max_age: float) -> bool:
    """Load a sibling worker's complete snapshot if it is younger than max_age and newer
    than this worker's own copy. Never raises."""
    import time
    mtime = _snapshot_mtime("complete")
    if mtime is None or time.time() - mtime >= max_age or mtime <= _cma_cache["fetched_at"] + 0.5:
        return False
    got = await asyncio.to_thread(_read_snapshot, "complete")
    if not got:
        return False
    market, fetched_at, as_of = got
    if time.time() - fetched_at >= max_age or fetched_at <= _cma_cache["fetched_at"]:
        return False
    store_cma_projects(market, fetched_at=fetched_at, as_of=as_of)
    return True


async def _adopt_shared_partial() -> bool:
    """A sibling's partial pull from the last cool-down window, served flagged partial."""
    import time
    mtime = _snapshot_mtime("partial")
    if mtime is None or time.time() - mtime >= CMA_FAILURE_COOLDOWN_SECONDS:
        return False
    got = await asyncio.to_thread(_read_snapshot, "partial")
    if not got:
        return False
    market, fetched_at, as_of = got
    _cma_cache["partial_projects"] = market
    _cma_cache["partial_at"] = fetched_at
    _cma_cache["partial_as_of"] = as_of
    return True


async def _fetch_into_cache() -> bool:
    """ONE pull from URA into the cache -- or, when a sibling worker already pulled, its
    snapshot. Never raises.

    Returns True when a complete copy was stored. On a partial pull (some batches failed
    or ran out of time) the batches that DID arrive are kept as a short-lived partial copy:
    served, flagged partial, never promoted to the 12-hour copy. Caller holds _cma_lock.

    Across workers: only the worker holding the container-wide pull lock talks to URA.
    The others wait for it (as requests already wait for the in-worker pull), then load
    its snapshot -- or, if it failed, take the same cool-down (and its partial copy)."""
    import time
    access_key = os.environ.get("URA_ACCESS_KEY", "")
    if not access_key:
        return False
    if await _adopt_shared_snapshot(CMA_CACHE_TTL_SECONDS):
        return True
    started = time.time()
    async with _CrossWorkerPullLock(CMA_CROSS_WORKER_WAIT_SECONDS) as pull_lock:
        if pull_lock.waited:
            # A sibling was pulling while we waited: use its result if it got one.
            if await _adopt_shared_snapshot(CMA_CACHE_TTL_SECONDS):
                return True
            failed_at = _snapshot_mtime("failed")
            if failed_at is not None and failed_at >= started - 1:
                _cma_cache["last_failure_at"] = time.time()
                await _adopt_shared_partial()
                return False
        try:
            token = await get_token(access_key)
            market, batch_report = await _pull_compact(access_key, token, CMA_BATCH_BUDGET_SECONDS)
        except Exception as e:
            _cma_cache["last_failure_at"] = time.time()
            _mark_shared_failure()
            print(f"CMA URA fetch failed: {type(e).__name__}: {e}")
            _alert_failure(f"URA comparables fetch failed ({type(e).__name__}). Pricing reports are "
                           "serving the last saved copy if there is one; retrying in a few minutes.")
            return False

        now = time.time()
        ok = len(batch_report) == 4 and all(b.get("ok") for b in batch_report)
        if ok:
            store_cma_projects(market, fetched_at=now)
            await asyncio.to_thread(_write_snapshot, market, now, _cma_cache["as_of"], "complete")
            return True

        _cma_cache["last_failure_at"] = time.time()
        _mark_shared_failure()
        failed = [b.get("batch") for b in batch_report if not b.get("ok")]
        print(f"CMA URA fetch partial: batches {failed} failed")
        _alert_failure(f"URA comparables fetch incomplete: batches {failed} failed. Pricing reports "
                       "use the last complete copy if there is one, otherwise flag results as partial.")
        if market.project_count:
            _cma_cache["partial_projects"] = market
            _cma_cache["partial_at"] = now
            _cma_cache["partial_as_of"] = _sgt_today().isoformat()
            await asyncio.to_thread(_write_snapshot, market, now, _cma_cache["partial_as_of"], "partial")
            # Free it when the cool-down ends rather than holding it until the next request.
            try:
                asyncio.get_running_loop().call_later(CMA_FAILURE_COOLDOWN_SECONDS + 0.1, _drop_expired_partial)
            except Exception:
                pass
        return False


def _ensure_background_refresh() -> None:
    """Start ONE background refresh unless one is already running or URA is in its failure
    cool-down. Agents are never made to wait on it."""
    import time
    global _cma_refresh_task
    if _cma_refresh_task is not None and not _cma_refresh_task.done():
        return
    if time.time() - _cma_cache["last_failure_at"] < CMA_FAILURE_COOLDOWN_SECONDS:
        return

    async def _run():
        async with _get_cma_lock():
            if time.time() - _cma_cache["fetched_at"] < CMA_CACHE_TTL_SECONDS:
                return   # someone else already refreshed it
            await _fetch_into_cache()

    _cma_refresh_task = asyncio.get_running_loop().create_task(_run())
    _BACKGROUND.add(_cma_refresh_task)
    _cma_refresh_task.add_done_callback(_BACKGROUND.discard)


async def _get_cma_projects():
    """(projects, meta) for a CMA. meta = {"data_as_of", "stale", "partial"}.
    Raises RuntimeError with an agent-readable message when there is nothing usable.

      fresh copy (< 12h)              -> served.
      older complete copy (< 7 days)  -> served AT ONCE, marked stale, while one
                                         background refresh runs (stale-while-revalidate).
      no usable copy                  -> this request (and any arriving meanwhile) waits
                                         for ONE pull; on a partial pull the batches that
                                         arrived are served, flagged partial.
    """
    import time
    _drop_expired_partial()
    now = time.time()
    age = now - _cma_cache["fetched_at"]
    if _cma_cache["projects"] is not None and age < CMA_CACHE_TTL_SECONDS:
        return _cached_meta(False)
    if _cma_cache["projects"] is not None and age < CMA_MAX_STALE_SECONDS:
        _ensure_background_refresh()
        return _cached_meta(True)

    async with _get_cma_lock():
        now = time.time()
        if _cma_cache["projects"] is not None and now - _cma_cache["fetched_at"] < CMA_CACHE_TTL_SECONDS:
            return _cached_meta(False)   # another request refreshed it while we waited
        partial_fresh = (_cma_cache["partial_projects"] is not None
                         and now - _cma_cache["partial_at"] < CMA_FAILURE_COOLDOWN_SECONDS)
        if now - _cma_cache["last_failure_at"] < CMA_FAILURE_COOLDOWN_SECONDS:
            if partial_fresh:
                return _partial_meta()
            raise RuntimeError("URA's data service isn't responding right now -- please try again in a few minutes")
        if not os.environ.get("URA_ACCESS_KEY", ""):
            raise RuntimeError("URA_ACCESS_KEY not configured")
        if await _fetch_into_cache():
            return _cached_meta(False)
        if _cma_cache["partial_projects"] is not None and time.time() - _cma_cache["partial_at"] < CMA_FAILURE_COOLDOWN_SECONDS:
            return _partial_meta()
        raise RuntimeError("Couldn't reach URA's data service right now -- please try again shortly")


async def generate_cma(street_keyword: str, property_type: str = "", land_size_sqft: float = 0,
                       window_months: int = 24, project_name: str = "",
                       built_up_sqft: float = 0) -> dict:
    """Comparable-sales report.

    LANDED (and blank/unknown types): exactly as before -- Land-type sales on the street,
    valued on LAND PSF x the subject's land size. The landed record and stats shapes are
    unchanged; this only ADDS keys (source notice, valuation basis, data freshness).

    CONDO family (Condominium / Executive Condominium (EC) / Apartment): Strata-type
    sales in the named project (or on the street), valued on BUILT-UP PSF x the
    subject's built-up size. Land size is ignored for these. PENTHOUSE takes this path too
    (it is a strata unit), with a premium caveat in valuation_caveat."""
    projects, meta = await _get_cma_projects()
    ura_types = cma_strata_types(property_type)
    if isinstance(projects, CompactMarket):
        # Only the projects this report can use, as the same dicts as before (original
        # order) -- indexed lookups instead of walking the whole market.
        projects = projects.candidates(street_keyword, project_name if ura_types else "")
    if ura_types:
        # Extraction walks the whole market: off the event loop so 50 agents' reports
        # don't hold up each other's requests on this worker.
        found = await asyncio.to_thread(
            _extract_strata_comparables_detailed,
            projects, street_keyword, ura_types, window_months, project_name)
        records = found["records"]
        # Built-up PSF x built-up size. Land size is never used here -- a penthouse has
        # none of its own, so it gets a PSF range without an estimate until a built-up
        # size is entered.
        stats = compute_cma_stats(records, built_up_sqft)
        stats["valuation_basis"] = "built_up_psf"
        stats["subject_area_sqft"] = built_up_sqft or 0
        stats["matched_on"] = found["matched_on"]
        stats["project_name"] = project_name or ""
        stats["matched_project"] = found["matched_project"]
        stats["project_match"] = found["project_match"]
        stats["type_note"] = found["type_note"]
        stats["valuation_caveat"] = PENTHOUSE_CAVEAT if is_penthouse_type(property_type) else ""
    else:
        records = extract_comparable_transactions(projects, street_keyword, property_type, window_months)
        stats = compute_cma_stats(records, land_size_sqft)
        stats["valuation_basis"] = "land_psf"
        stats["subject_area_sqft"] = land_size_sqft or 0
        stats["matched_on"] = "street" if records else ""
    stats["street_keyword"] = street_keyword
    stats["property_type"] = property_type
    stats["window_months"] = window_months
    stats["generated_at"] = date.today().isoformat()
    stats["data_as_of"] = meta.get("data_as_of")
    stats["data_stale"] = bool(meta.get("stale"))
    stats["data_partial"] = bool(meta.get("partial"))
    stats.update(source_notice())
    return stats

async def refresh_market_pulse(include_survey: bool = False) -> dict:
    """Full refresh cycle with diagnostics: token -> fetch (4 batches) ->
    filter -> compute. Never raises for a URA-side problem; instead returns a
    structured result the caller inspects:

        {
          "ok":           bool,      # True ONLY if all 4 batches succeeded and stats computed
          "stats":        dict|None, # the row to persist (present whenever computable)
          "partial":      bool,      # <4 batches succeeded -> under-count, must NOT be saved as live
          "batches_ok":   int,       # 0..4
          "batch_report": list,      # per-batch {batch, ok, status, projects, message}
          "raw_projects": int,       # merged project entries across succeeded batches
          "token_ok":     bool,      # False => AccessKey invalid/expired or token endpoint down
          "error":        str|None,  # set on config/token failure
        }

    Reliability rule (the fix for "stuck at 5 units"): a partial fetch returns
    fewer GCB areas than reality, so it is treated as a FAILURE. The caller
    keeps the last known-good row rather than overwriting it with an
    under-count that would still wear the LIVE badge."""
    result = {
        "ok": False, "stats": None, "partial": False, "batches_ok": 0,
        "batch_report": [], "raw_projects": 0, "token_ok": False, "error": None,
    }
    access_key = os.environ.get("URA_ACCESS_KEY", "")
    if not access_key:
        result["error"] = "URA_ACCESS_KEY not set"
        return result

    try:
        token = await get_token(access_key)
        result["token_ok"] = True
    except Exception as e:
        # get_token raises on Status != "Success" -- i.e. "Invalid Access Key".
        # This is the definitive "the key needs renewing" signal.
        result["error"] = str(e) or "URA token request failed"
        return result

    # ONE download at a time per worker: the CMA cache refresh and this daily pull share
    # _cma_lock, so on the worker that owns the Market Pulse loop they never run side by
    # side (double the URA quota, double the peak memory). A complete pull here also seeds
    # the CMA copy (below), so a CMA refresh waiting on the lock then finds it fresh and
    # downloads nothing -- one download serves both.
    reused = False
    async with _get_cma_lock():
        import time
        # A sibling worker's pull from the last few minutes counts as "just downloaded".
        if not include_survey:
            await _adopt_shared_snapshot(PULSE_REUSE_CMA_COPY_SECONDS)
        age = time.time() - _cma_cache["fetched_at"]
        if (not include_survey and _cma_cache["projects"] is not None
                and age < PULSE_REUSE_CMA_COPY_SECONDS):
            # A complete CMA download finished moments ago (it held the lock while we
            # waited): reuse it instead of pulling the whole market a second time. The
            # slim copy keeps every field the GCB stats read (tested identical). Admin
            # diagnostics (include_survey) always pull fresh -- they are the raw truth.
            projects = _cma_cache["projects"]
            batch_report = [{"batch": b, "ok": True, "status": "Reused", "projects": 0,
                             "message": f"reused the pricing-report download from {int(age)}s ago"}
                            for b in (1, 2, 3, 4)]
            reused = True
        else:
            async with _CrossWorkerPullLock(CMA_CROSS_WORKER_WAIT_SECONDS) as pull_lock:
                if (not include_survey and pull_lock.waited
                        and await _adopt_shared_snapshot(PULSE_REUSE_CMA_COPY_SECONDS)):
                    # A sibling finished a pull while we waited: use it.
                    projects = _cma_cache["projects"]
                    batch_report = [{"batch": b, "ok": True, "status": "Reused", "projects": 0,
                                     "message": "reused a sibling worker's download"} for b in (1, 2, 3, 4)]
                    reused = True
                else:
                    projects, batch_report = await _pull_compact(access_key, token, None)
            if not reused and len(batch_report) == 4 and all(b["ok"] for b in batch_report) and projects.project_count:
                # A complete pull is exactly what CMA needs too: hand it over -- still
                # inside the lock, so a CMA refresh queued behind us finds it fresh and
                # downloads nothing -- and to the sibling workers via the snapshot.
                # Never allowed to break the refresh itself.
                try:
                    now = time.time()
                    store_cma_projects(projects, fetched_at=now)
                    await asyncio.to_thread(_write_snapshot, projects, now, _cma_cache["as_of"], "complete")
                except Exception as e:
                    print(f"CMA cache seed from Market Pulse refresh failed (harmless): {e}")
    batches_ok = sum(1 for b in batch_report if b["ok"])
    result["batch_report"] = batch_report
    result["batches_ok"] = batches_ok
    result["raw_projects"] = len(projects)
    result["partial"] = batches_ok < 4

    # Only GCB-whitelisted streets feed these stats, so only those projects are rebuilt.
    stats = compute_market_pulse_stats(projects.gcb_projects() if isinstance(projects, CompactMarket)
                                       else projects)
    result["stats"] = stats  # exposed for diagnostics even when incomplete
    result["ok"] = (batches_ok == 4 and stats is not None)

    if include_survey:
        # Admin diagnostics only: the matched GCB records (for spot-checking every
        # included street) plus the full detached-land street universe (for
        # deciding what to whitelist). Kept off the normal refresh path.
        everything = projects.materialise_all() if isinstance(projects, CompactMarket) else projects
        result["gcb_records"] = _extract_gcb_transactions(everything, window_months=12)
        result["detached_land_streets"] = survey_detached_land_streets(everything, window_months=24)
    return result
