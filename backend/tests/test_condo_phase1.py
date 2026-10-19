"""NestList Home, Phase 1 -- condo family (Condominium / Executive Condominium (EC) /
Apartment). Run:  python3 tests/test_condo_phase1.py   (exit 1 on any failure)

What is being protected:

1. LANDED CMA IS BYTE-FOR-BYTE UNCHANGED. The comparables + stats for every landed type
   (and blank / unknown / Penthouse, which stay on the landed path) are compared against
   tests/golden/ura_market_pulse_pre_condo.py -- a frozen copy of the module as it was
   before this change -- on a fixture full of condo rows that the landed path must ignore.
   Also on the SLIMMED cache copy, which must give the identical answer.
2. Condo comps: Strata rows only, condo+apartment pooled, EC kept separate, bulk sales
   dropped, project match preferred over street, no unit numbers, built-up PSF valuation.
3. The URA cache: 50 concurrent CMA requests cause ONE fetch; an outage serves the last
   complete copy marked stale (or a clean error with no copy); a partial fetch is flagged
   and never cached; the fetch has a hard time budget.
4. Every CMA / Market Pulse response carries the Open Data Licence source notice.
5. main.py condo helpers, the condo write-up prompt branch, the text-only fallback (no
   "Land size: 0 sqft" for a condo), the PropertyGuru export, and that the project name
   never reaches a buyer-facing field or the model.

main.py cannot be imported here (no supabase/anthropic on this machine), so its
functions are EXECUTED from source against stubs -- compiled with main.py's own file name
and line numbers, so a line tracer sees them as main.py lines.
"""
import ast
import asyncio
import importlib.util
import io
import logging
import os
import re
import sys
import time
import types
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, BACKEND)

import ura_market_pulse as NEW  # noqa: E402
import video_renderer as VR     # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "ura_pre_condo", os.path.join(_HERE, "golden", "ura_market_pulse_pre_condo.py"))
OLD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(OLD)

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + str(detail)[:300]) if detail else ""))


# ---------------------------------------------------------------------------
# Fixture: URA-shaped raw projects, dated relative to today so windows stay valid.
# ---------------------------------------------------------------------------
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
            "floorRange": "-", "typeOfSale": "3", "noOfUnits": "1",
            "marketSegment": "CCR", "nettPrice": ""}
    base.update(kw)
    return base


RAW = [
    {"project": "LANDED HOUSING DEVELOPMENT", "street": "NASSIM ROAD", "x": "y", "marketSegment": "CCR",
     "transaction": [
         txn(),
         txn(),  # exact duplicate -> de-duped by both versions
         txn(contractDate=mmyy(14), price="12000000", area="1700"),
         txn(contractDate=mmyy(30), price="9000000"),              # outside 24m
         txn(propertyType="Semi-Detached", price="6000000", area="600"),
         txn(propertyType="Semi-detached", price="6100000", area="610"),
         txn(propertyType="Terrace", price="4000000", area="300", contractDate=mmyy(11)),
         txn(propertyType="Detached", price="abc"),                 # junk price
         txn(propertyType="Detached", area="0"),                    # zero area
         txn(propertyType="Detached", contractDate="bad"),
     ]},
    {"project": "THE NASSIM", "street": "CUSCADEN WALK", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="5000000", area="250",
            floorRange="06-10", typeOfSale="3", tenure="Freehold", contractDate=mmyy(1)),
        txn(typeOfArea="Strata", propertyType="Condominium", price="5200000", area="260",
            floorRange="11-15", typeOfSale="1", contractDate=mmyy(13)),
        txn(typeOfArea="Strata", propertyType="Apartment", price="2500000", area="120",
            floorRange="01-05", typeOfSale="2", contractDate=mmyy(3)),
        txn(typeOfArea="Strata", propertyType="Condominium", price="30000000", area="1500",
            noOfUnits="6"),                                         # bulk deal -> dropped
        txn(typeOfArea="Strata", propertyType="Strata Detached", price="9000000", area="500"),
        txn(typeOfArea="Strata", propertyType="Condominium", price="4000000", area="200",
            contractDate=mmyy(40)),                                 # outside window
    ]},
    {"project": "PARC CANBERRA", "street": "CANBERRA DRIVE", "transaction": [
        txn(typeOfArea="Strata", propertyType="Executive Condominium", price="1300000",
            area="110", floorRange="06-10", tenure="99 yrs lease commencing from 2018",
            district="27", contractDate=mmyy(4)),
        txn(typeOfArea="Strata", propertyType="Condominium", price="1600000",
            area="100", district="27", contractDate=mmyy(5)),
    ]},
    {"project": "NASSIM PARK RESIDENCES", "street": "NASSIM ROAD", "transaction": [
        txn(typeOfArea="Strata", propertyType="Condominium", price="7000000", area="300",
            floorRange="01-05", contractDate=mmyy(6)),
    ]},
]

LANDED_CASES = ["Good Class Bungalow (GCB)", "Detached/Bungalow", "Semi-Detached",
                "Inter-Terrace", "Corner Terrace", "Penthouse", "", "Something Unknown"]

print("\n[1] landed CMA: identical to the pre-change module, raw and slimmed")
SLIM = NEW._slim_projects(RAW)
for ptype in LANDED_CASES:
    for street in ("Nassim Road", "nassim", "9 Nassim Road #01-01"):
        for win in (12, 24, 60):
            old_recs = OLD.extract_comparable_transactions(RAW, street, ptype, win)
            new_recs = NEW.extract_comparable_transactions(RAW, street, ptype, win)
            slim_recs = NEW.extract_comparable_transactions(SLIM, street, ptype, win)
            ok = old_recs == new_recs == slim_recs
            o = OLD.compute_cma_stats(old_recs, 5000)
            n = NEW.compute_cma_stats(new_recs, 5000)
            if not (ok and o == n):
                check(f"landed {ptype!r} {street!r} {win}m", False, f"{old_recs} vs {new_recs}")
                break
    else:
        check(f"landed {ptype!r}: records + stats identical (3 streets x 3 windows, raw + slim)", True)
check("landed path never returns a Strata row",
      all(r["area_sqft"] != round(250 * NEW.SQM_TO_SQFT)
          for r in NEW.extract_comparable_transactions(RAW, "Nassim", "", 60)))
