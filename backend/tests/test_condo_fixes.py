"""Fixes after condo Phase 1 (Semi-D CMA, buyer matching, date sort, Penthouse CMA, help
text, project-name matching, unit-number false positives, log redaction, cross-worker
alert dedupe, Studio). Run:  python3 tests/test_condo_fixes.py   (exit 1 on any failure)

main.py cannot be imported here (no supabase/anthropic locally), so its functions are
EXECUTED from source, compiled under main.py's own file name and line numbers -- a line
tracer (see --coverage) sees them as main.py lines.

  python3 tests/test_condo_fixes.py --coverage   also lists changed lines (vs 97549be)
                                                 that no test executed.
"""
import ast
import asyncio
import importlib.util
import io
import logging
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import types
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, BACKEND)

COVER = "--coverage" in sys.argv
EXECUTED = set()
if COVER:
    _targets = {os.path.join(BACKEND, "main.py"), os.path.join(BACKEND, "ura_market_pulse.py")}

    def _tracer(frame, event, arg):
        if frame.f_code.co_filename in _targets:
            EXECUTED.add((frame.f_code.co_filename, frame.f_lineno))
            return _tracer
        return None
    sys.settrace(_tracer)
    import threading
    threading.settrace(_tracer)

import ura_market_pulse as NEW  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ura_pre_condo", os.path.join(_HERE, "golden", "ura_market_pulse_pre_condo.py"))
OLD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(OLD)

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + str(detail)[:300]) if detail else ""))


def mmyy(months_ago):
    now = datetime.utcnow()
    y, m = now.year, now.month - months_ago
    while m <= 0:
        m += 12
        y -= 1
    return f"{m:02d}{y % 100:02d}"


def txn(**kw):
    base = {"typeOfArea": "Land", "propertyType": "Detached", "contractDate": mmyy(2),
            "price": "10000000", "area": "1500", "district": "10", "tenure": "Freehold",
            "floorRange": "-", "typeOfSale": "3", "noOfUnits": "1"}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
print("\n[1] Semi-Detached CMA: URA spelling, any case/spacing; nothing else changes")
for v in ("Semi-detached", "Semi-Detached", "SEMI-DETACHED", "Semi Detached", "semidetached"):
    check(f"_norm_ura_type({v!r}) == 'semidetached'", NEW._norm_ura_type(v) == "semidetached")
check("Detached / Strata Detached / Strata Semi-detached stay distinct",
      len({NEW._norm_ura_type(v) for v in ("Detached", "Strata Detached", "Strata Semi-detached",
                                           "Semi-detached", "Terrace", "Strata Terrace")}) == 6)
check("the OLD map sent Semi-Detached to Detached sales (the bug)",
      OLD._map_property_type("Semi-Detached") == "Detached")
for label in ("Semi-Detached", "Semi-detached", "semi detached", "Semi-D", "SEMI-D house"):
    check(f"NEW map: {label!r} -> Semi-Detached", NEW._map_property_type(label) == "Semi-Detached")
for label, exp in (("Good Class Bungalow (GCB)", "Detached"), ("Detached/Bungalow", "Detached"),
                   ("Inter-Terrace", "Terrace"), ("Corner Terrace", "Terrace"), ("", ""),
                   ("Penthouse", ""), ("Something Unknown", ""), (None, "")):
    check(f"NEW map unchanged for {label!r}", NEW._map_property_type(label) == exp == OLD._map_property_type(label or ""))

SEMI_RAW = [{"project": "LANDED", "street": "LORONG CHUAN", "transaction": [
    txn(propertyType="Semi-detached", price="3000000", area="300"),
    txn(propertyType="Semi-Detached", price="3100000", area="301", contractDate=mmyy(3)),
    txn(propertyType="SEMI-DETACHED", price="3200000", area="302", contractDate=mmyy(4)),
    txn(propertyType="Semi Detached", price="3300000", area="303", contractDate=mmyy(5)),
    txn(propertyType="Detached", price="9000000", area="900"),
    txn(propertyType="Terrace", price="2000000", area="150"),
    txn(typeOfArea="Strata", propertyType="Strata Semi-detached", price="2500000", area="200"),
]}]
semi = NEW.extract_comparable_transactions(SEMI_RAW, "Lorong Chuan", "Semi-Detached", 24)
check("Semi-D report: every Semi-D spelling, no Detached/Terrace/strata",
      sorted(r["price"] for r in semi) == [3000000.0, 3100000.0, 3200000.0, 3300000.0], [r["price"] for r in semi])
check("Semi-D report previously showed Detached sales instead",
      [r["property_type"] for r in OLD.extract_comparable_transactions(SEMI_RAW, "Lorong Chuan", "Semi-Detached", 24)]
      == ["Detached"])
check("Detached report on the same street: Detached only",
      [r["price"] for r in NEW.extract_comparable_transactions(SEMI_RAW, "Lorong Chuan", "Detached/Bungalow", 24)] == [9000000.0])
