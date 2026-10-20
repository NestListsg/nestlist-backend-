"""Buyer-facing links stop working when a listing is moved to Deleted Listings, and work
again when it is restored. Run:  python3 tests/test_public_availability.py  (exit 1 on fail)

Covers every unauthenticated route that serves listing data:
  GET  /api/public/listings/{id}            (full uuid and the 8-hex short link)
  GET  /api/public/enquiry/{handle}/{code}  (memorable link)
  POST /api/public/enquiries                (enquiry form submit)
and that the agent's own authed views (My Listings, Deleted Listings) are unchanged.

main.py cannot be imported here (no supabase/anthropic locally); its functions are
EXECUTED from source with main.py's own file name and line numbers.
"""
import ast
import asyncio
import io
import logging
import os
import re
import sys
import types
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(_HERE)
sys.path.insert(0, BACKEND)

from fastapi import HTTPException  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + str(detail)[:300]) if detail else ""))


MAIN = os.path.join(BACKEND, "main.py")
SRC = io.open(MAIN, encoding="utf-8").read()
LINES = SRC.splitlines()
NODES = {}
TREE = ast.parse(SRC)
for n in TREE.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        NODES[n.name] = n
    elif isinstance(n, ast.Assign):
        for t in n.targets:
            if isinstance(t, ast.Name):
                NODES[t.id] = n

LOG = logging.getLogger("public-availability-test")
LOG.addHandler(logging.NullHandler())
LOG.propagate = False

LISTING_ID = "5cc93b41-1111-2222-3333-444455556666"
ROW = {"id": LISTING_ID, "agent_id": "a1", "status": "active", "code": "TESTCONDO",
       "property_type": "Condominium", "location": "1 Sims Ave #05-11", "price": "1500000"}
SERVED, ENQUIRIES = [], []


class _Q:
    def __init__(self, table):
        self.table, self.op, self.payload = table, "select", None

    def select(self, *a):
        return self

    def update(self, payload):
        self.op, self.payload = "update", payload
        return self

    def insert(self, payload):
        self.op, self.payload = "insert", payload
        return self

    def __getattr__(self, name):          # eq / ilike / gte / lte / order / limit / neq
        return lambda *a, **k: self

    def execute(self):
        if self.table == "agents":
            return types.SimpleNamespace(data=[{"id": "a1"}])
        if self.table == "listings":
            if self.op == "update":
                ROW.update(self.payload)
                return types.SimpleNamespace(data=[dict(ROW)])
            return types.SimpleNamespace(data=[dict(ROW)])
        if self.table == "enquiries":
            ENQUIRIES.append(self.payload)
            return types.SimpleNamespace(data=[dict(self.payload or {}, id="E1")])
        return types.SimpleNamespace(data=[])


class _DB:
    def table(self, name):
        return _Q(name)


def _db_execute(build, **k):
    return build(_DB()).execute()


async def _db_execute_async(build, **k):
    return build(_DB()).execute()


NS = {"re": re, "uuid": __import__("uuid"), "datetime": datetime, "logger": LOG, "os": os, "asyncio": asyncio, "json": __import__("json"),
      "HTTPException": HTTPException, "BaseModel": BaseModel, "Field": Field, "Request": object,
      "Depends": lambda x=None: None, "get_current_agent": None,
      "db_execute": _db_execute, "db_execute_async": _db_execute_async, "get_db": lambda: _DB(),
      "_client_ip": lambda request: "1.2.3.4", "_rate_limited": lambda *a, **k: False,
      "_public_listing_view_hits": {}, "_public_enquiry_hits": {},
      "_public_listing_payload": lambda listing: (SERVED.append(listing["id"]), {"id": listing["id"]})[1]}


def run_nodes(*names):
    for name in names:
        n = NODES[name]
        code = "\n" * (n.lineno - 1) + "\n".join(LINES[n.lineno - 1:n.end_lineno])
        exec(compile(code, MAIN, "exec"), NS)


run_nodes("LISTING_UNAVAILABLE_DETAIL", "_listing_is_public", "_require_public_listing", "_is_valid_uuid",
          "_ilike_literal", "get_public_listing", "get_public_listing_by_codes", "PublicEnquiryRequest",
          "delete_listing", "restore_listing")


