"""NestList Home -- Jane's decisions of 2026-10-20 (condo family = Condominium / EC /
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

# ---------------------------------------------------------------------------
print("\n[1] project name public for condo family; hidden on request; landed unchanged")
P = NS["_public_project_name"]
check("condo: public", P({"property_type": "Condominium", "attributes": {"project_name": "Parc Esta"}}) == "Parc Esta")
check("EC / Apartment: public",
      P({"property_type": "Executive Condominium (EC)", "attributes": {"project_name": "Piermont Grand"}}) == "Piermont Grand"
      and P({"property_type": "Apartment", "attributes": {"project_name": "Pearl Bank"}}) == "Pearl Bank")
check("unit number never public even if one slipped into storage",
      P({"property_type": "Condominium", "attributes": {"project_name": "The Sail #66-01"}}) == "The Sail")
check("hide_project_name -> not public", P({"property_type": "Condominium",
                                           "attributes": {"project_name": "Parc Esta", "hide_project_name": True}}) == "")
check("landed / Penthouse / mixed condo+landed -> never",
      P({"property_type": "Semi-Detached", "attributes": {"project_name": "X Estate"}}) == ""
      and P({"property_type": "Penthouse", "attributes": {"project_name": "The Sail"}}) == ""
      and P({"property_type": "Condominium", "property_types": ["Condominium", "Corner Terrace"],
             "attributes": {"project_name": "Mix"}}) == "")
check("no attributes -> blank, never raises", P({"property_type": "Condominium"}) == "" and P(None) == "")
a = NS["_build_listing_attributes"](types.SimpleNamespace(project_name="Parc Esta", hide_project_name=True,
                                                          price_label="negotiable", ec_eligibility="CITIZENS_PRS"))
check("attributes: hide flag, price label and EC eligibility normalised",
      a == {"project_name": "Parc Esta", "hide_project_name": True, "price_label": "Negotiable",
            "ec_eligibility": "citizens_prs"}, a)
check("attributes: junk price label / eligibility dropped, hide only when True",
      NS["_build_listing_attributes"](types.SimpleNamespace(price_label="cheap!", ec_eligibility="everyone",
                                                            hide_project_name="yes")) == {})
merged = NS["_merge_listing_attributes"]({"project_name": "Parc Esta", "hide_project_name": True,
                                          "owner_consent_confirmed": True, "owner_consent_at": "T"},
                                         NS["ListingRequest"](property_type="Condominium", location="x", bedrooms="2",
                                                              features="", price_label="From"))
check("edit sending only price_label keeps hide flag and consent", merged == {
    "project_name": "Parc Esta", "hide_project_name": True, "price_label": "From",
    "owner_consent_confirmed": True, "owner_consent_at": "T"}, merged)
merged = NS["_merge_listing_attributes"]({"project_name": "Parc Esta", "hide_project_name": True},
                                         NS["ListingRequest"](property_type="Condominium", location="x", bedrooms="2",
                                                              features="", hide_project_name=False))
check("edit sending hide_project_name=false un-hides", merged == {"project_name": "Parc Esta"}, merged)

# ---------------------------------------------------------------------------
print("\n[2] price shown openly for condo family, with its label")
CP = NS["_condo_price_text"]
for price, label, exp in [("1500000", "", "SGD 1.5M"), ("1500000", "From", "From SGD 1.5M"),
                          ("1500000", "negotiable", "SGD 1.5M (Negotiable)"),
                          ("1500000", "View to Offer", "SGD 1.5M · View to Offer"),
                          ("", "View to Offer", "View to Offer"), ("", "", "Price on request"),
                          ("0", "From", "Price on request")]:
    check(f"price {price!r} + {label!r} -> {exp!r}", CP(price, label) == exp, CP(price, label))
VO = NS["_home_video_overlay"]
condo_l = {"property_type": "Condominium", "price": "1500000", "attributes": {"price_label": "Negotiable"}}
landed_l = {"property_type": "Semi-Detached", "price": "4200000", "attributes": {"price_label": "Negotiable"}}
contact, pt, st = VO(condo_l, CEA_AGENT, "SGD 1.5M", ["3 Rooms"])
check("video, condo: price + CEA lines under the phone on the end slate",
      contact == "9123 4567\nSGD 1.5M (Negotiable)\nCEA Reg No: R123456A\n"
                 "Agency: NL Realty Pte Ltd (Licence No: L3008899K)" and st[0] == "SGD 1.5M (Negotiable)", contact)
contact, pt, st = VO(landed_l, CEA_AGENT, "SGD 4.2M", ["4 Rooms"])
check("video, landed: contact exactly as before, no price label, no CEA",
      contact == "9123 4567" and pt == "SGD 4.2M" and st == ["4 Rooms"])
contact, _, _ = VO(condo_l, BARE_AGENT, "SGD 1.5M", [])
check("video, condo with incomplete CEA: price only, no CEA lines", contact == "8888 0000\nSGD 1.5M (Negotiable)")
check("video overlay never raises", VO(None, None, "x", []) == ("", "x", []))

# ---------------------------------------------------------------------------
print("\n[3] CEA details")
NC = NS["_normalize_cea_field"]
for f, raw, exp in [("cea_reg_no", "r123456a", ("R123456A", "")), ("cea_reg_no", " R 123456-A ", ("R123456A", "")),
                    ("agency_licence_no", "l3008899k", ("L3008899K", "")), ("cea_reg_no", "", ("", "")),
                    ("agency_name", "  NL   Realty ", ("NL Realty", ""))]:
    check(f"{f} {raw!r} -> {exp}", NC(f, raw) == exp, NC(f, raw))
for f, raw in [("cea_reg_no", "123456"), ("cea_reg_no", "R12345A"), ("agency_licence_no", "L300889K"),
               ("agency_licence_no", "R123456A")]:
    check(f"{f} {raw!r} rejected with a plain message", NC(f, raw)[1].startswith("That doesn't look like"))
check("missing fields listed", NS["_cea_missing"](BARE_AGENT) == ["cea_reg_no", "agency_name", "agency_licence_no"]
      and NS["_cea_missing"](CEA_AGENT) == [])
block = NS["_cea_text_block"](CEA_AGENT)
check("write-up block: name | phone, reg no, agency (licence)",
      block == "Janel Chee | 9123 4567\nCEA Reg No: R123456A\nAgency: NL Realty Pte Ltd (Licence No: L3008899K)", block)
txt = NS["_append_cea_block"]("Lovely home.\n\nYour Home. Your Next Chapter.", CEA_AGENT)
check("appended after the closing; idempotent", txt.endswith(block) and NS["_append_cea_block"](txt, CEA_AGENT) == txt)
check("incomplete profile -> text untouched", NS["_append_cea_block"]("x", BARE_AGENT) == "x")
check("only condo-family-only listings get CEA",
      NS["_home_listing_needs_cea"]("Condominium") and NS["_home_listing_needs_cea"]("Executive Condominium (EC)")
      and not NS["_home_listing_needs_cea"]("Penthouse") and not NS["_home_listing_needs_cea"]("Good Class Bungalow (GCB)")
      and not NS["_home_listing_needs_cea"]("Condominium", ["Condominium", "Semi-Detached"]))
check("licence line for the poster footer", NS["_cea_licence_line"](CEA_AGENT)
      == "CEA Reg No: R123456A · Agency: NL Realty Pte Ltd (Licence No: L3008899K)"
      and NS["_cea_licence_line"](BARE_AGENT) == "")

# profile endpoints
class _Q:
    def __init__(self, db, table):
        self.db, self.table, self.op, self.payload = db, table, "select", None

    def select(self, *a):
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def eq(self, *a):
        return self

    def limit(self, *a):
        return self

    def execute(self):
        self.db.log.append((self.table, self.op, self.payload))
        if self.table == "agents":
            if self.op == "update":
                self.db.agent.update(self.payload)
            return types.SimpleNamespace(data=[dict(self.db.agent)])
        if self.op == "select":
            return types.SimpleNamespace(data=[dict(self.db.listing)] if self.db.listing else [])
        return types.SimpleNamespace(data=[dict(self.db.listing or {"id": "L-new"}, **(self.payload or {}))])


class _DB:
    def __init__(self, agent=None, listing=None):
        self.agent, self.listing, self.log = dict(agent or {}), listing, []

    def table(self, name):
        return _Q(self, name)


def _use(db):
    NS["db_execute"] = lambda build, **k: build(db).execute()

    async def _a(build, **k):
        return build(db).execute()
    NS["db_execute_async"] = _a
    NS["get_db"] = lambda: db


run_nodes("update_profile", "patch_profile_cea", "get_profile")
PROFILE = dict(name="Janel", agency="NL", specialty="homes", tone="warm", emphasis="x", signature="s")
db = _DB(dict(BARE_AGENT, password_hash="h"))
_use(db)
out = NS["update_profile"](NS["ProfileUpdate"](**PROFILE), agent=db.agent)
upd = [e for e in db.log if e[1] == "update"][0][2]
check("PUT without CEA fields (older client): nothing CEA written", not any(f in upd for f in NS["CEA_PROFILE_FIELDS"]))
check("profile response always carries CEA keys + what is missing, never the hash",
      out["cea_reg_no"] == "" and out["cea_missing"] == ["cea_reg_no", "agency_name", "agency_licence_no"]
      and "password_hash" not in out and out["cea_details_supported"] is True)
db.log.clear()
out = NS["update_profile"](NS["ProfileUpdate"](**PROFILE, cea_reg_no="r123456a", agency_licence_no="l3008899k",
                                               agency_name="NL Realty Pte Ltd"), agent=db.agent)
check("PUT with CEA fields: normalised and saved", out["cea_reg_no"] == "R123456A"
      and out["agency_licence_no"] == "L3008899K" and out["cea_missing"] == [])
try:
    NS["update_profile"](NS["ProfileUpdate"](**PROFILE, cea_reg_no="12345"), agent=db.agent)
    check("PUT with a bad reg no -> 400", False)
except HTTPException as e:
    check("PUT with a bad reg no -> 400, nothing written", e.status_code == 400 and db.agent["cea_reg_no"] == "R123456A")
out = NS["patch_profile_cea"](NS["ProfileCEAUpdate"](agency_name="New Agency"), agent=db.agent)
check("PATCH one field changes only that one", out["agency_name"] == "New Agency" and out["cea_reg_no"] == "R123456A")
out = NS["patch_profile_cea"](NS["ProfileCEAUpdate"](cea_reg_no=""), agent=db.agent)
check("PATCH '' clears", out["cea_reg_no"] == "" and out["cea_missing"] == ["cea_reg_no"])
NS["_agent_cea_columns_supported"] = False
db.log.clear()
NS["update_profile"](NS["ProfileUpdate"](**PROFILE, cea_reg_no="R123456A"), agent=db.agent)
upd = [e for e in db.log if e[1] == "update"][0][2]
check("before migration 003: PUT still works, CEA silently not written", "cea_reg_no" not in upd)
try:
    NS["patch_profile_cea"](NS["ProfileCEAUpdate"](cea_reg_no="R123456A"), agent=db.agent)
    check("before migration 003: PATCH -> 503", False)
except HTTPException as e:
    check("before migration 003: PATCH -> clear 503", e.status_code == 503)
NS["_agent_cea_columns_supported"] = True
mig = io.open(os.path.join(BACKEND, "migrations", "003_agent_cea_details.sql"), encoding="utf-8").read()
check("migration 003: three columns, idempotent, RLS lock re-asserted",
      all(f"ADD COLUMN IF NOT EXISTS {c} text" in mig for c in ("cea_reg_no", "agency_name", "agency_licence_no"))
      and "REVOKE ALL ON TABLE agents FROM anon;" in mig and "REVOKE ALL ON TABLE agents FROM authenticated;" in mig
      and "ENABLE ROW LEVEL SECURITY" in mig and "DROP " not in mig.upper())

# public page agent block
pab = NS["_public_agent_block"]
check("public agent block, condo: CEA details", pab(CEA_AGENT, True)["cea_reg_no"] == "R123456A"
      and pab(CEA_AGENT, True)["agency_licence_no"] == "L3008899K")
check("public agent block, landed: exactly {name, agency} -- never reg/licence",
      pab(CEA_AGENT, False) == {"name": "Janel Chee", "agency": "NL Realty"})
check("public agent block, condo + incomplete: blank CEA keys", pab(BARE_AGENT, True)["cea_reg_no"] == "")

# ---------------------------------------------------------------------------
print("\n[4] owner consent")
CC = NS["_consent_attributes_for_create"]
CU = NS["_consent_attributes_for_update"]
LR = NS["ListingRequest"]
base = dict(property_type="Condominium", location="x", bedrooms="2", features="")
ticked = CC(LR(**base, owner_consent_confirmed=True))
check("create, ticked -> True + timestamp", ticked["owner_consent_confirmed"] is True
      and ticked["owner_consent_at"].startswith("20"))
check("create, explicitly NOT ticked -> recorded False (blocks posting)",
      CC(LR(**base, owner_consent_confirmed=False)) == {"owner_consent_confirmed": False})
check("create, not sent (stale cached client) -> NO key at all (posts like a legacy listing)",
      CC(LR(**base)) == {} and CC(LR(**base, owner_consent_confirmed=None)) == {} and CC(types.SimpleNamespace()) == {})
check("edit not sending consent: saved value untouched", CU({"owner_consent_confirmed": True}, LR(**base)) == {})
check("edit ticking: True + new timestamp",
      CU({"owner_consent_confirmed": False}, LR(**base, owner_consent_confirmed=True))["owner_consent_confirmed"] is True)
check("edit re-ticking keeps the original timestamp",
      CU({"owner_consent_confirmed": True, "owner_consent_at": "2026-10-01T00:00:00+00:00"},
         LR(**base, owner_consent_confirmed=True))["owner_consent_at"] == "2026-10-01T00:00:00+00:00")
check("edit unticking: False, timestamp cleared",
      CU({"owner_consent_confirmed": True, "owner_consent_at": "T"}, LR(**base, owner_consent_confirmed=False))
      == {"owner_consent_confirmed": False, "owner_consent_at": None})
B = NS["_owner_consent_blocks_posting"]
check("posting gate: new listing unconfirmed -> blocked",
      B({"attributes": {"owner_consent_confirmed": False}}))
check("posting gate: confirmed -> allowed", not B({"attributes": {"owner_consent_confirmed": True}}))
check("posting gate: listing from before this deploy (no key / no attributes) -> allowed, as before",
      not B({"attributes": {"project_name": "x"}}) and not B({}) and not B({"attributes": None}))

NS["requests"] = types.SimpleNamespace(post=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not post")))
NS["LINKEDIN_API_VERSION"] = "202401"
for model in ("FacebookPostRequest", "LinkedInPostRequest", "InstagramPostRequest"):
    run_nodes(model)
run_nodes("post_to_facebook", "post_to_linkedin", "post_to_instagram")
AG_POST = dict(CEA_AGENT, fb_page_id="p", fb_page_access_token="t", instagram_business_account_id="i",
               linkedin_access_token="t", linkedin_person_urn="u")
for fn, model in (("post_to_facebook", "FacebookPostRequest"), ("post_to_linkedin", "LinkedInPostRequest"),
                  ("post_to_instagram", "InstagramPostRequest")):
    _use(_DB(AG_POST, {"id": "L1", "poster_url": "https://x/p.jpg", "attributes": {"owner_consent_confirmed": False}}))
    try:
        NS[fn]("L1", NS[model](caption="hi"), agent=AG_POST)
        check(f"{fn}: unconfirmed new listing refused", False)
    except HTTPException as e:
        check(f"{fn}: unconfirmed new listing refused with a clear 409, nothing sent",
              e.status_code == 409 and "owner's consent" in e.detail, e.detail)
    _use(_DB(dict(AG_POST, fb_page_id=None, instagram_business_account_id=None, linkedin_access_token=None),
             {"id": "L1", "poster_url": "https://x/p.jpg"}))
    try:
        NS[fn]("L1", NS[model](caption="hi"), agent=dict(AG_POST, fb_page_id=None, instagram_business_account_id=None,
                                                          linkedin_access_token=None))
    except HTTPException as e:
        check(f"{fn}: pre-deploy listing (no consent key) passes the gate (stops later, as before)",
              e.status_code != 409 and "consent" not in e.detail, e.detail)

# ---------------------------------------------------------------------------
print("\n[5] EC eligibility")
y = datetime.utcnow().year
W = NS["_ec_compliance_warnings"]
check("EC under 5 years from TOP -> MOP warning", any("Minimum Occupation Period" in w for w in
                                                      W("Executive Condominium (EC)", y - 3, "")))
check("EC 5-10 years + all_buyers -> check eligibility", any("Citizens and PRs" in w for w in
                                                             W("Executive Condominium (EC)", y - 7, "all_buyers")))
check("EC 10+ years + citizens_prs -> check eligibility", any("open to all buyers" in w for w in
                                                              W("Executive Condominium (EC)", y - 12, "citizens_prs")))
check("EC consistent -> no warning", W("Executive Condominium (EC)", y - 7, "citizens_prs") == []
      and W("Executive Condominium (EC)", y - 12, "all_buyers") == [] and W("Executive Condominium (EC)", 0, "") == [])
check("not an EC -> never a warning", W("Condominium", y - 1, "all_buyers") == [] and W("Semi-Detached", y - 1, "") == [])


def req(**kw):
    base = dict(property_type="", bedrooms="", bathrooms="", storeys=0, built_up=0, land_size=0,
                features="", tenure="", tenure_detail="", floor_level="", project_name="", price="")
    base.update(kw)
    return types.SimpleNamespace(**base)


fb = NS["_build_writeup_facts_block"](req(property_type="Executive Condominium (EC)", ec_eligibility="citizens_prs"), "D")
check("EC FACTS: eligibility exactly from the field", "Buyer eligibility (state exactly this, nothing more): "
      "Singapore Citizens and Permanent Residents only" in fb, fb)
fb = NS["_build_writeup_facts_block"](req(property_type="Executive Condominium (EC)", top_year=y - 12), "D")
check("EC FACTS without the field: nothing about eligibility (never inferred from TOP)", "eligib" not in fb.lower())
fb = NS["_build_writeup_facts_block"](req(property_type="Condominium", ec_eligibility="all_buyers"), "D")
check("non-EC with the field set: no eligibility line", "eligib" not in fb.lower())
check("prompt rules: eligibility only from a FACTS line", 'unless FACTS\n  gives a "Buyer eligibility" line' in SRC
      and 'unless the facts give a "Buyer eligibility" line' in SRC)

# ---------------------------------------------------------------------------
print("\n[6] generate_listing: CEA on condo write-ups only, consent recorded, warnings")
SEEN = []


class _Resp:
    def __init__(self, text):
        self.content = [types.SimpleNamespace(text=text)]


async def fake_claude(**kw):
    SEEN.append(kw["messages"][0]["content"])
    return _Resp("Headline\n\nBody.\n\nYour Home. Your Next Chapter.")

NS.update({"create_claude_message": fake_claude, "_strip_house_number": lambda x: str(x or ""),
           "_derive_listing_code": lambda x: None, "_unique_listing_code": lambda *a, **k: "C",
           "MAX_LISTING_PHOTOS": 15, "WRITEUP_MAX_IMAGES": 8, "_scrub_own_street": lambda t, loc, d: t})
run_nodes("_generate_listing_text_only", "_resolve_writeup_district", "_writeup_district_str", "WRITING_STYLE_OPTIONS", "_apply_writing_style",
          "generate_listing", "update_listing", "_public_listing_payload")
db = _DB(CEA_AGENT)
_use(db)
out = asyncio.run(NS["generate_listing"](LR(property_type="Condominium", location="1 Sims Ave #05-11", bedrooms="2",
                                            features="", project_name="Parc Esta", owner_consent_confirmed=True,
                                            price="1500000"), agent=CEA_AGENT))
ins = [e for e in db.log if e[1] == "insert"][0][2]
check("condo write-up ends with the CEA block", ins["content"].endswith(NS["_cea_text_block"](CEA_AGENT)) and
      out["cea_missing"] == [], ins["content"][-120:])
check("consent recorded True with a timestamp", ins["attributes"]["owner_consent_confirmed"] is True
      and ins["attributes"]["owner_consent_at"])
db = _DB(BARE_AGENT)
_use(db)
out = asyncio.run(NS["generate_listing"](LR(property_type="Apartment", location="x", bedrooms="2", features=""),
                                         agent=BARE_AGENT))
ins = [e for e in db.log if e[1] == "insert"][0][2]
check("condo + incomplete CEA: no block, warning + cea_missing returned",
      "CEA Reg No" not in ins["content"] and out["cea_missing"] == ["cea_reg_no", "agency_name", "agency_licence_no"]
      and any("CEA details are incomplete" in w for w in out["compliance"]["warnings"]))
check("stale client (no consent sent) -> no consent key, so posting is not blocked",
      "owner_consent_confirmed" not in ins.get("attributes", {}))
db = _DB(CEA_AGENT)
_use(db)
out = asyncio.run(NS["generate_listing"](LR(property_type="Semi-Detached", location="9 X Road", bedrooms="4",
                                            features="", land_size=3000), agent=CEA_AGENT))
ins = [e for e in db.log if e[1] == "insert"][0][2]
check("new listing inserted with status 'active' explicitly (landed)", ins.get("status") == "active", ins.get("status"))
NS["_attributes_column_supported"] = False
db = _DB(CEA_AGENT)
_use(db)
asyncio.run(NS["generate_listing"](LR(property_type="Condominium", location="x", bedrooms="2", features="",
                                      owner_consent_confirmed=True), agent=CEA_AGENT))
ins_noattr = [e for e in db.log if e[1] == "insert"][0][2]
check("new listing inserted 'active' also before the attributes column exists",
      ins_noattr.get("status") == "active" and "attributes" not in ins_noattr)
NS["_attributes_column_supported"] = True
check("LANDED write-up: never the reg/licence numbers, no CEA warning, cea_missing []",
      "CEA" not in ins["content"] and "R123456A" not in ins["content"] and out["cea_missing"] == []
      and not any("CEA" in w for w in out["compliance"]["warnings"]))
db = _DB(CEA_AGENT)
_use(db)
out = asyncio.run(NS["generate_listing"](LR(property_type="Executive Condominium (EC)", location="x", bedrooms="3",
                                            features="", top_year=y - 2, ec_eligibility="citizens_prs"),
                                         agent=CEA_AGENT))
check("EC under MOP: warning in compliance", any("Minimum Occupation Period" in w for w in out["compliance"]["warnings"]))
check("EC eligibility reached the prompt from the field", "Singapore Citizens and Permanent Residents only" in SEEN[-1])

# update_listing returns compliance_warnings
db = _DB(CEA_AGENT, {"id": "L1", "code": "C", "property_type": "Executive Condominium (EC)",
                     "attributes": {"top_year": y - 3}})
_use(db)
row = NS["update_listing"]("L1", LR(property_type="Executive Condominium (EC)", location="x", bedrooms="3",
                                    features="", owner_consent_confirmed=True), agent=CEA_AGENT)
upd = [e for e in db.log if e[1] == "update"][0][2]
check("edit: consent ticked recorded", upd["attributes"]["owner_consent_confirmed"] is True)
check("edit: response carries compliance_warnings (MOP)", any("Minimum Occupation" in w for w in row["compliance_warnings"]))

# generate-writeup (standalone): CEA appended for condo only
async def fake_photos_writeup(agent, req, urls, district, context=None, price=None):
    return "Body.\n\nYour Home. Your Next Chapter."
NS.update({"_generate_writeup_from_photos": fake_photos_writeup, "_generate_writeup_hits": {},
           "WRITEUP_HOURLY_LIMIT_PER_WORKER": 8, "WRITEUP_UNAVAILABLE_DETAIL": "x"})
run_nodes("generate_writeup")
WR = NS["WriteupRequest"]
r = asyncio.run(NS["generate_writeup"](WR(property_type="Condominium"), agent=CEA_AGENT))
check("generate-writeup condo: CEA block appended", r["writeup"].endswith(NS["_cea_text_block"](CEA_AGENT))
      and r["cea_missing"] == [] and r["warnings"] == [])
r = asyncio.run(NS["generate_writeup"](WR(property_type="Condominium"), agent=BARE_AGENT))
check("generate-writeup condo, incomplete CEA: warning", "CEA" not in r["writeup"] and r["warnings"]
      and r["cea_missing"])
r = asyncio.run(NS["generate_writeup"](WR(property_type="Condominium", property_types=["Condominium", "Semi-Detached"]),
                                       agent=CEA_AGENT))
check("N1 generate-writeup condo + landed types: no CEA", "CEA" not in r["writeup"] and r["cea_missing"] == [])
r = asyncio.run(NS["generate_writeup"](WR(property_type="Condominium", property_types=["Condominium", "Apartment"]),
                                       agent=CEA_AGENT))
check("N1 generate-writeup all condo types: CEA", r["writeup"].endswith(NS["_cea_text_block"](CEA_AGENT)))
r = asyncio.run(NS["generate_writeup"](WR(property_type="Detached/Bungalow"), agent=CEA_AGENT))
check("generate-writeup landed: unchanged text, no CEA", r["writeup"] == "Body.\n\nYour Home. Your Next Chapter."
      and r["cea_missing"] == [] and r["warnings"] == [])

# seller lead: recorded as not-yet-confirmed
run_nodes("SellerLeadRequest", "_seller_lead_payload", "create_seller")
db = _DB(CEA_AGENT)
_use(db)
NS["create_seller"](NS["SellerLeadRequest"](**{k: v for k, v in dict(
    seller_name="S", location="x", property_type="Condominium").items()}), agent=CEA_AGENT)
ins = [e for e in db.log if e[1] == "insert"][0][2]
check("new seller lead without consent: no key (no attributes written)", "attributes" not in ins)
check("new seller lead still created as 'lead' (never public)", ins.get("status") == "lead")
db = _DB(CEA_AGENT)
_use(db)
NS["create_seller"](NS["SellerLeadRequest"](seller_name="S", location="x", owner_consent_confirmed=False), agent=CEA_AGENT)
check("new seller lead with consent explicitly false: recorded",
      [e for e in db.log if e[1] == "insert"][0][2]["attributes"] == {"owner_consent_confirmed": False})
NS["_attributes_column_supported"] = False
db = _DB(CEA_AGENT)
_use(db)
NS["create_seller"](NS["SellerLeadRequest"](seller_name="S", location="x", property_type="Condominium"), agent=CEA_AGENT)
check("new seller lead before migration 002: no attributes written",
      "attributes" not in [e for e in db.log if e[1] == "insert"][0][2])
NS["_attributes_column_supported"] = True

# CEA column probe
run_nodes("_probe_agent_cea_columns")
_use(_DB(CEA_AGENT))
NS["_probe_agent_cea_columns"]()
check("CEA probe: columns present -> supported", NS["_agent_cea_columns_supported"] is True)


class _Boom:
    def table(self, n):
        raise RuntimeError("column agents.cea_reg_no does not exist")
NS["get_db"] = lambda: _Boom()
NS["_probe_agent_cea_columns"]()
check("CEA probe: missing columns -> not supported, never raises", NS["_agent_cea_columns_supported"] is False)
NS["_agent_cea_columns_supported"] = True
_su = "\n".join(LINES[NODES["startup_event"].lineno - 1:NODES["startup_event"].end_lineno])
check("startup runs the CEA probe", "_probe_agent_cea_columns" in _su)
check("video render passes the Home overlay contact to the renderer",
      SRC.count("video_contact_line, price_text, stats = _home_video_overlay(listing, agent, price_text, stats)") == 1
      # ALL THREE video templates feed the renderer the Home overlay's contact line --
      # the Classic through the tuple above, the Dramatic and the Elegant through their
      # own calls (neither draws a price or stats, so those go in empty and the contact
      # is all it takes). Counted, so a FOURTH template that forgets the CEA lines is
      # noticed here. The Elegant was the third, added 2026-10-27, and this check is how
      # its CEA lines were noticed.
      and SRC.count("agent_contact_line=video_contact_line,") == 3
      and SRC.count('_home_video_overlay(listing, agent, "", [])[0]') == 2)

# ---------------------------------------------------------------------------
print("\n[7] public page")
db = _DB(CEA_AGENT)
_use(db)
pub = NS["_public_listing_payload"]({"id": "L1", "agent_id": "a1", "property_type": "Executive Condominium (EC)",
                                     "location": "1 Canberra Dr #05-11", "district": "27", "price": "1300000",
                                     "content": "Live at Parc Canberra.", "features": "",
                                     "attributes": {"project_name": "Parc Canberra", "price_label": "From",
                                                    "ec_eligibility": "citizens_prs", "top_year": 2020}})
check("EC page: project name, price, price label, eligibility, CEA agent block",
      pub["project_name"] == "Parc Canberra" and pub["price"] == "1300000" and pub["price_label"] == "From"
      and pub["ec_eligibility"] == "citizens_prs" and pub["agent"]["cea_reg_no"] == "R123456A"
      and "Parc Canberra" in pub["content"], pub)
check("EC page: still no street, unit or TOP year", "Canberra Dr" not in repr(pub) and "05-11" not in repr(pub)
      and "2020" not in repr(pub))
sel = [e for e in db.log if e[0] == "agents"]
pub_l = NS["_public_listing_payload"]({"id": "L2", "agent_id": "a1", "property_type": "Good Class Bungalow (GCB)",
                                       "location": "1 Nassim Road", "price": "30000000", "content": "x",
                                       "attributes": {"price_label": "From", "project_name": "X"}})
check("LANDED page: no project name, no price label, no eligibility, agent = {name, agency} only",
      pub_l["project_name"] == "" and pub_l["price_label"] == "" and pub_l["ec_eligibility"] == ""
      and set(pub_l["agent"]) == {"name", "agency"})

print("\n[7b] N2: CEA block never served on a non-Home listing")
BLOCK = NS["_cea_text_block"](CEA_AGENT)
WITH = "A lovely home.\n\nYour Home. Your Next Chapter.\n\nJanel | NL Realty Specialist\n\n" + BLOCK
st = NS["_strip_cea_block"](WITH)
check("strip removes name|phone line, reg no and agency licence, keeps the rest",
      "CEA Reg No" not in st and "Licence No" not in st and "9123 4567" not in st
      and st.endswith("Janel | NL Realty Specialist"), repr(st))
check("strip: one-line poster form removed too",
      NS["_strip_cea_block"]("x\n" + NS["_cea_licence_line"](CEA_AGENT)) == "x")
check("strip leaves ordinary text alone (incl. a '|' line not above a CEA line)",
      NS["_strip_cea_block"]("A | B\nAgency: we love it") == "A | B\nAgency: we love it")
check("strip: a text that is only the block comes back empty; None/'' never raise",
      NS["_strip_cea_block"](BLOCK) == "" and NS["_strip_cea_block"](None) is None
      and NS["_strip_cea_block"]("") == "")
H = NS["_home_safe_text"]
check("Home listing keeps its block", H({"property_type": "Condominium"}, WITH) == WITH)
check("type changed to landed -> block gone", "CEA Reg No" not in H({"property_type": "Semi-Detached"}, WITH))
check("mixed condo + landed -> block gone",
      "CEA" not in H({"property_type": "Condominium", "property_types": ["Condominium", "Corner Terrace"]}, WITH))
db = _DB(CEA_AGENT)
_use(db)
pub = NS["_public_listing_payload"]({"id": "L9", "agent_id": "a1", "property_type": "Semi-Detached",
                                     "location": "9 X Road", "price": "1", "content": WITH})
check("buyer page, ex-condo now landed: no CEA in content", "CEA Reg No" not in pub["content"]
      and "Licence" not in pub["content"])
run_nodes("_pg_export_price", "_pg_export_count", "_pg_export_measure", "_build_propertyguru_text_block")
pg = NS["_build_propertyguru_text_block"]({"property_type": "Detached/Bungalow", "location": "x", "content": WITH})
check("PropertyGuru export, landed: no CEA", "CEA Reg No" not in pg)
pg = NS["_build_propertyguru_text_block"]({"property_type": "Condominium", "location": "x", "content": WITH})
check("PropertyGuru export, condo: CEA kept", "CEA Reg No: R123456A" in pg)
check("My Listings rows are served through the same strip",
      'row["content"] = _outward_content(row, row.get("content"))' in SRC)

# ---------------------------------------------------------------------------
print("\n[8] poster: condo price + CEA footer; landed poster byte-identical")
import poster_renderer as PR  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

def _git_show(spec):
    """Source of a file at a pushed commit, or None when not inside the git repo (e.g. a
    deploy image) -- the golden comparisons are then SKIPPED, never an error."""
    try:
        r = subprocess.run(["git", "show", spec], cwd=REPO, capture_output=True, text=True, timeout=30)
        return r.stdout if r.returncode == 0 and r.stdout else None
    except Exception:
        return None


args = dict(property_type="SEMI-DETACHED", district="DISTRICT 15", price_text="SGD 4.2M",
            stats=["4 Rooms", "3 Baths"], agent_name="Janel", agent_contact_line="9123 4567")
old_src = _git_show("7ef82be:backend/poster_renderer.py")
check("golden source lookup outside git -> None (test skips, never errors)",
      _git_show("0000000000000000000000000000000000000000:nope") is None)
if old_src is None:
    print("  SKIP  landed poster golden (not inside the git repo)")
else:
    OLD_PR = types.ModuleType("poster_renderer_old")
    OLD_PR.__file__ = os.path.join(BACKEND, "poster_renderer.py")
    exec(compile(old_src, OLD_PR.__file__, "exec"), OLD_PR.__dict__)
    same = True
    # The golden set is the templates that EXISTED at 7ef82be. Anything added since (the
    # Bold family) has no counterpart there -- iterating PR.TEMPLATES instead would compare
    # a new template against the old module's unknown-id fallback and always "fail".
    for tid in OLD_PR.TEMPLATES:
        if PR.render_poster(**args, template_id=tid).tobytes() != OLD_PR.render_poster(**args, template_id=tid).tobytes():
            same = False
    check(f"no compliance line: all {len(OLD_PR.TEMPLATES)} templates pixel-identical to before", same)
band = PR.render_poster(**args, template_id="editorial", compliance_line=NS["_cea_licence_line"](CEA_AGENT))
check("with the CEA line: still 1200x1500 (Instagram 4:5), differs from the plain poster",
      band.size == (1200, 1500) and band.tobytes() != PR.render_poster(**args, template_id="editorial").tobytes())
check("band never raises on junk (returns the poster)", PR._add_compliance_band(Image.new("RGB", (10, 10)), "x").size
      in ((1200, 1500), (10, 10)))

POSTER_CALLS = []
# The photograph the concept crop is handed. None = nothing to crop, so fal gets the
# original url -- which is what every case here wants except the crop case itself.
CONCEPT_PHOTO = [None]
# None = mimic a healthy render (the concept is used when one was supplied); False =
# force the renderer's own fallback, which is what the guards do when the concept cannot
# be shown honestly. That second case is the one the API used to lie about.
CONCEPT_RESULT = [None]


def _fake_render_report(**k):
    POSTER_CALLS.append(k)
    used = (bool(k.get("makeover_photo_url")) if CONCEPT_RESULT[0] is None
            else CONCEPT_RESULT[0])
    return PR.PosterResult(Image.new("RGB", (4, 4)), used)


NS["poster_renderer"] = types.SimpleNamespace(
    render_poster=lambda **k: _fake_render_report(**k).image,
    render_poster_report=_fake_render_report,
    PosterResult=PR.PosterResult,
    # The Potential paths use the REAL module functions, so this exercises the actual
    # budget/cache/billing code rather than a second copy of it.
    makeover_image=PR.makeover_image,
    # The crop that happens BEFORE fal sees the photograph. Injected rather than
    # downloading: None means "no photo to crop", which is the uncropped path every case
    # below but the crop case itself takes.
    concept_source_photo=lambda url: CONCEPT_PHOTO[0],
    POTENTIAL_SOURCE_ROLE=PR.POTENTIAL_SOURCE_ROLE,
    read_makeover_cache=PR.read_makeover_cache,
    read_makeover_approvals=PR.read_makeover_approvals,
    write_makeover_cache=PR.write_makeover_cache,
    potential_cache_key=PR.potential_cache_key,
    POTENTIAL_BUDGET_SECONDS=PR.POTENTIAL_BUDGET_SECONDS,
    MAX_POTENTIAL_MAKEOVERS_PER_AGENT_PER_MONTH=PR.MAX_POTENTIAL_MAKEOVERS_PER_AGENT_PER_MONTH,
)
NS["POSTER_TEMPLATES"] = [{"id": "editorial"}]
# The Potential style's module-level dependencies. Off by default, so the three CEA cases
# below take exactly the path they always did.
NS["POTENTIAL_TEMPLATE_ID"] = "potential"
NS["POTENTIAL_MONTH_COLUMN"] = PR.POTENTIAL_USAGE_MONTH_COLUMN
NS["POTENTIAL_COUNT_COLUMN"] = PR.POTENTIAL_USAGE_COUNT_COLUMN
NS["POTENTIAL_CACHE_COLUMN"] = "potential_images"
NS["POTENTIAL_CONCEPT_NOT_MADE_WARNING"] = (
    "The concept for this photo hasn't been made yet, so this poster uses the real "
    "photo. Make the concept first and take a look at it.")
# Mirrors main.py: Potential is only offered when it is available.
NS["_poster_templates_offered"] = lambda: (
    [t for t in NS["POSTER_TEMPLATES"] if t["id"] != "potential"]
    + ([t for t in NS["POSTER_TEMPLATES"] if t["id"] == "potential"]
       if NS["_potential_available"]() else []))
RATE_ASKED = []
NS["_poster_concept_hits"] = {}
NS["_poster_concept_burst_hits"] = {}
NS["_rate_limited"] = lambda store, key, limit=5, window_seconds=3600, record=True: (
    RATE_ASKED.append((limit, window_seconds)), RATE_BLOCK[0])[1]
RATE_BLOCK = [False]
NS["_potential_available"] = lambda: False
NS["_potential_makeovers_used"] = lambda agent_id: 0


class _Storage:
    def from_(self, b):
        return self

    def upload(self, *a, **k):
        return None

    def get_public_url(self, f):
        return "https://cdn/" + f


class _DBS(_DB):
    storage = _Storage()


run_nodes("generate_poster")
for ptype, agent, expect_line, expect_price in [
        ("Condominium", CEA_AGENT, NS["_cea_licence_line"](CEA_AGENT), "SGD 1.5M (Negotiable)"),
        ("Condominium", BARE_AGENT, "", "SGD 1.5M (Negotiable)"),
        ("Semi-Detached", CEA_AGENT, "", "SGD 1.5M")]:
    db = _DBS(agent, {"id": "L1", "property_type": ptype, "price": "1500000", "images": ["https://img/1.jpg"],
                      "attributes": {"price_label": "Negotiable"}})
    _use(db)
    res = NS["generate_poster"]("L1", agent=agent)
    k = POSTER_CALLS[-1]
    check(f"poster {ptype} ({'CEA' if not NS['_cea_missing'](agent) else 'no CEA'}): price {expect_price!r}, "
          f"footer {'set' if expect_line else 'blank'}",
          k["price_text"] == expect_price and k["compliance_line"] == expect_line
          and (res["cea_missing"] == [] if (expect_line or ptype == "Semi-Detached") else res["warnings"]), (k, res))

# ---------------------------------------------------------------------------
# The Potential style is the only poster that spends money, and generate_poster cannot be
# imported (no supabase/anthropic here), so this is the only place its wiring can
# actually be RUN rather than read. Three things are worth proving: that the style off
# spends nothing, that the style on forwards the concept and counts what fal charged in
# the POTENTIAL columns (not the video ones), and that fal failing still yields a poster.
print("\n[8b] potential poster: generate-poster is a GATE, and never spends")

# Jane's decision of 2026-11-12: a human looks at every AI makeover before any buyer
# does. So generate-poster cannot generate a concept at all -- it can only use one that
# is cached AND approved. generate_poster cannot be imported (no supabase/anthropic
# here), so this is the only place the gate can be RUN rather than read.

TALLY, STORED, MERGED = [], [], []
NS["_add_old_to_new_images"] = lambda agent_id, n, month_column=None, value_column=None, unit=None: (
    TALLY.append((agent_id, n, month_column, value_column, unit)), True)[1]
NS["_store_generated_image"] = lambda lid, role, key, data: (
    STORED.append((lid, role, key)), "https://cdn/makeover.jpg")[1]
NS["_merge_potential_cache"] = lambda lid, aid, added=None, approve=None: (
    MERGED.append((lid, aid, added, approve)), True)[1]

PHOTO = "https://img/1.jpg"
KEY = PR.potential_cache_key(PHOTO)


def _listing(concepts=None, approved=None):
    return {"id": "L1", "property_type": "Semi-Detached", "price": "1500000",
            "images": [PHOTO],
            "potential_images": PR.write_makeover_cache(concepts or {}, approved or [])}


def _poster(available=True, listing=None, concept=None, **kw):
    TALLY.clear(), STORED.clear(), POSTER_CALLS.clear(), MERGED.clear()
    NS["POSTER_TEMPLATES"] = [{"id": "editorial"}, {"id": "potential"}]
    NS["_potential_available"] = lambda: available
    CONCEPT_RESULT[0] = concept
    _use(_DBS(BARE_AGENT, dict(listing or _listing())))
    try:
        return NS["generate_poster"]("L1", template_id="potential", agent=BARE_AGENT, **kw)
    finally:
        CONCEPT_RESULT[0] = None
        NS["POSTER_TEMPLATES"] = [{"id": "editorial"}]
        NS["_potential_available"] = lambda: False


res = _poster()
check("no concept made yet: real photo, nothing bought, and the agent is told",
      POSTER_CALLS[-1]["makeover_photo_url"] is None and res["concept_used"] is False
      and not TALLY and not STORED
      and any("hasn't been made yet" in w for w in res["warnings"]), res["warnings"])

# --- the other fault found by running this for real on 2026-11-17 ----------
# The poster showed 5 rooms, 5,868 sqft built-up and SGD 1,875 psf, and left 5,573 sqft of
# LAND off the page -- the one number a landed buyer leads with. The cause was that the
# endpoint only ever passed the legacy four-slot `stats`, so the renderer derived its facts
# from that; `land_size` has never been in it. These four cases pin the fix: land size is
# on, bathrooms is the slot it took, a listing without land shows three facts rather than an
# empty slot, and no other template's input changes.
MUGLISTON = dict(_listing({KEY: "https://cdn/c.jpg"}, [KEY]))
MUGLISTON.update({"land_size": 5573, "built_up": 5868, "bedrooms": "5",
                  "bathrooms": "6", "price": "11000000"})
res = _poster(listing=MUGLISTON)
facts = POSTER_CALLS[-1]["facts"] or []
check("the Potential poster is handed land size FIRST, then built-up, rooms, psf",
      [(f["label"], f["value"], f["unit"]) for f in facts]
      == [("Land size", "5,573", "sqft"), ("Built-up", "5,868", "sqft"),
          ("Rooms", "5", ""), ("Price", "1,875", "psf")], facts)
check("bathrooms is the slot land size took, so it is not among them",
      not any("ath" in f["label"] for f in facts))

no_land = dict(MUGLISTON)
no_land["land_size"] = 0
_poster(listing=no_land)
blank = [f for f in (POSTER_CALLS[-1]["facts"] or []) if not f["value"]]
check("no land area: the item is blank so the renderer drops it -- nothing is invented",
      len(blank) == 1 and blank[0]["label"] == "Land size",
      POSTER_CALLS[-1]["facts"])

NS["POSTER_TEMPLATES"] = [{"id": "editorial"}]
_use(_DBS(BARE_AGENT, dict(MUGLISTON)))
NS["generate_poster"]("L1", template_id="editorial", agent=BARE_AGENT)
check("every other template is handed facts=None, exactly as before",
      POSTER_CALLS[-1]["facts"] is None and POSTER_CALLS[-1]["template_id"] == "editorial")

res = _poster(listing=_listing({KEY: "https://cdn/c.jpg"}))
check("CONCEPT CACHED BUT NOT APPROVED: still the real photo -- the gate holds",
      POSTER_CALLS[-1]["makeover_photo_url"] is None and res["concept_used"] is False
      and any("hasn't been approved" in w for w in res["warnings"]), res["warnings"])

res = _poster(listing=_listing({KEY: "https://cdn/c.jpg"}, [KEY]))
check("cached AND approved: the concept is used, and nothing is bought",
      POSTER_CALLS[-1]["makeover_photo_url"] == "https://cdn/c.jpg"
      and res["concept_used"] is True and not TALLY and not STORED, res)

res = _poster(listing=_listing({KEY: "https://cdn/c.jpg"}), approve_concept_key=KEY)
check("pressing Use it approves that exact concept and renders it",
      POSTER_CALLS[-1]["makeover_photo_url"] == "https://cdn/c.jpg"
      and res["concept_used"] is True
      and MERGED and MERGED[-1][3] == [KEY], MERGED)

res = _poster(listing=_listing({KEY: "https://cdn/c.jpg"}),
              approve_concept_key="v1.deadbeef:poster_potential:somethingelse")
check("an approval for a DIFFERENT concept approves nothing (a reworded prompt)",
      POSTER_CALLS[-1]["makeover_photo_url"] is None
      and res["concept_used"] is False and not MERGED, (res, MERGED))

res = _poster(approve_concept_key=KEY)
check("you cannot approve a concept that does not exist",
      POSTER_CALLS[-1]["makeover_photo_url"] is None and not MERGED, MERGED)

res = _poster(available=False, listing=_listing({KEY: "https://cdn/c.jpg"}, [KEY]))
check("the kill switch means off, even for an approved concept",
      POSTER_CALLS[-1]["makeover_photo_url"] is None and res["concept_used"] is False)

# Finding 5. The switch is the backout, so flipping it off must not break the button.
check("switched off, asked for by name: falls back instead of 400",
      res["poster_template_id"] == "editorial"
      and any("isn't available right now" in w for w in res["warnings"]), res)

off_listing = dict(_listing({KEY: "https://cdn/c.jpg"}, [KEY]))
off_listing["poster_template_id"] = "potential"
TALLY.clear(), POSTER_CALLS.clear()
NS["POSTER_TEMPLATES"] = [{"id": "editorial"}, {"id": "potential"}]
NS["_potential_available"] = lambda: False
_use(_DBS(BARE_AGENT, off_listing))
try:
    res = NS["generate_poster"]("L1", agent=BARE_AGENT)
finally:
    NS["POSTER_TEMPLATES"] = [{"id": "editorial"}]
check("switched off, stored as the listing default: still renders, still warns",
      res["poster_url"] and res["poster_template_id"] == "editorial"
      and any("isn't available right now" in w for w in res["warnings"]), res)
check("the agent's real choice is not overwritten during a backout",
      "if template_id and chosen_template_id == template_id:" in SRC)

try:
    NS["POSTER_TEMPLATES"] = [{"id": "editorial"}]
    _use(_DBS(BARE_AGENT, dict(_listing())))
    NS["generate_poster"]("L1", template_id="no-such-style", agent=BARE_AGENT)
    check("a genuinely unknown style is still refused", False)
except HTTPException as e:
    check("a genuinely unknown style is still refused with 400",
          e.status_code == 400 and "Unknown" in e.detail, e.detail)

# F1 still holds on top of the gate: approved is not the same as drawn.
res = _poster(listing=_listing({KEY: "https://cdn/c.jpg"}, [KEY]), concept=False)
check("approved but the guards refused it: concept_used FALSE and the agent is told",
      res["concept_used"] is False
      and any("could not be shown" in w for w in res["warnings"]), res["warnings"])

# A TRIPWIRE, NOT A PROOF, and nobody should cite it as one. It scans the source text of
# generate_poster for a direct call, so it catches someone re-adding the obvious line and
# would miss a call smuggled through a lambda or a helper. What actually keeps this
# endpoint free is that it has no path to makeover_image at all; this just makes the
# obvious regression loud.
check("tripwire: no direct fal call in generate_poster's own source",
      "fal_images.generate" not in SRC.split("def generate_poster(")[1].split("\ndef ")[0])

# ---------------------------------------------------------------------------
print("\n[8c] the concept endpoint: the only place that spends")

NS["_potential_allowance"] = None
run_nodes("_potential_allowance", "_potential_listing_or_404", "_potential_photo_state",
          "get_poster_concept_state", "generate_poster_concept")


def _ok_generate(prompt, urls, deadline):
    return b"\xff\xd8jpeg-bytes"


def _concept(generate=_ok_generate, used=0, listing=None, available=True, **kw):
    TALLY.clear(), STORED.clear(), MERGED.clear()
    NS["_potential_available"] = lambda: available
    NS["_potential_makeovers_used"] = lambda agent_id: used
    NS["fal_images"] = types.SimpleNamespace(generate=generate)
    _use(_DBS(BARE_AGENT, dict(listing or _listing())))
    os.environ["FAL_KEY"] = "local-injection-not-a-real-key"
    try:
        return NS["generate_poster_concept"]("L1", agent=BARE_AGENT, **kw)
    finally:
        os.environ.pop("FAL_KEY", None)
        NS["_potential_available"] = lambda: False
        NS["_potential_makeovers_used"] = lambda agent_id: 0


res = _concept()
check("a first concept is generated, stored and returned FOR REVIEW -- no poster",
      res["concept_url"] == "https://cdn/makeover.jpg" and res["photo_url"] == PHOTO
      and res["concept_key"] == KEY and res["from_cache"] is False
      and "poster_url" not in res, res)
check("one image billed to the POTENTIAL columns",
      TALLY == [(BARE_AGENT["id"], 1, PR.POTENTIAL_USAGE_MONTH_COLUMN,
                 PR.POTENTIAL_USAGE_COUNT_COLUMN, "concept image")], TALLY)
check("the concept is cached by MERGE, and is NOT approved by being made",
      MERGED and MERGED[-1][2] == {KEY: "https://cdn/makeover.jpg"}
      and MERGED[-1][3] is None, MERGED)
check("the allowance reported is what is left AFTER this one",
      res["allowance"] == {"used": 1, "limit": 20, "remaining": 19}, res["allowance"])

res = _concept(listing=_listing({KEY: "https://cdn/c.jpg"}))
check("a cached concept is free to look at again",
      res["concept_url"] == "https://cdn/c.jpg" and res["from_cache"] is True
      and not TALLY and not MERGED, res)

# --- the crop, wired through the real endpoint -----------------------------
# The two faults found by running this for real on 2026-11-17: a parked lorry the model
# refused to remove whatever the prompt said, and a poster with no land size on it. This
# half is the lorry. The endpoint must send fal a CROPPED copy of the photograph, stored
# under its own role, and must still produce a concept when the crop cannot be made.
SENT_TO_FAL = []


def _watch_generate(prompt, urls, deadline):
    SENT_TO_FAL.append(list(urls))
    return b"\xff\xd8jpeg-bytes"


WIDE_PHOTO = Image.new("RGB", (640, 480), (150, 150, 145))
for _x in range(128, 512, 40):
    ImageDraw.Draw(WIDE_PHOTO).rectangle((_x, 0, _x + 20, 480), fill=(30, 30, 30))

CONCEPT_PHOTO[0] = WIDE_PHOTO
SENT_TO_FAL.clear()
try:
    res = _concept(generate=_watch_generate)
finally:
    CONCEPT_PHOTO[0] = None
check("the concept is generated from a CROPPED copy, not the photo as shot",
      res["concept_url"] == "https://cdn/makeover.jpg"
      and SENT_TO_FAL and SENT_TO_FAL[-1] != [PHOTO], SENT_TO_FAL)
check("the crop is uploaded under its own role, so it can never be mistaken for a concept",
      any(role == PR.POTENTIAL_SOURCE_ROLE for _lid, role, _key in STORED)
      and any(role == PR.POTENTIAL_ROLE for _lid, role, _key in STORED), STORED)
check("only the CONCEPT is cached against the listing -- never the crop",
      MERGED and MERGED[-1][2] == {KEY: "https://cdn/makeover.jpg"}, MERGED)

SENT_TO_FAL.clear()
res = _concept(generate=_watch_generate)
check("no photo to crop: fal gets the original and a concept still comes out",
      res["concept_url"] == "https://cdn/makeover.jpg"
      and SENT_TO_FAL[-1] == [PHOTO], SENT_TO_FAL)


class _Billed(PR.fal_images.FalTemporary):
    images_billed = 1


def _incident(prompt, urls, deadline):
    raise _Billed("every collection attempt failed")


res = _concept(generate=_incident)
check("fal incident: no concept, a reason, and the picture fal CHARGED for is counted",
      res["concept_url"] is None and res["warnings"] and TALLY[0][1] == 1, (res, TALLY))


def _busy(prompt, urls, deadline):
    raise PR.fal_images.FalRateLimited("at this account's concurrency limit")


res = _concept(generate=_busy)
check("fal busy: nothing billed, a reason the agent can act on",
      res["concept_url"] is None and not TALLY
      and any("busy" in w for w in res["warnings"]), res["warnings"])

res = _concept(used=PR.MAX_POTENTIAL_MAKEOVERS_PER_AGENT_PER_MONTH)
check("over the ceiling: nothing generated, nothing billed, allowance reads zero left",
      res["concept_url"] is None and not TALLY
      and res["allowance"]["remaining"] == 0
      and any("allowance" in w for w in res["warnings"]), res)

res = _concept(used=None)
check("the tally could not be READ: nothing generated and no number is shown",
      res["concept_url"] is None and not TALLY and res["allowance"] is None
      and res["warnings"], res)

try:
    _concept(available=False)
    check("the style being off refuses the concept call", False)
except HTTPException as e:
    check("the style being off refuses the concept call with 400 and a reason",
          e.status_code == 400 and "not switched on" in e.detail, e.detail)

# The free state call the app uses to label the button before anything is pressed.
_use(_DBS(BARE_AGENT, _listing({KEY: "https://cdn/c.jpg"}, [KEY])))
NS["_potential_available"] = lambda: True
try:
    state = NS["get_poster_concept_state"]("L1", agent=BARE_AGENT)
finally:
    NS["_potential_available"] = lambda: False
# Finding 2. The one paid signed-in endpoint now has a short window and an hourly one.
check("the concept endpoint asks for a burst limit and an hourly limit",
      (8, 60) in RATE_ASKED and (30, 3600) in RATE_ASKED, RATE_ASKED)
RATE_BLOCK[0] = True
try:
    _concept()
    check("a rate-limited press is refused", False)
except HTTPException as e:
    check("a rate-limited press is refused with 429 and nothing is bought",
          e.status_code == 429 and not TALLY, (e.status_code, TALLY))
finally:
    RATE_BLOCK[0] = False

check("the state call reports per-photo concept and approval, and spends nothing",
      state["available"] is True
      and state["photos"] == [{"photo_index": 0, "concept_key": KEY,
                               "photo_url": PHOTO, "concept_url": "https://cdn/c.jpg",
                               "has_concept": True, "approved": True}]
      and state["allowance"] == {"used": 0, "limit": 20, "remaining": 20}, state)
# The picker's contract with the frontend. `slow`/`slow_seconds` appear on Potential and
# on nothing else -- it is the only template that waits on a third party, and the app has
# to be able to warn before the press rather than leave an agent watching a spinner.
NS["POTENTIAL_POSTER_TEMPLATE"] = {"id": "potential", "name": "Potential",
                                   "thumbnail_url": "https://t/potential.jpg"}
NS["POSTER_TEMPLATES"] = [{"id": "editorial", "name": "Editorial",
                           "thumbnail_url": "https://t/editorial.jpg"}]
run_nodes("_poster_templates_offered", "get_poster_templates")
NS["_potential_available"] = lambda: True
rows = NS["get_poster_templates"](agent=BARE_AGENT)
check("picker: Potential offered when available, marked slow; others unmarked",
      [r["id"] for r in rows] == ["editorial", "potential"]
      and "slow" not in rows[0] and rows[1]["slow"] is True
      and rows[1]["slow_seconds"] == PR.POTENTIAL_BUDGET_SECONDS + 15, rows)
NS["_potential_available"] = lambda: False
check("picker: Potential absent entirely when it is not available",
      [r["id"] for r in NS["get_poster_templates"](agent=BARE_AGENT)] == ["editorial"])
check("the thumbnail the picker asks for is derived from the shared base",
      'f"{POSTER_THUMBNAIL_BASE}/potential.jpg"' in SRC)

# ---------------------------------------------------------------------------
print("\n[9] video end slate: extra lines drawn; single-line contact pixel-identical")
import video_renderer as VR  # noqa: E402
old_vr = _git_show("7ef82be:backend/video_renderer.py")
face = Image.new("RGB", (400, 500), (120, 90, 70))
if old_vr is None:
    print("  SKIP  landed end-slate golden (not inside the git repo)")
else:
    tree = ast.parse(old_vr)
    fn = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_render_contact_card"][0]
    old_ns = dict(VR.__dict__)
    exec(compile("\n".join(old_vr.splitlines()[fn.lineno - 1:fn.end_lineno]), "old_vr", "exec"), old_ns)
    for photo in (None, face):
        check(f"single-line contact ({'photo' if photo else 'no photo'}): identical to before",
              VR._render_contact_card("Janel", "9123 4567", photo).tobytes()
              == old_ns["_render_contact_card"]("Janel", "9123 4567", photo).tobytes())
multi = VR._render_contact_card("Janel", "9123 4567\nSGD 1.5M\nCEA Reg No: R123456A\nAgency: X (Licence No: L3008899K)", face)
check("multi-line contact renders (and differs)", multi.size == (VR.VW, VR.VH)
      and multi.tobytes() != VR._render_contact_card("Janel", "9123 4567", face).tobytes())

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