check("landed records have exactly the pre-change keys",
      set(NEW.extract_comparable_transactions(RAW, "Nassim Road", "", 24)[0])
      == {"street", "price", "area_sqft", "psf", "property_type", "contract_date", "district", "tenure"})
check("Market Pulse GCB stats identical on raw vs slim input (beyond refreshed_at)",
      {k: v for k, v in (NEW.compute_market_pulse_stats(RAW) or {}).items() if k != "refreshed_at"}
      == {k: v for k, v in (OLD.compute_market_pulse_stats(RAW) or {}).items() if k != "refreshed_at"})

print("\n[2] condo-family type detection (unknowns never crash, never go strata)")
for label, expect in [("Condominium", ("Condominium", "Apartment")),
                      ("Executive Condominium (EC)", ("Executive Condominium",)),
                      ("Apartment", ("Apartment", "Condominium")),
                      ("condo", ("Condominium", "Apartment")), ("EC", ("Executive Condominium",)),
                      ("Walk-up apartment", ("Apartment", "Condominium")),
                      ("Penthouse", ()), ("Semi-Detached", ()), ("", ()), (None, ()), (123, ()),
                      ("Inter-Terrace", ()), ("Good Class Bungalow (GCB)", ())]:
    got = NEW.strata_ura_types(label)
    check(f"strata_ura_types({label!r}) -> {expect}", got == expect, got)

print("\n[3] condo comparables")
recs, on = NEW._extract_strata_comparables(RAW, "Cuscaden Walk", ("Condominium", "Apartment"), 24, "")
check("street match finds the condo + apartment sales, not EC/land/bulk/strata-landed/old",
      on == "street" and sorted(r["price"] for r in recs) == [2500000.0, 5000000.0, 5200000.0],
      [r["price"] for r in recs])
AREA_SQM = {5000000.0: 250, 5200000.0: 260, 2500000.0: 120}
check("built-up PSF = price / strata floor area",
      all(r["psf"] == round(r["price"] / (AREA_SQM[r["price"]] * NEW.SQM_TO_SQFT)) for r in recs))
check("no record carries a unit number field; floor is a range",
      all(set(r) == {"street", "project", "price", "area_sqft", "psf", "property_type",
                     "contract_date", "district", "tenure", "floor_range", "sale_type"} for r in recs)
      and all(not re.search(r"#\d", str(r.values())) for r in recs))
check("sale type labelled", {r["sale_type"] for r in recs} == {"New Sale", "Sub Sale", "Resale"})
check("newest first by real date", [r["contract_date"] for r in recs]
      == sorted([r["contract_date"] for r in recs], key=lambda d: (d[3:], d[:2]), reverse=True))
recs, on = NEW._extract_strata_comparables(RAW, "Nassim Road", ("Condominium", "Apartment"), 24,
                                           "Nassim Park Residences")
check("project name preferred over street", on == "project" and [r["price"] for r in recs] == [7000000.0])
recs, on = NEW._extract_strata_comparables(RAW, "Nassim Road", ("Condominium", "Apartment"), 24,
                                           "No Such Project")
check("unknown project falls back to street", on == "street" and [r["price"] for r in recs] == [7000000.0])
recs, on = NEW._extract_strata_comparables(RAW, "", ("Condominium", "Apartment"), 24, "")
check("nothing to match -> empty, not everything", recs == [] and on == "")
recs, on = NEW._extract_strata_comparables(RAW, "Canberra", ("Executive Condominium",), 24, "")
check("EC comps are EC only", [r["property_type"] for r in recs] == ["Executive Condominium"])
recs2, _ = NEW._extract_strata_comparables(SLIM, "Canberra", ("Executive Condominium",), 24, "")
check("strata extraction identical on slimmed cache", recs == recs2)


# ---------------------------------------------------------------------------
# Cache + generate_cma, with URA stubbed.
# ---------------------------------------------------------------------------
class FakeURA:
    def __init__(self, projects=RAW, fail=None, partial=False, delay=0.0):
        self.calls = 0
        self.projects, self.fail, self.partial, self.delay = projects, fail, partial, delay

    async def token(self, key):
        return "tok"

    async def fetch(self, key, token, batch_timeout=None):
        self.calls += 1
        self.batch_timeout = batch_timeout
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise self.fail
        report = [{"batch": b, "ok": not (self.partial and b == 3)} for b in (1, 2, 3, 4)]
        return list(self.projects), report


def reset_cache():
    NEW._cma_cache.update({"projects": None, "fetched_at": 0.0, "as_of": None, "last_failure_at": 0.0,
                           "partial_projects": None, "partial_at": 0.0, "partial_as_of": None})
    NEW._cma_lock = None
    NEW._cma_refresh_task = None


async def drain_background():
    while NEW._BACKGROUND:
        await asyncio.gather(*list(NEW._BACKGROUND), return_exceptions=True)


def install(fake):
    NEW.get_token = fake.token
    NEW.fetch_all_transactions = fake.fetch


os.environ["URA_ACCESS_KEY"] = "test-placeholder-not-a-real-key"
_REAL_TOKEN, _REAL_FETCH = NEW.get_token, NEW.fetch_all_transactions

print("\n[4] generate_cma: landed vs condo valuation, source notice")


async def scenario_values():
    reset_cache()
    install(FakeURA())
    landed = await NEW.generate_cma("Nassim Road", "Detached/Bungalow", 5000, 24)
    old_core = OLD.compute_cma_stats(OLD.extract_comparable_transactions(RAW, "Nassim Road", "Detached/Bungalow", 24), 5000)
    check("landed core stats identical to pre-change", {k: landed[k] for k in old_core} == old_core)
    check("landed valued on land PSF x land size",
          landed["valuation_basis"] == "land_psf" and landed["estimated_value_low"] == round(5000 * landed["min_psf"]))
    condo = await NEW.generate_cma("Cuscaden Walk", "Condominium", 5000, 24, built_up_sqft=1200)
    check("condo valued on built-up PSF x built-up (land size ignored)",
          condo["valuation_basis"] == "built_up_psf"
          and condo["estimated_value_low"] == round(1200 * condo["min_psf"])
          and condo["estimated_value_high"] == round(1200 * condo["max_psf"]), condo)
    condo0 = await NEW.generate_cma("Cuscaden Walk", "Condominium", 5000, 24)
    check("condo with no built-up -> comps but no estimate (never land x condo PSF)",
          condo0["comparable_count"] == 3 and condo0["estimated_value_low"] == 0)
    for r in (landed, condo):
        check(f"source notice on {r['valuation_basis']} response",
              r["source_attribution"].startswith("Contains information from the URA Data Service")
              and r["source_licence_url"] == "https://data.gov.sg/open-data-licence"
              and r["source_disclaimer"] == "Indicative only; verify with official records."
              and "endorse" not in r["source_attribution"].lower())
    check("fresh data not flagged", not condo["data_stale"] and not condo["data_partial"] and condo["data_as_of"])