check("Terrace report: Terrace only",
      [r["price"] for r in NEW.extract_comparable_transactions(SEMI_RAW, "Lorong Chuan", "Corner Terrace", 24)] == [2000000.0])

# Golden: every other landed type -> same rows as the frozen pre-condo module (re-sorted by
# real date, the one deliberate change), on a fixture that crosses a year boundary.
GOLD_RAW = [{"project": "LANDED", "street": "NASSIM ROAD", "transaction": [
    txn(contractDate=mmyy(k), price=str(5_000_000 + k * 10_000), area=str(1000 + k),
        propertyType=("Detached", "Terrace", "Semi-detached")[k % 3]) for k in range(0, 30)
] + [txn(), txn()]}]


def _real(recs):
    return sorted(recs, key=lambda r: (int(r["contract_date"][3:]), int(r["contract_date"][:2])), reverse=True)


for ptype in ("Good Class Bungalow (GCB)", "Detached/Bungalow", "Inter-Terrace", "Corner Terrace",
              "Penthouse", "", "Something Unknown"):
    ok = True
    for win in (12, 24, 60):
        o = _real(OLD.extract_comparable_transactions(GOLD_RAW, "Nassim", ptype, win))
        n = NEW.extract_comparable_transactions(GOLD_RAW, "Nassim", ptype, win)
        if o != n or OLD.compute_cma_stats(o, 4000) != NEW.compute_cma_stats(n, 4000):
            ok = False
    check(f"golden: {ptype!r} same rows + stats as pre-change (3 windows)", ok)
check("Market Pulse GCB stats unchanged",
      {k: v for k, v in (NEW.compute_market_pulse_stats(GOLD_RAW) or {}).items() if k != "refreshed_at"}
      == {k: v for k, v in (OLD.compute_market_pulse_stats(GOLD_RAW) or {}).items() if k != "refreshed_at"})
check("GCB detached-street survey unchanged",
      NEW.survey_detached_land_streets(GOLD_RAW) == OLD.survey_detached_land_streets(GOLD_RAW))

# ---------------------------------------------------------------------------
print("\n[3] newest-20 by real date across a year boundary")
YEAR_RAW = [{"project": "L", "street": "NASSIM ROAD", "transaction": [
    txn(contractDate=mmyy(k), price=str(5_000_000 + k * 10_000), area=str(1000 + k)) for k in range(25)]}]
newest = NEW.compute_cma_stats(NEW.extract_comparable_transactions(YEAR_RAW, "Nassim", "Detached/Bungalow", 60), 0)
expected = {5_000_000.0 + k * 10_000 for k in range(20)}
check("newest 20 are the 20 latest months", {r["price"] for r in newest["comparables"]} == expected)
old_newest = OLD.compute_cma_stats(OLD.extract_comparable_transactions(YEAR_RAW, "Nassim", "Detached/Bungalow", 60), 0)
check("(the old text sort got this wrong)", {r["price"] for r in old_newest["comparables"]} != expected)
check("_contract_date_key: bad value sorts last, never raises",
      NEW._contract_date_key("bad") == (0, 0) and NEW._contract_date_key(None) == (0, 0)
      and NEW._contract_date_key("01/2026") > NEW._contract_date_key("12/2025"))

# ---------------------------------------------------------------------------
print("\n[4] Penthouse: strata (built-up PSF) CMA, Prestige classification untouched")
check("cma_strata_types('Penthouse') = Condominium/Apartment",
      NEW.cma_strata_types("Penthouse") == ("Condominium", "Apartment"))
check("strata_ura_types / is_strata_type unchanged for Penthouse (stays Prestige)",
      NEW.strata_ura_types("Penthouse") == () and not NEW.is_strata_type("Penthouse"))
check("cma_strata_types for condo = strata_ura_types; landed = ()",
      NEW.cma_strata_types("Executive Condominium (EC)") == ("Executive Condominium",)
      and NEW.cma_strata_types("Semi-Detached") == () and NEW.cma_strata_types(None) == ())

PH_RAW = [
    {"project": "THE SAIL @ MARINA BAY", "street": "MARINA BOULEVARD", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="6000000", area="300", floorRange="66-70",
            district="01"),
        txn(typeOfArea="Strata", propertyType="Apartment", price="2000000", area="100", district="01"),
        txn(typeOfArea="Land", propertyType="Detached", price="30000000", area="1500", district="01"),
    ]},
    {"project": "MARINA BAY RESIDENCES", "street": "MARINA BOULEVARD", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="4000000", area="200", district="01")]},
    {"project": "MARINA BAY SUITES", "street": "CENTRAL BOULEVARD", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="4100000", area="210", district="01")]},
    {"project": "PARC ESTA", "street": "SIMS AVENUE", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="1500000", area="90", district="14")]},
    {"project": "PARC CLEMATIS", "street": "JALAN LEMPENG", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="1600000", area="95", district="05")]},
    {"project": "PIERMONT GRAND", "street": "SUMANG WALK", "transaction": [
        txn(typeOfArea="Strata", propertyType="Executive Condominium", price="1300000", area="100", district="19")]},
    {"project": "OLD TOWER", "street": "SUMANG WALK", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="1400000", area="100", district="19",
            contractDate=mmyy(50))]},
    {"project": "SUMANG CONDO", "street": "SUMANG WALK", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="1450000", area="100", district="19")]},
    {"project": "RIVIERE", "street": "JIAK KIM STREET", "transaction": [
        txn(typeOfArea="Strata", propertyType="Apartment", price="3000000", area="120", district="03")]},
    {"project": "DUNMAN VIEW", "street": "DUNMAN ROAD", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="1700000", area="110", district="15")]},
    {"project": "GRAND DUNMAN", "street": "DUNMAN ROAD", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="2100000", area="100", district="15")]},
    {"project": "REFLECTIONS AT KEPPEL BAY", "street": "KEPPEL BAY VIEW", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="2800000", area="140", district="04")]},
    {"project": "ONE PEARL BANK", "street": "PEARL BANK", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="1900000", area="80", district="03")]},
]


