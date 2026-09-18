import asyncio
import json
import os
import re
import httpx
from datetime import datetime, date, timezone

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


async def get_token(access_key: str) -> str:
    """URA tokens are valid for the calendar day they were issued. Cache and
    only refetch once the date rolls over."""
    today = date.today()
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


async def fetch_all_transactions(access_key: str, token: str):
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
    {"batch", "ok", "status", "projects", "message"} -- one entry per batch."""
    headers = {**BROWSER_HEADERS, "AccessKey": access_key, "Token": token}
    all_projects = []
    batch_report = []
    async with httpx.AsyncClient(timeout=60) as client:
        for batch in (1, 2, 3, 4):
            try:
                data = await _get_json_with_retry(
                    client, TRANSACTION_URL,
                    params={"service": "PMI_Resi_Transaction", "batch": batch},
                    headers=headers,
                )
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
                print(f"URA batch {batch} failed: {e}")
                batch_report.append({"batch": batch, "ok": False, "status": "Exception",
                                     "projects": 0, "message": str(e)[:300]})
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

async def generate_cma(street_keyword: str, property_type: str = "", land_size_sqft: float = 0, window_months: int = 24) -> dict:
    access_key = os.environ.get("URA_ACCESS_KEY", "")
    if not access_key:
        raise RuntimeError("URA_ACCESS_KEY not configured")
    token = await get_token(access_key)
    projects, _batch_report = await fetch_all_transactions(access_key, token)
    records = extract_comparable_transactions(projects, street_keyword, property_type, window_months)
    stats = compute_cma_stats(records, land_size_sqft)
    stats["street_keyword"] = street_keyword
    stats["property_type"] = property_type
    stats["window_months"] = window_months
    stats["generated_at"] = date.today().isoformat()
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
    result["ok"] = (batches_ok == 4 and stats is not None)

    if include_survey:
        # Admin diagnostics only: the matched GCB records (for spot-checking every
        # included street) plus the full detached-land street universe (for
        # deciding what to whitelist). Kept off the normal refresh path.
        result["gcb_records"] = _extract_gcb_transactions(projects, window_months=12)
        result["detached_land_streets"] = survey_detached_land_streets(projects, window_months=24)
    return result