asyncio.run(scenario_values())

print("\n[5] URA cache: single-flight, stale-while-revalidate, outage, per-batch budget")
ALERTS = []


async def collect_alert(msg):
    ALERTS.append(msg)
NEW.set_failure_alert(collect_alert)


async def scenario_single_flight():
    reset_cache()
    fake = FakeURA(delay=0.2)
    install(fake)
    results = await asyncio.gather(*[NEW.generate_cma("Nassim Road", "", 0, 24) for _ in range(50)])
    check("cold worker: 50 concurrent CMAs -> 1 URA fetch", fake.calls == 1, fake.calls)
    check("...and all 50 got the same answer", all(r["comparables"] == results[0]["comparables"] for r in results))
    check("CMA pulls use the per-batch budget, not an overall cap",
          fake.batch_timeout == NEW.CMA_BATCH_BUDGET_SECONDS and not hasattr(NEW, "CMA_FETCH_BUDGET_SECONDS"))
    await NEW.generate_cma("Cuscaden Walk", "Condominium", 0, 24)
    check("a later request inside the TTL fetches nothing", fake.calls == 1)

asyncio.run(scenario_single_flight())


async def scenario_swr():
    reset_cache()
    install(FakeURA())
    await NEW.generate_cma("Nassim Road", "", 0, 24)
    NEW._cma_cache["fetched_at"] -= NEW.CMA_CACHE_TTL_SECONDS + 1     # expired, < 7 days
    slow = FakeURA(delay=1.0)
    install(slow)
    t0 = time.time()
    rs = await asyncio.gather(*[NEW.generate_cma("Nassim Road", "", 0, 24) for _ in range(50)])
    took = time.time() - t0
    check("expired copy: 50 requests answered at once from the old copy, marked stale",
          took < 0.5 and all(r["data_stale"] and r["comparable_count"] > 0 for r in rs), round(took, 2))
    await drain_background()
    check("...while exactly ONE background refresh ran", slow.calls == 1, slow.calls)
    r = await NEW.generate_cma("Nassim Road", "", 0, 24)
    check("...and the next request gets the fresh copy", not r["data_stale"] and slow.calls == 1)

asyncio.run(scenario_swr())


async def scenario_outage():
    reset_cache()
    ALERTS.clear()
    install(FakeURA())
    await NEW.generate_cma("Nassim Road", "", 0, 24)
    NEW._cma_cache["fetched_at"] -= NEW.CMA_CACHE_TTL_SECONDS + 1
    down = FakeURA(fail=RuntimeError("URA down"))
    install(down)
    r = await NEW.generate_cma("Nassim Road", "", 0, 24)
    await drain_background()
    check("URA down + older complete copy -> served, marked stale", r["data_stale"] and r["comparable_count"] > 0)
    for _ in range(5):
        await NEW.generate_cma("Nassim Road", "", 0, 24)
    await drain_background()
    check("cooldown: an outage is not re-hit on every click", down.calls == 1, down.calls)
    check("the outage was alerted (Telegram hook)", len(ALERTS) == 1 and "URA" in ALERTS[0], ALERTS)
    reset_cache()
    install(FakeURA(fail=RuntimeError("URA down")))
    try:
        await NEW.generate_cma("Nassim Road", "", 0, 24)
        check("URA down + no copy -> clean RuntimeError", False)
    except RuntimeError as e:
        check("URA down + no copy -> clean RuntimeError (agent-readable)", "URA" in str(e), e)
    reset_cache()
    install(FakeURA())
    await NEW.generate_cma("Nassim Road", "", 0, 24)
    NEW._cma_cache["fetched_at"] -= NEW.CMA_MAX_STALE_SECONDS + 1     # far too old
    install(FakeURA(fail=RuntimeError("URA down")))
    try:
        await NEW.generate_cma("Nassim Road", "", 0, 24)
        check("copy older than the stale limit is NOT served", False)
    except RuntimeError:
        check("copy older than the stale limit is NOT served", True)

asyncio.run(scenario_outage())


async def scenario_partial():
    reset_cache()
    fake = FakeURA(partial=True)
    install(fake)
    r = await NEW.generate_cma("Nassim Road", "", 0, 24)
    check("partial pull with no copy -> answered, flagged partial", r["data_partial"] and not r["data_stale"])
    check("partial pull never becomes the 12-hour copy", NEW._cma_cache["projects"] is None)
    r2 = await NEW.generate_cma("Nassim Road", "", 0, 24)
    check("within the cooldown the partial copy is served again, no re-fetch",
          r2["data_partial"] and fake.calls == 1, fake.calls)

asyncio.run(scenario_partial())


async def scenario_batch_budget():
    """The REAL fetch_all_transactions, with batch 3 hanging: the other three must be kept."""
    reset_cache()
    NEW.fetch_all_transactions = _REAL_FETCH
    NEW.get_token = FakeURA().token
    saved_retry, saved_budget = NEW._get_json_with_retry, NEW.CMA_BATCH_BUDGET_SECONDS

    async def fake_get(client, url, **kw):
        b = kw["params"]["batch"]
        if b == 3:
            await asyncio.sleep(30)
        return {"Status": "Success", "Result": [RAW[0]] if b == 1 else [RAW[1]] if b == 2 else [RAW[3]]}
    NEW._get_json_with_retry = fake_get
    NEW.CMA_BATCH_BUDGET_SECONDS = 0.3
    t0 = time.time()
    r = await NEW.generate_cma("Nassim Road", "", 0, 24)
    check("a hanging batch is cut at its own budget; the pull returns promptly",
          time.time() - t0 < 3, round(time.time() - t0, 2))
    check("...the three finished batches are kept and served, flagged partial",
          r["data_partial"] and r["comparable_count"] > 0 and NEW._cma_cache["projects"] is None)
    projects, report = await _REAL_FETCH("k", "t")   # Market Pulse path: no batch budget
    NEW._get_json_with_retry, NEW.CMA_BATCH_BUDGET_SECONDS = saved_retry, saved_budget
    check("Market Pulse pull (no batch_timeout) keeps the original no-ceiling behaviour",
          all(b["ok"] for b in report) and len(report) == 4)