class FakeURA:
    def __init__(self, projects):
        self.projects = projects

    async def token(self, key):
        return "tok"

    async def fetch(self, key, token, batch_timeout=None):
        return list(self.projects), [{"batch": b, "ok": True} for b in (1, 2, 3, 4)]


def reset_cache():
    NEW._cma_cache.update({"projects": None, "fetched_at": 0.0, "as_of": None, "last_failure_at": 0.0,
                           "partial_projects": None, "partial_at": 0.0, "partial_as_of": None})
    NEW._cma_lock = None
    NEW._cma_refresh_task = None


os.environ["URA_ACCESS_KEY"] = "test-placeholder-not-a-real-key"
_REAL = (NEW.get_token, NEW.fetch_all_transactions)


async def _cma(*a, **k):
    reset_cache()
    fake = FakeURA(PH_RAW)
    NEW.get_token, NEW.fetch_all_transactions = fake.token, fake.fetch
    try:
        return await NEW.generate_cma(*a, **k)
    finally:
        NEW.get_token, NEW.fetch_all_transactions = _REAL

ph = asyncio.run(_cma("Marina Boulevard", "Penthouse", 5000, 24, project_name="The Sail", built_up_sqft=4000))
check("Penthouse valued on built-up PSF against the development's strata sales",
      ph["valuation_basis"] == "built_up_psf" and ph["matched_on"] == "project"
      and sorted(c["price"] for c in ph["comparables"]) == [2000000.0, 6000000.0], ph.get("comparables"))
check("Penthouse: never a land sale", all(c["property_type"] != "Detached" for c in ph["comparables"]))
check("Penthouse: estimate = built-up x PSF (land size ignored)",
      ph["estimated_value_low"] == round(4000 * ph["min_psf"]) and ph["subject_area_sqft"] == 4000)
check("Penthouse premium caveat in the response", "premium" in ph["valuation_caveat"])
ph2 = asyncio.run(_cma("Marina Boulevard", "Penthouse", 5000, 24))
check("Penthouse without built-up: comps by street, no estimate from land size",
      ph2["matched_on"] == "street" and ph2["comparable_count"] == 3 and ph2["estimated_value_low"] == 0, ph2)
condo = asyncio.run(_cma("Marina Boulevard", "Condominium", 0, 24, project_name="The Sail", built_up_sqft=1000))
check("condo: no penthouse caveat, matched name reported",
      condo["valuation_caveat"] == "" and condo["matched_project"] == "THE SAIL @ MARINA BAY"
      and condo["project_match"] == "close")
landed = asyncio.run(_cma("Marina Boulevard", "Detached/Bungalow", 1000, 24))
check("landed response: no new strata keys", landed["valuation_basis"] == "land_psf" and "matched_project" not in landed)

# ---------------------------------------------------------------------------
print("\n[6] project-name matching: close but conservative, then street")
D = NEW._extract_strata_comparables_detailed
CA = ("Condominium", "Apartment")
r = D(PH_RAW, "Marina Boulevard", CA, 24, "The Sail")
check("'The Sail' + its street -> URA 'THE SAIL @ MARINA BAY' (close)",
      r["matched_on"] == "project" and r["project_match"] == "close" and r["matched_project"] == "THE SAIL @ MARINA BAY")
r = D(PH_RAW, "", CA, 24, "The Sail")
check("'The Sail' with no street: one significant word -> ambiguous, nothing chosen",
      r["project_match"] == "ambiguous" and r["records"] == [] and r["matched_project"] == "")
r = D(PH_RAW, "", CA, 24, "Riviere Tower")
check("S1: 'Riviere Tower' never matches RIVIERE (URA name shorter)",
      r["matched_project"] == "" and r["records"] == [] and r["project_match"] == "")
r = D(PH_RAW, "Jiak Kim Street", CA, 24, "Riviere Tower")
check("S1: ...not even with RIVIERE's own street (street sales used, labelled street)",
      r["matched_on"] == "street" and r["matched_project"] == "")
