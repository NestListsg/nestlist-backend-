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


async def fetch_all_transactions(access_key: str, token: str, batch_timeout: float = None):
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
    exactly the original behaviour."""
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
                    all_projects.extend(projects)
                    batch_report.append({"batch": batch, "ok": True, "status": status,
                                         "projects": len(projects), "message": None})
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
            if txn.get("propertyType") != "Detached":
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
            if txn.get("propertyType") != "Detached":
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
_PROPERTY_TYPE_MAP = [
    ("good class bungalow", "Detached"),
    ("detached/bungalow", "Detached"),
    ("detached", "Detached"),
    ("semi-detached", "Semi-Detached"),
    ("inter-terrace", "Terrace"),
    ("corner terrace", "Terrace"),
    ("terrace", "Terrace"),
]

def _map_property_type(nestlist_type: str) -> str:
    key = (nestlist_type or "").strip().lower()
    for prefix, ura_type in _PROPERTY_TYPE_MAP:
        if prefix in key:
            return ura_type
    return ""


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


def _extract_strata_comparables(projects: list, street_keyword: str, ura_types: tuple,
                                window_months: int, project_name: str = "") -> tuple:
    """Condo-family comparables. Returns (records, matched_on).

    Matching: when the agent gave a project name and URA has sales in a project of that
    name, comps are THAT development (the most like-for-like comparison there is);
    otherwise the street keyword, exactly as landed matches. matched_on is "project",
    "street" or "" (nothing matched) so the report can say which it used.

    Granularity: project + street + a floor RANGE ("06-10") is the finest URA publishes --
    there are no unit numbers in this feed at all, and none are produced here."""
    now = datetime.utcnow()

    def within_window(year, month):
        months_ago = (now.year - year) * 12 + (now.month - month)
        return 0 <= months_ago < window_months

    wanted_project = _norm_project(project_name)
    candidates = []
    if wanted_project:
        candidates = [p for p in projects if _norm_project(p.get("project", "")) == wanted_project]
    matched_on = "project" if candidates else ""
    if not candidates and street_keyword:
        candidates = [p for p in projects if _matches_street_keyword(p.get("street", ""), street_keyword)]
        matched_on = "street" if candidates else ""

    records = []
    seen = set()
    for project in candidates:
        street = project.get("street", "")
        project_label = str(project.get("project", "") or "").strip()
        for txn in project.get("transaction", []) or []:
            if txn.get("typeOfArea") != "Strata":
                continue
            if txn.get("propertyType") not in ura_types:
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
    # Newest first. Sorted on (year, month), not on the "MM/YYYY" display string, so a
    # December sale never sorts above a later January one.
    records.sort(key=lambda r: (r["contract_date"][3:], r["contract_date"][:2]), reverse=True)
    return records, matched_on

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
            if ura_type and txn.get("propertyType") != ura_type:
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
    records.sort(key=lambda r: r["contract_date"], reverse=True)
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


def store_cma_projects(projects: list, already_slim: bool = False) -> None:
    """Seed the CMA cache from a COMPLETE (all four batches) fetch -- the daily Market
    Pulse refresh calls this so the worker that owns it never fetches twice."""
    import time
    _cma_cache["projects"] = projects if already_slim else _slim_projects(projects)
    _cma_cache["fetched_at"] = time.time()
    _cma_cache["as_of"] = _sgt_today().isoformat()
    _cma_cache["partial_projects"] = None


def _cached_meta(stale: bool):
    return _cma_cache["projects"], {"data_as_of": _cma_cache["as_of"], "stale": stale, "partial": False}


def _partial_meta():
    return _cma_cache["partial_projects"], {"data_as_of": _cma_cache["partial_as_of"],
                                            "stale": False, "partial": True}


async def _fetch_into_cache() -> bool:
    """ONE pull from URA into the cache. Never raises.

    Returns True when a complete copy was stored. On a partial pull (some batches failed
    or ran out of time) the batches that DID arrive are kept as a short-lived partial copy:
    served, flagged partial, never promoted to the 12-hour copy. Caller holds _cma_lock."""
    import time
    access_key = os.environ.get("URA_ACCESS_KEY", "")
    if not access_key:
        return False
    try:
        token = await get_token(access_key)
        projects, batch_report = await fetch_all_transactions(
            access_key, token, batch_timeout=CMA_BATCH_BUDGET_SECONDS)
    except Exception as e:
        _cma_cache["last_failure_at"] = time.time()
        print(f"CMA URA fetch failed: {type(e).__name__}: {e}")
        _alert_failure(f"URA comparables fetch failed ({type(e).__name__}). Pricing reports are "
                       "serving the last saved copy if there is one; retrying in a few minutes.")
        return False

    ok = len(batch_report) == 4 and all(b.get("ok") for b in batch_report)
    if ok:
        # Slimming walks every transaction in the market -- off the event loop, so the
        # worker's other requests aren't held up while it runs.
        store_cma_projects(await asyncio.to_thread(_slim_projects, projects), already_slim=True)
        return True

    _cma_cache["last_failure_at"] = time.time()
    failed = [b.get("batch") for b in batch_report if not b.get("ok")]
    print(f"CMA URA fetch partial: batches {failed} failed")
    _alert_failure(f"URA comparables fetch incomplete: batches {failed} failed. Pricing reports "
                   "use the last complete copy if there is one, otherwise flag results as partial.")
    if projects:
        _cma_cache["partial_projects"] = await asyncio.to_thread(_slim_projects, projects)
        _cma_cache["partial_at"] = time.time()
        _cma_cache["partial_as_of"] = _sgt_today().isoformat()
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
        global _cma_lock
        if _cma_lock is None:
            _cma_lock = asyncio.Lock()
        async with _cma_lock:
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
    global _cma_lock
    now = time.time()
    age = now - _cma_cache["fetched_at"]
    if _cma_cache["projects"] is not None and age < CMA_CACHE_TTL_SECONDS:
        return _cached_meta(False)
    if _cma_cache["projects"] is not None and age < CMA_MAX_STALE_SECONDS:
        _ensure_background_refresh()
        return _cached_meta(True)

    if _cma_lock is None:
        _cma_lock = asyncio.Lock()
    async with _cma_lock:
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
    subject's built-up size. Land size is ignored for these."""
    projects, meta = await _get_cma_projects()
    ura_types = strata_ura_types(property_type)
    if ura_types:
        records, matched_on = _extract_strata_comparables(
            projects, street_keyword, ura_types, window_months, project_name)
        stats = compute_cma_stats(records, built_up_sqft)
        stats["valuation_basis"] = "built_up_psf"
        stats["subject_area_sqft"] = built_up_sqft or 0
        stats["matched_on"] = matched_on
        stats["project_name"] = project_name or ""
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

    projects, batch_report = await fetch_all_transactions(access_key, token)
    batches_ok = sum(1 for b in batch_report if b["ok"])
    result["batch_report"] = batch_report
    result["batches_ok"] = batches_ok
    result["raw_projects"] = len(projects)
    result["partial"] = batches_ok < 4

    stats = compute_market_pulse_stats(projects)
    result["stats"] = stats  # exposed for diagnostics even when incomplete
    if batches_ok == 4 and projects:
        # A complete pull is exactly what CMA needs too: hand it over so this worker's
        # next CMA doesn't fetch the whole market a second time. Never allowed to break
        # the refresh itself.
        try:
            store_cma_projects(await asyncio.to_thread(_slim_projects, projects), already_slim=True)
        except Exception as e:
            print(f"CMA cache seed from Market Pulse refresh failed (harmless): {e}")
    result["ok"] = (batches_ok == 4 and stats is not None)

    if include_survey:
        # Admin diagnostics only: the matched GCB records (for spot-checking every
        # included street) plus the full detached-land street universe (for
        # deciding what to whitelist). Kept off the normal refresh path.
        result["gcb_records"] = _extract_gcb_transactions(projects, window_months=12)
        result["detached_land_streets"] = survey_detached_land_streets(projects, window_months=24)
    return result