NEW.CMA_BATCH_BUDGET_SECONDS, _saved_budget = NEW.CMA_BATCH_BUDGET_SECONDS, NEW.CMA_BATCH_BUDGET_SECONDS
asyncio.run(scenario_batch_budget())
NEW.CMA_BATCH_BUDGET_SECONDS = _saved_budget

print("\n[5b] URA token is cached by the SINGAPORE day")
from datetime import timezone, timedelta  # noqa: E402
check("_sgt_today is UTC+8", NEW._sgt_today() == datetime.now(timezone(timedelta(hours=8))).date())
NEW._token_cache.update({"token": "cached-token", "fetched_on": NEW._sgt_today()})
check("same SGT day -> cached token, no network", asyncio.run(_REAL_TOKEN("k")) == "cached-token")
NEW._token_cache.update({"token": None, "fetched_on": None})


async def scenario_seed_from_pulse():
    reset_cache()
    fake = FakeURA()
    install(fake)
    res = await NEW.refresh_market_pulse()
    check("complete Market Pulse refresh seeds the CMA cache", NEW._cma_cache["projects"] is not None and res["batches_ok"] == 4)
    await NEW.generate_cma("Nassim Road", "", 0, 24)
    check("...so the next CMA does not fetch again", fake.calls == 1, fake.calls)
    reset_cache()
    install(FakeURA(partial=True))
    await NEW.refresh_market_pulse()
    check("a partial Market Pulse refresh does not seed it", NEW._cma_cache["projects"] is None)

asyncio.run(scenario_seed_from_pulse())
NEW.get_token, NEW.fetch_all_transactions = _REAL_TOKEN, _REAL_FETCH
reset_cache()

# ---------------------------------------------------------------------------
# main.py, executed from source.
# ---------------------------------------------------------------------------
MAIN = os.path.join(BACKEND, "main.py")
SRC = io.open(MAIN, encoding="utf-8").read()
LINES = SRC.splitlines()
TREE = ast.parse(SRC)
NODES = {}
for n in TREE.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        NODES[n.name] = n
    elif isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                NODES[t.id] = n

LOG = logging.getLogger("condo-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False

NS = {"re": re, "datetime": datetime, "logger": LOG, "ura_market_pulse": NEW, "Optional": None, "types": types}


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
          "apply_listing_copy_guards", "_guard_features")
run_span("_fmt_writeup_num", "_build_writeup_facts_block")
run_nodes("_strip_land_fields_for_strata", "_generate_listing_text_only", "_basic_listing_fallback",
          "_normalize_district", "_extract_district_token", "_listing_district_label",
          "_clean_property_types", "_listing_property_types", "_pg_export_price",
          "_pg_export_count", "_pg_export_measure", "_build_propertyguru_text_block")

print("\n[6] tenure / floor / project-name normalisation")
nt = NS["_normalize_tenure"]
for raw, exp in [("Freehold", ("Freehold", "")), ("FH", ("Freehold", "")),
                 ("99 yrs lease commencing from 2018", ("99-year leasehold", "")),
                 ("99-year leasehold", ("99-year leasehold", "")), ("999 years", ("999-year leasehold", "")),
                 ("103-year leasehold", ("Other", "103-year leasehold")), ("", ("", "")), (None, ("", ""))]:
    check(f"tenure {raw!r} -> {exp}", nt(raw) == exp, nt(raw))
check("floor band only", [NS["_normalize_floor_level"](x) for x in ("high", "Mid floor", "middle", "12", "#12-03", "")]
      == ["High", "Mid", "Mid", "", "", ""])
cp = NS["_clean_project_name"]
for raw, exp in [("The Sail @ Marina Bay #12-03", "The Sail @ Marina Bay"),
                 ("#05-11 Parc Esta", "Parc Esta"), ("Blk 12 The Interlace", "The Interlace"),
                 ("Unit 5-12 Reflections", "Reflections"), ("8 Saint Thomas", "8 Saint Thomas"),
                 ("26 Newton #03-01", "26 Newton"), ("8 Hullet", "8 Hullet"),
                 ("One Pearl Bank unit 12-03", "One Pearl Bank"), ("", ""), (None, "")]:
    check(f"project {raw!r} -> {exp!r}", cp(raw) == exp, cp(raw))
attrs = NS["_build_listing_attributes"](types.SimpleNamespace(
    project_name="Parc Esta #05-11", tenure="99 yrs", tenure_detail="", floor_level="high",
    top_year=2022, lease_start_year=1800))
check("attributes normalised, bad year dropped",
      attrs == {"project_name": "Parc Esta", "tenure": "99-year leasehold", "floor_level": "High", "top_year": 2022}, attrs)
check("landed / old request -> no attributes", NS["_build_listing_attributes"](types.SimpleNamespace(property_type="GCB")) == {})
check("strata fields always complete, blank for landed",
      NS["_listing_strata_fields"]({"attributes": None}) == {"project_name": "", "tenure": "", "tenure_detail": "",
                                                             "floor_level": "", "top_year": 0, "lease_start_year": 0})

print("\n[6b] unit-number pattern: catches units, not ordinary ranges")
UR = NS["_UNIT_NUMBER_RE"]
for t in ["#12-03", "# 12 - 03", "unit 12-03", "Unit no. 5-11", "apt 7-123", "12-03", "05-11",
          "12-103", "#05-11A", "high floor 12-03 unit"]:
    check(f"unit caught: {t!r}", bool(UR.search(t)))
for t in ["10-15 minutes", "20-30 guests", "2018-2020", "$1.2-1.5M", "12-03-2024", "10-12", "1-2",
          "9123-4567", "3-4 bedrooms", "5-10 mins walk", "06-10 floors", "12-100 sqft", "2-storey",
          "24-7", "S$1,200-1,500", "30-40 years", "05-10 min", "1,970 sqft", "D15"]:
    check(f"not a unit: {t!r}", not UR.search(t), UR.search(t))