r = D(PH_RAW, "", CA, 24, "Dunman")
check("S1: 'Dunman' with no street -> ambiguous, no DUNMAN VIEW guess",
      r["project_match"] == "ambiguous" and r["records"] == [] and r["matched_project"] == "")
r = D(PH_RAW, "Dunman Road", CA, 24, "Dunman")
check("N5: 'Dunman' + Dunman Road: GRAND DUNMAN is on it too -> ambiguous, street sales used",
      r["project_match"] == "ambiguous" and r["matched_project"] == "" and r["matched_on"] == "street"
      and sorted(x["price"] for x in r["records"]) == [1700000.0, 2100000.0], r)
r = D(PH_RAW, "Jiak Kim Street", CA, 24, "Riviere")
check("N5: one word + street with only one development carrying it -> matched (exact here)",
      r["matched_project"] == "RIVIERE")
r = D(PH_RAW, "Dunman Road", CA, 24, "Grand Dunman")
check("'Grand Dunman' -> exact GRAND DUNMAN", r["project_match"] == "exact" and r["matched_project"] == "GRAND DUNMAN")
r = D(PH_RAW, "", CA, 24, "Reflections")
check("'Reflections' (one word) with no street -> ambiguous", r["project_match"] == "ambiguous" and not r["records"])
r = D(PH_RAW, "Keppel Bay View", CA, 24, "Reflections")
check("'Reflections' + Keppel Bay View -> REFLECTIONS AT KEPPEL BAY",
      r["matched_project"] == "REFLECTIONS AT KEPPEL BAY" and r["project_match"] == "close")
r = D(PH_RAW, "", CA, 24, "Reflections Keppel Bay")
check("'Reflections Keppel Bay' (3 words, no street) -> REFLECTIONS AT KEPPEL BAY",
      r["matched_project"] == "REFLECTIONS AT KEPPEL BAY")
r = D(PH_RAW, "", CA, 24, "Pearl Bank")
check("'Pearl Bank' (2 words, no street) -> ONE PEARL BANK",
      r["matched_project"] == "ONE PEARL BANK" and r["project_match"] == "close")
r = D(PH_RAW, "", CA, 24, "One Pearl Bank")
check("'One Pearl Bank' -> exact", r["project_match"] == "exact")
r = D(PH_RAW, "", CA, 24, "the sail at marina bay")
check("'the sail at marina bay' -> exact ('@' = 'at')", r["project_match"] == "exact" and len(r["records"]) == 2)
r = D(PH_RAW, "", CA, 24, "Marina Bay")
check("'Marina Bay' fits 3 developments -> none chosen (ambiguous)",
      r["project_match"] == "ambiguous" and r["records"] == [] and r["matched_on"] == "")
r = D(PH_RAW, "Marina Boulevard", CA, 24, "Marina Bay")
check("ambiguous + street given -> street sales, honestly labelled", r["matched_on"] == "street" and r["matched_project"] == "")
r = D(PH_RAW, "", CA, 24, "Marina Bay Residences")
check("'Marina Bay Residences' never matches 'Marina Bay Suites'",
      r["matched_project"] == "MARINA BAY RESIDENCES" and [x["price"] for x in r["records"]] == [4000000.0])
r = D(PH_RAW, "", CA, 24, "Parc")
check("'Parc' alone (many Parc projects) -> no match", r["records"] == [] and r["project_match"] in ("", "ambiguous"))
r = D(PH_RAW, "Sims Avenue", CA, 24, "Parc")
check("'Parc' + street Sims Avenue -> the one Parc on that street", r["matched_project"] == "PARC ESTA")
r = D(PH_RAW, "Jalan Lempeng", CA, 24, "Parc Esta")
check("exact name wins even when the street typed is different", r["matched_project"] == "PARC ESTA")
r = D(PH_RAW, "Sims Avenue", CA, 24, "Sail")
check("close match on a DIFFERENT street than the agent's -> rejected, street used",
      r["matched_on"] == "street" and r["matched_project"] == "")
for short in ("One", "8", "The"):
    check(f"too-short name {short!r} never close-matches", not D(PH_RAW, "", CA, 24, short)["records"])
r = D(PH_RAW, "", CA, 24, "Piermont Grand")
check("EC entered as Condominium: the development's EC sales used, with a note",
      r["matched_on"] == "project" and [x["property_type"] for x in r["records"]] == ["Executive Condominium"]
      and "Executive Condominium" in r["type_note"])
r = D(PH_RAW, "Sumang Walk", CA, 24, "Old Tower")
check("named development with no sales in window -> street fallback",
      r["matched_on"] == "street" and [x["price"] for x in r["records"]] == [1450000.0])
r = D(PH_RAW, "", CA, 24, "Old Tower")
check("...and with no street -> empty, not everything", r["records"] == [] and r["matched_on"] == "")
check("2-value wrapper still returns (records, matched_on)",
      NEW._extract_strata_comparables(PH_RAW, "Marina Boulevard", CA, 24, "The Sail")[1] == "project")
