"""The compact URA market copy + the shared on-disk snapshot (memory work). Run:
    python3 tests/test_ura_compact_cache.py      (exit 1 on any failure)

  1. Same answers: on a realistic synthetic market, every landed + condo report built from
     the compact copy is identical to the same report on the full slimmed list (the
     pre-change cache), and Market Pulse stats are identical.
  2. Odd rows (junk price, missing fields, non-dict rows) survive exactly.
  3. The street matcher equals _matches_street_keyword for every street.
  4. Snapshot: round-trip, atomic replace (no torn read, no temp left), corrupt / wrong
     version / unusable directory -> ignored, never an error.
  5. Across REAL separate processes ("workers"): 4 cold workers -> ONE URA pull, all four
     answer; a sibling's failure is shared (no 4x outage hammering).
"""
import asyncio
import json
import os
import random
import re
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, BACKEND)

import ura_market_pulse as U  # noqa: E402

U.URA_SNAPSHOT_DIR = tempfile.mkdtemp(prefix="nl-ura-compact-")

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


WORDS = ("PARC SAIL MARINA BAY RESIDENCES SUITES GRAND VIEW HILL PARK GARDENS TOWER REFLECTIONS PEARL "
         "BANK DUNMAN RIVIERE ONE THE AT KEPPEL NASSIM CLUNY BELMONT BINJAI LEEDON").split()
SUFFIX = ("ROAD", "AVENUE", "DRIVE", "WALK", "PARK", "CLOSE", "HILL")


def synth(seed=11, target=12_000):
    rnd = random.Random(seed)
    streets = sorted({f"{rnd.choice(WORDS)} {rnd.choice(SUFFIX)}" for _ in range(400)})
    streets += ["NASSIM ROAD", "CLUNY HILL", "BELMONT ROAD", "BINJAI PARK", "LEEDON PARK"]
    out, n = [], 0
    while n < target:
        street = rnd.choice(streets)
        if rnd.random() < 0.3:
            name, area_t = "LANDED HOUSING DEVELOPMENT", "Land"
            types = ("Detached", "Semi-detached", "Semi-Detached", "Terrace")
        else:
            name = " ".join(rnd.choice(WORDS) for _ in range(rnd.randint(1, 3)))
            area_t = "Strata"
            types = ("Condominium", "Apartment", "Executive Condominium", "Strata Detached")
        txns = []
        for _ in range(rnd.randint(1, 15)):
            txns.append({"area": f"{rnd.uniform(40, 1500):.1f}", "floorRange": rnd.choice(("-", "06-10", "11-15")),
                         "noOfUnits": rnd.choice(("1", "1", "1", "3")), "contractDate": mmyy(rnd.randint(0, 70)),
                         "typeOfSale": rnd.choice("123"), "price": str(rnd.randint(500_000, 40_000_000)),
                         "propertyType": rnd.choice(types), "district": rnd.choice(("10", "11", "15", "21", "09")),
                         "typeOfArea": area_t, "tenure": rnd.choice(("Freehold", "99 yrs lease commencing from 2001")),
                         "nettPrice": "", "marketSegment": "CCR"})
        n += len(txns)
        out.append({"street": street, "project": name, "x": "1", "y": "2", "transaction": txns})
    # odd rows the old code had to cope with
    out[0]["transaction"] += [
        {"price": "abc", "area": "100", "typeOfArea": "Land", "propertyType": "Detached", "contractDate": mmyy(1)},
        {"price": "1,000,000", "area": "100", "typeOfArea": "Land", "propertyType": "Detached",
         "contractDate": mmyy(1), "district": "10", "tenure": "Freehold", "floorRange": "-",
         "typeOfSale": "3", "noOfUnits": "1"},
        {"price": None, "area": "0", "typeOfArea": "Strata", "propertyType": "Condominium"},
        "not a dict",
    ]
    out.append("not a project")
    out.append({"street": "EMPTY ROAD", "project": "X", "transaction": []})
    return out


RAW = synth()
SLIM = U._slim_projects(RAW)
MARKET = U._compact_from_projects(RAW)

print("\n[1] identical answers from the compact copy")
check("same projects kept as _slim_projects (empty / non-dict dropped)", MARKET.project_count == len(SLIM))
check("materialised copy gives the same rows (price/area as numbers)",
      all(a["street"] == b["street"] and a["project"] == b["project"] and len(a["transaction"]) == len(b["transaction"])
          for a, b in zip(MARKET.materialise_all(), SLIM)))