gf = NS["_guard_features"]
check("features guard strips units, keeps the rest",
      gf("Pool view, unit 05-11, 3 bedrooms, 10-15 mins to MRT") == "Pool view, 3 bedrooms, 10-15 mins to MRT",
      gf("Pool view, unit 05-11, 3 bedrooms, 10-15 mins to MRT"))
check("features guard on empty / None", gf("") == "" and gf(None) is None)

print("\n[7] project name stays agent-only")
listing = {"attributes": {"project_name": "Parc Esta", "show_project_name": True}}
check("public gate returns nothing (not approved), even with an opt-in flag", NS["_public_project_name"](listing) == "")
check("project name scrubbed from outward text",
      NS["_scrub_project_name"]("Welcome to Parc Esta, a bright home.", listing, "District 14")
      == "Welcome to District 14, a bright home.")
check("a number in front of a scrubbed name goes with it (never '26 District 11')",
      NS["_scrub_project_name"]("Live at 26 Newton today.", {"attributes": {"project_name": "Newton Suites"}}, "District 11")
      == "Live at 26 Newton today." and
      NS["_scrub_project_name"]("Live at 26 Newton Suites today.", {"attributes": {"project_name": "Newton Suites"}}, "District 11")
      == "Live at District 11 today.")
check("short one-word names are not scrubbed (would eat ordinary words)",
      NS["_scrub_project_name"]("Sky views", {"attributes": {"project_name": "Sky"}}, "District 1") == "Sky views")
check("scrub is a no-op on landed", NS["_scrub_project_name"]("A home", {}, "District 10") == "A home")

print("\n[8] write-up FACTS block and prompt branch")
def req(**kw):
    base = dict(property_type="", bedrooms="", bathrooms="", storeys=0, built_up=0, land_size=0,
                features="", tenure="", tenure_detail="", floor_level="", project_name="", price="")
    base.update(kw)
    return types.SimpleNamespace(**base)

landed_req = req(property_type="Semi-Detached", bedrooms="5", bathrooms="4", storeys=2,
                 built_up=4000, land_size=3000, features="pool")
check("landed FACTS block exactly as before",
      NS["_build_writeup_facts_block"](landed_req, "District 10") == "\n".join([
          "FACTS:", "- Property type: Semi-Detached",
          "- District (area context only — never name the street): District 10",
          "- Bedrooms: 5", "- Bathrooms: 4", "- Storeys: 2", "- Built-up size: 4,000 sqft",
          "- Land size: 3,000 sqft", "- Agent-noted features: pool"]))
condo_req = req(property_type="Condominium", bedrooms="3", built_up=1100, land_size=5000,
                tenure="Freehold", floor_level="High", project_name="Parc Esta", features="near park")
fb = NS["_build_writeup_facts_block"](condo_req, "District 14")
check("condo FACTS: tenure + floor band, no land size, no project name",
      "- Tenure: Freehold" in fb and "- Floor level: High floor" in fb
      and "Land size" not in fb and "Parc Esta" not in fb, fb)
check("EC FACTS carry nothing about eligibility",
      not re.search(r"eligib|MSOP|privati", NS["_build_writeup_facts_block"](req(property_type="Executive Condominium (EC)"), "Singapore")))
base_sys = "...HARD RULE...\nYour Vision. Your Legacy. {line}\n...Your Vision. Your Legacy."
check("landed system prompt untouched", NS["_apply_strata_writeup_rules"](base_sys, "Corner Terrace") is base_sys)
cs = NS["_apply_strata_writeup_rules"](base_sys, "Apartment")
check("condo prompt: Home closing everywhere + rule block",
      "Your Vision" not in cs and cs.count("Your Home. Your Next Chapter.") >= 2
      and "never state or imply who is eligible" in cs and "prestigious" in cs)
doc = io.open(os.path.join(BACKEND, "docs", "listing-copy-generation-prompt.md"), encoding="utf-8").read()
check("the live prompt doc contains the landed closing the swap relies on", "Your Vision. Your Legacy." in doc)
check("embedded fallback prompt has no *** asterisks", "***" not in "\n".join(
      LINES[NODES["_WRITEUP_PROMPT_FALLBACK_SYSTEM"].lineno - 1:NODES["_WRITEUP_PROMPT_FALLBACK_SYSTEM"].end_lineno]))

print("\n[9] no-photo text fallback")
SEEN = []


class _Resp:
    def __init__(self, text):
        self.content = [types.SimpleNamespace(text=text)]


async def fake_claude(**kw):
    SEEN.append(kw["messages"][0]["content"])
    return _Resp("Headline\n\nBody.")

NS["create_claude_message"] = fake_claude
AGENT = {"id": "a1", "name": "Joshua", "agency": "NL", "specialty": "homes"}
asyncio.run(NS["_generate_listing_text_only"](AGENT, condo_req, "District 14"))
p = SEEN[-1]
check("condo prompt: no land size, no 0-value lines, no project name",
      "Land size" not in p and ": 0" not in p and "Parc Esta" not in p, p[:400])
check("condo prompt: condo rules + Home closing", "never state or imply who is eligible" in p
      and "Your Home. Your Next Chapter." in p)
asyncio.run(NS["_generate_listing_text_only"](AGENT, req(property_type="Inter-Terrace", bedrooms="4", land_size=0), "District 15"))
p = SEEN[-1]
check("landed prompt: blank land size no longer shown as 0, no condo rules",
      "Land size" not in p and "eligible" not in p and "Your Home" not in p and "Inter-Terrace" in p)
asyncio.run(NS["_generate_listing_text_only"](AGENT, landed_req, "District 10"))
check("landed prompt: real land size still given", "- Land size: 3,000 sqft" in SEEN[-1])
check("basic template grammar for EC", NS["_basic_listing_fallback"](AGENT, req(property_type="Executive Condominium (EC)"), "District 27")
      .split("\n\n")[1].startswith("An executive condominium"))
check("basic template unchanged for landed", NS["_basic_listing_fallback"](AGENT, landed_req, "District 10")
      .split("\n\n")[1].startswith("A semi-detached in District 10."))

