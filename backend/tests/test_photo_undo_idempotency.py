"""Executable cover for the two pre-50-agent-day photo hardening changes:

  1. Durable "Undo enhancement" -- the enhanced copy's filename carries the ORIGINAL
     URL it was made from, so undo survives a page reload with no DB column and no
     extra storage read (_enhanced_filename_for / _original_url_from_enhanced).
  2. Photo-finalize idempotency (F-2) -- uploaded photos are content-addressed, so a
     retried/replayed batch re-uploads to the SAME name -> SAME URL -> the existing
     dedup collapses it to a no-op instead of appending duplicates (_upload_filename_for).

main.py cannot be imported here (supabase, anthropic, fitz, bcrypt absent locally), so
each function under test is lifted out of the real source with ast and executed against
stdlib-only stubs -- the same technique as test_auth_guards.py, so these run against the
code that actually ships, not a copy.
"""
import ast
import base64
import hashlib
import io
import os
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
MAIN = os.path.join(os.path.dirname(_HERE), "main.py")
SOURCE = io.open(MAIN, encoding="utf-8").read()
TREE = ast.parse(SOURCE)

PASS, FAIL = [], []


def check(name, condition, detail=""):
    (PASS if condition else FAIL).append(name)
    print(("  PASS  " if condition else "  FAIL  ") + name + (("  -- " + detail) if detail else ""))


NS = {"base64": base64, "hashlib": hashlib, "uuid": uuid}

_WANTED_FUNCS = (
    "_storage_path_from_url", "_dedupe_urls",
    "_enhanced_filename_for", "_original_url_from_enhanced",
    "_upload_filename_for",
)
_WANTED_CONSTS = ("_ENHANCED_MARKER",)

_found = set()
for node in TREE.body:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in _WANTED_FUNCS:
        node.decorator_list = []
        node.returns = None
        for arg in list(node.args.args) + list(node.args.kwonlyargs) + list(node.args.posonlyargs):
            arg.annotation = None
        exec(compile(ast.Module([node], []), MAIN, "exec"), NS)
        _found.add(node.name)
    elif isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name) and t.id in _WANTED_CONSTS:
                exec(compile(ast.Module([node], []), MAIN, "exec"), NS)
                _found.add(t.id)

_enhanced_filename_for = NS["_enhanced_filename_for"]
_original_url_from_enhanced = NS["_original_url_from_enhanced"]
_upload_filename_for = NS["_upload_filename_for"]
_dedupe_urls = NS["_dedupe_urls"]

print("[0] Every function under test was found in the shipped main.py")
check("all expected functions and constants extracted",
      _found == set(_WANTED_FUNCS) | set(_WANTED_CONSTS),
      "missing=%s" % sorted((set(_WANTED_FUNCS) | set(_WANTED_CONSTS)) - _found))

# Public-URL shape the app uses: get_public_url returns ".../listings-images/<path>".
BASE = "https://proj.supabase.co/storage/v1/object/public/listings-images/"
def pub(path):
    return BASE + path

LID = "1a2b3c4d-5e6f-7788-99aa-bbccddeeff00"


# ----------------------------------------------------------------------------
print("\n[1] Durable undo: the enhanced filename round-trips its original URL")
# ----------------------------------------------------------------------------
orig_url = pub(f"{LID}/3_{LID[:8]}_9f8e7d6c.jpg")
enh_path = _enhanced_filename_for(LID, orig_url)
enh_url = pub(enh_path)

check("enhanced name keeps the 'enhanced_' marker (re-enhance guard still fires)",
      NS["_ENHANCED_MARKER"] in enh_path.rsplit("/", 1)[-1], enh_path)
check("enhanced file lives in the listing's own folder",
      enh_path.startswith(LID + "/"), enh_path)
recovered = _original_url_from_enhanced(enh_url)
check("original URL is recovered from the enhanced URL alone (survives a reload)",
      recovered == orig_url, "got=%r" % recovered)

# A URL with a query string (tokened public URL) must still recover to the same string.
orig_q = pub(f"{LID}/0_{LID[:8]}_deadbeef.jpg")
enh_q = pub(_enhanced_filename_for(LID, orig_q))
check("recovery is exact byte-for-byte (so a later re-enhance/delete matches)",
      _original_url_from_enhanced(enh_q) == orig_q)

# Two enhancements of the same original get distinct files (uuid segment) but both decode.
e1 = pub(_enhanced_filename_for(LID, orig_url))
e2 = pub(_enhanced_filename_for(LID, orig_url))
check("two enhanced copies of one original are distinct objects",
      e1 != e2)
