"""(harness copied from test_home_decisions) NestList Home -- Jane's decisions of 2026-10-20 (condo family = Condominium / EC /
Apartment). Run:  python3 tests/test_home_decisions.py   (exit 1 on any failure)

  1. project name public for condo-family (hide_project_name opt-out), units stripped
  2. price shown openly for condo-family (price_label), landed unchanged
  3. CEA details (profile fields, migration-gated) on condo-family materials ONLY
  4. owner consent recorded on every listing; auto-posting refuses an unconfirmed NEW one
  5. EC eligibility only from the agent's field; MOP / mismatch warnings
  + golden: landed posters, landed end slates and landed public pages unchanged.

main.py cannot be imported here (no supabase/anthropic locally); its functions are
EXECUTED from source with main.py's own file name and line numbers.
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
import types
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
REPO = os.path.dirname(BACKEND)
sys.path.insert(0, BACKEND)

import ura_market_pulse as URA  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + str(detail)[:300]) if detail else ""))


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

LOG = logging.getLogger("home-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False

from fastapi import HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402
from typing import Optional, List  # noqa: E402

NS = {"re": re, "datetime": datetime, "timezone": timezone, "logger": LOG, "ura_market_pulse": URA,
      "types": types, "math": math, "os": os, "asyncio": asyncio, "io": io, "BaseModel": BaseModel,
      "Field": Field, "Optional": Optional, "List": List, "HTTPException": HTTPException,
      "Depends": lambda x=None: None, "get_current_agent": None, "uuid": __import__("uuid"),
      "_agent_cea_columns_supported": True, "_attributes_column_supported": True,
      "_district_column_supported": False, "_property_types_column_supported": False}


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
          "_MD_HEADING_RE", "_MD_BULLET_RE", "_MD_RULE_RE", "_MD_STRONG_RE", "_MD_STAR_RE", "_MD_UNDERSCORE_RE", "_strip_markdown", "apply_listing_copy_guards", "_guard_features")
run_span("_fmt_writeup_num", "_build_writeup_facts_block")
run_nodes("_basic_listing_fallback", "_normalize_district", "_extract_district_token", "_listing_district_label",
          "_clean_property_types", "_listing_property_types", "_resolve_property_types", "MAX_PROPERTY_TYPES",
          "_strip_land_fields_for_strata", "ListingRequest", "WriteupRequest", "ProfileUpdate", "ProfileCEAUpdate",
          "_cea_profile_updates", "_agent_response", "_public_agent_block", "OWNER_CONSENT_REQUIRED_DETAIL")
NS.update({"_can_use_instagram_beta": lambda a: True, "_can_use_facebook_beta": lambda a: True,
           "_can_use_linkedin_beta": lambda a: True})

CEA_AGENT = {"id": "a1", "name": "Janel Chee", "agency": "NL Realty", "specialty": "homes", "contact": "9123 4567",
             "cea_reg_no": "R123456A", "agency_name": "NL Realty Pte Ltd", "agency_licence_no": "L3008899K"}
BARE_AGENT = {"id": "a2", "name": "Joshua", "agency": "NL", "specialty": "homes", "contact": "8888 0000"}


print("\n[1] the TESTEC sample")
TESTEC = ("*TEST LISTING – please ignore.*\n\nA bright two-bedroom executive condominium in District 19.\n\n"
          "Your Home. Your Next Chapter.")
SM = NS["_strip_markdown"]
check("TESTEC: asterisk wrapper gone, words kept",
      SM(TESTEC) == TESTEC.replace("*TEST LISTING – please ignore.*", "TEST LISTING – please ignore."), SM(TESTEC))

print("\n[2] every wrapper / marker form")
for raw, exp in [
        ("***Freehold*** gem", "Freehold gem"), ("**Freehold** gem", "Freehold gem"), ("*Freehold* gem", "Freehold gem"),
        ("__Freehold__ gem", "Freehold gem"), ("_Freehold_ gem", "Freehold gem"),
        ("A **bright, airy** home.", "A bright, airy home."), ("(*quiet street*)", "(quiet street)"),
        ("Rare: **5-bedroom** *corner* unit.", "Rare: 5-bedroom corner unit."),
        ("**A *very* rare home**", "A very rare home"),
        ("# Welcome Home", "Welcome Home"), ("## The Living Spaces\nLight-filled.", "The Living Spaces\nLight-filled."),
        ("###### Deep", "Deep"),
        ("* Pool\n* Gym", "• Pool\n• Gym"), ("- Pool\n- Gym", "• Pool\n• Gym"),
        ("  - indented bullet", "  • indented bullet"), ("*   wide gap", "• wide gap"),
        ("* **Pool** view", "• Pool view"),
        ("Intro\n---\nBody", "Intro\n\nBody"), ("Intro\n***\nBody", "Intro\n\nBody"),
        ("*Line one*\n*Line two*", "Line one\nLine two")]:
    check(f"{raw!r} -> {exp!r}", SM(raw) == exp, repr(SM(raw)))

print("\n[3] left untouched")
for keep in ["Rated 5* by residents", "5* and 4* reviews", "3*4 metres", "2 * 3 = 6", "SGD 1.5M", "S$1,250,000*",
             "*Terms apply", "Prices from $1.2M - $1.5M", "10 - 15 mins to MRT", "-5% from launch", "5-star finishes",
             "https://nestlist.sg/joshua/YOE", "https://example.com/a_b_c?x=*y*z", "john_doe@example.com",
             "smart_home_ready", "#NestListHome #SGProperty", "Unit #05-11 is gone", "Corner unit, 3*",
             "Your Vision. Your Legacy.", "Your Home. Your Next Chapter.", "a * b * c", "CEA Reg No: R123456A",
             "Janel Chee | 9123 4567\nCEA Reg No: R123456A\nAgency: NL Realty Pte Ltd (Licence No: L3008899K)",
             "Tenure: 99-year leasehold", "2-storey, 4 bedrooms", "**", "*", "_", "", "Pool\nGym\n\nGarden",
             "Price: 1,200,000 (negotiable)", "co-op_", "_x", "x_", "• Pool", "• Pool\n• Gym",
             "  • indented", "-", "- ", "*bold*text"]:
    check(f"untouched: {keep!r}", SM(keep) == keep, repr(SM(keep)))
check("None / '' safe", SM(None) is None and SM("") == "")

print("\n[4] idempotent")
for raw in [TESTEC, "**A *very* rare home**", "# H\n* a\n- b\n---\n__x__ _y_", "• a\n- b\n* c"]:
    once = SM(raw)
    check(f"twice == once for {raw[:20]!r}", SM(once) == once)

print("\n[5] plain write-ups pass through byte-for-byte (no other output changes)")
PLAIN = [
    "Discover a rare freehold semi-detached home in District 10.\n\nSet on 3,003 sqft of land, the 2-storey "
    "home offers 5 bedrooms and 4 bathrooms.\n\nYour Vision. Your Legacy. A home for the next generation.\n\n"
    "Janel Chee | NL Realty Specialist",
    "A bright 3-bedroom condominium in District 14 -- high floor, 1,100 sqft, 10-15 mins to the MRT.\n\n"
    "Your Home. Your Next Chapter. Arrange a viewing today.",
]
for t in PLAIN:
    check("plain write-up unchanged", SM(t) == t)

print("\n[6] wired into generation (landed and condo) and serving")
class _Resp:
    def __init__(self, text):
        self.content = [types.SimpleNamespace(type="text", text=text)]
        self.stop_reason = "end_turn"

MD_OUT = "*TEST LISTING – please ignore.*\n\n## Living\n* Bright **airy** spaces\n\nYour Home. Your Next Chapter."
async def fake_claude(**kw):
    return _Resp(MD_OUT)

class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload = db, table, "select", None
    def select(self, *a): return self
    def insert(self, p): self.op, self.payload = "insert", p; return self
    def eq(self, *a): return self
    def execute(self):
        self.db.log.append((self.table, self.op, self.payload))
        if self.op == "insert":
            return types.SimpleNamespace(data=[dict(self.payload, id="L-new")])
        return types.SimpleNamespace(data=[dict(CEA_AGENT)])

class _DB:
    def __init__(self): self.log = []
    def table(self, n): return _Q(self, n)

DB = _DB()
async def _dba(build, **k): return build(DB).execute()
NS.update({"create_claude_message": fake_claude, "db_execute": lambda build, **k: build(DB).execute(),
           "db_execute_async": _dba, "_strip_house_number": lambda x: str(x or ""),
           "_derive_listing_code": lambda x: None, "_unique_listing_code": lambda *a, **k: "C",
           "MAX_LISTING_PHOTOS": 15, "WRITEUP_MAX_IMAGES": 8, "_scrub_own_street": lambda t, loc, d: t,
           "_load_writeup_prompt": lambda: ("SYSTEM Your Vision. Your Legacy.", "USER {facts_block}"),
           "_fill_placeholders": lambda t, subs: t})
run_nodes("_generate_listing_text_only", "_resolve_writeup_district", "_writeup_district_str", "WRITING_STYLE_OPTIONS",
          "_apply_writing_style", "_STRAY_STREET_RE", "_strip_stray_street_names", "_generate_writeup_from_photos",
          "generate_listing", "_public_listing_payload", "_pg_export_price", "_pg_export_count", "_pg_export_measure",
          "_build_propertyguru_text_block")
LR = NS["ListingRequest"]
EXPECT = "TEST LISTING – please ignore.\n\nLiving\n• Bright airy spaces\n\nYour Home. Your Next Chapter."
for ptype, photos in (("Semi-Detached", []), ("Condominium", []), ("Semi-Detached", ["https://img/1.jpg"]),
                      ("Condominium", ["https://img/1.jpg"])):
    DB.log.clear()
    asyncio.run(NS["generate_listing"](LR(property_type=ptype, location="x", bedrooms="3", features="",
                                          photo_urls=photos), agent=dict(BARE_AGENT)))
    saved = [e for e in DB.log if e[1] == "insert"][0][2]["content"]
    check(f"generate {ptype} ({'photo' if photos else 'no-photo'} path): saved plain", saved == EXPECT, repr(saved))
async def _down(**kw):
    raise RuntimeError("anthropic down")
NS["create_claude_message"] = _down
DB.log.clear()
asyncio.run(NS["generate_listing"](LR(property_type="Condominium", location="x", bedrooms="3",
                                      features="*Corner* unit, **pool view**"), agent=dict(BARE_AGENT)))
saved = [e for e in DB.log if e[1] == "insert"][0][2]["content"]
check("fallback template path: plain too", "*" not in saved and "Corner unit, pool view" in saved, repr(saved))
NS["create_claude_message"] = fake_claude
w = asyncio.run(NS["_generate_writeup_from_photos"](dict(BARE_AGENT), types.SimpleNamespace(
    property_type="Condominium", bedrooms="3", bathrooms="", storeys=0, built_up=0, land_size=0, features="",
    tenure="", tenure_detail="", floor_level="", project_name=""), [], "District 9", "t"))
check("generate-writeup endpoint's generator returns plain text", w == EXPECT, repr(w))
check("rewrite-selection strips too", "rewritten = _strip_markdown(rewritten)\n" in SRC)

OLD = {"id": "L1", "agent_id": "a1", "status": "active", "property_type": "Executive Condominium (EC)",
       "location": "x", "price": "1", "content": TESTEC, "features": "*pool*"}
pub = NS["_public_listing_payload"](OLD)
check("buyer page: stored TESTEC content served plain", pub["content"].startswith("TEST LISTING – please ignore."),
      pub["content"][:60])
check("buyer page: agent-typed features are left as typed (not a write-up)", pub["features"] == "*pool*")
pg = NS["_build_propertyguru_text_block"](dict(OLD, property_type="Semi-Detached"))
check("PropertyGuru export: plain", "*TEST" not in pg and "TEST LISTING – please ignore." in pg)
check("My Listings rows go through the same serve-time strip",
      'row["content"] = _outward_content(row, row.get("content"))' in SRC)
cea_text = "Body.\n\n" + NS["_cea_text_block"](CEA_AGENT)
check("Home listing: CEA block survives the strip", NS["_outward_content"]({"property_type": "Condominium"}, cea_text)
      == cea_text)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