print("\n[10] land fields on a condo listing")
w = []
r = req(property_type="Condominium", land_size=5000, plot_width=10, plot_depth=0, site_coverage=0)
NS["_strip_land_fields_for_strata"](r, ["Condominium"], w)
check("condo: land size/plot zeroed, agent told", r.land_size == 0 and r.plot_width == 0 and len(w) == 1, w)
r = req(property_type="Condominium", land_size=5000, plot_width=0, plot_depth=0, site_coverage=0)
NS["_strip_land_fields_for_strata"](r, ["Condominium", "Semi-Detached"], [])
check("mixed condo + landed listing keeps land size", r.land_size == 5000)
r = req(property_type="Semi-Detached", land_size=5000, plot_width=9, plot_depth=0, site_coverage=0)
NS["_strip_land_fields_for_strata"](r, [], [])
check("landed untouched", r.land_size == 5000 and r.plot_width == 9)

print("\n[11] PropertyGuru export (agent's own copy)")
pg_landed = NS["_build_propertyguru_text_block"]({"property_type": "Semi-Detached", "location": "9 X Road",
                                                  "land_size": 3000, "attributes": None})
check("landed export has no condo lines", not re.search(r"Project Name|Tenure|Floor Level|TOP Year", pg_landed))
pg_condo = NS["_build_propertyguru_text_block"]({"property_type": "Condominium", "location": "Parc Esta #05-11",
                                                 "attributes": {"project_name": "Parc Esta", "tenure": "Freehold",
                                                                "floor_level": "High", "top_year": 2022}})
check("condo export carries project, tenure, floor band, TOP",
      all(x in pg_condo for x in ("Project Name: Parc Esta", "Tenure: Freehold", "Floor Level: High floor", "TOP Year: 2022")))

print("\n[12] video music: condo family stays WARM (content-studio: no change)")
for t in ("Condominium", "Executive Condominium (EC)", "Apartment"):
    check(f"{t} -> warm", VR._music_tier(t) == VR.TIER_WARM)

print("\n[13] wiring present in main.py")
check("public payload uses the project-name scrub", SRC.count("_scrub_project_name(public_") == 2)
pub = "\n".join(LINES[NODES["_public_listing_payload"].lineno - 1:NODES["_public_listing_payload"].end_lineno])
check("public payload never returns project_name", '"project_name"' not in pub)
check("attributes write is probe-gated on create and update",
      SRC.count("if _attributes_column_supported and _attrs:") == 1 and "if _attributes_column_supported:\n        _prev" in SRC)
check("market pulse GET carries the source notice", SRC.count("ura_market_pulse.source_notice()") >= 3)
mig = io.open(os.path.join(BACKEND, "migrations", "002_listing_attributes.sql"), encoding="utf-8").read()
check("migration is additive + re-asserts the lock",
      "ADD COLUMN IF NOT EXISTS attributes jsonb" in mig and "REVOKE ALL ON TABLE listings FROM anon" in mig
      and "DROP" not in mig.upper().replace("DROPPED", ""))

print("\n[14] endpoints executed against stubs: create, update, public page, CMA")
from fastapi import HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402


class FakeQuery:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload, self.cols = db, table, "select", None, None

    def select(self, cols="*"):
        self.cols = cols
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def eq(self, *a):
        return self

    def execute(self):
        self.db.log.append((self.table, self.op, self.cols, self.payload))
        if self.table == "agents":
            return types.SimpleNamespace(data=[{"name": "Joshua", "agency": "NL"}])
        if self.op == "select":
            return types.SimpleNamespace(data=[dict(self.db.row)])
        return types.SimpleNamespace(data=[dict(self.db.row, **self.payload)])


class FakeDB:
    def __init__(self, row=None):
        self.row, self.log = row or {"id": "L1", "code": "YOE"}, []

    def table(self, name):
        return FakeQuery(self, name)


DB = FakeDB()


def fake_db_execute(build, attempts=3, what="", idempotent=True):
    return build(DB).execute()


async def fake_db_execute_async(build, attempts=3, what="", idempotent=True):
    return build(DB).execute()

NS.update({
    "BaseModel": BaseModel, "Field": Field, "Optional": __import__("typing").Optional,
    "HTTPException": HTTPException, "Depends": lambda x: None, "get_current_agent": None,
    "db_execute": fake_db_execute, "db_execute_async": fake_db_execute_async,
    "_strip_house_number": lambda x: str(x or ""), "_derive_listing_code": lambda x: None,
    "_unique_listing_code": lambda *a, **k: "CODE", "MAX_LISTING_PHOTOS": 15,
    "WRITEUP_MAX_IMAGES": 8, "_district_column_supported": False,
    "_property_types_column_supported": False, "os": os, "asyncio": asyncio,
    "_scrub_own_street": lambda t, loc, d: t,
})
run_nodes("MAX_PROPERTY_TYPES", "ListingRequest", "CMARequest", "_resolve_property_types", "_resolve_writeup_district",
          "_writeup_district_str", "WRITING_STYLE_OPTIONS", "_apply_writing_style",
          "generate_listing", "update_listing", "_public_listing_payload", "generate_cma_report")

CONDO_BODY = dict(property_type="Condominium", location="Parc Esta #05-11", land_size=4000,
                  bedrooms="3", features="Parc Esta clubhouse", project_name="Parc Esta #05-11",
                  tenure="99 yrs", floor_level="high", top_year=2022, district="14")
for supported in (False, True):
    NS["_attributes_column_supported"] = supported
    DB.log.clear()
    out = asyncio.run(NS["generate_listing"](NS["ListingRequest"](**CONDO_BODY), agent=AGENT))
    ins = [e for e in DB.log if e[1] == "insert"][0][3]
    check(f"create (column {'present' if supported else 'absent'}): condo saved, land size off, agent warned",
          ins["land_size"] == 0 and any("doesn't apply" in w for w in out["compliance"]["warnings"])
          and out["listing"]["attributes_saved"] is supported)
    check(f"create (column {'present' if supported else 'absent'}): attributes written only when the column exists",
          ("attributes" in ins) is supported
          and (not supported or ins["attributes"]["project_name"] == "Parc Esta"))