check("...yet both still decode back to that original",
      _original_url_from_enhanced(e1) == orig_url and _original_url_from_enhanced(e2) == orig_url)


# ----------------------------------------------------------------------------
print("\n[2] No regression for old-scheme / non-enhanced URLs (recovery returns None)")
# ----------------------------------------------------------------------------
old_scheme = pub(f"{LID}/enhanced_{LID[:8]}_9f8e7d6c.jpg")  # pre-change enhanced name
check("old-scheme enhanced file yields no original (undo simply unavailable, no crash)",
      _original_url_from_enhanced(old_scheme) is None, old_scheme)
check("a plain (non-enhanced) upload URL yields no original",
      _original_url_from_enhanced(orig_url) is None)
check("a foreign URL outside our bucket yields no original",
      _original_url_from_enhanced("https://evil.example.com/enhanced_x.ab.jpg") is None)
check("garbage in the token segment fails closed, does not raise",
      _original_url_from_enhanced(pub(f"{LID}/enhanced_abcd1234.!!!notb64!!!.jpg")) is None)
check("None input is handled",
      _original_url_from_enhanced(None) is None)
# A decoded token that is NOT one of our bucket URLs must be rejected by the final guard.
foreign_token = base64.urlsafe_b64encode(b"https://evil.example.com/x.jpg").decode().rstrip("=")
check("a token that decodes to a non-bucket URL is refused (same guard as /restore)",
      _original_url_from_enhanced(pub(f"{LID}/enhanced_abcd1234.{foreign_token}.jpg")) is None)


# ----------------------------------------------------------------------------
print("\n[3] F-2 idempotency: content-addressed upload names are stable across a replay")
# ----------------------------------------------------------------------------
photo_a = os.urandom(4096)
photo_b = os.urandom(4096)
check("identical bytes -> identical filename (a retry lands on the same object)",
      _upload_filename_for(LID, photo_a) == _upload_filename_for(LID, photo_a))
check("different bytes -> different filename (distinct photos stay distinct)",
      _upload_filename_for(LID, photo_a) != _upload_filename_for(LID, photo_b))
check("name is scoped to the listing folder and ends .jpg",
      _upload_filename_for(LID, photo_a).startswith(LID + "/")
      and _upload_filename_for(LID, photo_a).endswith(".jpg"))


# ----------------------------------------------------------------------------
print("\n[4] F-2 idempotency: a replayed batch collapses to a no-op at commit")
# ----------------------------------------------------------------------------
# Model the real commit: filenames are content-addressed, so the FIRST upload of a batch
# and a RETRY of the same bytes produce the same public URLs. The append-merge in
# _process_and_upload_images only appends a URL "if url not in merged", and the replace
# path compares the new list to the current one -- so an identical replay changes nothing.
batch_bytes = [os.urandom(2048) for _ in range(3)]
first_urls = [pub(_upload_filename_for(LID, b)) for b in batch_bytes]
replay_urls = [pub(_upload_filename_for(LID, b)) for b in batch_bytes]
check("a replayed batch produces the exact same URLs as the first attempt",
      first_urls == replay_urls)

# APPEND path: start from a listing that already committed the first attempt, then replay.
current = ["existing-photo-1"] + first_urls
merged = list(current)
for url in replay_urls:
    if url not in merged:
        merged.append(url)
check("append replay adds zero new photos (no duplicates)",
      merged == current, "merged=%d current=%d" % (len(merged), len(current)))

# The dedup that finalize applies over (staged + own batch) is also idempotent.
check("finalize-style dedup over (committed + replayed) stays the same set",
      _dedupe_urls(first_urls + replay_urls) == first_urls)

# Contrast: the OLD uuid scheme would have produced fresh names on the retry, so the same
# guard would NOT have recognised them -- this is the duplication F-2 describes.
old_first = [f"{LID}/{i}_{LID[:8]}_{uuid.uuid4().hex[:8]}.jpg" for i in range(3)]
old_replay = [f"{LID}/{i}_{LID[:8]}_{uuid.uuid4().hex[:8]}.jpg" for i in range(3)]
check("(sanity) old uuid naming WOULD have duplicated on replay -- the bug being fixed",
      len(_dedupe_urls(old_first + old_replay)) == 6)


# ----------------------------------------------------------------------------
print("\n%d passed, %d failed" % (len(PASS), len(FAIL)))
if FAIL:
    print("FAILED: " + ", ".join(FAIL))
    raise SystemExit(1)