streets = sorted({p["street"] for p in SLIM})
names = sorted({p["project"] for p in SLIM if p["project"] != "LANDED HOUSING DEVELOPMENT"})
keywords = [s.title() for s in streets[:120]] + ["Nassim", "9 Nassim Road #01-01", "Park", "Bay Road", "", "zzz"]
bad = []
for kw in keywords:
    sub = MARKET.candidates(kw)
    for ptype in ("", "Semi-Detached", "Detached/Bungalow", "Corner Terrace", "Something"):
        for win in (12, 24, 60):
            a = U.extract_comparable_transactions(SLIM, kw, ptype, win)
            b = U.extract_comparable_transactions(sub, kw, ptype, win)
            if a != b or U.compute_cma_stats(a, 3000) != U.compute_cma_stats(b, 3000):
                bad.append((kw, ptype, win))
check(f"landed: {len(keywords) * 15} reports identical (keywords x types x windows)", not bad, bad[:3])
bad = []
rnd = random.Random(5)
cases = [(rnd.choice(streets).title(), rnd.choice(names).title()) for _ in range(150)]
cases += [("", n.title()) for n in names[:60]] + [(s.title(), "") for s in streets[:40]]
cases += [("", "Dunman"), ("Dunman Road", "Dunman"), ("", "The Sail"), ("", "Parc"), ("", "8"), ("Nassim Road", "Nassim")]
for kw, pn in cases:
    sub = MARKET.candidates(kw, pn)
    for types in (("Condominium", "Apartment"), ("Executive Condominium",)):
        a = U._extract_strata_comparables_detailed(SLIM, kw, types, 24, pn)
        b = U._extract_strata_comparables_detailed(sub, kw, types, 24, pn)
        if a != b:
            bad.append((kw, pn, types))
check(f"condo: {len(cases) * 2} reports identical (street / project / both / ambiguous)", not bad, bad[:3])
check("Market Pulse GCB stats identical from the compact copy",
      {k: v for k, v in (U.compute_market_pulse_stats(SLIM) or {}).items() if k != "refreshed_at"}
      == {k: v for k, v in (U.compute_market_pulse_stats(MARKET.gcb_projects()) or {}).items() if k != "refreshed_at"})
check("detached-land survey identical from the materialised copy",
      U.survey_detached_land_streets(SLIM) == U.survey_detached_land_streets(MARKET.materialise_all()))


async def _gen_both():
    U.store_cma_projects(MARKET)
    U._cma_cache["partial_projects"] = None
    out = []
    for kw, pt, pn in (("Nassim Road", "Semi-Detached", ""), ("Nassim", "Detached/Bungalow", ""),
                       (streets[3].title(), "Condominium", names[2].title()), ("", "Penthouse", names[5].title())):
        r = await U.generate_cma(kw, pt, 2500, 24, project_name=pn, built_up_sqft=900)
        out.append(r)
    return out
_os = os.environ.get("URA_ACCESS_KEY")
os.environ["URA_ACCESS_KEY"] = "test-placeholder-not-a-real-key"
res = asyncio.run(_gen_both())
U.store_cma_projects(SLIM)   # list input is compacted too
res_list = asyncio.run(_gen_both())
check("generate_cma end to end: same report from compact and from list seeding",
      [{k: v for k, v in r.items() if k != "generated_at"} for r in res]
      == [{k: v for k, v in r.items() if k != "generated_at"} for r in res_list])

print("\n[2] odd rows survive exactly")
odd_txns = MARKET._project(0)["transaction"][-3:]
check("junk price / comma price / missing fields kept verbatim (not coerced)",
      odd_txns == [{"price": "abc", "area": "100", "typeOfArea": "Land", "propertyType": "Detached", "contractDate": mmyy(1)},
                   SLIM[0]["transaction"][-2], {"price": None, "area": "0", "typeOfArea": "Strata",
                                                "propertyType": "Condominium"}], odd_txns)
check("a bool price is not taken for a number",
      U._compact_from_projects([{"street": "A", "project": "B", "transaction": [
          dict(SLIM[1]["transaction"][0], price=True)]}]).odd != {})

print("\n[3] street matcher = _matches_street_keyword")
mism = [(s, kw) for kw in keywords + ["Blk 9 #01-01 Minaret Walk", "No. 9, Nassim", "#03-04"]
        for s in streets + ["", None] if U._street_matcher(kw)(s) != U._matches_street_keyword(s, kw)]
check("identical on every street x keyword", not mism, mism[:3])

print("\n[4] snapshot on disk")
now = time.time()
check("write ok", U._write_snapshot(MARKET, now, "2026-10-21"))
got = U._read_snapshot()
check("round-trip: same copy, time and date", got and got[1] == now and got[2] == "2026-10-21"
      and got[0].materialise_all() == MARKET.materialise_all())