def public_views():
    """(by uuid, by short link, by memorable link) -> each "ok" or (status, detail)."""
    out = []
    for call in (lambda: NS["get_public_listing"](LISTING_ID, None),
                 lambda: NS["get_public_listing"](LISTING_ID[:8], None),
                 lambda: NS["get_public_listing_by_codes"]("joshua", "TESTCONDO", None)):
        try:
            call()
            out.append("ok")
        except HTTPException as e:
            out.append((e.status_code, e.detail))
    return out


GONE = (404, "This listing is no longer available")

print("\n[1] active listing: every public link works")
ROW["status"] = "active"
check("active -> all three public links serve the listing", public_views() == ["ok"] * 3, public_views())

print("\n[2] moved to Deleted Listings: every public link is gone, with a clear message")
NS["delete_listing"](LISTING_ID, agent={"id": "a1"})
check("archive endpoint set status archived (nothing deleted)", ROW["status"] == "archived" and ROW["id"] == LISTING_ID)
SERVED.clear()
check("archived -> 404 'This listing is no longer available' on uuid, short and memorable links",
      public_views() == [GONE] * 3, public_views())
check("archived -> the payload builder is never even called (no data leaves)", SERVED == [])

print("\n[3] restored: links work again, decided at request time")
NS["restore_listing"](LISTING_ID, agent={"id": "a1"})
check("restored -> all three public links serve it again", public_views() == ["ok"] * 3, public_views())

print("\n[4] seller leads and any other status are never public")
for status in ("lead", "draft", "", None, "ARCHIVED", "Archived "):
    ROW["status"] = status
    check(f"status {status!r} -> unavailable", public_views() == [GONE] * 3, public_views())
ROW["status"] = " Active "
check("status compared without case/space ('Active') -> available", public_views() == ["ok"] * 3)

print("\n[5] enquiry form on an archived listing")
created = []


async def _no_claude(**k):
    raise AssertionError("no AI scoring for an unavailable listing")

NS["create_claude_message"] = _no_claude
run_nodes("create_public_enquiry")
EQ = NS["PublicEnquiryRequest"]
ROW["status"] = "archived"
ENQUIRIES.clear()
try:
    asyncio.run(NS["create_public_enquiry"](EQ(listing_id=LISTING_ID, client_name="B", message="hi"), None))
    check("enquiry on archived -> refused", False)
except HTTPException as e:
    check("enquiry on archived -> 404 'This listing is no longer available', no lead stored",
          (e.status_code, e.detail) == GONE and ENQUIRIES == [], (e.status_code, e.detail))
check("enquiry lookup selects status (so the check can't silently see None)",
      'select("id, agent_id, location, property_type, price, status")' in SRC)
check("honeypot still answers success without touching anything",
      asyncio.run(NS["create_public_enquiry"](EQ(listing_id=LISTING_ID, client_name="B", website="x"), None))
      == {"success": True})

print("\n[6] the agent's own views are unchanged")
check("unknown id still 'Listing not found' (distinct from 'no longer available')",
      "Listing not found" in SRC and NS["LISTING_UNAVAILABLE_DETAIL"] != "Listing not found")
# Every route that uses the availability check is a PUBLIC one; no authed route does.
users = []
for n in TREE.body:
    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name not in ("_require_public_listing", "_listing_is_public"):
        body = "\n".join(LINES[n.lineno - 1:n.end_lineno])
        if "_require_public_listing(" in body or "_listing_is_public(" in body:
            users.append((n.name, "get_current_agent" in body))
check("only public routes check availability (authed My/Deleted Listings untouched)",
      sorted(u for u, _ in users) == ["create_public_enquiry", "get_public_listing", "get_public_listing_by_codes"]
      and not any(authed for _, authed in users), users)
gl = "\n".join(LINES[NODES["get_listings"].lineno - 1:NODES["get_listings"].end_lineno])
check("My Listings / Deleted Listings query still filters by the requested status only",
      'query = query.eq("status", status)' in gl and 'if status in ("active", "archived"):' in gl)

print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