check("_is_close_project guards", not NEW._is_close_project((), ("a",)) and not NEW._is_close_project(("12",), ("12", "x"))
      and not NEW._is_close_project(("riviere", "tower"), ("riviere",)))

# ---------------------------------------------------------------------------
# main.py, executed from source.
# ---------------------------------------------------------------------------
MAIN = os.path.join(BACKEND, "main.py")
SRC = io.open(MAIN, encoding="utf-8").read()
LINES = SRC.splitlines()
NODES = {}
for n in ast.parse(SRC).body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        NODES[n.name] = n
    elif isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                NODES[t.id] = n


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        self.messages.append(record.getMessage())


LOG = logging.getLogger("condo-fixes-test")
CAP = _Capture()
LOG.addHandler(CAP)
LOG.propagate = False
LOG.setLevel(logging.DEBUG)

NS = {"re": re, "datetime": datetime, "logger": LOG, "ura_market_pulse": NEW, "types": types,
      "math": math, "os": os, "time": time, "fcntl": __import__("fcntl"), "asyncio": asyncio}


def run_nodes(*names):
    for name in names:
        n = NODES[name]
        code = "\n" * (n.lineno - 1) + "\n".join(LINES[n.lineno - 1:n.end_lineno])
        exec(compile(code, MAIN, "exec"), NS)


def run_span(first, last):
    a, b = NODES[first].lineno, NODES[last].end_lineno
    code = "\n" * (a - 1) + "\n".join(LINES[a - 1:b])
    exec(compile(code, MAIN, "exec"), NS)


run_nodes("_HOUSE_NUMBER_PREFIX_RE", "_UNIT_NUMBER_RE", "_STREET_SUFFIXES", "_ADDRESS_NUMBER_RE",
          "_NON_STREET_WORDS", "_address_number_hits", "_strip_address_numbers", "_to_number",
          "_format_price_millions", "_price_leak_strings", "_strip_price_leaks",
          "apply_listing_copy_guards", "_STRAY_STREET_RE", "_strip_stray_street_names")
run_span("_fmt_writeup_num", "_build_writeup_facts_block")
run_nodes("_basic_listing_fallback", "_normalize_district", "_extract_district_token",
          "_listing_match_district", "_buyer_match_districts", "_compute_buyer_listing_match",
          "_listing_district_label", "_clean_property_types", "_listing_property_types",
          "_pg_export_price", "_pg_export_count", "_pg_export_measure", "_build_propertyguru_text_block",
          "_listing_attributes", "_listing_strata_fields", "NESTLIST_APP_HELP",
          "_last_alert_times", "_CROSS_WORKER_ALERT_KEYS", "_ALERT_MARKER_DIR",
          "_claim_alert_across_workers", "send_telegram_alert_throttled")

print("\n[7] unit-number pattern: audit false positives gone, real units still caught")
UR = NS["_UNIT_NUMBER_RE"]
for t in ["Districts 09-11", "district 09-11", "floor range (06-10)", "(01-05)", "open 09-11 am",
          "09-11pm", "renovated in 08-09", "10-100m", "lot 12-345", "Lot 12-345", "year 99-103",
          "MK 05-123", "from 05-10", "between 07-08", "floors 06-10", "10-100M budget", "12-345K"]:
    check(f"not a unit: {t!r}", not UR.search(t), UR.search(t))
for t in ["#12-03", "unit 12-03", "Unit 05-11", "05-11", "12-103", "high floor 12-03 unit", "#06-10",
          "unit 06-10", "corner unit 05-11A", "(05-11)", "12-103A", "apt 7-123", "#05-11A",
          "Pool view, 05-11, quiet", "Districts 09-11 near unit #12-03"]:
    check(f"unit caught: {t!r}", bool(UR.search(t)))
check("'Districts 09-11 near unit #12-03' keeps the district range, strips the unit",
      NS["_strip_address_numbers"]("Districts 09-11 near unit #12-03")[0].startswith("Districts 09-11"))

print("\n[8] copy guard logs a count, never the stripped values")
CAP.messages.clear()
out = NS["apply_listing_copy_guards"]("Lovely home at 12 Nassim Road, unit #05-11, quiet.", context="t")
check("unit + house number stripped", "05-11" not in out and "12 Nassim" not in out, out)
check("log says something was stripped", any("stripped 2 house/unit number(s)" in m for m in CAP.messages), CAP.messages)
check("log never carries the values", not any(re.search(r"05-11|\b12\b", m) for m in CAP.messages), CAP.messages)
CAP.messages.clear()
NS["_strip_stray_street_names"]("Steps from Orchard Road and more.", context="t")
check("stray-street log redacted too", CAP.messages and not any("Orchard" in m for m in CAP.messages), CAP.messages)

print("\n[2] buyer matching: stored district, tenure preference")
M = NS["_compute_buyer_listing_match"]
L = lambda **k: dict({"property_type": "Semi-Detached", "location": "12 Lorong Chuan", "price": "3000000"}, **k)
check("stored district matches (address has no district)",
      M({"districts": "D15"}, L(district="15"))["is_match"])