DB.log.clear()
asyncio.run(NS["generate_listing"](NS["ListingRequest"](**dict(CONDO_BODY, features="Corner unit #05-11, 10-15 mins to MRT")), agent=AGENT))
ins = [e for e in DB.log if e[1] == "insert"][0][3]
check("create: features unit-number guarded before the prompt and the row",
      "05-11" not in ins["features"] and "10-15 mins" in ins["features"] and "05-11" not in SEEN[-1], ins["features"])
check("create: write-up prompt never saw the project name", "Parc Esta\n" not in SEEN[-1]
      and "- Agent-noted features: Corner" in SEEN[-1])
DB.log.clear()
NS["_attributes_column_supported"] = True
out = asyncio.run(NS["generate_listing"](NS["ListingRequest"](property_type="Semi-Detached", location="9 X Road",
                                                               land_size=3000, bedrooms="4", features=""), agent=AGENT))
ins = [e for e in DB.log if e[1] == "insert"][0][3]
check("create landed: land size kept, no attributes written, no condo warning",
      ins["land_size"] == 3000 and "attributes" not in ins
      and not any("doesn't apply" in w for w in out["compliance"]["warnings"]))

async def _boom(*a, **k):
    raise RuntimeError("anthropic down")
NS["create_claude_message"] = _boom
out = asyncio.run(NS["generate_listing"](NS["ListingRequest"](**CONDO_BODY), agent=AGENT))
check("no-photo write-up with Anthropic down -> listing still created from the basic template",
      bool(out["listing"]) and out["listing"]["content"].startswith("Condominium in District 14\n\nA condominium"),
      out["listing"]["content"][:80])
NS["create_claude_message"] = fake_claude

DB.row = {"id": "L1", "code": "YOE", "attributes": {"project_name": "Old", "tenure": "Freehold", "hdb_town": "Tampines"}}
DB.log.clear()
row = NS["update_listing"]("L1", NS["ListingRequest"](**dict(CONDO_BODY, tenure="", floor_level="")), agent=AGENT)
upd = [e for e in DB.log if e[1] == "update"][0][3]
sel = [e for e in DB.log if e[1] == "select"][0][2]
check("update: reads attributes only when the column exists", "attributes" in sel)
check("update: owned keys replaced (cleared tenure clears), foreign keys kept, land zeroed",
      upd["attributes"] == {"hdb_town": "Tampines", "project_name": "Parc Esta", "top_year": 2022}
      and upd["land_size"] == 0, upd.get("attributes"))
check("update: response carries flat condo fields", row["project_name"] == "Parc Esta" and row["tenure"] == "")
DB.row = {"id": "L1", "code": "YOE", "attributes": {"project_name": "Parc Esta", "tenure": "Other",
                                                     "tenure_detail": "103-year leasehold", "floor_level": "High",
                                                     "top_year": 2022, "hdb_town": "Tampines"}}
DB.log.clear()
NS["update_listing"]("L1", NS["ListingRequest"](property_type="Condominium", location="x", bedrooms="3",
                                                features="Nice, unit 05-11"), agent=AGENT)
upd = [e for e in DB.log if e[1] == "update"][0][3]
check("update that omits the condo fields keeps every saved value", upd["attributes"] == DB.row["attributes"],
      upd["attributes"])
check("update: features unit-number guarded on save", "05-11" not in upd["features"], upd["features"])
DB.log.clear()
NS["update_listing"]("L1", NS["ListingRequest"](property_type="Condominium", location="x", bedrooms="3", features="",
                                                floor_level="Low"), agent=AGENT)
upd = [e for e in DB.log if e[1] == "update"][0][3]
check("update that sends ONE condo field changes only that one",
      upd["attributes"] == dict(DB.row["attributes"], floor_level="Low"), upd["attributes"])
NS["_attributes_column_supported"] = False
DB.log.clear()
NS["update_listing"]("L1", NS["ListingRequest"](**CONDO_BODY), agent=AGENT)
check("update before migration: no attributes selected or written",
      all("attributes" not in str(e[2]) and "attributes" not in (e[3] or {}) for e in DB.log))

pub = NS["_public_listing_payload"]({"id": "L1", "agent_id": "a1", "property_type": "Condominium",
                                     "location": "Parc Esta #05-11", "district": "14", "price": "1500000",
                                     "content": "Parc Esta is lovely.", "features": "Parc Esta gym",
                                     "attributes": {"project_name": "Parc Esta", "tenure": "Freehold",
                                                    "floor_level": "High", "top_year": 2022}})
blob = repr(pub)
check("public page: no project name anywhere, no TOP year, no unit number",
      "Parc Esta" not in blob and "2022" not in blob and "#05" not in blob, blob[:300])
check("public page: tenure + floor band shown", pub["tenure"] == "Freehold" and pub["floor_level"] == "High")
pub_o = NS["_public_listing_payload"]({"id": "L3", "agent_id": "a1", "property_type": "Apartment", "location": "x",
                                       "price": "1", "content": "x", "features": "Corner unit #05-11, unit 12-03",
                                       "attributes": {"tenure": "Other", "tenure_detail": "call me 9123 4567 lol"}})
check("public page: tenure_detail (agent free text) never served; 'Other' alone shows blank",
      pub_o["tenure"] == "" and "9123" not in repr(pub_o))
check("public page: unit numbers stripped from old, unguarded features on serve",
      "05-11" not in pub_o["features"] and "12-03" not in pub_o["features"], pub_o["features"])
pub_l = NS["_public_listing_payload"]({"id": "L2", "agent_id": "a1", "property_type": "GCB", "location": "1 Nassim Road",
                                       "price": "1", "content": "x"})
check("public page landed: condo keys blank", pub_l["tenure"] == "" and pub_l["floor_level"] == "")

CALLS = []
async def fake_gen(street, ptype, land, win, project_name="", built_up_sqft=0):
    CALLS.append((street, ptype, land, win, project_name, built_up_sqft))
    return {"ok": True}
NS["ura_market_pulse"] = types.SimpleNamespace(is_strata_type=NEW.is_strata_type, generate_cma=fake_gen)
os.environ["URA_ACCESS_KEY"] = "test-placeholder-not-a-real-key"
CR = NS["CMARequest"]
asyncio.run(NS["generate_cma_report"](CR(street="", property_type="Condominium", project_name="Parc Esta #05-11",
                                         built_up=1100, window_months=999), agent=AGENT))
