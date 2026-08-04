import asyncio
import json
import os
import re
import httpx
from datetime import datetime, date

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

# The 39 URA-gazetted Good Class Bungalow Areas. Compound entries ("First
# Avenue / Third Avenue") are split into individual street-name tokens so
# each can be matched independently against a transaction's "street" field.
GCB_AREA_TOKENS = [
    "Belmont Park", "Cornwall Gardens", "Leedon Park", "Bin Tong Park",
    "Dalvey Estate", "Maryland Estate", "Binjai Park", "Eng Neo Avenue",
    "Nassim Road", "Brizay Park", "Ewart Park", "Oei Tiong Ham Park",
    "Bukit Sedap", "First Avenue", "Third Avenue", "Queen Astrid Park",
    "Bukit Tunggal", "Ford Avenue", "Raffles Park", "Caldecott Hill Estate",
    "Fourth Avenue", "Sixth Avenue", "Rebecca Park", "Camden Park",
    "Gallop Road", "Woollerton Park", "Ridley Park", "Chatsworth Park",
    "Garlick Avenue", "Ridout Park", "Chee Hoon Avenue", "Holland Park",
    "Swiss Club Road", "Chestnut Avenue", "Holland Rise", "Victoria Park",
    "Cluny Hill", "Kilburn Estate", "Windsor Park", "Cluny Park",
    "King Albert Park", "White House Park",
]

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


async def fetch_all_transactions(access_key: str, token: str) -> list:
    """Fetch all 4 batches of PMI_Resi_Transaction (split by postal district
    ranges) and return the merged list of project entries."""
    headers = {**BROWSER_HEADERS, "AccessKey": access_key, "Token": token}
    all_projects = []
    async with httpx.AsyncClient(timeout=60) as client:
        for batch in (1, 2, 3, 4):
            data = await _get_json_with_retry(
                client, TRANSACTION_URL,
                params={"service": "PMI_Resi_Transaction", "batch": batch},
                headers=headers,
            )
            if data.get("Status") == "Success":
                all_projects.extend(data.get("Result", []))
            else:
                # Don't let one failed batch silently make results look
                # sparser than reality -- this is visible in Railway logs
                # so a "no matching transactions" report can be told apart
                # from a genuine data gap during troubleshooting.
                print(f"URA batch {batch} did not return Success: {data.get('Message')}")
    return all_projects


def _matches_gcb_area(street: str) -> bool:
    street_lower = (street or "").lower()
    return any(token.lower() in street_lower for token in GCB_AREA_TOKENS)


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
    for project in projects:
        street = project.get("street", "")
        if not _matches_gcb_area(street):
            continue
        for txn in project.get("transaction", []):
            if txn.get("propertyType") != "Detached":
                continue
            if txn.get("typeOfArea") != "Land":
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
            psf = price / (area_sqm * SQM_TO_SQFT)
            records.append({"street": street, "price": price, "psf": psf})
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
    projects = await fetch_all_transactions(access_key, token)
    records = extract_comparable_transactions(projects, street_keyword, property_type, window_months)
    stats = compute_cma_stats(records, land_size_sqft)
    stats["street_keyword"] = street_keyword
    stats["property_type"] = property_type
    stats["window_months"] = window_months
    stats["generated_at"] = date.today().isoformat()
    return stats

async def refresh_market_pulse() -> dict:
    """Full refresh cycle: token -> fetch -> filter -> compute. Returns the
    stats dict to upsert, or None if URA_ACCESS_KEY isn't set or no
    qualifying transactions were found (caller should keep prior values)."""
    access_key = os.environ.get("URA_ACCESS_KEY", "")
    if not access_key:
        return None
    token = await get_token(access_key)
    projects = await fetch_all_transactions(access_key, token)
    return compute_market_pulse_stats(projects)