check("stored district wins over a different district in the address",
      not M({"districts": "D10"}, L(district="15", location="District 10 Road"))["is_match"]
      and M({"districts": "D15"}, L(district="15", location="District 10 Road"))["is_match"])
check("no stored district -> address fallback still works ('District 09' = D9)",
      M({"districts": "D9"}, L(location="Somewhere, District 09"))["is_match"])
check("no district anywhere -> district-filtered buyer not matched", not M({"districts": "D15"}, L())["is_match"])
check("buyer districts with spaces/zeros normalised", M({"districts": " D05 ,D15"}, L(district="5"))["is_match"])
check("reason names the district", "In preferred area (D15)" in M({"districts": "D15"}, L(district="15"))["reasons"])
FH = {"attributes": {"tenure": "Freehold"}}
LH99 = {"attributes": {"tenure": "99-year leasehold"}}
LH999 = {"attributes": {"tenure": "999-year leasehold"}}
OTHER = {"attributes": {"tenure": "Other", "tenure_detail": "103-year leasehold"}}
for pref, listing, match, unconfirmed in [
        ("Freehold", FH, True, False), ("Freehold", LH99, False, False), ("Freehold", LH999, False, False),
        ("999-year Leasehold", LH999, True, False), ("999-year Leasehold", FH, True, False),
        ("999-year Leasehold", LH99, False, False), ("99-year Leasehold", LH99, True, False),
        ("99-year Leasehold", FH, True, False), ("Freehold", {}, True, True), ("Freehold", OTHER, True, True),
        ("Freehold", {"tenure": "freehold"}, True, False)]:
    res = M({"tenure_pref": pref}, L(**listing))
    check(f"tenure pref {pref!r} vs {listing or 'no tenure'} -> match={match}, unconfirmed={unconfirmed}",
          res["is_match"] == match and (not match or res["tenure_unconfirmed"] == unconfirmed), res)
for pref in ("", "Any", "any", None, "whatever"):
    res = M({"tenure_pref": pref, "districts": "D15"}, L(district="15", **LH99))
    check(f"tenure pref {pref!r} = no filter", res["is_match"] and not res["tenure_unconfirmed"]
          and not any("Tenure" in x for x in res["reasons"]), res)
check("tenure + other criteria combine (wrong district still excluded)",
      not M({"tenure_pref": "Freehold", "districts": "D10"}, L(district="15", **FH))["is_match"])
check("condo listing tenure works the same", M({"tenure_pref": "Freehold"},
      L(property_type="Condominium", **FH))["reasons"] == ["Tenure: Freehold"])
check("zero-preference buyer still matches nothing", not M({}, L(**FH))["is_match"])


# endpoint ordering (temperature first, then confirmed tenure first)
class _Q:
    def __init__(self, rows):
        self.rows = rows

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.rows)


class _DB:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Q(self.tables[name])


BUYERS = [{"id": "b1", "name": "Unconfirmed", "tenure_pref": "Freehold", "temperature": "HOT"},
          {"id": "b2", "name": "Confirmed", "tenure_pref": "Freehold", "temperature": "HOT"},
          {"id": "b3", "name": "Warm", "tenure_pref": "Freehold", "temperature": "WARM"}]
NS.update({"HTTPException": Exception, "Depends": lambda x: None, "get_current_agent": None, "app": types.SimpleNamespace(
    get=lambda *a, **k: (lambda f: f))})


run_nodes("get_listing_matches", "get_buyer_matches")
NS["get_db"] = lambda: _DB({"listings": [L(id="L1", **FH)], "buyers": BUYERS})
lm = NS["get_listing_matches"]("L1", agent={"id": "a1"})
check("listing matches carry tenure_unconfirmed", all("tenure_unconfirmed" in m for m in lm) and len(lm) == 3)
NS["get_db"] = lambda: _DB({"listings": [L(id="L1")], "buyers": BUYERS})
lm = NS["get_listing_matches"]("L1", agent={"id": "a1"})
check("no tenure on listing: all kept, all flagged, temperature order kept",
      [m["name"] for m in lm] == ["Unconfirmed", "Confirmed", "Warm"] and all(m["tenure_unconfirmed"] for m in lm))
NS["get_db"] = lambda: _DB({"listings": [L(id="L1", district="15")], "buyers": [
    {"id": "x1", "name": "NeedsCheck", "tenure_pref": "Freehold", "districts": "D15", "temperature": "HOT"},
    {"id": "x2", "name": "Clean", "districts": "D15", "temperature": "HOT"},
    {"id": "x3", "name": "Cold", "districts": "D15", "temperature": "COLD"}]})
lm = NS["get_listing_matches"]("L1", agent={"id": "a1"})
check("listing view: same temperature -> buyer with nothing to check first; temperature still leads",
      [m["name"] for m in lm] == ["Clean", "NeedsCheck", "Cold"], [m["name"] for m in lm])