check("CMA condo: project name only is enough, unit stripped, window clamped, built-up passed",
      CALLS[-1] == ("", "Condominium", 0, 60, "Parc Esta", 1100), CALLS[-1])
asyncio.run(NS["generate_cma_report"](CR(street="Nassim Road", property_type="Detached/Bungalow", land_size=5000,
                                         project_name="ignored"), agent=AGENT))
check("CMA landed: same arguments as before, project name ignored",
      CALLS[-1] == ("Nassim Road", "Detached/Bungalow", 5000, 24, "", 0), CALLS[-1])
for body, why in [(CR(street="", property_type="Detached/Bungalow"), "landed needs a street"),
                  (CR(street="", property_type="Condominium", project_name="#05-11"), "unit-only project is empty")]:
    try:
        asyncio.run(NS["generate_cma_report"](body, agent=AGENT))
        check("CMA 400: " + why, False)
    except HTTPException as e:
        check("CMA 400: " + why, e.status_code == 400, e.detail)
async def fake_down(*a, **k):
    raise RuntimeError("Couldn't reach URA's data service right now -- please try again shortly")
NS["ura_market_pulse"].generate_cma = fake_down
try:
    asyncio.run(NS["generate_cma_report"](CR(street="Nassim Road"), agent=AGENT))
except HTTPException as e:
    check("CMA with URA down -> 503 with a readable message", e.status_code == 503 and "URA" in e.detail)
NS["ura_market_pulse"] = NEW

print("\n[15] photo write-up prompt, Smart Fill, Market Pulse notice (executed)")
import json as _json  # noqa: E402
NS["json"] = _json
NS["__file__"] = MAIN
run_nodes("_WRITEUP_PROMPT_FALLBACK_SYSTEM", "_WRITEUP_PROMPT_FALLBACK_USER", "_WRITEUP_PROMPT_PATH",
          "_WRITEUP_PROMPT_CACHE", "_load_writeup_prompt", "_fill_placeholders", "_STRAY_STREET_RE",
          "_strip_stray_street_names", "_generate_writeup_from_photos")
SYS = []


async def fake_vision(**kw):
    SYS.append(kw["system"])
    return types.SimpleNamespace(stop_reason="end_turn",
                                 content=[types.SimpleNamespace(type="text", text="Headline\n\nBody.")])
NS["create_claude_message"] = fake_vision
asyncio.run(NS["_generate_writeup_from_photos"](AGENT, landed_req, ["https://x/1.jpg"], "District 10", "t"))
asyncio.run(NS["_generate_writeup_from_photos"](AGENT, condo_req, ["https://x/1.jpg"], "District 14", "t"))
check("photo write-up (live doc): landed keeps 'Your Vision. Your Legacy.', no condo rules",
      "Your Vision. Your Legacy." in SYS[0] and "never state or imply who is eligible" not in SYS[0])
check("photo write-up (live doc): condo gets Home closing + rules, no landed closing",
      "Your Vision" not in SYS[1] and "Your Home. Your Next Chapter." in SYS[1]
      and "never state or imply who is eligible" in SYS[1])
NS["_WRITEUP_PROMPT_CACHE"] = (NS["_WRITEUP_PROMPT_FALLBACK_SYSTEM"], NS["_WRITEUP_PROMPT_FALLBACK_USER"])
asyncio.run(NS["_generate_writeup_from_photos"](AGENT, condo_req, ["https://x/1.jpg"], "District 14", "t"))
check("photo write-up (embedded fallback): condo swap works there too, no asterisks",
      "Your Vision" not in SYS[-1] and "Your Home. Your Next Chapter." in SYS[-1] and "***" not in SYS[-1])
check("Other tenure passes the agent's own words",
      "- Tenure: 103-year leasehold" in NS["_build_writeup_facts_block"](
          req(property_type="Apartment", tenure="103-year leasehold"), "Singapore"))

NS["Request"] = object
run_nodes("extract_listing_image")


class FakeRequest:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


async def fake_extract(**kw):
    return types.SimpleNamespace(content=[types.SimpleNamespace(text=_json.dumps({
        "property_type": "Executive Condominium (EC)", "project_name": "Parc Canberra #06-12",
        "tenure": "99 yrs lease commencing from 2018", "district": "27", "bathrooms": ""}))])
NS["create_claude_message"] = fake_extract
ex = asyncio.run(NS["extract_listing_image"](FakeRequest({"images": [{"image_data": "eA=="}]})))
check("Smart Fill: EC type, project name without unit, tenure normalised",
      ex["property_type"] == "Executive Condominium (EC)" and ex["project_name"] == "Parc Canberra"
      and ex["tenure"] == "99-year leasehold" and ex["tenure_detail"] == "", ex)


class PulseDB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, n):
        return self

    def select(self, *a):
        return self

    def eq(self, *a):
        return self

    def execute(self):
        return types.SimpleNamespace(data=self.rows)
run_nodes("get_market_pulse")
for rows, label in (([{"id": 1, "gcb_transactions": "40 units", "source": "ura_api"}], "stored row"), ([], "manual fallback")):
    NS["get_db"] = lambda rows=rows: PulseDB(rows)
    mp = NS["get_market_pulse"]()
    check(f"Market Pulse ({label}) carries the source notice",
          mp["source_licence_url"] == "https://data.gov.sg/open-data-licence" and mp["source_disclaimer"]
          and mp["source_attribution"])

print("\n[16] Market Pulse save drops non-column keys")
run_nodes("_MARKET_PULSE_COLUMNS", "_market_pulse_row")
NS["_market_pulse_columns_supported"] = True
row = NS["_market_pulse_row"]({"gcb_transactions": "40 units", "source": "manual", "partial": False,
                               **NEW.source_notice(), "batches_ok": 4, "raw_projects": 999, "bogus": 1})
check("source_* / diagnostics / unknown keys dropped, real columns kept",
      row == {"id": 1, "gcb_transactions": "40 units", "source": "manual", "partial": False}, row)
NS["_market_pulse_columns_supported"] = False
check("freshness columns still dropped before their migration",
      "partial" not in NS["_market_pulse_row"]({"partial": False, "refreshed_at": "x", "gcb_largest": "1"}))

print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
sys.exit(1 if FAIL else 0)