check("no temp file left behind", not [f for f in os.listdir(U.URA_SNAPSHOT_DIR) if f.endswith(".tmp")])
check("directory is private (0700)", (os.stat(U.URA_SNAPSHOT_DIR).st_mode & 0o077) == 0)
path = U._snapshot_path("complete")
with open(path, "rb") as fh:
    good = fh.read()
with open(path, "wb") as fh:
    fh.write(good[: len(good) // 2])
check("truncated snapshot -> ignored (None), never raises", U._read_snapshot() is None)
import marshal  # noqa: E402
with open(path, "wb") as fh:
    fh.write(marshal.dumps({"v": 999, "state": {}}))
check("wrong version -> ignored", U._read_snapshot() is None)
state = MARKET.to_state()
state["price"] = state["price"][:-8]
with open(path, "wb") as fh:
    fh.write(marshal.dumps({"v": U._SNAPSHOT_VERSION, "fetched_at": now, "as_of": "x", "state": state}))
check("inconsistent columns -> ignored", U._read_snapshot() is None)
# atomic replace while a reader holds the old file open: it reads the old one whole
U._write_snapshot(MARKET, now, "old")
fh = open(path, "rb")
small = U._compact_from_projects(SLIM[:3])
U._write_snapshot(small, now + 1, "new")
old_blob = fh.read()
fh.close()
check("a reader mid-swap reads the old file whole (no torn read)",
      marshal.loads(old_blob)["as_of"] == "old" and U._read_snapshot()[2] == "new")
saved = U.URA_SNAPSHOT_DIR
blocker = tempfile.mktemp(prefix="nl-ura-notadir-")
open(blocker, "w").close()
U.URA_SNAPSHOT_DIR = blocker            # a FILE where the directory should be
check("unusable directory -> sharing off, nothing raises",
      U._snapshot_dir() is None and U._write_snapshot(MARKET, now, "x") is False and U._read_snapshot() is None)
U.URA_SNAPSHOT_DIR = saved

print("\n[5] separate worker processes share ONE pull")
WORKER = textwrap.dedent('''
    import asyncio, os, sys, time, json
    sys.path.insert(0, sys.argv[1])
    import ura_market_pulse as U
    U.URA_SNAPSHOT_DIR = sys.argv[2]
    raw = json.load(open(sys.argv[3]))
    mode = sys.argv[4]
    async def token(key):
        return "tok"
    async def fetch(key, token, batch_timeout=None, on_batch=None):
        with open(os.path.join(sys.argv[2], "calls.log"), "a") as fh:
            fh.write(f"{os.getpid()}\\n")
        await asyncio.sleep(1.5)
        if mode == "down":
            raise RuntimeError("URA down")
        return raw, [{"batch": b, "ok": True} for b in (1, 2, 3, 4)]
    U.get_token, U.fetch_all_transactions = token, fetch
    os.environ["URA_ACCESS_KEY"] = "test-placeholder"
    async def main():
        try:
            r = await U.generate_cma("Nassim Road", "Detached/Bungalow", 3000, 60)
            print(json.dumps({"count": r["comparable_count"], "stale": r["data_stale"]}))
        except RuntimeError as e:
            print(json.dumps({"error": str(e)}))
    asyncio.run(main())
''')
work = tempfile.mkdtemp(prefix="nl-ura-workers-")
script = os.path.join(work, "worker.py")
with open(script, "w") as fh:
    fh.write(WORKER)
raw_path = os.path.join(work, "raw.json")
with open(raw_path, "w") as fh:
    json.dump(RAW, fh)


def run_workers(mode, n=4):
    snap = tempfile.mkdtemp(prefix="nl-ura-snapdir-")
    procs = [subprocess.Popen([sys.executable, script, BACKEND, snap, raw_path, mode],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) for _ in range(n)]
    outs = [p.communicate(timeout=120) for p in procs]
    calls = open(os.path.join(snap, "calls.log")).read().split() if os.path.exists(os.path.join(snap, "calls.log")) else []
    results = []
    for out, err in outs:
        line = [x for x in out.splitlines() if x.startswith("{")]
        results.append(json.loads(line[-1]) if line else {"crash": err[-300:]})
    return calls, results


expected = U.compute_cma_stats(U.extract_comparable_transactions(SLIM, "Nassim Road", "Detached/Bungalow", 60), 3000)
calls, results = run_workers("ok")
check("4 cold workers at once -> exactly ONE URA pull", len(calls) == 1, calls)
check("...and all 4 answer with the same, correct report",
      all(r.get("count") == expected["comparable_count"] for r in results), results)
calls, results = run_workers("down")
check("URA down: the failure is shared -> ONE attempt, not one per worker", len(calls) == 1, calls)
check("...every worker gives the clean agent-readable error", all("URA" in r.get("error", "") for r in results), results)

if _os is None:
    os.environ.pop("URA_ACCESS_KEY", None)
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