LISTINGS = [L(id="L-none", district="15"), L(id="L-fh", district="15", **FH), L(id="L-99", district="15", **LH99)]
NS["get_db"] = lambda: _DB({"listings": LISTINGS, "buyers": [{"id": "b9", "name": "B", "tenure_pref": "Freehold"}]})
bm = NS["get_buyer_matches"]("b9", agent={"id": "a1"})
check("buyer view: 99-year excluded, confirmed freehold ranked above unconfirmed",
      [m["listing_id"] for m in bm] == ["L-fh", "L-none"] and [m["tenure_unconfirmed"] for m in bm] == [False, True], bm)

print("\n[5] help text no longer says landed-only")
help_text = NS["NESTLIST_APP_HELP"]
check("help: no 'landed properties only'", "landed properties only" not in help_text)
check("help: names condos, ECs, apartments, penthouses", all(w in help_text for w in
      ("condominiums", "executive condominiums", "apartments", "penthouses")))

print("\n[9] Telegram alert dedupe across workers")
SENT = []


async def fake_send(message, chat_id=None):
    SENT.append(message)

NS["send_telegram_alert"] = fake_send
tmp = tempfile.mkdtemp(prefix="nl-alert-test-")
NS["_ALERT_MARKER_DIR"] = tmp


def worker_send(key, cooldown=600):
    NS["_last_alert_times"].clear()       # a different worker: its own empty memory
    asyncio.run(NS["send_telegram_alert_throttled"](key, "URA down", cooldown_seconds=cooldown))


for _ in range(4):
    worker_send("ura_cma_fetch_failed")
check("4 workers, one URA outage -> ONE alert", len(SENT) == 1, len(SENT))
marker = os.path.join(tmp, "nestlist_alert_ura_cma_fetch_failed.marker")
with open(marker, "w") as fh:
    fh.write(str(time.time() - 700))
worker_send("ura_cma_fetch_failed")
check("after the cooldown the next worker alerts again", len(SENT) == 2)
SENT.clear()
for _ in range(3):
    worker_send("jwt_invalid")
check("other alert keys keep the per-worker behaviour (unchanged)", len(SENT) == 3)
SENT.clear()
NS["_ALERT_MARKER_DIR"] = os.path.join(tmp, "does-not-exist")
worker_send("ura_cma_fetch_failed")
check("marker unusable -> alert still sent (never swallowed)", len(SENT) == 1)
NS["_ALERT_MARKER_DIR"] = tmp
with open(marker, "w") as fh:
    fh.write("garbage")
SENT.clear()
worker_send("ura_cma_fetch_failed")
check("corrupt marker -> treated as no previous alert", len(SENT) == 1)
for offset, sends, label in ((0.0005, 0, "a sibling's claim a hair in the future (rounding) -> quiet"),
                             (86400, 1, "a marker far in the future (clock jump) -> alert, never swallowed")):
    with open(marker, "w") as fh:
        fh.write(f"{time.time() + offset:.3f}")
    SENT.clear()
    worker_send("ura_cma_fetch_failed")
    check(label, len(SENT) == sends, len(SENT))
