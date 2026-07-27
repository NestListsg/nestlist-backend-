import asyncio
import json
import os
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