import fcntl  # noqa: E402
hold = open(marker, "a+")
fcntl.flock(hold.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
with open(marker, "w") as fh:
    pass
SENT.clear()
worker_send("ura_cma_fetch_failed")
check("a sibling mid-claim (lock busy) -> this worker stays quiet, never blocks", len(SENT) == 0)
hold.close()

print("\n[10] Studio: bedrooms 0 on a condo-family listing")
run_nodes("_is_condo_family_only", "_is_studio", "_listing_is_studio", "_req_is_studio", "_bedrooms_display",
          "_TENURE_RANK", "_TENURE_PREF_NONE", "_tenure_pref_rank", "_listing_tenure", "_listing_tenure_text")


def req(**kw):
    base = dict(property_type="", bedrooms="", bathrooms="", storeys=0, built_up=0, land_size=0,
                features="", tenure="", tenure_detail="", floor_level="", project_name="", price="")
    base.update(kw)
    return types.SimpleNamespace(**base)


fb = NS["_build_writeup_facts_block"](req(property_type="Condominium", bedrooms="0", bathrooms="1"), "District 9")
check("condo FACTS: 'Bedrooms: Studio', never 0", "- Bedrooms: Studio" in fb and "Bedrooms: 0" not in fb, fb)
fb = NS["_build_writeup_facts_block"](req(property_type="Semi-Detached", bedrooms="0"), "District 9")
check("landed FACTS with 0: unchanged ('Bedrooms: 0')", "- Bedrooms: 0" in fb)
fb = NS["_build_writeup_facts_block"](req(property_type="Condominium", bedrooms="2"), "District 9")
check("condo with 2 bedrooms: unchanged", "- Bedrooms: 2" in fb)
mixed = req(property_type="Condominium", bedrooms="0", property_types=["Condominium", "Penthouse"])
check("condo + penthouse listing is not condo-only -> not Studio", not NS["_req_is_studio"](mixed))
AG = {"name": "J", "agency": "NL"}
fbk = NS["_basic_listing_fallback"](AG, req(property_type="Apartment", bedrooms="0", bathrooms="1"), "District 9")
check("fallback copy: 'a studio layout', no '0 bedrooms'", "studio layout" in fbk and "0 bedrooms" not in fbk, fbk)
fbk = NS["_basic_listing_fallback"](AG, req(property_type="Inter-Terrace", bedrooms="0"), "District 9")
check("fallback copy landed with 0: unchanged", "0 bedrooms" in fbk)
pg = NS["_build_propertyguru_text_block"]({"property_type": "Condominium", "bedrooms": "0", "location": "x"})
check("PropertyGuru export: 'Bedrooms: Studio'", "Bedrooms: Studio" in pg, pg)
pg = NS["_build_propertyguru_text_block"]({"property_type": "Detached/Bungalow", "bedrooms": "0", "location": "x"})
check("PropertyGuru export landed 0: still skipped", "Bedrooms" not in pg)
check("help-bot listing context: Studio / raw value",
      NS["_bedrooms_display"]({"property_type": "Executive Condominium (EC)", "bedrooms": "0"}) == "Studio"
      and NS["_bedrooms_display"]({"property_type": "GCB", "bedrooms": "0"}) == "0"
      and NS["_bedrooms_display"]({"property_type": "Condominium", "bedrooms": None}) == "")
check("'Studio' typed as a word also counts; junk never raises",
      NS["_is_studio"]("studio", "Condominium") and not NS["_is_studio"]("abc", "Condominium")
      and not NS["_is_studio"]("0", "") and not NS["_is_studio"]("0.5", "Condominium"))
check("help-bot tenure text from attributes",
      NS["_listing_tenure_text"](OTHER) == "103-year leasehold" and NS["_listing_tenure_text"](FH) == "Freehold"
      and NS["_listing_tenure_text"]({}) == "")
# Poster + video stat lines: the same one-expression form in both builders.
stat_lines = [ln for ln in LINES if '"Studio" if _listing_is_studio(listing) else (f"{bedrooms_val} Rooms"' in ln]
check("poster AND video stats use the Studio rule (2 builders)", len(stat_lines) == 2)
check("no '0 Rooms' path left for a studio: helper says Studio for condo 0",
      NS["_listing_is_studio"]({"property_type": "Condominium", "bedrooms": "0"})
      and not NS["_listing_is_studio"]({"property_type": "Corner Terrace", "bedrooms": "0"}))

print("\n[4b] CMA endpoint: Penthouse sends its project name")
CALLS = []


async def fake_gen(street, ptype, land, win, project_name="", built_up_sqft=0):
    CALLS.append((street, ptype, land, win, project_name, built_up_sqft))
    return {"ok": True}

from pydantic import BaseModel, Field  # noqa: E402
NS.update({"BaseModel": BaseModel, "Field": Field})
run_nodes("CMARequest", "_clean_project_name", "generate_cma_report")
NS["ura_market_pulse"] = types.SimpleNamespace(cma_strata_types=NEW.cma_strata_types, generate_cma=fake_gen)
NS["app"] = types.SimpleNamespace(post=lambda *a, **k: (lambda f: f))
asyncio.run(NS["generate_cma_report"](NS["CMARequest"](street="", property_type="Penthouse",
                                                        project_name="The Sail #66-01", built_up=4000), agent=None))
check("Penthouse: project name (unit stripped) + built-up passed through",
      CALLS[-1] == ("", "Penthouse", 0, 24, "The Sail", 4000), CALLS[-1])
asyncio.run(NS["generate_cma_report"](NS["CMARequest"](street="Nassim Road", property_type="Semi-Detached",
                                                        project_name="x", land_size=3000), agent=None))
check("landed: unchanged arguments", CALLS[-1] == ("Nassim Road", "Semi-Detached", 3000, 24, "", 0))

# ---------------------------------------------------------------------------
if COVER:
    sys.settrace(None)
    diff = subprocess.run(["git", "diff", "-U0", "97549be", "--", "backend/main.py", "backend/ura_market_pulse.py"],
                          cwd=os.path.dirname(BACKEND), capture_output=True, text=True).stdout
    changed, cur = {}, None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            cur = os.path.join(os.path.dirname(BACKEND), line[6:])
        m = re.match(r"@@ -\S+ \+(\d+)(?:,(\d+))? @@", line)
        if m and cur:
            start, count = int(m.group(1)), int(m.group(2) or 1)
            changed.setdefault(cur, set()).update(range(start, start + count))
    print("\n[coverage] changed executable lines not executed by this test:")
    for path, lines in changed.items():
        src = io.open(path, encoding="utf-8").read().splitlines()
        missed = []
        for ln in sorted(lines):
            text = src[ln - 1].strip() if ln - 1 < len(src) else ""
            if not text or text.startswith(("#", '"""', ")", "]", "}")) or text.endswith(('"""',)):
                continue
            if (path, ln) not in EXECUTED:
                missed.append(ln)
        print(f"  {os.path.basename(path)}: {len(missed)} -> {missed}")

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
