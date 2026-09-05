from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
from supabase import create_client
import bcrypt
import os
import jwt
import anthropic
import requests
import base64
import io
import json
import asyncio
import httpx
import uuid
import re
import secrets
import hashlib
import threading
import time
import random
import logging
from datetime import datetime, timedelta, date, timezone
from PIL import Image as PILImage, ImageEnhance, ImageOps, ImageStat
import fitz
import poster_renderer
import video_renderer
import ura_market_pulse
import autoenhance
import cloudinary_enhance

app = FastAPI()

# Real error text goes to the Railway logs; agents get a plain-English sentence.
# Exception strings from Supabase/Cloudinary/Pillow leak internal paths and query
# details, and mean nothing to a property agent staring at a failed upload.
logger = logging.getLogger("nestlist")
if not logger.handlers:
    # uvicorn only configures its own loggers, so ours needs a handler or
    # anything below WARNING is swallowed and never reaches the Railway logs.
    _log_handler = logging.StreamHandler()
    _log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [nestlist] %(message)s"))
    logger.addHandler(_log_handler)
    logger.setLevel(logging.INFO)

async def send_telegram_alert(message: str, chat_id: str = None):
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    target_chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot_token or not target_chat_id:
        return
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": target_chat_id, "text": message, "parse_mode": "HTML"}
            )
    except Exception:
        pass

async def send_whatsapp_alert(phone_number: str, message: str):
    """Sends a new-lead notification via Meta's WhatsApp Cloud API. This needs
    NestList's own WhatsApp Business number plus an approved message template --
    until that Meta Business setup is done, this silently no-ops (same
    degrade-gracefully pattern as URA_ACCESS_KEY / RESEND_API_KEY not being set)."""
    access_token = os.environ.get("WHATSAPP_ACCESS_TOKEN", "")
    phone_number_id = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
    template_name = os.environ.get("WHATSAPP_LEAD_TEMPLATE_NAME", "new_lead_alert")
    digits = re.sub(r"\D", "", phone_number or "")
    if len(digits) == 8:
        digits = "65" + digits
    if not access_token or not phone_number_id or not digits:
        return
    plain_message = re.sub(r"<[^>]+>", "", message)
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://graph.facebook.com/v21.0/{phone_number_id}/messages",
                headers={"Authorization": f"Bearer {access_token}"},
                json={
                    "messaging_product": "whatsapp",
                    "to": digits,
                    "type": "template",
                    "template": {
                        "name": template_name,
                        "language": {"code": "en"},
                        "components": [{"type": "body", "parameters": [{"type": "text", "text": plain_message[:1024]}]}],
                    },
                },
                timeout=10,
            )
    except Exception:
        pass


async def send_password_reset_email(to_email: str, name: str, reset_link: str) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.resend.com/emails",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "from": "NestList <noreply@nestlist.sg>",
                    "to": [to_email],
                    "subject": "Reset your NestList password",
                    "html": f"""<p>Hi {name or 'there'},</p>
                        <p>We received a request to reset your NestList password. Tap the button below to choose a new one:</p>
                        <p><a href="{reset_link}" style="display:inline-block;background:#D4AF37;color:#0E2820;font-weight:bold;text-decoration:none;padding:14px 28px;border-radius:4px;margin:8px 0;">Reset My Password</a></p>
                        <p style="font-size:13px;color:#666;">If the button above doesn't work, copy this link and paste it into your browser:<br>{reset_link}</p>
                        <p>This link expires in 1 hour. If you didn't request this, you can safely ignore this email.</p>
                        <p>— NestList</p>""",
                },
                timeout=10,
            )
            return response.status_code == 200
    except Exception:
        return False

async def register_telegram_webhook():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return
    webhook_url = "https://nestlist-backend-production-870a.up.railway.app/api/telegram/webhook"
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"https://api.telegram.org/bot{bot_token}/setWebhook",
                json={"url": webhook_url},
                timeout=10
            )
    except Exception:
        pass

async def _check_anthropic_key(api_key: str) -> bool:
    if not api_key:
        return False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 10, "messages": [{"role": "user", "content": "ping"}]},
                timeout=10
            )
            return response.status_code != 401
    except Exception:
        return False

async def _supabase_heartbeat():
    try:
        get_db().table("agents").select("id").limit(1).execute()
    except Exception:
        pass

async def monitor_api_key():
    while True:
        await asyncio.sleep(3600)
        await _supabase_heartbeat()
        primary_key = os.environ.get("ANTHROPIC_API_KEY", "")
        backup_key = os.environ.get("ANTHROPIC_API_KEY_BACKUP", "")
        if not primary_key:
            await send_telegram_alert("🚨 <b>NestList Alert</b>\n\nANTHROPIC_API_KEY is missing.\n\nAgents cannot generate listings.\n\nFix: Add key at console.anthropic.com")
            continue
        primary_ok = await _check_anthropic_key(primary_key)
        if not primary_ok:
            backup_ok = await _check_anthropic_key(backup_key)
            if backup_ok:
                await send_telegram_alert("⚠️ <b>NestList Warning</b>\n\nPrimary Anthropic API key is invalid, but the backup key is active — agents are unaffected.\n\nPlease replace the primary key in Railway when convenient (no rush).\n\nTime: " + datetime.now().strftime("%d %b %Y %H:%M"))
            else:
                await send_telegram_alert("🚨 <b>NestList Alert</b>\n\nBoth the primary and backup Anthropic API keys are invalid. Agents CANNOT generate listings.\n\nFix:\n1. console.anthropic.com\n2. Generate new key(s)\n3. Update ANTHROPIC_API_KEY and/or ANTHROPIC_API_KEY_BACKUP in Railway\n\nTime: " + datetime.now().strftime("%d %b %Y %H:%M"))

async def create_claude_message(**kwargs):
    primary_key = os.environ.get("ANTHROPIC_API_KEY", "")
    try:
        client = anthropic.AsyncAnthropic(api_key=primary_key)
        return await client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        backup_key = os.environ.get("ANTHROPIC_API_KEY_BACKUP", "")
        if not backup_key:
            raise
        await send_telegram_alert("⚠️ <b>NestList Alert</b>\n\nPrimary Anthropic API key failed — automatically switched to the backup key, agents are unaffected.\n\nPlease check/replace the primary key in Railway when convenient (no rush).")
        client = anthropic.AsyncAnthropic(api_key=backup_key)
        return await client.messages.create(**kwargs)

async def refresh_market_pulse_loop():
    while True:
        try:
            stats = await ura_market_pulse.refresh_market_pulse()
            if stats:
                get_db().table("market_pulse").upsert({"id": 1, **stats}).execute()
        except Exception as e:
            await send_telegram_alert_throttled("market_pulse_refresh_failed",
                f"⚠️ <b>NestList Warning</b>\n\nMarket Pulse auto-refresh from URA failed: {e}\n\nThe panel will keep showing its last known values.")
        await asyncio.sleep(86400)  # URA recommends refreshing daily

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(monitor_api_key())
    asyncio.create_task(refresh_market_pulse_loop())
    # Backgrounded so a slow database can't hold up the port binding.
    asyncio.create_task(asyncio.to_thread(_probe_images_cas_encoding))
    await send_telegram_alert("✅ <b>NestList Backend Started</b>\n\nAPI monitoring active. You will be alerted if the Anthropic key expires.")
    await register_telegram_webhook()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.options("/{path:path}")
async def options_handler(path: str):
    from fastapi.responses import Response
    response = Response()
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "*"
    return response

JWT_SECRET = os.environ.get("JWT_SECRET", "nestlist-secret-2026")
security = HTTPBearer()

_supabase = None

def get_db():
    global _supabase
    if _supabase is None:
        url = os.environ.get("SUPABASE_URL", "")
        key = os.environ.get("SUPABASE_KEY", "")
        _supabase = create_client(url, key)
    return _supabase

# ================================
# MODELS
# ================================
class LoginRequest(BaseModel):
    email: str
    password: str

class RegisterRequest(BaseModel):
    email: str
    password: str
    name: str
    agency: str
    specialty: str

class ListingRequest(BaseModel):
    property_type: str
    location: str
    land_size: int = 0
    built_up: int = 0
    bedrooms: str
    bathrooms: str = ""
    price: str
    features: str
    plot_width: float = 0
    plot_depth: float = 0
    storeys: float = 0
    site_coverage: float = 0
    sg_citizen: bool = False

class ListingContentRequest(BaseModel):
    content: str

class RewriteSelectionRequest(BaseModel):
    selected_text: str
    instruction: str = ""
    # The editor's live content, which after the first rewrite no longer matches
    # what's saved -- the frontend applies each rewrite client-side and only saves
    # later. Optional so older clients keep working against the saved copy.
    current_text: str = ""

class ProfileUpdate(BaseModel):
    name: str
    agency: str
    specialty: str
    tone: str
    emphasis: str
    signature: str
    contact: str = ""
    poster_color: str = "#1a1a5c"
    poster_template_id: str = "editorial"
    notification_channel: str = "telegram"
    whatsapp_number: str = ""

class ProfilePhotoRequest(BaseModel):
    image_data: str

class EmailChangeRequest(BaseModel):
    new_email: str
    current_password: str

class TokenExchangeRequest(BaseModel):
    user_token: str

class InstagramOAuthCallbackRequest(BaseModel):
    code: str
    state: str

class InstagramPostRequest(BaseModel):
    caption: str

class FacebookOAuthCallbackRequest(BaseModel):
    code: str
    state: str

class FacebookPostRequest(BaseModel):
    caption: str

class LinkedInOAuthCallbackRequest(BaseModel):
    code: str
    state: str

class LinkedInPostRequest(BaseModel):
    caption: str

class PasswordResetRequest(BaseModel):
    email: str
    website: str = ""  # honeypot field, must stay empty

class PasswordResetConfirm(BaseModel):
    token: str
    new_password: str

class CMARequest(BaseModel):
    street: str
    property_type: str = ""
    land_size: float = 0
    window_months: int = 24

class PublicEnquiryRequest(BaseModel):
    listing_id: str
    client_name: str
    phone: str = ""
    email: str = ""
    message: str = ""
    website: str = ""  # honeypot field, must stay empty

class BuyerRequest(BaseModel):
    name: str
    phone: str = ""
    email: str = ""
    temperature: str = "WARM"
    status: str = "new"
    contact_date: str = ""
    contact_via: str = ""
    budget_min: float = 0
    budget_max: float = 0
    timeline: str = ""
    districts: str = ""
    property_types: str = ""
    land_min: float = 0
    tenure_pref: str = ""
    buying_for: str = ""
    sold_house: str = ""
    financing: str = ""
    must_haves: str = ""
    deal_breakers: str = ""
    notes: str = ""

class BuyerPropertyRequest(BaseModel):
    listing_id: str = ""
    address: str = ""
    price: float = 0
    kind: str
    date: str = ""
    agent_name: str = ""
    interest: str = ""
    feedback: str = ""

class SellerLeadRequest(BaseModel):
    seller_name: str
    seller_phone: str = ""
    seller_email: str = ""
    location: str = ""
    property_type: str = ""
    price: str = ""  # asking price expectation, same field a real listing uses
    land_size: int = 0
    motivation: str = ""
    timeline: str = ""
    mandate_type: str = ""
    temperature: str = "WARM"
    seller_notes: str = ""

class ChatMessage(BaseModel):
    role: str
    content: str

class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    listing_id: str = None  # optional -- the listing the agent currently has open, if any

# ================================
# AUTH HELPERS
# ================================
def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, hashed: str) -> bool:
    try:
        if hashed.startswith("$2b$") or hashed.startswith("$2a$"):
            return bcrypt.checkpw(password.encode(), hashed.encode())
        return password == hashed
    except:
        return False

def create_token(agent_id: str) -> str:
    payload = {"agent_id": agent_id, "exp": datetime.utcnow() + timedelta(days=30)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

_last_alert_times = {}

async def send_telegram_alert_throttled(key: str, message: str, cooldown_seconds: int = 600):
    now = datetime.utcnow()
    last = _last_alert_times.get(key)
    if last and (now - last).total_seconds() < cooldown_seconds:
        return
    _last_alert_times[key] = now
    await send_telegram_alert(message)

async def get_current_agent(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Your session has expired. Please log in again.")
    except jwt.InvalidTokenError:
        await send_telegram_alert_throttled(
            "jwt_invalid",
            "🚨 <b>NestList Alert</b>\n\nAgents are being rejected with an invalid-signature token error — this usually means JWT_SECRET changed on Railway. Every logged-in agent will need to log in again. Check the JWT_SECRET env var."
        )
        raise HTTPException(status_code=401, detail="Invalid session. Please log in again.")

    agent_id = payload["agent_id"]
    try:
        result = get_db().table("agents").select("*").eq("id", agent_id).execute()
    except Exception:
        await send_telegram_alert_throttled(
            "db_unreachable",
            "⚠️ <b>NestList Warning</b>\n\nAgents are being blocked from logging in — the database is temporarily unreachable. This is not an auth problem; check Supabase status."
        )
        raise HTTPException(status_code=503, detail="Temporarily unable to verify your session. Please try again in a moment.")

    if not result.data:
        raise HTTPException(status_code=401, detail="Agent not found. Please log in again.")
    return result.data[0]

# ================================
# INSTAGRAM AUTO-POSTING (hidden beta, allowlisted accounts only until Meta App Review is approved)
# ================================
INSTAGRAM_BETA_ALLOWLIST = {"leesbjane@gmail.com"}
INSTAGRAM_OAUTH_REDIRECT_URI = "https://nestlist.sg/auth/instagram/callback"

def _can_use_instagram_beta(agent) -> bool:
    return agent.get("email") in INSTAGRAM_BETA_ALLOWLIST

# ================================
# FACEBOOK AUTO-POSTING (hidden beta, same allowlist gate as Instagram until Meta App Review
# approves pages_manage_posts at Advanced Access). A genuinely separate connect flow from
# Instagram's -- posting to a Page needs the pages_manage_posts scope, which Instagram's
# connect flow never requests, so an existing Instagram connection alone isn't enough.
# Both flows write the same fb_* columns on the agent record since they're really the same
# underlying "connect your Facebook Page" action, just entered from two different buttons.
# ================================
FACEBOOK_BETA_ALLOWLIST = {"leesbjane@gmail.com"}
FACEBOOK_OAUTH_REDIRECT_URI = "https://nestlist.sg/auth/facebook/callback"

def _can_use_facebook_beta(agent) -> bool:
    return agent.get("email") in FACEBOOK_BETA_ALLOWLIST

# ================================
# LINKEDIN AUTO-POSTING (hidden beta, same rollout discipline as Instagram/Facebook --
# gated to Jane's own account first, opened up once verified live). Unlike Facebook,
# LinkedIn's w_member_social permission posts to a member's OWN personal feed -- there's
# no "Pages only" platform wall here, so this can genuinely reach every agent once out of
# beta, not just the minority with a business Page. Requires the "Sign In with LinkedIn
# using OpenID Connect" and "Share on LinkedIn" products enabled on the LinkedIn Developer
# app (self-serve, not a lengthy special-access review like Meta's Advanced Access).
# ================================
LINKEDIN_BETA_ALLOWLIST = {"leesbjane@gmail.com"}
LINKEDIN_OAUTH_REDIRECT_URI = "https://nestlist.sg/auth/linkedin/callback"
LINKEDIN_API_VERSION = "202601"  # Linkedin-Version header, YYYYMM format -- bump periodically

def _can_use_linkedin_beta(agent) -> bool:
    return agent.get("email") in LINKEDIN_BETA_ALLOWLIST

def _agent_response(agent) -> dict:
    out = {k: v for k, v in agent.items() if k != "password_hash"}
    out["can_use_instagram_beta"] = _can_use_instagram_beta(agent)
    out["can_use_facebook_beta"] = _can_use_facebook_beta(agent)
    out["can_use_linkedin_beta"] = _can_use_linkedin_beta(agent)
    return out

# ================================
# AUTH ROUTES
# ================================
@app.post("/api/login")
def login(req: LoginRequest):
    result = get_db().table("agents").select("*").eq("email", req.email).execute()
    if not result.data:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    agent = result.data[0]
    if not verify_password(req.password, agent["password_hash"]):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    token = create_token(str(agent["id"]))
    return {"token": token, "agent": _agent_response(agent)}

@app.post("/api/register")
def register(req: RegisterRequest):
    existing = get_db().table("agents").select("id").eq("email", req.email).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="Email already registered")
    result = get_db().table("agents").insert({
        "email": req.email,
        "password_hash": hash_password(req.password),
        "name": req.name,
        "agency": req.agency,
        "specialty": req.specialty,
        "tone": "Warm & Conversational",
        "emphasis": "Lifestyle & Prestige",
        "signature": "Where your next chapter begins.",
        "tier": "prestige"
    }).execute()
    agent = result.data[0]
    token = create_token(str(agent["id"]))
    return {"token": token, "agent": _agent_response(agent)}

@app.post("/api/password-reset/request")
async def request_password_reset(req: PasswordResetRequest, request: Request):
    if req.website:
        return {"success": True}

    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    if _rate_limited(_password_reset_hits, client_ip, limit=5, window_seconds=3600):
        raise HTTPException(status_code=429, detail="Too many reset requests — please try again later")

    generic_response = {"success": True, "message": "If that email is registered with NestList, a reset link has been sent."}

    result = get_db().table("agents").select("id, email, name").eq("email", req.email).execute()
    if not result.data:
        return generic_response  # never reveal whether an email is registered
    agent = result.data[0]

    get_db().table("password_resets").update({"used_at": datetime.utcnow().isoformat()}) \
        .eq("agent_id", agent["id"]).is_("used_at", "null").execute()

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    get_db().table("password_resets").insert({
        "agent_id": agent["id"],
        "token_hash": token_hash,
        "expires_at": (datetime.utcnow() + timedelta(hours=1)).isoformat(),
    }).execute()

    reset_link = f"https://nestlist.sg/reset-password?token={raw_token}"
    sent = await send_password_reset_email(agent["email"], agent.get("name", ""), reset_link)
    if not sent:
        await send_telegram_alert_throttled(
            "password_reset_email_failed",
            "⚠️ <b>NestList Warning</b>\n\nA password reset email failed to send via Resend. Check RESEND_API_KEY in Railway and Resend's dashboard for delivery issues."
        )
    return generic_response

@app.post("/api/password-reset/confirm")
def confirm_password_reset(req: PasswordResetConfirm):
    if not req.token or len(req.token) < 20:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Please request a new one.")
    if len(req.new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")

    token_hash = hashlib.sha256(req.token.encode()).hexdigest()
    result = get_db().table("password_resets").select("*").eq("token_hash", token_hash).execute()
    if not result.data:
        raise HTTPException(status_code=400, detail="This reset link is invalid or has expired. Please request a new one.")
    reset_row = result.data[0]

    if reset_row.get("used_at"):
        raise HTTPException(status_code=400, detail="This reset link has already been used. Please request a new one.")

    expires_at = datetime.fromisoformat(reset_row["expires_at"].replace("Z", "+00:00")).replace(tzinfo=None)
    if datetime.utcnow() > expires_at:
        raise HTTPException(status_code=400, detail="This reset link has expired. Please request a new one.")

    get_db().table("agents").update({"password_hash": hash_password(req.new_password)}).eq("id", reset_row["agent_id"]).execute()
    get_db().table("password_resets").update({"used_at": datetime.utcnow().isoformat()}).eq("id", reset_row["id"]).execute()

    return {"success": True}

# ================================
# LISTING COPY GUARDS
# ================================
# Janel's field feedback (see prototypes/output/listing_copy_rules.md): generated
# copy must never carry a house/unit number, and the listing story must never
# carry the asking price in any form. Prompt wording alone is not a guarantee --
# the model can reconstruct a number on a retry or an odd input -- so there are
# two deterministic layers plus a price check, all of which STRIP rather than
# reject. A stripped sentence still reads fine; a failed generation costs the
# agent a listing.

# Layer 1a: leading block/house number -- "22G Tembeling Road" -> "Tembeling Road".
_HOUSE_NUMBER_PREFIX_RE = re.compile(r"^\s*\d{1,4}[A-Za-z]?\s+(?=\D)")
# Layer 1b/2b: HDB-style unit number anywhere -- "#03-04".
_UNIT_NUMBER_RE = re.compile(r"#\d{1,3}-\d{1,5}[A-Za-z]?")

# Layer 2a: a house number the model reconstructed mid-sentence. Anchored on
# Singapore street suffixes so plain listing numbers ("1,970 sqft", "3 bedrooms")
# are never touched -- only a number sitting immediately in front of a street name.
_STREET_SUFFIXES = (
    r"Road|Ave(?:nue)?|St(?:reet)?|Dr(?:ive)?|Close|Lane|Walk|Park|Pl(?:ace)?|"
    r"Crescent|Terrace|View|Rise|Way|Grove|Gardens?|Heights|Boulevard|Bt|Jalan|"
    r"Lorong|Track|Link|Green|Hill|Court|Ct"
)
_ADDRESS_NUMBER_RE = re.compile(
    r"\b\d{1,4}[A-Za-z]?\s+(?=((?:[A-Z][a-z]+\s+){0,2})(?:" + _STREET_SUFFIXES + r")\b)"
)
# A few street suffixes double as ordinary listing words ("Terrace" in
# "Inter-Terrace", "Park" in "3 Bedroom Park View"), so a match whose in-between
# words are room/measurement words is a false positive and is left alone. Without
# this, "3 Bedroom Terrace" would silently lose its bedroom count.
_NON_STREET_WORDS = {
    "bedroom", "bedrooms", "bed", "beds", "bath", "baths", "bathroom", "bathrooms",
    "room", "rooms", "storey", "storeys", "story", "stories", "sqft", "sqm",
    "square", "feet", "foot", "metre", "metres", "meter", "meters", "car", "cars",
    "level", "levels", "unit", "units", "million", "psf",
}


def _strip_house_number(location) -> str:
    """Street/area name only -- leading house or block number and any #NN-NN unit
    number removed. The raw value still goes to the DB (agents want their own
    record of the exact unit); only copy-facing text uses this."""
    text = str(location or "")
    text = _UNIT_NUMBER_RE.sub("", text)
    text = _HOUSE_NUMBER_PREFIX_RE.sub("", text)
    # Tidy up punctuation orphaned by the removals ("12 Marine Terrace, #03-04").
    text = re.sub(r"\s{2,}", " ", text)
    text = re.sub(r"\s+,", ",", text)
    text = re.sub(r",\s*,", ",", text)
    return text.strip().strip(",").strip()


def _address_number_hits(text: str) -> list:
    """Matches of the address-anchored house-number pattern, false positives dropped."""
    hits = []
    for match in _ADDRESS_NUMBER_RE.finditer(text):
        between = (match.group(1) or "").split()
        if any(word.lower() in _NON_STREET_WORDS for word in between):
            continue
        hits.append(match)
    return hits


def _strip_address_numbers(text: str) -> tuple:
    """Layer 2. Returns (cleaned_text, list_of_stripped_fragments)."""
    stripped = []
    hits = _address_number_hits(text)
    if hits:
        out, last = [], 0
        for match in hits:
            out.append(text[last:match.start()])
            stripped.append(match.group(0).strip())
            last = match.end()
        out.append(text[last:])
        text = "".join(out)
    for match in _UNIT_NUMBER_RE.finditer(text):
        stripped.append(match.group(0))
    if stripped:
        text = _UNIT_NUMBER_RE.sub("", text)
        text = re.sub(r"[ \t]{2,}", " ", text)
        text = re.sub(r"\s+([,.;])", r"\1", text)
    return text, stripped


def _price_leak_strings(price, built_up) -> list:
    """Every literal spelling of THIS listing's price we refuse to see in copy.
    Exact strings, not a generic number pattern -- a generic pattern would also
    match land size / sqft figures like "1,970"."""
    price_num = _to_number(price)
    if price_num <= 0:
        return []
    millions = _format_price_millions(price_num)          # "5.35M"
    bare = millions[:-1] if millions.endswith("M") else millions
    figures = [
        f"{price_num:,.0f}",                              # 5,350,000
        f"{price_num:.0f}",                               # 5350000
        millions,                                         # 5.35M
        f"{bare} million",
    ]
    built_up_num = _to_number(built_up)
    if built_up_num > 0:
        psf = round(price_num / built_up_num)
        figures += [f"{psf:,} psf", f"{psf} psf"]
    # Each figure also in its currency-prefixed spellings, so "SGD 5.35M" is
    # removed whole instead of leaving an orphaned "SGD" behind.
    candidates = set()
    for figure in figures:
        candidates.update({figure, f"SGD {figure}", f"S${figure}", f"${figure}"})
    # Longest first, so the prefixed form always wins over the bare one.
    return sorted({c for c in candidates if c}, key=len, reverse=True)


def _strip_price_leaks(text: str, price, built_up) -> tuple:
    """Drops the sentence carrying a price figure. Falls back to redacting just
    the figure if sentence-dropping would gut the write-up."""
    leaks = _price_leak_strings(price, built_up)
    if not leaks:
        return text, []
    lowered = text.lower()
    found = [leak for leak in leaks if leak.lower() in lowered]
    if not found:
        return text, []

    def _has_leak(chunk: str) -> bool:
        low = chunk.lower()
        return any(leak.lower() in low for leak in found)

    def _redact(chunk: str) -> str:
        for leak in found:
            chunk = re.sub(re.escape(leak), "", chunk, flags=re.IGNORECASE)
        # Tidy the separator the figure was hanging off ("Tembeling Road — ").
        chunk = re.sub(r"[ \t]{2,}", " ", chunk).strip()
        return chunk.strip(" \t—–-·|,;:").strip()

    kept_lines = []
    for line in text.split("\n"):
        if not _has_leak(line):
            kept_lines.append(line)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", line)
        kept = [s for s in sentences if not _has_leak(s)]
        if kept:
            kept_lines.append(" ".join(kept).strip())
        else:
            # Every sentence on this line carried the figure (typically a
            # headline). Redact the figure rather than losing the whole line.
            kept_lines.append(_redact(line))
    cleaned = "\n".join(kept_lines)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip(), found


def apply_listing_copy_guards(text: str, price=None, built_up=None, context: str = "listing") -> str:
    """Layer 2 + price guard over generated copy. Never raises and never returns
    empty -- if the guard itself breaks, the agent still gets their write-up and
    the failure goes to the Railway logs for Jane to see."""
    if not text:
        return text
    try:
        cleaned, address_hits = _strip_address_numbers(text)
        cleaned, price_hits = _strip_price_leaks(cleaned, price, built_up)
        if address_hits:
            logger.warning("copy guard [%s]: stripped house/unit number(s) %s", context, address_hits)
        if price_hits:
            logger.warning("copy guard [%s]: stripped price leak(s) %s", context, price_hits)
        return cleaned if cleaned.strip() else text
    except Exception as e:
        logger.error("copy guard [%s] failed, returning ungated text: %s", context, e)
        return text


# ================================
# LISTINGS ROUTES
# ================================
@app.get("/api/listings")
def get_listings(status: str = "active", agent=Depends(get_current_agent)):
    query = get_db().table("listings").select("*").eq("agent_id", agent["id"])
    if status in ("active", "archived"):
        query = query.eq("status", status)
    else:
        # Any other value (e.g. "all", used by My Listings) means "everything
        # relevant to My Listings" -- seller leads live in this same table but
        # belong to the Sellers page, not My Listings, so they're excluded here
        # regardless of what value was passed.
        query = query.neq("status", "lead")
    result = query.order("created_at", desc=True).execute()
    rows = result.data or []
    # Additive field -- `location` stays exactly as the agent typed it (their own
    # record), `display_location` is the house/unit-number-free version every
    # copy surface (captions, poster text, public page) should render instead.
    for row in rows:
        row["display_location"] = _strip_house_number(row.get("location"))
    return rows

@app.post("/api/listings/generate")
async def generate_listing(req: ListingRequest, agent=Depends(get_current_agent)):
    gcb_zones = [
        "nassim", "cluny", "white house park", "dalvey", "ladyhill",
        "cornwall", "king albert park", "raffles park", "swiss club",
        "victoria park", "holland", "bin tong park", "leedon",
        "maryland", "bishopsgate", "fourth avenue", "grange", "jervois",
        "rochalie", "linden", "chee hoon", "swettenham", "tanglin",
        "chestnut", "sunset", "upper bukit timah", "rifle range",
        "spring grove", "belmont", "windsor"
    ]
    issues, warnings, passed = [], [], []
    is_gcb = "gcb" in req.property_type.lower() or "bungalow" in req.property_type.lower()

    if is_gcb:
        if any(z in req.location.lower() for z in gcb_zones):
            passed.append("Location confirmed within gazetted GCBa zone")
        else:
            warnings.append("Location could not be verified as GCBa zone — please confirm with URA.")
        if req.land_size >= 15069:
            passed.append(f"Land size {req.land_size:,} sqft meets URA minimum")
        elif req.land_size >= 14000:
            warnings.append(f"Land size {req.land_size:,} sqft is slightly below URA minimum")
        elif req.land_size > 0:
            issues.append(f"Land size {req.land_size:,} sqft does not meet GCB minimum of 15,069 sqft")
        if req.plot_width >= 18.5:
            passed.append(f"Plot width {req.plot_width}m meets URA minimum")
        elif req.plot_width > 0:
            issues.append(f"Plot width {req.plot_width}m does not meet URA minimum of 18.5m")
        if req.plot_depth >= 30:
            passed.append(f"Plot depth {req.plot_depth}m meets URA minimum")
        elif req.plot_depth > 0:
            issues.append(f"Plot depth {req.plot_depth}m does not meet URA minimum of 30m")
        if req.site_coverage > 0:
            if req.site_coverage <= 40:
                passed.append(f"Site coverage {req.site_coverage}% within URA maximum")
            else:
                issues.append(f"Site coverage {req.site_coverage}% exceeds URA maximum of 40%")
        if req.storeys > 0:
            if req.storeys <= 2:
                passed.append(f"{req.storeys} storey(s) meets URA maximum")
            else:
                issues.append(f"{req.storeys} storeys exceeds URA maximum of 2 for GCB")
        if not req.sg_citizen:
            issues.append("GCB purchases restricted to Singapore Citizens only")
        else:
            passed.append("Buyer confirmed as Singapore Citizen")

    is_terrace = req.property_type.lower() in ("inter-terrace", "corner terrace")
    if is_terrace:
        # URA's minimum land size/frontage differs for inter- vs corner-terrace (150sqm/6m vs
        # 200sqm/8m), so a mismatch there is a useful signal to double-check. Note this can only
        # catch an Inter-Terrace mislabeled as too small/large for its own minimum — it CANNOT
        # distinguish Corner-Terrace from Semi-Detached, since URA sets an identical 200sqm/8m
        # minimum for both. That distinction is structural (a terrace house is one unit in a row
        # of 3+, semi-detached is a pair of exactly 2) and isn't derivable from size alone, so the
        # wording below deliberately doesn't claim more certainty than the numbers actually give.
        land_size_sqm = req.land_size / 10.7639 if req.land_size else 0
        is_corner = "corner" in req.property_type.lower()
        if is_corner:
            if req.land_size > 0 and land_size_sqm < 200:
                warnings.append(f"Land size {req.land_size:,} sqft ({land_size_sqm:.0f} sqm) is below URA's Corner Terrace minimum of 200 sqm — please verify this is actually a Corner Terrace.")
            if req.plot_width > 0 and req.plot_width < 8:
                warnings.append(f"Frontage {req.plot_width}m is below URA's Corner Terrace minimum of 8m — please verify this is actually a Corner Terrace.")
        else:
            if req.land_size > 0 and land_size_sqm >= 200 and req.plot_width >= 8:
                warnings.append(f"Land size ({land_size_sqm:.0f} sqm) and frontage ({req.plot_width}m) meet or exceed URA's minimum for Corner Terrace/Semi-Detached (200 sqm / 8m), which is unusually large for Inter-Terrace. Please double-check the exact property type — it may be a Corner Terrace or Semi-Detached instead.")
            elif req.land_size > 0 and land_size_sqm < 150:
                warnings.append(f"Land size {req.land_size:,} sqft ({land_size_sqm:.0f} sqm) is below URA's Inter-Terrace minimum of 150 sqm.")

    if issues:
        return {"compliance": {"passed": passed, "warnings": warnings, "issues": issues}, "listing": None}

    # Layer 1 of the copy guard: the house/unit number is removed BEFORE the
    # prompt is built, because the model mostly just echoes the Location it is
    # given. The rule text below is belt; this is braces.
    display_location = _strip_house_number(req.location)

    prompt = f"""You are {agent['name']} from {agent['agency']}, a specialist in {agent['specialty']}.
Your tone: {agent.get('tone', 'Warm & Conversational')}
You emphasise: {agent.get('emphasis', 'Lifestyle & Prestige')}
Your signature phrase: "{agent.get('signature', 'Where your next chapter begins.')}"

Write a property listing for:
- Type: {req.property_type}
- Location: {display_location}
- Land size: {req.land_size:,} sqft
- Built-up: {req.built_up:,} sqft
- Bedrooms: {req.bedrooms}
- Bathrooms: {req.bathrooms}
- Features: {req.features}

Follow these rules with no exceptions:

1. NEVER include a house or unit number. If Location contains one (e.g. "22G Tembeling Road",
   "#03-04 Amber Road", "12A Jalan Sempadan"), drop it and refer only to the street or area name
   ("Tembeling Road"). Do not invent a substitute number, and do not restate the number even once
   for "colour."

2. Write the way a knowledgeable person would actually talk to a buyer, not the way a brochure
   does. Avoid stock real-estate phrases — "coveted", "established enclave", "prestigious address",
   "epitome of luxury", "nestled in", "boasts", "sprawling". If a plain word says it, use the plain
   word. Elegant is fine; inflated is not.

3. Every descriptive claim must be traceable to something in the facts above. Do not call the
   street "sought-after," "prestigious," or "coveted" unless a fact above actually supports it. If
   you're reaching for a superlative and can't point to what earns it, describe what's concretely
   there instead — the layout, the orientation, the space, the light — rather than a status claim.
   A modest, honest line beats an impressive-sounding one that isn't backed up.

4. Do NOT mention price anywhere, in any form — no figure, no "attractively priced," no "priced to
   sell," no range. Price is shown to buyers separately; leave it out of the write-up entirely.

Write:
1. A compelling headline (no house/unit number, no price)
2. Three short paragraphs in your personal voice — grounded, specific to the facts given, warm
   rather than formal
3. A warm call to action (no price)
4. End with: {agent['name']} | {agent['agency']} Specialist"""

    response = await create_claude_message(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    listing_text = response.content[0].text.strip().replace('**', '').replace('---', '').replace('# ', '').strip()
    # Layer 2 + price guard: catches anything the model reconstructed on its own.
    listing_text = apply_listing_copy_guards(
        listing_text, price=req.price, built_up=req.built_up, context=f"generate:{agent['id']}"
    )

    saved = get_db().table("listings").insert({
        "agent_id": agent["id"],
        "location": req.location,
        "price": req.price,
        "property_type": req.property_type,
        "content": listing_text,
        "land_size": req.land_size,
        "built_up": req.built_up,
        "bedrooms": req.bedrooms,
        "bathrooms": req.bathrooms,
        "plot_width": req.plot_width,
        "plot_depth": req.plot_depth,
        "storeys": req.storeys,
        "site_coverage": req.site_coverage,
        "features": req.features,
    }).execute()

    listing_row = saved.data[0]
    # `location` in the DB stays as the agent typed it; copy surfaces use this.
    listing_row["display_location"] = display_location

    return {
        "compliance": {"passed": passed, "warnings": warnings, "issues": issues},
        "listing": listing_row
    }

MAX_LISTING_PHOTOS = 15  # per listing, not per request -- see _process_and_upload_images

# Every photo-array change is a read-modify-write: SELECT images -> change the
# list in Python -> UPDATE the whole array back. Two requests touching the same
# listing at the same time clobber each other (both read 14 photos, both write
# 13, one delete silently vanishes -- or worse, a slow enhance writes back a
# snapshot that resurrects a photo the agent deleted while it was running).
#
# These per-listing locks serialise those sections inside ONE worker. Railway
# runs 4 uvicorn workers, so they are not a distributed lock and never were
# enough on their own. They are kept only because they are free and stop
# same-worker traffic (an agent double-clicking Delete) from burning retries.
#
# The actual guard is the compare-and-swap below.
#
# The map grows by one small lock per listing touched since boot (a few KB for
# every listing NestList has); worker restarts clear it. Not worth evicting --
# eviction would need to drop locks other threads are about to acquire.
_listing_image_locks = {}
_listing_image_locks_guard = threading.Lock()


def _listing_image_lock(listing_id: str) -> threading.Lock:
    with _listing_image_locks_guard:
        lock = _listing_image_locks.get(listing_id)
        if lock is None:
            lock = threading.Lock()
            _listing_image_locks[listing_id] = lock
        return lock


# ---------------------------------------------------------------------------
# Atomic photo-array updates (compare-and-swap)
# ---------------------------------------------------------------------------
# The UPDATE carries the array we just read as an extra WHERE filter, so it only
# lands if nobody changed the array in between. Zero rows back means we lost the
# race: re-read, re-apply the change to the fresh array, try again. That makes
# the read-modify-write atomic across all 4 workers without a Postgres function
# or a version column, either of which would need a migration Jane has to run by
# hand in the Supabase SQL editor.
_IMAGES_CAS_MAX_ATTEMPTS = 6
_IMAGES_CAS_BASE_BACKOFF = 0.05  # seconds; doubles each retry, capped, plus jitter
_IMAGES_CAS_MAX_BACKOFF = 0.5

# PostgREST wants the previous array spelled the way the column's type expects,
# and we cannot see the schema from inside the app: a jsonb column wants
# ["a","b"], a text[] column wants {"a","b"}. Passing the wrong one is *rejected*
# ("malformed array literal" / "invalid input syntax for type json") rather than
# silently matching nothing, so we try both once and remember which one the table
# accepts. Only a success is cached -- a transient network blip must not pin us
# to the wrong answer for the life of the worker.
_IMAGES_CAS_ENCODING = None
_IMAGES_CAS_ENCODING_GUARD = threading.Lock()


def _encode_images_filter(previous: list, encoding: str) -> str:
    if encoding == "json":
        return json.dumps(list(previous), separators=(",", ":"))
    escaped = []
    for url in previous:
        text = str(url).replace("\\", "\\\\").replace('"', '\\"')
        escaped.append(f'"{text}"')
    return "{" + ",".join(escaped) + "}"


def _try_images_cas(supabase, listing_id, agent_id, previous, new_images, encoding) -> bool:
    """One compare-and-swap attempt with one encoding. True if the row was
    updated, False if the array had already moved on. Raises if PostgREST
    rejects the filter (wrong encoding) or the database is unreachable."""
    resp = (
        supabase.table("listings")
        .update({"images": new_images})
        .eq("id", listing_id)
        .eq("agent_id", agent_id)
        .filter("images", "eq", _encode_images_filter(previous, encoding))
        .execute()
    )
    return bool(resp.data)


def _cas_update_images(supabase, listing_id, agent_id, previous, new_images):
    """True = applied, False = someone else got there first, None = this table
    will not accept a compare-and-swap filter at all (caller falls back)."""
    global _IMAGES_CAS_ENCODING

    encoding = _IMAGES_CAS_ENCODING
    if encoding:
        # Known-good encoding: let real database errors propagate to the caller
        # instead of mistaking them for an encoding problem.
        return _try_images_cas(supabase, listing_id, agent_id, previous, new_images, encoding)

    failures = []
    for candidate in ("json", "array"):
        try:
            applied = _try_images_cas(supabase, listing_id, agent_id, previous, new_images, candidate)
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        with _IMAGES_CAS_ENCODING_GUARD:
            _IMAGES_CAS_ENCODING = candidate
        logger.info("listings.images compare-and-swap using %r encoding", candidate)
        return applied

    logger.error(
        "listings.images will not take a compare-and-swap filter (%s) -- "
        "falling back to a plain update for this write",
        " | ".join(failures),
    )
    return None


def _probe_images_cas_encoding():
    """Work out at boot which literal PostgREST accepts for listings.images, by
    running the same filter against a SELECT that cannot match any row. Nothing
    is read and nothing is written: the wrong encoding is rejected outright,
    which is the signal we want, and the right one simply returns no rows.

    Purely an early-warning convenience -- the first real photo change would
    work it out anyway. Doing it at startup puts the answer in the deploy logs
    and behind /api/health, so a broken compare-and-swap is visible before an
    agent hits it rather than after."""
    global _IMAGES_CAS_ENCODING
    if _IMAGES_CAS_ENCODING:
        return _IMAGES_CAS_ENCODING

    supabase = get_db()
    failures = []
    for candidate in ("json", "array"):
        try:
            (
                supabase.table("listings")
                .select("id")
                .eq("id", "00000000-0000-0000-0000-000000000000")
                .filter("images", "eq", _encode_images_filter(["__probe__"], candidate))
                .execute()
            )
        except Exception as exc:
            failures.append(f"{candidate}: {exc}")
            continue
        with _IMAGES_CAS_ENCODING_GUARD:
            _IMAGES_CAS_ENCODING = candidate
        logger.info("listings.images compare-and-swap encoding: %r -- photo writes are atomic", candidate)
        return candidate

    logger.error(
        "Could not determine a compare-and-swap encoding for listings.images (%s). "
        "Photo writes will fall back to non-atomic updates.",
        " | ".join(failures),
    )
    return None


def _mutate_listing_images(listing_id: str, agent_id: str, mutate) -> list:
    """Read the listing's photo array, hand it to `mutate`, and write the result
    back only if nothing else changed it in the meantime -- retrying against a
    fresh read each time.

    `mutate(current, ctx)` returns the new array, or None to leave the array
    alone. `ctx["attempt"]` is the 0-based attempt number, which mutators use to
    tell "this photo was never there" (attempt 0) from "this photo is gone
    because our own write probably landed" (later attempts). Raising from
    `mutate` aborts without writing anything.

    Mutators must be idempotent: an UPDATE can succeed and still report no rows
    if the response is lost, and we would then re-apply the change to an array
    that already contains it.
    """
    supabase = get_db()
    delay = _IMAGES_CAS_BASE_BACKOFF

    with _listing_image_lock(listing_id):
        for attempt in range(_IMAGES_CAS_MAX_ATTEMPTS):
            result = (
                supabase.table("listings")
                .select("images")
                .eq("id", listing_id)
                .eq("agent_id", agent_id)
                .execute()
            )
            if not result.data:
                raise HTTPException(status_code=404, detail="Listing not found")

            previous = result.data[0].get("images") or []
            new_images = mutate(list(previous), {"attempt": attempt})
            if new_images is None:
                return list(previous)

            applied = _cas_update_images(supabase, listing_id, agent_id, previous, new_images)
            if applied:
                return new_images
            if applied is None:
                # No compare-and-swap available: do what the code did before
                # (plain update, in-process lock only) rather than refusing to
                # save the agent's change at all. Logged loudly above.
                (
                    supabase.table("listings")
                    .update({"images": new_images})
                    .eq("id", listing_id)
                    .eq("agent_id", agent_id)
                    .execute()
                )
                return new_images

            time.sleep(delay + random.uniform(0, delay))
            delay = min(delay * 2, _IMAGES_CAS_MAX_BACKOFF)

    logger.warning(
        "Gave up after %s compare-and-swap attempts on listing %s",
        _IMAGES_CAS_MAX_ATTEMPTS, listing_id,
    )
    raise HTTPException(
        status_code=503,
        detail="Couldn't save that photo change because the listing was being updated at the same time -- please try again.",
    )


def _locate_image(images: list, url: str, preferred_index=None):
    """Position of `url` in `images`, or None. Prefers `preferred_index` when
    that slot still holds the same URL: rows written by the old filename
    collision bug can hold the same URL twice, and list.index() always returns
    the first one, so an index-addressed request could hit the wrong copy."""
    if preferred_index is not None and 0 <= preferred_index < len(images):
        if images[preferred_index] == url:
            return preferred_index
    for i, existing in enumerate(images):
        if existing == url:
            return i
    return None


# --- Multi-batch upload sessions -------------------------------------------
# A big upload arrives as several requests because the production edge proxy
# kills request bodies over ~10MB mid-stream. Committing each batch straight
# into `images` makes the whole upload non-atomic: if batch 3 of 4 fails the
# agent is left with a half-built listing and no rollback, and a retry that
# starts again with a replace batch wipes what did land.
#
# With an upload_session the batches only *stage* their URLs and the photo array
# is written exactly once, when the client sends finalize. Staging lives in the
# storage bucket (no migration needed): one small JSON manifest per batch, named
# after the batch number, under {listing_id}/_upload-sessions/{session}/.
#
# One file per batch, never a shared file, so batches never read-modify-write
# each other -- and a retried batch simply overwrites its own manifest instead
# of duplicating its photos.
_UPLOAD_SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_UPLOAD_STAGING_DIR = "_upload-sessions"


def _staging_dir(listing_id: str, session_id: str) -> str:
    return f"{listing_id}/{_UPLOAD_STAGING_DIR}/{session_id}"


_STAGING_UNAVAILABLE = "Couldn't gather the photos you just uploaded -- please try uploading them again"


def _read_staged_urls(supabase, listing_id: str, session_id: str, strict: bool):
    """(urls staged so far by this session in batch order, number of batch
    manifests found). `strict` is for the finalize step, where quietly losing a
    manifest would quietly lose an agent's photos -- better to fail and let them
    retry than to commit a partial set."""
    directory = _staging_dir(listing_id, session_id)
    try:
        entries = supabase.storage.from_("listings-images").list(directory)
    except Exception:
        logger.exception("Could not list upload session %s", directory)
        if strict:
            raise HTTPException(status_code=503, detail=_STAGING_UNAVAILABLE)
        return [], 0

    urls = []
    found = 0
    for entry in sorted(entries or [], key=lambda e: e.get("name") or ""):
        name = entry.get("name") or ""
        if not name.endswith(".json"):
            continue
        found += 1
        try:
            raw = supabase.storage.from_("listings-images").download(f"{directory}/{name}")
            urls.extend(json.loads(raw.decode("utf-8")))
        except Exception:
            logger.exception("Could not read upload manifest %s/%s", directory, name)
            if strict:
                raise HTTPException(status_code=503, detail=_STAGING_UNAVAILABLE)
    return urls, found


def _clear_staging(supabase, listing_id: str, session_id: str):
    """Best effort. Only the small JSON manifests are removed -- never a photo."""
    directory = _staging_dir(listing_id, session_id)
    try:
        entries = supabase.storage.from_("listings-images").list(directory)
        paths = [f"{directory}/{e['name']}" for e in (entries or []) if e.get("name")]
        if paths:
            supabase.storage.from_("listings-images").remove(paths)
    except Exception:
        logger.exception("Could not clear upload session %s", directory)


def _dedupe_urls(urls: list) -> list:
    seen = set()
    out = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _process_and_upload_images(listing_id: str, agent_id: str, images: list, append: bool = False,
                               upload_session: str = None, batch_index: int = 0,
                               finalize: bool = True) -> dict:
    """append=False (default): this upload becomes the listing's complete photo
    set, replacing whatever was there -- the original single-request behavior.
    append=True: these photos are added AFTER the listing's existing ones.

    upload_session/finalize are optional and additive. Without a session every
    request commits on its own, exactly as before. With one, batches stage and
    only the finalize call touches the listing.
    """
    supabase = get_db()

    # Check ownership before spending 30 seconds on uploads. The old replace path
    # skipped this and just updated zero rows, so a wrong listing id looked like
    # a successful upload that vanished.
    owner = supabase.table("listings").select("images").eq("id", listing_id).eq("agent_id", agent_id).execute()
    if not owner.data:
        raise HTTPException(status_code=404, detail="Listing not found")

    existing_urls = (owner.data[0].get("images") or []) if append else []
    staged_urls = _read_staged_urls(supabase, listing_id, upload_session, strict=False)[0] if upload_session else []

    # The 15-photo cap is a per-listing limit, not a per-request one. Before
    # append existed one request WAS the whole set, so capping the batch meant
    # the same thing; with append an agent could upload three batches of 15 and
    # end up with 45 photos, which the poster/video/zip paths are not sized for.
    room = max(0, MAX_LISTING_PHOTOS - len(existing_urls) - len(staged_urls))
    if room <= 0 and not (upload_session and staged_urls):
        raise HTTPException(
            status_code=400,
            detail=f"This listing already has the maximum of {MAX_LISTING_PHOTOS} photos. Remove one before adding more."
        )
    # A session whose earlier batches already filled the cap must still be able
    # to finalize. Erroring here instead would strand every photo the agent had
    # uploaded so far, which is the exact failure this rewrite exists to remove.
    capped = len(images) > room
    images = images[:room]
    start_index = len(existing_urls) + len(staged_urls)

    batch_urls = []
    for i, img in enumerate(images):
        image_data = img.get("image_data")
        img_bytes = base64.b64decode(image_data)
        pil_img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
        pil_img.thumbnail((1920, 1920))
        # Intentionally no automatic enhancement here -- stored as-is so the
        # agent's later "Enhance" click in My Listings (cloudinary_enhance)
        # is the *only* thing that ever processes a photo, applied once, to
        # a clean source. Stacking upload-time auto-enhance underneath it
        # produced double-processed, worse-looking photos (see Jane's report).

        buffer = io.BytesIO()
        pil_img.save(buffer, format="JPEG", quality=80)
        buffer.seek(0)
        compressed = buffer.read()

        # The trailing random token is what makes this safe. The old name was
        # f"{start_index + i}_{listing_id[:8]}.jpg", i.e. derived purely from how
        # many photos the array currently held -- which collides the moment a
        # photo has been deleted. A listing that once had 15 photos and lost one
        # has 14 entries but still has files 0..14 sitting in storage, so the
        # next appended photo computed "14_..." and, because the upload runs with
        # upsert, silently overwrote the agent's existing photo 14 with the new
        # image. The listing then rendered the same photo twice and the original
        # was gone for good. A unique name can never overwrite an existing photo,
        # and it also defuses two concurrent appends racing on the same index.
        filename = f"{listing_id}/{start_index + i}_{listing_id[:8]}_{uuid.uuid4().hex[:8]}.jpg"
        supabase.storage.from_("listings-images").upload(
            filename,
            compressed,
            {"content-type": "image/jpeg", "upsert": "true"}
        )

        url = supabase.storage.from_("listings-images").get_public_url(filename)
        batch_urls.append(url)

    if upload_session:
        try:
            supabase.storage.from_("listings-images").upload(
                f"{_staging_dir(listing_id, upload_session)}/{batch_index:04d}.json",
                json.dumps(batch_urls).encode("utf-8"),
                {"content-type": "application/json", "upsert": "true"},
            )
        except Exception:
            logger.exception("Could not stage upload batch %s for listing %s", batch_index, listing_id)
            raise HTTPException(
                status_code=503,
                detail="Couldn't save this batch of photos -- please try again",
            )

        if not finalize:
            pending = _dedupe_urls(staged_urls + batch_urls)
            return {"success": True, "image_urls": pending, "committed": False,
                    "staged_count": len(pending), "capped": capped}

        # Re-read so batches that landed on other workers are included. Our own
        # batch is appended too, in case the storage listing hasn't caught up
        # yet -- _dedupe_urls keeps it from being counted twice.
        session_urls, found = _read_staged_urls(supabase, listing_id, upload_session, strict=True)
        all_urls = _dedupe_urls(session_urls + batch_urls)

        # batch_index is 0-based and contiguous, so finalizing batch N means N+1
        # manifests should exist. Fewer means the listing hasn't caught up (or a
        # batch never landed) and committing now would quietly drop photos --
        # worst of all on a replace, which would delete what did land. Refuse
        # instead: the agent retries and nothing is lost.
        if found < batch_index + 1:
            logger.warning(
                "Upload session %s/%s finalized with %s of %s batches staged",
                listing_id, upload_session, found, batch_index + 1,
            )
            raise HTTPException(status_code=503, detail=_STAGING_UNAVAILABLE)
    else:
        all_urls = batch_urls

    def mutate(current, ctx):
        if append:
            merged = list(current)
            for url in all_urls:
                if url not in merged:
                    merged.append(url)
            merged = merged[:MAX_LISTING_PHOTOS]
            return None if merged == current else merged
        # Replace: this upload becomes the listing's complete photo set.
        replacement = all_urls[:MAX_LISTING_PHOTOS]
        return None if replacement == current else replacement

    final_urls = _mutate_listing_images(listing_id, agent_id, mutate)

    if upload_session:
        _clear_staging(supabase, listing_id, upload_session)

    return {"success": True, "image_urls": final_urls, "committed": True,
            "staged_count": 0, "capped": capped}

PDF_MIN_PHOTO_DIM = 400  # skip embedded logos/icons/decorative graphics smaller than this
# Size alone does not separate photos from graphics: brochures use big solid-colour
# background panels and colour bars that clear PDF_MIN_PHOTO_DIM comfortably. One
# such panel (a 500x500 block of flat dark green) is sitting in a live listing right
# now and renders as an empty-looking tile. Both thresholds below are deliberately
# extreme -- a real photo scores orders of magnitude above them -- because wrongly
# dropping one of an agent's photos is far worse than letting a graphic through,
# which the agent can remove with one click.
PDF_MIN_DISTINCT_COLOURS = 8    # out of 1024 sampled pixels; a flat panel scores 1
PDF_MAX_PHOTOS = MAX_LISTING_PHOTOS  # a PDF can't fill a listing past its own cap


def _is_flat_graphic(pil_img) -> bool:
    """True for solid-colour blocks -- brochure furniture rather than
    photographs. Judged purely on how many distinct colours survive a 32x32
    downsample, so the cost is the same whether the source is 500px or 5000px:
    the flat dark-green panel sitting in a live listing scores 1, a real photo
    scores hundreds.

    A greyscale std-dev test used to run alongside this (drop anything under
    3.0) and has been removed: measured against real listing photos it flagged a
    plain white wall (0.54), a night shot (0.50) and an overcast exterior (1.70)
    as graphics. Dropping one of an agent's photos is far worse than letting a
    graphic through, which the agent removes with one click."""
    try:
        small = pil_img.resize((32, 32))
        colours = small.getcolors(32 * 32)
        return colours is not None and len(colours) <= PDF_MIN_DISTINCT_COLOURS
    except Exception:
        # Never let the filter itself lose a photo -- if it cannot judge, keep it.
        return False


def _extract_photos_from_pdf(pdf_data_b64: str) -> dict:
    """Pulls every embedded raster image out of a PDF (e.g. a marketing brochure),
    skipping small non-photo graphics (logos, icons, decorative lines) and exact
    duplicates (a letterhead logo repeated on every page). Returns
    {"images": [{"image_data": <base64 jpeg>}, ...], "skipped_graphics": n,
    "skipped_duplicates": n} -- the counts let the UI say "N graphics filtered"
    so an agent who expected 12 photos and got 9 can see why. This function only
    extracts and re-encodes, it doesn't touch storage or the DB.
    """
    pdf_bytes = base64.b64decode(pdf_data_b64)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")

    results = []
    seen_xrefs = set()
    seen_hashes = set()
    skipped_graphics = 0
    skipped_duplicates = 0

    try:
        for page in doc:
            if len(results) >= PDF_MAX_PHOTOS:
                break
            for img in page.get_images(full=True):
                if len(results) >= PDF_MAX_PHOTOS:
                    break

                xref = img[0]
                if xref in seen_xrefs:
                    continue
                seen_xrefs.add(xref)

                try:
                    extracted = doc.extract_image(xref)
                    pil_img = PILImage.open(io.BytesIO(extracted["image"])).convert("RGB")
                except Exception:
                    continue

                if pil_img.width < PDF_MIN_PHOTO_DIM or pil_img.height < PDF_MIN_PHOTO_DIM:
                    skipped_graphics += 1
                    continue

                if _is_flat_graphic(pil_img):
                    skipped_graphics += 1
                    continue

                content_hash = hashlib.sha1(extracted["image"]).hexdigest()
                if content_hash in seen_hashes:
                    skipped_duplicates += 1
                    continue
                seen_hashes.add(content_hash)

                pil_img.thumbnail((1920, 1920))
                buffer = io.BytesIO()
                pil_img.save(buffer, format="JPEG", quality=85)
                results.append({"image_data": base64.b64encode(buffer.getvalue()).decode("ascii")})
    finally:
        doc.close()

    return {
        "images": results,
        "skipped_graphics": skipped_graphics,
        "skipped_duplicates": skipped_duplicates,
    }


@app.post("/api/listings/extract-pdf-photos")
async def extract_pdf_photos(request: Request, agent=Depends(get_current_agent)):
    body = await request.json()
    pdf_data = body.get("pdf_data")
    if not pdf_data:
        raise HTTPException(status_code=400, detail="No PDF provided")

    try:
        extracted = await asyncio.to_thread(_extract_photos_from_pdf, pdf_data)
    except Exception:
        logger.exception("PDF photo extraction failed")
        raise HTTPException(status_code=400, detail="Could not read this PDF -- please try a different file")

    if not extracted["images"]:
        raise HTTPException(status_code=400, detail="No photos found in this PDF")

    # "images" is unchanged for the deployed frontend; the counts are additive.
    return {
        "images": extracted["images"],
        "skipped": extracted["skipped_graphics"],
        "skipped_graphics": extracted["skipped_graphics"],
        "skipped_duplicates": extracted["skipped_duplicates"],
    }


@app.post("/api/listings/{listing_id}/upload-images")
async def upload_listing_images(listing_id: str, request: Request, agent=Depends(get_current_agent)):
    try:
        body = await request.json()
        images = body.get("images", [])
        append = bool(body.get("append", False))

        # Additive: without upload_session this behaves exactly as before, one
        # commit per request. With it, batches stage and only finalize commits.
        upload_session = body.get("upload_session") or None
        batch_index = body.get("batch_index", 0)
        finalize = bool(body.get("finalize", True)) if upload_session else True

        if not images:
            raise HTTPException(status_code=400, detail="No images provided")

        if upload_session and not _UPLOAD_SESSION_RE.match(str(upload_session)):
            raise HTTPException(status_code=400, detail="Invalid upload session id")

        try:
            batch_index = int(batch_index)
        except (TypeError, ValueError):
            batch_index = 0
        if batch_index < 0 or batch_index > 999:
            raise HTTPException(status_code=400, detail="Invalid batch number")

        if len(images) > MAX_LISTING_PHOTOS:
            images = images[:MAX_LISTING_PHOTOS]

        return await asyncio.to_thread(
            _process_and_upload_images,
            listing_id, agent["id"], images, append,
            upload_session, batch_index, finalize,
        )

    except HTTPException:
        # Let deliberate 4xx answers (e.g. "listing is already at 15 photos")
        # through as themselves instead of relabelling them a 500, which would
        # show the agent a scary server error for an ordinary limit.
        raise
    except Exception:
        logger.exception("Photo upload failed for listing %s", listing_id)
        raise HTTPException(status_code=500, detail="Something went wrong uploading these photos -- please try again")

def _delete_listing_image(listing_id: str, agent_id: str, image_index, image_url) -> list:
    """The storage file is deliberately left in place: an accidental delete stays
    recoverable, and orphans are harmless now that upload filenames are unique."""
    target = {"url": image_url}

    def mutate(current, ctx):
        if target["url"] is None:
            # Index-addressed (older frontend). Resolve to a URL on the first
            # read so a compare-and-swap retry can never land on a different
            # photo than the one the agent clicked.
            if image_index is None or image_index < 0 or image_index >= len(current):
                raise HTTPException(status_code=400, detail="Invalid image index")
            target["url"] = current[image_index]

        pos = _locate_image(current, target["url"], image_index)
        if pos is None:
            if ctx["attempt"] > 0:
                # Our own earlier attempt almost certainly landed (or another
                # request removed the same photo). Either way the agent's intent
                # is satisfied -- don't show them an error for a done deed.
                return None
            raise HTTPException(status_code=404, detail="That photo has already been removed")
        return current[:pos] + current[pos + 1:]

    return _mutate_listing_images(listing_id, agent_id, mutate)


@app.delete("/api/listings/{listing_id}/images/{image_index}")
def delete_listing_image(listing_id: str, image_index: int, image_url: str = None, agent=Depends(get_current_agent)):
    # image_url is optional and additive. Positional indices alone are unsafe:
    # a client whose copy of the array is stale (a batch finished, or another
    # tab deleted something) deletes whatever photo now sits at that position.
    # When the URL is supplied the server matches on it and the index is only a
    # hint for picking between duplicate entries.
    return {"images": _delete_listing_image(listing_id, agent["id"], image_index, image_url)}


@app.delete("/api/listings/{listing_id}/images")
def delete_listing_image_by_url(listing_id: str, image_url: str, agent=Depends(get_current_agent)):
    """Index-free form of the delete above -- preferred for new frontend code."""
    return {"images": _delete_listing_image(listing_id, agent["id"], None, image_url)}


# Enhancing is slow (fetch + Cloudinary round trip) and not idempotent in the way
# that matters: enhancing an already-enhanced photo visibly over-processes it.
# This set stops a double-click re-entering while the first pass is in flight.
# It is per-worker and therefore best effort -- the compare-and-swap on the array
# is what actually keeps the outcome correct across Railway's 4 workers.
_enhancing_urls = set()
_enhancing_guard = threading.Lock()

# Filename marker on the enhanced copy -- doubles as the "already enhanced" flag,
# so no extra column is needed to remember it.
_ENHANCED_MARKER = "enhanced_"


def _begin_enhance(url: str) -> bool:
    with _enhancing_guard:
        if url in _enhancing_urls:
            return False
        _enhancing_urls.add(url)
        return True


def _end_enhance(url: str):
    with _enhancing_guard:
        _enhancing_urls.discard(url)


def _enhance_listing_image(listing_id: str, agent_id: str, image_index, image_url) -> str:
    supabase = get_db()
    result = supabase.table("listings").select("images").eq("id", listing_id).eq("agent_id", agent_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    images = result.data[0].get("images") or []

    if image_url:
        pos = _locate_image(images, image_url, image_index)
        if pos is None:
            raise HTTPException(
                status_code=404,
                detail="That photo is no longer on this listing -- refresh the page and try again",
            )
        url = image_url
    else:
        if image_index is None or image_index < 0 or image_index >= len(images):
            raise HTTPException(status_code=400, detail="Invalid image index")
        pos = image_index
        url = images[pos]

    # Enhancing an enhanced photo stacks the processing and looks visibly worse
    # (the same reason upload-time auto-enhance was removed). Because the
    # enhanced copy lands under its own name we can just recognise it and stop.
    # Photos enhanced by the old in-place code aren't marked, so they can still
    # be re-enhanced -- same as today, no regression.
    if _ENHANCED_MARKER in url.rsplit("/", 1)[-1]:
        raise HTTPException(status_code=409, detail="This photo has already been enhanced")

    if not _begin_enhance(url):
        raise HTTPException(status_code=409, detail="This photo is already being enhanced -- give it a moment")

    try:
        fetch_resp = requests.get(url, timeout=30)
        if fetch_resp.status_code != 200:
            raise HTTPException(status_code=502, detail="Could not fetch this photo to enhance it")

        enhanced_bytes = cloudinary_enhance.enhance_image(fetch_resp.content)
        if not enhanced_bytes:
            raise HTTPException(status_code=503, detail="Enhancement isn't available right now -- please try again shortly")

        # Write the enhanced photo to a NEW file. The old code upserted it onto
        # the original's storage path, which destroyed the untouched original
        # (nothing to fall back to if the agent dislikes the result) and let a
        # second enhance read back the already-enhanced pixels and process them
        # again. A fresh name makes the operation non-destructive and means the
        # URL itself tells us whether a photo has been swapped yet.
        filename = f"{listing_id}/enhanced_{listing_id[:8]}_{uuid.uuid4().hex[:8]}.jpg"
        supabase.storage.from_("listings-images").upload(
            filename, enhanced_bytes, {"content-type": "image/jpeg", "upsert": "true"}
        )
        new_url = supabase.storage.from_("listings-images").get_public_url(filename)

        gone = {"value": False}

        def mutate(current, ctx):
            if new_url in current:
                return None  # already swapped in (our write landed, response lost)
            slot = _locate_image(current, url, pos)
            if slot is None:
                gone["value"] = True
                return None
            updated = list(current)
            updated[slot] = new_url
            return updated

        final_urls = _mutate_listing_images(listing_id, agent_id, mutate)

        if gone["value"] and new_url not in final_urls:
            # Used to return 200 with a URL pointing at a photo that is not on
            # the listing, so the UI showed a "done!" for nothing.
            raise HTTPException(
                status_code=404,
                detail="That photo was removed while it was being enhanced, so the enhanced version wasn't saved",
            )
        return new_url
    finally:
        _end_enhance(url)


@app.post("/api/listings/{listing_id}/images/{image_index}/enhance")
async def enhance_listing_image(listing_id: str, image_index: int, image_url: str = None, agent=Depends(get_current_agent)):
    # image_url optional and additive -- see delete_listing_image.
    new_url = await asyncio.to_thread(_enhance_listing_image, listing_id, agent["id"], image_index, image_url)
    return {"success": True, "image_url": new_url}


@app.post("/api/listings/{listing_id}/images/enhance")
async def enhance_listing_image_by_url(listing_id: str, image_url: str, agent=Depends(get_current_agent)):
    """Index-free form of the enhance above -- preferred for new frontend code."""
    new_url = await asyncio.to_thread(_enhance_listing_image, listing_id, agent["id"], None, image_url)
    return {"success": True, "image_url": new_url}

@app.post("/api/listings/{listing_id}/post-facebook")
def post_to_facebook(listing_id: str, req: FacebookPostRequest, agent=Depends(get_current_agent)):
    if not _can_use_facebook_beta(agent):
        raise HTTPException(status_code=403, detail="Facebook posting is not yet available on your account")

    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = result.data[0]

    if not listing.get("poster_url"):
        raise HTTPException(status_code=400, detail="Generate a poster before posting to Facebook")

    fb_page_id = agent.get("fb_page_id")
    fb_token = agent.get("fb_page_access_token")
    if not fb_page_id or not fb_token:
        raise HTTPException(status_code=400, detail="Connect Facebook in My Profile first")

    caption = req.caption[:2000]

    def _clear_facebook_connection():
        get_db().table("agents").update({
            "fb_user_access_token": None, "fb_page_id": None, "fb_page_access_token": None,
            "fb_page_name": None, "instagram_business_account_id": None,
            "instagram_username": None, "instagram_connected_at": None,
        }).eq("id", agent["id"]).execute()

    response = requests.post(
        f"https://graph.facebook.com/v25.0/{fb_page_id}/photos",
        data={"url": listing["poster_url"], "caption": caption, "access_token": fb_token}, timeout=15)
    data = response.json()
    if "id" in data:
        return {"success": True, "post_id": data["id"]}

    err = data.get("error", {})
    if err.get("code") == 190:
        _clear_facebook_connection()
        raise HTTPException(status_code=401, detail="Your Facebook connection has expired. Please reconnect in My Profile.")
    raise HTTPException(status_code=400, detail=err.get("message", "Failed to post to Facebook"))

@app.post("/api/listings/{listing_id}/post-linkedin")
def post_to_linkedin(listing_id: str, req: LinkedInPostRequest, agent=Depends(get_current_agent)):
    if not _can_use_linkedin_beta(agent):
        raise HTTPException(status_code=403, detail="LinkedIn posting is not yet available on your account")

    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = result.data[0]

    access_token = agent.get("linkedin_access_token")
    person_urn = agent.get("linkedin_person_urn")
    if not access_token or not person_urn:
        raise HTTPException(status_code=400, detail="Connect LinkedIn in My Profile first")

    caption = req.caption[:3000]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Linkedin-Version": LINKEDIN_API_VERSION,
        "X-Restli-Protocol-Version": "2.0.0",
        "Content-Type": "application/json",
    }

    def _clear_linkedin_connection():
        get_db().table("agents").update({
            "linkedin_access_token": None, "linkedin_refresh_token": None,
            "linkedin_person_urn": None, "linkedin_name": None, "linkedin_connected_at": None,
        }).eq("id", agent["id"]).execute()

    # Attach the poster if there is one -- LinkedIn requires a real 2-step upload (register,
    # then PUT the bytes) rather than Facebook's "give me a URL" shortcut. A failed upload
    # shouldn't block the whole post -- falls back to a text-only post rather than erroring out.
    media = None
    poster_url = listing.get("poster_url")
    if poster_url:
        try:
            init_resp = requests.post(
                "https://api.linkedin.com/rest/images?action=initializeUpload",
                headers=headers,
                json={"initializeUploadRequest": {"owner": person_urn}},
                timeout=15,
            )
            init_data = init_resp.json().get("value", {})
            upload_url = init_data.get("uploadUrl")
            image_urn = init_data.get("image")
            if upload_url and image_urn:
                img_bytes = requests.get(poster_url, timeout=15).content
                put_resp = requests.put(
                    upload_url, data=img_bytes,
                    headers={"Authorization": f"Bearer {access_token}"}, timeout=30,
                )
                if put_resp.status_code in (200, 201):
                    media = {"id": image_urn}
        except Exception:
            media = None

    body = {
        "author": person_urn,
        "commentary": caption,
        "visibility": "PUBLIC",
        "distribution": {"feedDistribution": "MAIN_FEED", "targetEntities": [], "thirdPartyDistributionChannels": []},
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": False,
    }
    if media:
        body["content"] = {"media": media}

    response = requests.post("https://api.linkedin.com/rest/posts", headers=headers, json=body, timeout=15)
    if response.status_code == 201:
        return {"success": True, "post_id": response.headers.get("x-restli-id", "")}

    if response.status_code == 401:
        _clear_linkedin_connection()
        raise HTTPException(status_code=401, detail="Your LinkedIn connection has expired. Please reconnect in My Profile.")

    try:
        err = response.json()
    except Exception:
        err = {}
    raise HTTPException(status_code=400, detail=err.get("message", "Failed to post to LinkedIn"))

# ================================
# IMAGE ENHANCEMENT
# ================================
# Tuned for a subtle, phone-camera-style "auto enhance" -- corrects flat/underexposed
# real estate photos without an oversaturated or artificial look. Kept as named
# constants so the look can be retuned later without hunting through the function body.
ENHANCE_COLOR = 1.12
ENHANCE_CONTRAST = 1.08
ENHANCE_BRIGHTNESS = 1.04
ENHANCE_SHARPNESS = 1.15

def _auto_enhance_photo(img: PILImage.Image) -> PILImage.Image:
    try:
        enhanced = ImageOps.autocontrast(img, cutoff=1, preserve_tone=True)
        enhanced = ImageEnhance.Color(enhanced).enhance(ENHANCE_COLOR)
        enhanced = ImageEnhance.Contrast(enhanced).enhance(ENHANCE_CONTRAST)
        enhanced = ImageEnhance.Brightness(enhanced).enhance(ENHANCE_BRIGHTNESS)
        enhanced = ImageEnhance.Sharpness(enhanced).enhance(ENHANCE_SHARPNESS)
        return enhanced
    except Exception:
        return img

def _to_number(value) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0

def _format_price_millions(value) -> str:
    num = _to_number(value)
    if num <= 0:
        return str(value) if value else ""
    millions = f"{num / 1_000_000:.2f}".rstrip("0").rstrip(".")
    return f"{millions}M"

# ================================
# POSTER GENERATION
# ================================
POSTER_THUMBNAIL_BASE = "https://www.nestlist.sg/poster-thumbnails"
POSTER_TEMPLATES = [
    {"id": "editorial", "name": "Editorial", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/editorial.jpg"},
    {"id": "gallery-frame", "name": "Gallery Frame", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/gallery-frame.jpg"},
    {"id": "bold-type", "name": "Bold Type", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/bold-type.jpg"},
    {"id": "vignette-frame", "name": "Vignette Frame", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/vignette-frame.jpg"},
    {"id": "postcard", "name": "Postcard", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/postcard.jpg"},
    {"id": "gold-frame", "name": "Gold Frame", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/gold-frame.jpg"},
    {"id": "top-banner-minimal", "name": "Top Banner Minimal", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/top-banner-minimal.jpg"},
    {"id": "numeral-focus", "name": "Numeral Focus", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/numeral-focus.jpg"},
    {"id": "corner-badge", "name": "Corner Badge", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/corner-badge.jpg"},
    {"id": "asymmetric-column", "name": "Asymmetric Column", "thumbnail_url": f"{POSTER_THUMBNAIL_BASE}/asymmetric-column.jpg"},
]

@app.get("/api/poster-templates")
def get_poster_templates(agent=Depends(get_current_agent)):
    return [{"id": t["id"], "name": t["name"], "thumbnail_url": t["thumbnail_url"]} for t in POSTER_TEMPLATES]

@app.post("/api/listings/{listing_id}/generate-poster")
def generate_poster(listing_id: str, photo_index: int = 0, template_id: str = None, agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = result.data[0]

    if template_id and template_id not in {t["id"] for t in POSTER_TEMPLATES}:
        raise HTTPException(status_code=400, detail="Unknown poster template")
    # Explicit choice wins and is remembered on the listing; otherwise fall back to
    # whatever this listing last used, then the agent's profile default.
    chosen_template_id = template_id or listing.get("poster_template_id") or agent.get("poster_template_id") or "editorial"

    images = listing.get("images") or []
    if not images:
        raise HTTPException(status_code=400, detail="Upload at least one photo before generating a poster")
    if photo_index < 0 or photo_index >= len(images):
        photo_index = 0

    price_num = _to_number(listing.get("price"))
    built_up_num = _to_number(listing.get("built_up"))
    price_psf = round(price_num / built_up_num) if built_up_num > 0 else 0
    bedrooms_match = re.search(r"\d+", str(listing.get("bedrooms") or ""))
    bathrooms_match = re.search(r"\d+", str(listing.get("bathrooms") or ""))
    bedrooms_val = bedrooms_match.group(0) if bedrooms_match else ""
    bathrooms_val = bathrooms_match.group(0) if bathrooms_match else ""

    # Posters/videos only ever draw the district token ("DISTRICT 15"), never the
    # street line -- but the location is sanitized first anyway so a house number
    # can never reach on-image text if this ever starts drawing more of it.
    poster_location = _strip_house_number(listing.get("location"))
    district_match = re.search(r"district\s*\d+", poster_location, re.IGNORECASE)
    property_type_text = (listing.get("property_type") or "").upper()
    district_text = district_match.group(0).upper() if district_match else ""

    stats = [
        f"{bedrooms_val} Rooms" if bedrooms_val else "",
        f"{bathrooms_val} Baths" if bathrooms_val else "",
        f"{built_up_num:,.0f} sqft" if built_up_num else "",
        f"SGD {price_psf:,} psf" if price_psf else "",
    ]

    try:
        poster_image = poster_renderer.render_poster(
            property_type=property_type_text,
            district=district_text,
            price_text=f"SGD {_format_price_millions(listing['price'])}",
            stats=stats,
            agent_name=agent["name"],
            agent_contact_line=agent.get("contact", ""),
            property_photo_url=images[photo_index],
            agent_photo_url=agent.get("photo_url"),
            template_id=chosen_template_id,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Poster rendering failed: {e}")

    buffer = io.BytesIO()
    poster_image.save(buffer, format="JPEG", quality=92)
    buffer.seek(0)

    supabase = get_db()
    filename = f"posters/{listing_id}.jpg"
    supabase.storage.from_("listings-images").upload(
        filename,
        buffer.read(),
        {"content-type": "image/jpeg", "upsert": "true"}
    )
    poster_url = f"{supabase.storage.from_('listings-images').get_public_url(filename)}?v={uuid.uuid4().hex[:8]}"

    update_payload = {"poster_url": poster_url}
    if template_id:
        update_payload["poster_template_id"] = template_id
    get_db().table("listings").update(update_payload).eq("id", listing_id).eq("agent_id", agent["id"]).execute()

    return {"poster_url": poster_url, "poster_template_id": chosen_template_id}

DEFAULT_VIDEO_TEMPLATE_ID = "classic"

VIDEO_TEMPLATES = [
    {"id": DEFAULT_VIDEO_TEMPLATE_ID, "name": "Classic Video"},
]

# Templates that shipped once and were then withdrawn. They stay listed here (but out
# of VIDEO_TEMPLATES, so the frontend picker never offers them again) purely so we can
# recognise the id when it turns up in old data: listings generated before the
# withdrawal still carry it in video_template_id, and a browser tab left open across
# the deploy still holds the old picker in memory. Anything naming a retired template
# quietly renders the default instead of erroring -- an agent regenerating an old
# listing must get a video, not a "Unknown video template" wall.
RETIRED_VIDEO_TEMPLATE_IDS = {"card"}  # "Card Overlay", withdrawn 2026-08-15

_LIVE_VIDEO_TEMPLATE_IDS = {t["id"] for t in VIDEO_TEMPLATES}


def _resolve_video_template_id(requested_id):
    """Maps a requested or stored template id onto one we can actually render.
    Returns None for an id we have never shipped, so the caller can reject it as a
    genuine client bug rather than silently papering over a typo."""
    if requested_id in _LIVE_VIDEO_TEMPLATE_IDS:
        return requested_id
    if requested_id in RETIRED_VIDEO_TEMPLATE_IDS:
        return DEFAULT_VIDEO_TEMPLATE_ID
    return None


@app.get("/api/video-templates")
def get_video_templates(agent=Depends(get_current_agent)):
    return VIDEO_TEMPLATES

# Video renders take 40-70s+ -- far too long to ride on a single HTTP request.
# Safari hard-kills any request around the 60s mark (agents saw 'Failed to
# fetch'/'Load failed' with no explanation), and platform blips (e.g. Railway's
# US West incident) kill long-lived connections first. So generate-video is
# fire-and-forget: the endpoint validates, marks the listing 'rendering', kicks
# the render into a background thread, and returns immediately. The frontend
# polls the listing until video_status lands on 'done' or 'failed'. Status
# lives in the DB (not process memory) because uvicorn runs multiple workers --
# the poll may be answered by a different worker than the one rendering.
_video_render_tasks = set()

def _render_video_job(listing_id: str, agent: dict, listing: dict, chosen_video_template_id: str, persist_video_template_id: bool, photo_index: int):
    try:
        # Re-read the listing at job start rather than trusting the snapshot the
        # endpoint captured. A render takes a minute or more, and in that window the
        # agent may well delete or reorder a photo from the same My Listings screen --
        # rendering the stale array would put a photo they just removed into a video
        # they are about to post. The freshest possible read is also the cheapest fix:
        # one extra select per render. If the re-read fails (DB blip), fall back to the
        # snapshot rather than losing the render entirely.
        try:
            fresh = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
            if fresh.data:
                listing = fresh.data[0]
        except Exception as e:
            logger.warning("video job %s: could not re-read listing (%s); using the queued snapshot", listing_id, e)

        images = listing.get("images") or []
        if not images:
            raise RuntimeError("This listing has no photos any more -- add a photo and generate the video again.")
        # The hero photo may have been the one deleted, which would leave photo_index
        # pointing past the end of the shortened array.
        if photo_index < 0 or photo_index >= len(images):
            photo_index = 0

        price_num = _to_number(listing.get("price"))
        built_up_num = _to_number(listing.get("built_up"))
        price_psf = round(price_num / built_up_num) if built_up_num > 0 else 0
        bedrooms_match = re.search(r"\d+", str(listing.get("bedrooms") or ""))
        bathrooms_match = re.search(r"\d+", str(listing.get("bathrooms") or ""))
        bedrooms_val = bedrooms_match.group(0) if bedrooms_match else ""
        bathrooms_val = bathrooms_match.group(0) if bathrooms_match else ""

        # Same as the poster path: only the district token is drawn, but the
        # location is sanitized before it is read so no house number can leak.
        video_location = _strip_house_number(listing.get("location"))
        district_match = re.search(r"district\s*\d+", video_location, re.IGNORECASE)
        property_type_text = (listing.get("property_type") or "").upper()
        district_text = district_match.group(0).upper() if district_match else ""

        stats = [
            f"{bedrooms_val} Rooms" if bedrooms_val else "",
            f"{bathrooms_val} Baths" if bathrooms_val else "",
            f"{built_up_num:,.0f} sqft" if built_up_num else "",
            f"SGD {price_psf:,} psf" if price_psf else "",
        ]

        video_bytes, degradations = video_renderer.render_property_video(
            image_urls=images,
            property_type=property_type_text,
            district=district_text,
            price_text=f"SGD {_format_price_millions(listing['price'])}",
            stats=stats,
            agent_name=agent["name"],
            agent_contact_line=agent.get("contact", ""),
            style=chosen_video_template_id,
            photo_index=photo_index,
            agent_photo_url=agent.get("photo_url"),
            # Room captions are model-written and burned into the video, so they go
            # through exactly the same house-number/price stripping as every other
            # copy surface. Passed in rather than imported because video_renderer
            # cannot import main (main imports it).
            copy_guard=apply_listing_copy_guards,
        )
        if degradations:
            # The agent still gets a video, so this is not a failure -- but a quietly
            # degraded render should be visible in Railway's logs, not indistinguishable
            # from a clean one.
            logger.warning("video %s rendered with degradations: %s", listing_id, "; ".join(degradations))

        supabase = get_db()
        filename = f"videos/{listing_id}.mp4"
        supabase.storage.from_("listings-images").upload(
            filename,
            video_bytes,
            {"content-type": "video/mp4", "upsert": "true"}
        )
        video_url = f"{supabase.storage.from_('listings-images').get_public_url(filename)}?v={uuid.uuid4().hex[:8]}"

        update_payload = {"video_url": video_url, "video_status": "done", "video_error": None}
        if persist_video_template_id:
            update_payload["video_template_id"] = chosen_video_template_id
        get_db().table("listings").update(update_payload).eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    except Exception as e:
        try:
            get_db().table("listings").update({
                "video_status": "failed",
                "video_error": str(e)[:500],
            }).eq("id", listing_id).eq("agent_id", agent["id"]).execute()
        except Exception:
            pass  # DB unreachable too -- the stale-lock timeout lets the agent retry

@app.post("/api/listings/{listing_id}/generate-video")
async def generate_video(listing_id: str, video_template_id: str = None, photo_index: int = 0, agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = result.data[0]

    # Same fallback precedence as generate-poster: explicit choice wins and is
    # remembered on the listing, otherwise fall back to what this listing last used.
    # Both paths go through _resolve_video_template_id so a retired template id --
    # from an old listing row or a stale browser tab -- degrades to the default
    # slideshow instead of failing the request.
    stored_video_template_id = listing.get("video_template_id")
    if video_template_id:
        resolved = _resolve_video_template_id(video_template_id)
        if resolved is None:
            raise HTTPException(status_code=400, detail="Unknown video template")
        chosen_video_template_id = resolved
    else:
        chosen_video_template_id = (
            _resolve_video_template_id(stored_video_template_id) if stored_video_template_id else None
        ) or DEFAULT_VIDEO_TEMPLATE_ID

    # Write the template back when the agent picked one (existing behaviour), and also
    # whenever the row's stored id isn't what we actually rendered -- that heals a
    # retired id in place so the fallback fires once per listing, not on every render.
    persist_video_template_id = bool(video_template_id) or (
        stored_video_template_id is not None and stored_video_template_id != chosen_video_template_id
    )

    images = listing.get("images") or []
    if not images:
        raise HTTPException(status_code=400, detail="Upload at least one photo before generating a video")
    if photo_index < 0 or photo_index >= len(images):
        photo_index = 0

    # One render at a time per listing. The 10-minute stale-lock cutoff exists so
    # a crashed worker (which can never write 'failed') doesn't lock the listing
    # out of video generation forever.
    if listing.get("video_status") == "rendering":
        started_raw = listing.get("video_render_started_at")
        still_fresh = False
        if started_raw:
            try:
                started_at = datetime.fromisoformat(str(started_raw).replace("Z", "+00:00"))
                still_fresh = (datetime.now(timezone.utc) - started_at) < timedelta(minutes=10)
            except Exception:
                still_fresh = False
        if still_fresh:
            raise HTTPException(status_code=409, detail="A video is already being generated for this listing -- it should be ready in a minute or two.")

    get_db().table("listings").update({
        "video_status": "rendering",
        "video_error": None,
        "video_render_started_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", listing_id).eq("agent_id", agent["id"]).execute()

    task = asyncio.create_task(asyncio.to_thread(
        _render_video_job, listing_id, agent, listing,
        chosen_video_template_id, persist_video_template_id, photo_index,
    ))
    _video_render_tasks.add(task)
    task.add_done_callback(_video_render_tasks.discard)

    return {"status": "rendering", "video_template_id": chosen_video_template_id}

def _wait_for_ig_container_ready(container_id: str, page_token: str, max_attempts: int = 10, delay_seconds: float = 1.5) -> bool:
    # Instagram fetches/processes the image asynchronously after the container is
    # created — publishing before that finishes fails with "Media ID is not
    # available", so poll status_code until it reports FINISHED.
    for _ in range(max_attempts):
        status_res = requests.get(
            f"https://graph.facebook.com/v25.0/{container_id}",
            params={"fields": "status_code", "access_token": page_token}, timeout=15)
        status_code = status_res.json().get("status_code")
        if status_code == "FINISHED":
            return True
        if status_code == "ERROR":
            return False
        time.sleep(delay_seconds)
    return False

@app.post("/api/listings/{listing_id}/post-instagram")
def post_to_instagram(listing_id: str, req: InstagramPostRequest, agent=Depends(get_current_agent)):
    if not _can_use_instagram_beta(agent):
        raise HTTPException(status_code=403, detail="Instagram posting is not yet available on your account")

    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = result.data[0]

    if not listing.get("poster_url"):
        raise HTTPException(status_code=400, detail="Generate a poster before posting to Instagram")

    ig_user_id = agent.get("instagram_business_account_id")
    page_token = agent.get("fb_page_access_token")
    if not ig_user_id or not page_token:
        raise HTTPException(status_code=400, detail="Connect Instagram in My Profile first")

    caption = req.caption[:2200]

    def _clear_instagram_connection():
        get_db().table("agents").update({
            "fb_user_access_token": None, "fb_page_id": None, "fb_page_access_token": None,
            "fb_page_name": None, "instagram_business_account_id": None,
            "instagram_username": None, "instagram_connected_at": None,
        }).eq("id", agent["id"]).execute()

    r1 = requests.post(f"https://graph.facebook.com/v25.0/{ig_user_id}/media", data={
        "image_url": listing["poster_url"], "caption": caption, "access_token": page_token,
    }, timeout=15)
    d1 = r1.json()
    if "id" not in d1:
        err = d1.get("error", {})
        if err.get("code") == 190:
            _clear_instagram_connection()
            raise HTTPException(status_code=401, detail="Your Instagram connection has expired. Please reconnect in My Profile.")
        raise HTTPException(status_code=400, detail=err.get("message", "Failed to create Instagram post"))
    container_id = d1["id"]

    if not _wait_for_ig_container_ready(container_id, page_token):
        raise HTTPException(status_code=400, detail="Instagram took too long to process the image. Please try again in a moment.")

    r2 = requests.post(f"https://graph.facebook.com/v25.0/{ig_user_id}/media_publish", data={
        "creation_id": container_id, "access_token": page_token,
    }, timeout=15)
    d2 = r2.json()
    if "id" not in d2:
        err = d2.get("error", {})
        if err.get("code") == 190:
            _clear_instagram_connection()
            raise HTTPException(status_code=401, detail="Your Instagram connection has expired. Please reconnect in My Profile.")
        raise HTTPException(status_code=400, detail=err.get("message", "Failed to publish to Instagram"))

    return {"success": True, "post_id": d2["id"]}

# ================================
# PROFILE ROUTE
# ================================
@app.put("/api/profile")
def update_profile(req: ProfileUpdate, agent=Depends(get_current_agent)):
    if req.notification_channel not in ("telegram", "whatsapp", "both"):
        raise HTTPException(status_code=400, detail="Invalid notification channel")
    get_db().table("agents").update(req.dict()).eq("id", agent["id"]).execute()
    result = get_db().table("agents").select("*").eq("id", agent["id"]).execute()
    agent = result.data[0]
    return _agent_response(agent)

@app.get("/api/profile")
def get_profile(agent=Depends(get_current_agent)):
    return _agent_response(agent)

@app.post("/api/profile/change-email")
def change_email(req: EmailChangeRequest, agent=Depends(get_current_agent)):
    if not verify_password(req.current_password, agent["password_hash"]):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    new_email = req.new_email.strip().lower()
    if "@" not in new_email or "." not in new_email.split("@")[-1]:
        raise HTTPException(status_code=400, detail="Enter a valid email address")
    if new_email == agent["email"]:
        raise HTTPException(status_code=400, detail="That's already your current email")
    existing = get_db().table("agents").select("id").eq("email", new_email).execute()
    if existing.data:
        raise HTTPException(status_code=400, detail="That email is already in use")
    get_db().table("agents").update({"email": new_email}).eq("id", agent["id"]).execute()
    result = get_db().table("agents").select("*").eq("id", agent["id"]).execute()
    return _agent_response(result.data[0])

@app.post("/api/profile/photo")
def upload_profile_photo(req: ProfilePhotoRequest, agent=Depends(get_current_agent)):
    try:
        img_bytes = base64.b64decode(req.image_data)
        pil_img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
        pil_img.thumbnail((800, 800))

        buffer = io.BytesIO()
        pil_img.save(buffer, format="JPEG", quality=85)
        buffer.seek(0)
        compressed = buffer.read()

        supabase = get_db()
        filename = f"agents/{agent['id']}.jpg"
        supabase.storage.from_("listings-images").upload(
            filename,
            compressed,
            {"content-type": "image/jpeg", "upsert": "true"}
        )
        photo_url = f"{supabase.storage.from_('listings-images').get_public_url(filename)}?v={uuid.uuid4().hex[:8]}"

        supabase.table("agents").update({"photo_url": photo_url}).eq("id", agent["id"]).execute()

        return {"photo_url": photo_url}
    except Exception:
        logger.exception("Profile photo upload failed for agent %s", agent["id"])
        raise HTTPException(status_code=500, detail="Something went wrong uploading that photo -- please try again")

@app.get("/api/profile/telegram-connect-link")
def get_telegram_connect_link(agent=Depends(get_current_agent)):
    bot_username = os.environ.get("TELEGRAM_BOT_USERNAME", "")
    if not bot_username:
        raise HTTPException(status_code=503, detail="Telegram connect is not configured")
    return {"link": f"https://t.me/{bot_username}?start={agent['id']}"}

@app.get("/api/profile/instagram-connect-link")
def get_instagram_connect_link(agent=Depends(get_current_agent)):
    if not _can_use_instagram_beta(agent):
        raise HTTPException(status_code=403, detail="Instagram connect is not yet available on your account")
    app_id = os.environ.get("FB_APP_ID", "")
    if not app_id:
        raise HTTPException(status_code=503, detail="Instagram connect is not configured")
    scope = "instagram_basic,instagram_content_publish,pages_show_list,pages_read_engagement,business_management"
    link = (
        "https://www.facebook.com/v25.0/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={INSTAGRAM_OAUTH_REDIRECT_URI}"
        f"&state={agent['id']}"
        f"&scope={scope}"
        "&response_type=code"
    )
    return {"link": link}

@app.post("/api/instagram/oauth-callback")
def instagram_oauth_callback(req: InstagramOAuthCallbackRequest):
    if not _is_valid_uuid(req.state):
        raise HTTPException(status_code=400, detail="Invalid connect request")

    agent_result = get_db().table("agents").select("id, email").eq("id", req.state).execute()
    if not agent_result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = agent_result.data[0]
    if not _can_use_instagram_beta(agent):
        raise HTTPException(status_code=403, detail="Instagram connect is not yet available on your account")

    app_id = os.environ.get("FB_APP_ID", "")
    app_secret = os.environ.get("FB_APP_SECRET", "")

    r1 = requests.get("https://graph.facebook.com/v25.0/oauth/access_token", params={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": INSTAGRAM_OAUTH_REDIRECT_URI, "code": req.code,
    }, timeout=15)
    d1 = r1.json()
    if "access_token" not in d1:
        raise HTTPException(status_code=400, detail=f"Instagram connect failed: {d1.get('error', {}).get('message', 'unknown error')}")
    short_lived_token = d1["access_token"]

    r2 = requests.get("https://graph.facebook.com/v25.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_lived_token,
    }, timeout=15)
    d2 = r2.json()
    if "access_token" not in d2:
        raise HTTPException(status_code=400, detail=f"Instagram connect failed: {d2.get('error', {}).get('message', 'unknown error')}")
    long_lived_user_token = d2["access_token"]

    r3 = requests.get("https://graph.facebook.com/v25.0/me/accounts", params={"access_token": long_lived_user_token}, timeout=15)
    d3 = r3.json()
    if not d3.get("data"):
        raise HTTPException(status_code=400, detail="No Facebook Page found for this account. Instagram connect requires a Facebook Page linked to your Instagram professional account.")
    page = d3["data"][0]
    page_id, page_name, page_token = page["id"], page.get("name"), page["access_token"]

    r4 = requests.get(f"https://graph.facebook.com/v25.0/{page_id}", params={
        "fields": "instagram_business_account", "access_token": page_token,
    }, timeout=15)
    d4 = r4.json()
    ig_account = d4.get("instagram_business_account")
    if not ig_account:
        raise HTTPException(status_code=400, detail="Your Facebook Page isn't linked to an Instagram professional account yet. Link it in the Instagram app under Settings > Account, then try again.")
    ig_user_id = ig_account["id"]

    r5 = requests.get(f"https://graph.facebook.com/v25.0/{ig_user_id}", params={
        "fields": "username", "access_token": page_token,
    }, timeout=15)
    ig_username = r5.json().get("username", "")

    get_db().table("agents").update({
        "fb_user_access_token": long_lived_user_token,
        "fb_page_id": page_id,
        "fb_page_access_token": page_token,
        "fb_page_name": page_name,
        "instagram_business_account_id": ig_user_id,
        "instagram_username": ig_username,
        "instagram_connected_at": datetime.utcnow().isoformat(),
    }).eq("id", agent["id"]).execute()

    return {"success": True, "instagram_username": ig_username, "page_name": page_name}

@app.get("/api/profile/facebook-connect-link")
def get_facebook_connect_link(agent=Depends(get_current_agent)):
    if not _can_use_facebook_beta(agent):
        raise HTTPException(status_code=403, detail="Facebook connect is not yet available on your account")
    app_id = os.environ.get("FB_APP_ID", "")
    if not app_id:
        raise HTTPException(status_code=503, detail="Facebook connect is not configured")
    # instagram_basic/instagram_content_publish are included as a bonus -- if the connected
    # Page also has a linked Instagram professional account, this one connect action enables
    # Instagram posting too, same as connecting Instagram directly would.
    scope = "pages_show_list,pages_read_engagement,pages_manage_posts,business_management,instagram_basic,instagram_content_publish"
    link = (
        "https://www.facebook.com/v25.0/dialog/oauth"
        f"?client_id={app_id}"
        f"&redirect_uri={FACEBOOK_OAUTH_REDIRECT_URI}"
        f"&state={agent['id']}"
        f"&scope={scope}"
        "&response_type=code"
    )
    return {"link": link}

@app.post("/api/facebook/oauth-callback")
def facebook_oauth_callback(req: FacebookOAuthCallbackRequest):
    if not _is_valid_uuid(req.state):
        raise HTTPException(status_code=400, detail="Invalid connect request")

    agent_result = get_db().table("agents").select("id, email").eq("id", req.state).execute()
    if not agent_result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = agent_result.data[0]
    if not _can_use_facebook_beta(agent):
        raise HTTPException(status_code=403, detail="Facebook connect is not yet available on your account")

    app_id = os.environ.get("FB_APP_ID", "")
    app_secret = os.environ.get("FB_APP_SECRET", "")

    r1 = requests.get("https://graph.facebook.com/v25.0/oauth/access_token", params={
        "client_id": app_id, "client_secret": app_secret,
        "redirect_uri": FACEBOOK_OAUTH_REDIRECT_URI, "code": req.code,
    }, timeout=15)
    d1 = r1.json()
    if "access_token" not in d1:
        raise HTTPException(status_code=400, detail=f"Facebook connect failed: {d1.get('error', {}).get('message', 'unknown error')}")
    short_lived_token = d1["access_token"]

    r2 = requests.get("https://graph.facebook.com/v25.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_lived_token,
    }, timeout=15)
    d2 = r2.json()
    if "access_token" not in d2:
        raise HTTPException(status_code=400, detail=f"Facebook connect failed: {d2.get('error', {}).get('message', 'unknown error')}")
    long_lived_user_token = d2["access_token"]

    r3 = requests.get("https://graph.facebook.com/v25.0/me/accounts", params={"access_token": long_lived_user_token}, timeout=15)
    d3 = r3.json()
    if not d3.get("data"):
        raise HTTPException(status_code=400, detail="No Facebook Page found for this account. Facebook posting requires a Facebook Page you manage.")
    page = d3["data"][0]
    page_id, page_name, page_token = page["id"], page.get("name"), page["access_token"]

    update_payload = {
        "fb_user_access_token": long_lived_user_token,
        "fb_page_id": page_id,
        "fb_page_access_token": page_token,
        "fb_page_name": page_name,
    }

    # Bonus: if this Page also has a linked Instagram professional account, pick it up too --
    # it's the same underlying connection, no reason to make the agent connect Instagram
    # separately if this already covers it.
    r4 = requests.get(f"https://graph.facebook.com/v25.0/{page_id}", params={
        "fields": "instagram_business_account", "access_token": page_token,
    }, timeout=15)
    ig_account = r4.json().get("instagram_business_account")
    if ig_account:
        ig_user_id = ig_account["id"]
        r5 = requests.get(f"https://graph.facebook.com/v25.0/{ig_user_id}", params={
            "fields": "username", "access_token": page_token,
        }, timeout=15)
        update_payload["instagram_business_account_id"] = ig_user_id
        update_payload["instagram_username"] = r5.json().get("username", "")
        update_payload["instagram_connected_at"] = datetime.utcnow().isoformat()

    get_db().table("agents").update(update_payload).eq("id", agent["id"]).execute()

    return {"success": True, "page_name": page_name}

@app.get("/api/profile/linkedin-connect-link")
def get_linkedin_connect_link(agent=Depends(get_current_agent)):
    if not _can_use_linkedin_beta(agent):
        raise HTTPException(status_code=403, detail="LinkedIn connect is not yet available on your account")
    client_id = os.environ.get("LINKEDIN_CLIENT_ID", "")
    if not client_id:
        raise HTTPException(status_code=503, detail="LinkedIn connect is not configured")
    scope = "openid%20profile%20w_member_social"
    link = (
        "https://www.linkedin.com/oauth/v2/authorization"
        "?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={LINKEDIN_OAUTH_REDIRECT_URI}"
        f"&state={agent['id']}"
        f"&scope={scope}"
    )
    return {"link": link}

@app.post("/api/linkedin/oauth-callback")
def linkedin_oauth_callback(req: LinkedInOAuthCallbackRequest):
    if not _is_valid_uuid(req.state):
        raise HTTPException(status_code=400, detail="Invalid connect request")

    agent_result = get_db().table("agents").select("id, email").eq("id", req.state).execute()
    if not agent_result.data:
        raise HTTPException(status_code=404, detail="Agent not found")
    agent = agent_result.data[0]
    if not _can_use_linkedin_beta(agent):
        raise HTTPException(status_code=403, detail="LinkedIn connect is not yet available on your account")

    client_id = os.environ.get("LINKEDIN_CLIENT_ID", "")
    client_secret = os.environ.get("LINKEDIN_CLIENT_SECRET", "")

    r1 = requests.post(
        "https://www.linkedin.com/oauth/v2/accessToken",
        data={
            "grant_type": "authorization_code", "code": req.code,
            "client_id": client_id, "client_secret": client_secret,
            "redirect_uri": LINKEDIN_OAUTH_REDIRECT_URI,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"}, timeout=15,
    )
    d1 = r1.json()
    if "access_token" not in d1:
        raise HTTPException(status_code=400, detail=f"LinkedIn connect failed: {d1.get('error_description', d1.get('error', 'unknown error'))}")
    access_token = d1["access_token"]
    refresh_token = d1.get("refresh_token")

    r2 = requests.get("https://api.linkedin.com/v2/userinfo", headers={"Authorization": f"Bearer {access_token}"}, timeout=15)
    d2 = r2.json()
    person_sub = d2.get("sub")
    if not person_sub:
        raise HTTPException(status_code=400, detail="LinkedIn connect failed: could not read member profile")
    person_urn = f"urn:li:person:{person_sub}"
    member_name = d2.get("name", "")

    get_db().table("agents").update({
        "linkedin_access_token": access_token,
        "linkedin_refresh_token": refresh_token,
        "linkedin_person_urn": person_urn,
        "linkedin_name": member_name,
        "linkedin_connected_at": datetime.utcnow().isoformat(),
    }).eq("id", agent["id"]).execute()

    return {"success": True, "name": member_name}

@app.get("/api/health")
def health():
    # photo_writes_atomic false means the compare-and-swap guard is not active
    # on this worker and concurrent photo changes can clobber each other -- the
    # rollout gate should check this. Deliberately a plain boolean: it says
    # whether the guard works, not anything about the schema.
    return {
        "status": "ok",
        "service": "NestList Prestige API",
        "photo_writes_atomic": bool(_IMAGES_CAS_ENCODING),
    }

@app.get("/api/telegram/debug-webhook")
async def debug_telegram_webhook():
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        return {"error": "TELEGRAM_BOT_TOKEN not set"}
    async with httpx.AsyncClient() as client:
        response = await client.get(f"https://api.telegram.org/bot{bot_token}/getWebhookInfo", timeout=10)
        return response.json()

@app.get("/api/instagram/debug-account")
def debug_instagram_account():
    fb_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
    fb_page_id = os.environ.get("FB_PAGE_ID", "")
    if not fb_token or not fb_page_id:
        return {"error": "FB_PAGE_ACCESS_TOKEN or FB_PAGE_ID not set"}
    response = requests.get(
        f"https://graph.facebook.com/v25.0/{fb_page_id}",
        params={"fields": "instagram_business_account", "access_token": fb_token}, timeout=15)
    return response.json()

@app.post("/api/extract-listing-image")
async def extract_listing_image(request: Request):
    try:
        body = await request.json()
        images = body.get("images", [])
        if not images:
            image_data = body.get("image_data")
            media_type = body.get("media_type", "image/jpeg")
            if image_data:
                images = [{"image_data": image_data, "media_type": media_type}]

        if not images:
            raise HTTPException(status_code=400, detail="No images provided")

        content = []
        for img in images[:5]:
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": img.get("media_type", "image/jpeg"),
                    "data": img.get("image_data")
                }
            })

        content.append({
            "type": "text",
            "text": """Extract property listing details from these images and return ONLY a JSON object with these exact fields:
{
  "property_type": "one of: Good Class Bungalow (GCB), Detached/Bungalow, Semi-Detached, Inter-Terrace, Corner Terrace, Penthouse",
  "location": "full address or area",
  "land_size": number in sqft or 0,
  "built_up": number in sqft or 0,
  "bedrooms": "number of bedrooms only, e.g. 4",
  "bathrooms": "number of bathrooms only, e.g. 3",
  "price": "e.g. 25,000,000",
  "features": "special features as comma separated text",
  "plot_width": number in metres or 0,
  "plot_depth": number in metres or 0,
  "storeys": number or 0,
  "site_coverage": number as percentage or 0
}
Do not guess or estimate any value that is not clearly shown or stated in the images. If a field cannot be determined from the images, use "" for text fields and 0 for number fields.
For property_type specifically: if any image contains an explicit official/source "Property Type" field or code, use that over your own inference from the general description — e.g. "CT" means Corner Terrace, "IT" or "ITR" means Inter-Terrace, "SD" means Semi-Detached, "DB" means Detached/Bungalow, "GCB" means Good Class Bungalow (GCB), "PH" means Penthouse. Inter-Terrace and Corner Terrace are frequently confused — an explicit source field always wins over inferring from land size or description text. If no image states or clearly implies a specific property type at all (no field, code, or explicit description), return "" for property_type rather than guessing from land size, price, or general impression — Corner Terrace, Inter-Terrace, and Semi-Detached can have very similar land sizes and are not reliably distinguishable from size alone, so leaving this blank for the agent to confirm is strongly preferred over a wrong guess.
For land_size and built_up specifically: these must be the raw size figure shown directly in or immediately after the "Land Size" / "Built-Up Size" (or "Estm. Land Size" / "Estm. Build-Up Size") field itself, typically followed by a unit like "sqft". Many source screenshots also show a separate small badge or chip positioned near that same field labeled "PSF" (price per square foot, e.g. "3,157 PSF") — this PSF number is a completely different figure and must NEVER be used as land_size or built_up, even when it sits close to, overlapping, or immediately beside the size number. For example, if a screenshot shows "Estm. Land Size (SQFT): 3003" next to a badge reading "3,157 PSF", land_size is 3003, not 3157 — read the digits under the size label, not the digits under or next to the PSF badge. Before finalizing, sanity-check that price divided by land_size (or built_up) is a plausible PSF for the property type and location shown — if it looks off by roughly an order of magnitude, or if land_size/built_up looks suspiciously close to a PSF figure visible elsewhere in the image, re-read the image and correct it rather than outputting the PSF value by mistake.
Return only valid JSON, nothing else."""
        })

        message = await create_claude_message(
            model="claude-sonnet-4-5",
            max_tokens=1000,
            messages=[{"role": "user", "content": content}]
        )

        text = message.content[0].text.strip()
        clean = text.replace("```json", "").replace("```", "").strip()
        extracted = json.loads(clean)
        return extracted

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# ================================
# MARKET PULSE ROUTES
# ================================
@app.get("/api/market-pulse")
def get_market_pulse():
    result = get_db().table("market_pulse").select("*").eq("id", 1).execute()
    if result.data:
        return result.data[0]
    return {
        "gcb_transactions": "~36 units",
        "gcb_total_value": "SGD 1.36B",
        "gcb_avg_psf": "SGD 2,134",
        "gcb_largest": "SGD 148M",
        "nassim_range": "SGD 2,500-4,000 psf",
        "last_updated": "Jan 2026",
        "source": "manual"
    }

@app.put("/api/market-pulse")
async def update_market_pulse(request: Request, agent=Depends(get_current_agent)):
    if agent["email"] != "leesbjane@gmail.com":
        raise HTTPException(status_code=403, detail="Not authorised")
    body = await request.json()
    body["last_updated"] = date.today().strftime("%b %Y")
    body["source"] = "manual"
    get_db().table("market_pulse").upsert({"id": 1, **body}).execute()
    return {"success": True}

@app.post("/api/market-pulse/refresh")
async def trigger_market_pulse_refresh(agent=Depends(get_current_agent)):
    if agent["email"] != "leesbjane@gmail.com":
        raise HTTPException(status_code=403, detail="Not authorised")
    if not os.environ.get("URA_ACCESS_KEY", ""):
        raise HTTPException(status_code=400, detail="URA_ACCESS_KEY is not set in Railway yet")
    stats = await ura_market_pulse.refresh_market_pulse()
    if not stats:
        raise HTTPException(status_code=502, detail="No qualifying GCB transactions found in the past 12 months, or the URA request failed")
    get_db().table("market_pulse").upsert({"id": 1, **stats}).execute()
    return stats

@app.post("/api/cma/generate")
async def generate_cma_report(req: CMARequest, agent=Depends(get_current_agent)):
    if not req.street:
        raise HTTPException(status_code=400, detail="Street or area name is required")
    if not os.environ.get("URA_ACCESS_KEY", ""):
        raise HTTPException(status_code=503, detail="URA market data isn't configured yet -- contact NestList support")
    try:
        result = await ura_market_pulse.generate_cma(req.street, req.property_type, req.land_size, req.window_months)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't reach URA's data service right now -- please try again shortly")
    return result

@app.post("/api/facebook/exchange-long-lived-token")
def exchange_long_lived_token(req: TokenExchangeRequest, agent=Depends(get_current_agent)):
    if agent["email"] != "leesbjane@gmail.com":
        raise HTTPException(status_code=403, detail="Not authorised")

    app_id = os.environ.get("FB_APP_ID", "")
    app_secret = os.environ.get("FB_APP_SECRET", "")
    fb_page_id = os.environ.get("FB_PAGE_ID", "")
    if not app_id or not app_secret:
        raise HTTPException(status_code=503, detail="FB_APP_ID or FB_APP_SECRET not configured in Railway")

    exchange_response = requests.get(
        "https://graph.facebook.com/v25.0/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": req.user_token
        }, timeout=15)
    exchange_data = exchange_response.json()
    if "access_token" not in exchange_data:
        raise HTTPException(status_code=400, detail=f"User token exchange failed: {exchange_data.get('error', {}).get('message', 'unknown error')}")
    long_lived_user_token = exchange_data["access_token"]

    accounts_response = requests.get(
        "https://graph.facebook.com/v25.0/me/accounts",
        params={"access_token": long_lived_user_token}, timeout=15)
    accounts_data = accounts_response.json()
    if "data" not in accounts_data:
        raise HTTPException(status_code=400, detail=f"Could not fetch Pages: {accounts_data.get('error', {}).get('message', 'unknown error')}")

    page_entry = next((p for p in accounts_data["data"] if p.get("id") == fb_page_id), None)
    if not page_entry:
        raise HTTPException(status_code=404, detail="NestList Page not found in returned accounts — check FB_PAGE_ID or that this account still has Page access")

    return {
        "long_lived_page_access_token": page_entry["access_token"],
        "page_name": page_entry.get("name"),
        "instructions": "Copy the long_lived_page_access_token value above into Railway as FB_PAGE_ACCESS_TOKEN, replacing the current short-lived one."
    }

# ================================
# PUBLIC ROUTES (no auth — buyer-facing)
# ================================
_public_enquiry_hits = {}
_password_reset_hits = {}

def _rate_limited(store: dict, key: str, limit: int = 5, window_seconds: int = 3600) -> bool:
    now = datetime.utcnow()
    hits = [t for t in store.get(key, []) if (now - t).total_seconds() < window_seconds]
    hits.append(now)
    store[key] = hits
    return len(hits) > limit

def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False


def _whatsapp_link_for(phone: str) -> str:
    """Buyer-to-agent contact is always direct -- a wa.me link opens the agent's own
    WhatsApp app, no NestList number or API involved. Singapore mobile numbers are
    8 digits; wa.me needs the country code, so add 65 when it looks like a bare
    local number."""
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    if len(digits) == 8:
        digits = "65" + digits
    return f"https://wa.me/{digits}"

@app.get("/api/public/listings/{listing_id}")
def get_public_listing(listing_id: str):
    if not _is_valid_uuid(listing_id):
        raise HTTPException(status_code=404, detail="Listing not found")
    result = get_db().table("listings").select("*").eq("id", listing_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = result.data[0]
    agent_result = get_db().table("agents").select("name, agency, specialty").eq("id", listing["agent_id"]).execute()
    agent_info = agent_result.data[0] if agent_result.data else {}
    return {
        "id": listing["id"],
        "property_type": listing["property_type"],
        "location": listing["location"],
        # Buyer-facing surface: the public page should render this, not `location`.
        # `location` is kept in the payload so nothing that already reads it breaks.
        "display_location": _strip_house_number(listing.get("location")),
        "price": listing["price"],
        "content": apply_listing_copy_guards(
            listing.get("content"),
            price=listing.get("price"),
            built_up=listing.get("built_up"),
            context=f"public:{listing['id']}",
        ),
        "images": listing.get("images") or [],
        "bedrooms": listing.get("bedrooms"),
        "land_size": listing.get("land_size"),
        "built_up": listing.get("built_up"),
        "features": listing.get("features"),
        "agent": {"name": agent_info.get("name"), "agency": agent_info.get("agency")}
    }

@app.post("/api/public/enquiries")
async def create_public_enquiry(req: PublicEnquiryRequest, request: Request):
    if req.website:
        return {"success": True}

    client_ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    if _rate_limited(_public_enquiry_hits, client_ip):
        raise HTTPException(status_code=429, detail="Too many enquiries — please try again later")

    if not _is_valid_uuid(req.listing_id):
        raise HTTPException(status_code=404, detail="Listing not found")

    listing_result = get_db().table("listings").select("id, agent_id, location, property_type, price").eq("id", req.listing_id).execute()
    if not listing_result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = listing_result.data[0]

    message = req.message.strip()[:2000]
    lead_score, ai_summary = "Warm", ""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key and message:
        try:
            prompt = f"""A prospective buyer submitted this enquiry for a Singapore property listing ({listing['property_type']} in {listing['location']}, asking SGD {listing['price']}):

"{message}"

Classify buyer intent and return ONLY a JSON object:
{{
  "lead_score": "Hot" or "Warm" or "Cold",
  "ai_summary": "one sentence, max 20 words, plain English summary of buyer intent, budget signal, and timeline if mentioned"
}}
Hot = clear budget/timeline mentioned, ready to view/buy soon. Warm = genuine interest, some detail, no urgency stated. Cold = vague, generic, or likely not a real buyer.
Return only valid JSON, nothing else."""
            ai_response = await create_claude_message(
                model="claude-sonnet-4-5",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            text = ai_response.content[0].text.strip().replace("```json", "").replace("```", "").strip()
            parsed = json.loads(text)
            lead_score = parsed.get("lead_score", "Warm")
            ai_summary = parsed.get("ai_summary", "")
        except Exception:
            pass

    saved = get_db().table("enquiries").insert({
        "agent_id": listing["agent_id"],
        "client_name": req.client_name,
        "phone": req.phone,
        "email": req.email,
        "client_type": "Buyer",
        "property_interest": f"{listing['property_type']} — {listing['location']}",
        "notes": message,
        "status": "Active",
        "source": "Public Listing Page",
        "listing_id": req.listing_id,
        "message": message,
        "lead_score": lead_score,
        "ai_summary": ai_summary,
    }).execute()

    agent_notif_result = get_db().table("agents").select(
        "telegram_chat_id, notification_channel, whatsapp_number"
    ).eq("id", listing["agent_id"]).execute()
    agent_notif = agent_notif_result.data[0] if agent_notif_result.data else {}
    agent_chat_id = agent_notif.get("telegram_chat_id")
    notification_channel = agent_notif.get("notification_channel") or "telegram"
    agent_whatsapp_number = agent_notif.get("whatsapp_number") or ""

    score_emoji = {"Hot": "🔥", "Warm": "🌤️", "Cold": "❄️"}.get(lead_score, "")
    whatsapp_link = _whatsapp_link_for(req.phone)
    phone_line = f'Phone: <a href="{whatsapp_link}">{req.phone}</a> 💬' if whatsapp_link else "Phone: not provided"
    alert_message = (
        f"{score_emoji} <b>New Lead: {lead_score}</b>\n\n"
        f"<b>{req.client_name}</b>\n"
        f"Listing: {listing['property_type']} — {listing['location']}\n"
        f"{phone_line}\n"
        f"Email: {req.email or 'not provided'}\n"
        f"Summary: {ai_summary or message[:200]}"
    )
    if notification_channel in ("telegram", "both"):
        await send_telegram_alert(alert_message, chat_id=agent_chat_id)
    if notification_channel in ("whatsapp", "both") and agent_whatsapp_number:
        await send_whatsapp_alert(agent_whatsapp_number, alert_message)

    return {"success": True, "id": saved.data[0]["id"]}

@app.post("/api/telegram/webhook")
async def telegram_webhook(request: Request):
    try:
        body = await request.json()
        message = body.get("message", {})
        text = message.get("text", "")
        chat_id = message.get("chat", {}).get("id")
        if text.startswith("/start ") and chat_id:
            payload = text[len("/start "):].strip()
            if _is_valid_uuid(payload):
                existing = get_db().table("agents").select("telegram_chat_id").eq("id", payload).execute()
                old_chat_id = existing.data[0].get("telegram_chat_id") if existing.data else None
                get_db().table("agents").update({"telegram_chat_id": chat_id}).eq("id", payload).execute()
                if old_chat_id and old_chat_id != chat_id:
                    await send_telegram_alert(
                        "⚠️ <b>Telegram connection replaced</b>\n\nYour NestList lead alerts were just redirected to a different Telegram chat. If this wasn't you, contact support immediately.",
                        chat_id=old_chat_id
                    )
                await send_telegram_alert(
                    "✅ <b>Connected!</b>\n\nYou'll now receive new lead alerts here.",
                    chat_id=chat_id
                )
    except Exception:
        pass
    return {"ok": True}

# ================================
# ENQUIRIES ROUTES
# ================================
@app.get("/api/enquiries")
def get_enquiries(agent=Depends(get_current_agent)):
    result = get_db().table("enquiries").select("*").eq("agent_id", agent["id"]).order("created_at", desc=True).execute()
    return result.data or []

@app.post("/api/enquiries")
async def create_enquiry(request: Request, agent=Depends(get_current_agent)):
    body = await request.json()
    body["agent_id"] = agent["id"]
    result = get_db().table("enquiries").insert(body).execute()
    return result.data[0]

@app.put("/api/enquiries/{enquiry_id}")
async def update_enquiry(enquiry_id: str, request: Request, agent=Depends(get_current_agent)):
    body = await request.json()
    result = get_db().table("enquiries").update(body).eq("id", enquiry_id).eq("agent_id", agent["id"]).execute()
    return result.data[0]

@app.delete("/api/enquiries/{enquiry_id}")
def delete_enquiry(enquiry_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("enquiries").delete().eq("id", enquiry_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Client record not found")
    return {"success": True}

@app.delete("/api/listings/{listing_id}")
def delete_listing(listing_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("listings").update({"status": "archived"}) \
        .eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    return {"success": True}

@app.post("/api/listings/{listing_id}/restore")
def restore_listing(listing_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("listings").update({"status": "active"}) \
        .eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    return {"success": True}

@app.delete("/api/listings/{listing_id}/permanent")
def delete_listing_permanently(listing_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("id, status").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    if result.data[0].get("status") != "archived":
        raise HTTPException(status_code=400, detail="Archive this listing from My Listings first before removing it permanently.")

    supabase = get_db()
    bucket = supabase.storage.from_("listings-images")
    try:
        files = bucket.list(listing_id)
        if files:
            bucket.remove([f"{listing_id}/{f['name']}" for f in files])
    except Exception:
        pass
    try:
        bucket.remove([f"posters/{listing_id}.jpg"])
    except Exception:
        pass

    get_db().table("listings").delete().eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    return {"success": True}

@app.patch("/api/listings/{listing_id}")
def update_listing(listing_id: str, req: ListingRequest, agent=Depends(get_current_agent)):
    existing = get_db().table("listings").select("id").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    updated = get_db().table("listings").update({
        # Saving here always promotes the row to "active" -- this is the same
        # endpoint used both for editing an existing active listing (already
        # active, so this is a no-op) and for finishing a seller lead's
        # "Convert to Listing" flow (status was "lead", now graduates to
        # "active" the moment the agent fills in the rest and saves).
        "status": "active",
        "property_type": req.property_type,
        "location": req.location,
        "land_size": req.land_size,
        "built_up": req.built_up,
        "bedrooms": req.bedrooms,
        "bathrooms": req.bathrooms,
        "price": req.price,
        "features": req.features,
        "plot_width": req.plot_width,
        "plot_depth": req.plot_depth,
        "storeys": req.storeys,
        "site_coverage": req.site_coverage,
    }).eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    return updated.data[0]

MAX_LISTING_CONTENT_CHARS = 20000

@app.patch("/api/listings/{listing_id}/content")
def update_listing_content(listing_id: str, req: ListingContentRequest, agent=Depends(get_current_agent)):
    # Lets an agent hand-edit the AI-generated write-up. Deliberately touches
    # ONLY the content column -- unlike PATCH /api/listings/{id}, this must not
    # promote a "lead" row to "active" just because someone polished the copy.
    content = (req.content or "").strip()
    if not content:
        raise HTTPException(status_code=400, detail="Write-up cannot be empty")
    if len(content) > MAX_LISTING_CONTENT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Write-up is too long (max {MAX_LISTING_CONTENT_CHARS} characters)"
        )

    existing = get_db().table("listings").select("id").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Listing not found")

    updated = get_db().table("listings").update({"content": content}) \
        .eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not updated.data:
        raise HTTPException(status_code=404, detail="Listing not found")

    return {"success": True, "content": updated.data[0].get("content", content)}


# Selective rewrite: the agent highlights a phrase or paragraph in the write-up
# and asks for just that bit to be redone. Deliberately READ-ONLY on the listing
# -- it returns the replacement and nothing else. The frontend swaps the span in
# the editor and saves through PATCH /api/listings/{id}/content as usual, so a
# rewrite the agent doesn't like costs them nothing and there is no window where
# a half-applied rewrite is sitting in the database.
MAX_SELECTION_CHARS = 3000

# Each call is a Claude request, so this is rate-limited like the assistant. The
# ceiling is higher because rewriting is iterative by nature -- an agent polishing
# one write-up may reasonably try a dozen spans, and re-roll a few of them.
_rewrite_selection_hits = {}


def _normalise_for_match(text: str) -> str:
    """Whitespace-only normalisation used as a fallback when the exact span isn't
    found. Browsers hand back non-breaking spaces and \\r\\n line endings that the
    stored copy doesn't have; those are not a real mismatch and shouldn't cost the
    agent a confusing error."""
    return re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()


@app.post("/api/listings/{listing_id}/rewrite-selection")
async def rewrite_listing_selection(listing_id: str, req: RewriteSelectionRequest, agent=Depends(get_current_agent)):
    if _rate_limited(_rewrite_selection_hits, agent["id"], limit=60, window_seconds=3600):
        raise HTTPException(
            status_code=429,
            detail="You've hit the hourly limit for rewrites -- please try again in a bit."
        )

    selected = (req.selected_text or "").strip()
    instruction = (req.instruction or "").strip()
    if not selected:
        raise HTTPException(status_code=400, detail="Select some text to rewrite first")
    if len(selected) > MAX_SELECTION_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"That selection is too long to rewrite (max {MAX_SELECTION_CHARS} characters) -- try one paragraph at a time"
        )
    if len(instruction) > 500:
        raise HTTPException(status_code=400, detail="Instruction is too long (max 500 characters)")

    current_text = (req.current_text or "").strip()
    if len(current_text) > MAX_LISTING_CONTENT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"Write-up is too long (max {MAX_LISTING_CONTENT_CHARS} characters)"
        )

    if not _is_valid_uuid(listing_id):
        raise HTTPException(status_code=404, detail="Listing not found")

    result = get_db().table("listings").select("content, price, built_up") \
        .eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")

    listing = result.data[0]
    # The selection is validated against whatever the agent is actually looking
    # at. Rewrites are applied in the editor and only saved later, so from the
    # second rewrite onward the editor has diverged from the saved copy -- and
    # checking the saved copy 409s on every rewrite after the first. The row is
    # still fetched above: it is what proves ownership, and it carries the price
    # and built-up the copy guards need.
    #
    # current_text is the agent's own unsaved draft, so it is trusted as context
    # but never written anywhere -- this endpoint still saves nothing.
    full_text = current_text or (listing.get("content") or "").strip()
    if not full_text:
        raise HTTPException(status_code=409, detail="This listing has no write-up to rewrite yet")

    # The frontend sends the exact highlighted string, so an exact hit is the
    # normal path. The normalised retry only rescues whitespace differences; a
    # genuinely stale selection (the text really isn't in the editor any more)
    # still gets a 409 rather than a rewrite of something that isn't there.
    if selected not in full_text and _normalise_for_match(selected) not in _normalise_for_match(full_text):
        raise HTTPException(
            status_code=409,
            detail="That selection is no longer in the write-up -- it may have been edited since. Reload the listing and select the text again."
        )

    length_rule = (
        "Match the tone and roughly the length of the original span"
        if not instruction else
        "Follow the agent's instruction above; otherwise keep the tone and roughly the length of the original span"
    )
    instruction_block = f"\nThe agent's instruction for this rewrite: \"{instruction}\"\n" if instruction else ""

    prompt = f"""Here is the full property write-up an agent is editing, for context only:

<write_up>
{full_text}
</write_up>

The agent has highlighted this exact span inside it and wants only this span rewritten:

<selected_span>
{selected}
</selected_span>
{instruction_block}
Write a replacement for ONLY the highlighted span. Rules, no exceptions:

1. Return the replacement text and nothing else -- no preamble, no explanation, no
   quotation marks around it, no markdown. It will be pasted straight back into the
   write-up in place of the highlighted span, so it must read correctly in that slot.
2. {length_rule}. Do not rewrite, extend, or summarise any other part of the write-up.
3. Never include a house or unit number, and never include a price in any form -- no
   figure, no range, no "attractively priced".
4. Plain, warm language. Avoid stock real-estate phrases -- "coveted", "established
   enclave", "prestigious address", "epitome of luxury", "nestled in", "boasts",
   "sprawling". If a plain word says it, use the plain word.
5. Every claim must be traceable to something already in the write-up above. No
   superlatives or status claims you can't point to -- describe what is concretely
   there instead."""

    try:
        response = await create_claude_message(
            model="claude-sonnet-4-5",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
            # The SDK default is 10 minutes, which is far too long for something the
            # agent is sat watching. Fail fast and let them retry instead.
            timeout=60.0,
        )
    except Exception as e:
        logger.error("rewrite-selection failed for agent %s: %s", agent["id"], e)
        raise HTTPException(status_code=502, detail="Rewrite is temporarily unavailable -- please try again in a moment.")

    rewritten = "".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    rewritten = rewritten.replace("**", "").replace("---", "").replace("# ", "").strip()
    # Models occasionally wrap a short span in quotes despite rule 1; strip them
    # only when they wrap the whole thing, never mid-sentence quotes.
    if len(rewritten) > 1 and rewritten[0] in "\"'“" and rewritten[-1] in "\"'”":
        rewritten = rewritten[1:-1].strip()

    if not rewritten:
        raise HTTPException(status_code=502, detail="Rewrite came back empty -- please try again.")

    rewritten = apply_listing_copy_guards(
        rewritten,
        price=listing.get("price"),
        built_up=listing.get("built_up"),
        context=f"rewrite:{agent['id']}",
    )

    return {"rewritten_text": rewritten}


@app.get("/api/listings/{listing_id}/download-images")
def download_listing_images(listing_id: str, agent=Depends(get_current_agent)):
    import zipfile
    from fastapi.responses import StreamingResponse

    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")

    listing = result.data[0]
    image_urls = listing.get("images") or []

    if not image_urls:
        raise HTTPException(status_code=404, detail="No images found for this listing")

    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
        for i, url in enumerate(image_urls):
            try:
                response = requests.get(url, timeout=10)
                if response.status_code == 200:
                    zip_file.writestr(f"property-photo-{i+1}.jpg", response.content)
            except Exception:
                continue

    zip_buffer.seek(0)

    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename=listing-photos-{listing_id[:8]}.zip"}
    )

# ================================
# BUYER MANAGEMENT
# ================================
_TEMP_ORDER = {"HOT": 0, "WARM": 1, "COLD": 2}

def _none_if_empty(value):
    return value if value not in ("", None) else None

@app.get("/api/buyers")
def get_buyers(agent=Depends(get_current_agent)):
    result = get_db().table("buyers").select("*").eq("agent_id", agent["id"]).order("created_at", desc=True).execute()
    buyers = result.data or []
    buyers.sort(key=lambda b: _TEMP_ORDER.get(b.get("temperature"), 99))

    buyer_ids = [b["id"] for b in buyers]
    if buyer_ids:
        props = get_db().table("buyer_properties").select("buyer_id, kind").in_("buyer_id", buyer_ids).execute().data or []
        counts = {}
        for p in props:
            bucket = counts.setdefault(p["buyer_id"], {"viewed_me": 0, "recommended": 0})
            if p["kind"] in bucket:
                bucket[p["kind"]] += 1
        for b in buyers:
            c = counts.get(b["id"], {"viewed_me": 0, "recommended": 0})
            b["viewed_count"] = c["viewed_me"]
            b["recommended_count"] = c["recommended"]

    return buyers

@app.post("/api/buyers")
def create_buyer(req: BuyerRequest, agent=Depends(get_current_agent)):
    payload = req.dict()
    payload["agent_id"] = agent["id"]
    payload["contact_date"] = _none_if_empty(payload["contact_date"])
    result = get_db().table("buyers").insert(payload).execute()
    return result.data[0]

@app.get("/api/buyers/{buyer_id}")
def get_buyer(buyer_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("buyers").select("*").eq("id", buyer_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Buyer not found")
    buyer = result.data[0]
    props_result = get_db().table("buyer_properties").select("*").eq("buyer_id", buyer_id).order("date", desc=True).execute()
    buyer["properties"] = props_result.data or []
    return buyer

@app.patch("/api/buyers/{buyer_id}")
def update_buyer(buyer_id: str, req: BuyerRequest, agent=Depends(get_current_agent)):
    existing = get_db().table("buyers").select("id").eq("id", buyer_id).eq("agent_id", agent["id"]).execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Buyer not found")
    payload = req.dict()
    payload["contact_date"] = _none_if_empty(payload["contact_date"])
    payload["updated_at"] = datetime.utcnow().isoformat()
    result = get_db().table("buyers").update(payload).eq("id", buyer_id).eq("agent_id", agent["id"]).execute()
    return result.data[0]

@app.delete("/api/buyers/{buyer_id}")
def delete_buyer(buyer_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("buyers").delete().eq("id", buyer_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Buyer not found")
    return {"success": True}

@app.post("/api/buyers/{buyer_id}/properties")
def add_buyer_property(buyer_id: str, req: BuyerPropertyRequest, agent=Depends(get_current_agent)):
    buyer_check = get_db().table("buyers").select("id").eq("id", buyer_id).eq("agent_id", agent["id"]).execute()
    if not buyer_check.data:
        raise HTTPException(status_code=404, detail="Buyer not found")
    payload = req.dict()
    payload["buyer_id"] = buyer_id
    payload["listing_id"] = _none_if_empty(payload["listing_id"])
    payload["date"] = _none_if_empty(payload["date"])
    result = get_db().table("buyer_properties").insert(payload).execute()
    return result.data[0]

@app.patch("/api/buyers/{buyer_id}/properties/{property_id}")
def update_buyer_property(buyer_id: str, property_id: str, req: BuyerPropertyRequest, agent=Depends(get_current_agent)):
    buyer_check = get_db().table("buyers").select("id").eq("id", buyer_id).eq("agent_id", agent["id"]).execute()
    if not buyer_check.data:
        raise HTTPException(status_code=404, detail="Buyer not found")
    payload = req.dict()
    payload["listing_id"] = _none_if_empty(payload["listing_id"])
    payload["date"] = _none_if_empty(payload["date"])
    result = get_db().table("buyer_properties").update(payload).eq("id", property_id).eq("buyer_id", buyer_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Property entry not found")
    return result.data[0]

@app.delete("/api/buyers/{buyer_id}/properties/{property_id}")
def delete_buyer_property(buyer_id: str, property_id: str, agent=Depends(get_current_agent)):
    buyer_check = get_db().table("buyers").select("id").eq("id", buyer_id).eq("agent_id", agent["id"]).execute()
    if not buyer_check.data:
        raise HTTPException(status_code=404, detail="Buyer not found")
    result = get_db().table("buyer_properties").delete().eq("id", property_id).eq("buyer_id", buyer_id).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Property entry not found")
    return {"success": True}

# ================================
# BUYER-LISTING MATCHING
# ================================
def _extract_district_token(location: str) -> str:
    match = re.search(r"district\s*(\d+)", str(location or ""), re.IGNORECASE)
    return f"D{match.group(1)}" if match else ""

def _compute_buyer_listing_match(buyer: dict, listing: dict) -> dict:
    """A match requires every preference the buyer actually set to be satisfied --
    an unset preference (empty district list, no budget, etc.) is treated as "no
    opinion" rather than a wildcard pass, so a buyer with zero preferences filled
    in produces zero matches instead of matching everything."""
    reasons = []
    criteria_checked = False

    buyer_types = [t for t in (buyer.get("property_types") or "").split(",") if t]
    if buyer_types:
        criteria_checked = True
        if listing.get("property_type") in buyer_types:
            reasons.append(f"Wants {listing.get('property_type')}")
        else:
            return {"is_match": False, "reasons": []}

    buyer_districts = [d for d in (buyer.get("districts") or "").split(",") if d]
    if buyer_districts:
        criteria_checked = True
        listing_district = _extract_district_token(listing.get("location"))
        if listing_district and listing_district in buyer_districts:
            reasons.append(f"In preferred area ({listing_district})")
        else:
            return {"is_match": False, "reasons": []}

    price = _to_number(listing.get("price"))
    budget_min = _to_number(buyer.get("budget_min"))
    budget_max = _to_number(buyer.get("budget_max"))
    if (budget_min or budget_max) and price:
        criteria_checked = True
        lo = budget_min or 0
        hi = budget_max or float("inf")
        stretch_hi = hi * 1.1 if hi != float("inf") else hi
        if lo <= price <= hi:
            reasons.append("Within budget")
        elif price <= stretch_hi:
            reasons.append("Within 10% of max budget")
        else:
            return {"is_match": False, "reasons": []}

    land_min = _to_number(buyer.get("land_min"))
    land_size = _to_number(listing.get("land_size"))
    if land_min and land_size:
        criteria_checked = True
        if land_size >= land_min:
            reasons.append(f"Meets min land size ({int(land_size):,} sqft)")
        else:
            return {"is_match": False, "reasons": []}

    return {"is_match": criteria_checked and len(reasons) > 0, "reasons": reasons}

@app.get("/api/listings/{listing_id}/matches")
def get_listing_matches(listing_id: str, agent=Depends(get_current_agent)):
    listing_result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not listing_result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = listing_result.data[0]
    buyers = get_db().table("buyers").select("*").eq("agent_id", agent["id"]).execute().data or []
    matches = []
    for buyer in buyers:
        result = _compute_buyer_listing_match(buyer, listing)
        if result["is_match"]:
            matches.append({
                "buyer_id": buyer["id"], "name": buyer["name"], "phone": buyer.get("phone", ""),
                "temperature": buyer.get("temperature"), "reasons": result["reasons"]
            })
    matches.sort(key=lambda m: {"HOT": 0, "WARM": 1, "COLD": 2}.get(m["temperature"], 9))
    return matches

@app.get("/api/buyers/{buyer_id}/matches")
def get_buyer_matches(buyer_id: str, agent=Depends(get_current_agent)):
    buyer_result = get_db().table("buyers").select("*").eq("id", buyer_id).eq("agent_id", agent["id"]).execute()
    if not buyer_result.data:
        raise HTTPException(status_code=404, detail="Buyer not found")
    buyer = buyer_result.data[0]
    listings = get_db().table("listings").select("*").eq("agent_id", agent["id"]).eq("status", "active").execute().data or []
    matches = []
    for listing in listings:
        result = _compute_buyer_listing_match(buyer, listing)
        if result["is_match"]:
            matches.append({
                "listing_id": listing["id"], "location": listing["location"], "price": listing["price"],
                "property_type": listing["property_type"], "reasons": result["reasons"]
            })
    return matches

# ================================
# SELLERS
# A seller lead lives in the same `listings` table as real listings (status
# "lead" instead of "active"/"archived"), since a seller lead IS a nascent
# listing -- the same property record just grows more complete over time.
# "Convert to Listing" is just editing it through the normal New Listing
# form, which promotes it to "active" on save (see update_listing above).
# ================================
def _seller_lead_payload(req: SellerLeadRequest, agent_id: str) -> dict:
    return {
        "agent_id": agent_id,
        "status": "lead",
        "seller_name": req.seller_name,
        "seller_phone": req.seller_phone,
        "seller_email": req.seller_email,
        "location": req.location,
        "property_type": req.property_type,
        "price": req.price,
        "land_size": req.land_size,
        "motivation": req.motivation,
        "timeline": req.timeline,
        "mandate_type": req.mandate_type,
        "temperature": req.temperature,
        "seller_notes": req.seller_notes,
        # Fields the full listings schema expects but a seller lead usually
        # doesn't have yet -- explicit safe defaults rather than nulls.
        "bedrooms": "",
        "bathrooms": "",
        "features": "",
        "content": "",
        "built_up": 0,
        "plot_width": 0,
        "plot_depth": 0,
        "storeys": 0,
        "site_coverage": 0,
    }

@app.get("/api/sellers")
def get_sellers(agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("*").eq("agent_id", agent["id"]).eq("status", "lead").order("created_at", desc=True).execute()
    sellers = result.data or []
    sellers.sort(key=lambda s: {"HOT": 0, "WARM": 1, "COLD": 2}.get(s.get("temperature"), 9))
    return sellers

@app.post("/api/sellers")
def create_seller(req: SellerLeadRequest, agent=Depends(get_current_agent)):
    result = get_db().table("listings").insert(_seller_lead_payload(req, agent["id"])).execute()
    return result.data[0]

@app.get("/api/sellers/{seller_id}")
def get_seller(seller_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("*").eq("id", seller_id).eq("agent_id", agent["id"]).eq("status", "lead").execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Seller not found")
    return result.data[0]

@app.patch("/api/sellers/{seller_id}")
def update_seller(seller_id: str, req: SellerLeadRequest, agent=Depends(get_current_agent)):
    existing = get_db().table("listings").select("id").eq("id", seller_id).eq("agent_id", agent["id"]).eq("status", "lead").execute()
    if not existing.data:
        raise HTTPException(status_code=404, detail="Seller not found")
    payload = _seller_lead_payload(req, agent["id"])
    payload.pop("agent_id")
    payload.pop("status")
    result = get_db().table("listings").update(payload).eq("id", seller_id).eq("agent_id", agent["id"]).execute()
    return result.data[0]

@app.delete("/api/sellers/{seller_id}")
def delete_seller(seller_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("listings").delete().eq("id", seller_id).eq("agent_id", agent["id"]).eq("status", "lead").execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Seller not found")
    return {"success": True}

# ================================
# AI CHAT ASSISTANT
# ================================
# General-purpose property Q&A -- not tied to a specific listing, but if the agent
# has one open it's passed as context so "what's the land area of this property"
# works without them re-typing the numbers. Backed by web search so answers about
# current rates, regulations, and market conditions are actually current rather
# than relying on the model's training data.
_chatbot_hits = {}

# Grounds "how do I..." questions about NestList itself -- without this the model
# has zero knowledge of the app's actual features and would either guess wrong or
# give a vague non-answer. Kept to real, current features only; the closing line
# tells the model to say so rather than invent a button/menu path that doesn't exist.
NESTLIST_APP_HELP = """
The agent is using NestList Prestige, a web app for real estate agents specializing in landed/GCB/ultra-luxury Singapore property. If they ask "how do I..." about the app itself (not a property question), use this reference:

- New Listing: fill in property details and NestList generates the listing description with AI.
- My Listings (Active Listings / Deleted Listings): manage listings. Expand a listing card to: enhance individual photos, download all photos as a ZIP, generate a branded poster (choose from several template styles), generate a property video (Classic Video style), and share to Facebook/Instagram/WhatsApp/LinkedIn/TikTok -- copy the caption and post manually, or (only for agents with it connected) post directly to Facebook/Instagram from the app via My Profile.
- Deleted Listings: a listing removed from Active Listings is archived here, not permanently gone -- it can be permanently deleted from this tab.
- Enquiries: buyer enquiries submitted through public listing pages, each with an AI-generated lead score.
- Buyer Management: buyer profiles and buyer-to-listing matching.
- Sellers: seller leads, which can be converted into a full listing.
- Pricing Reports: generates a comparable-transaction pricing report from live URA data (landed properties only).
- Dashboard: overview stats and the Singapore Market Pulse panel (live GCB market data from URA).
- My Profile: update agent details/photo, connect Instagram/Facebook for direct posting, set up notifications.
- Billing: subscription/plan management.

If asked about something not covered here, or you're not sure of the exact steps, say so plainly rather than guessing -- don't invent a button or menu path that may not exist.
"""

@app.post("/api/chat")
async def chat_with_assistant(req: ChatRequest, agent=Depends(get_current_agent)):
    if _rate_limited(_chatbot_hits, agent["id"], limit=40, window_seconds=3600):
        raise HTTPException(status_code=429, detail="You've hit the hourly limit for the assistant -- please try again in a bit.")
    if not req.messages:
        raise HTTPException(status_code=400, detail="No message provided")

    listing_context = ""
    if req.listing_id:
        result = get_db().table("listings").select("*").eq("id", req.listing_id).eq("agent_id", agent["id"]).execute()
        if result.data:
            listing = result.data[0]
            listing_context = f"""

The agent currently has this listing open. If their question refers to "this property" or "this listing", use these details rather than asking them to repeat it:
- Property type: {listing.get('property_type', '')}
- Location: {listing.get('location', '')}
- Price: SGD {listing.get('price', '')}
- Built-up area: {listing.get('built_up', '')} sqft
- Bedrooms: {listing.get('bedrooms', '')}
- Bathrooms: {listing.get('bathrooms', '')}
- Tenure: {listing.get('tenure', '')}"""

    system_prompt = (
        f"You are Mary, NestList's AI Assistant, helping a Singapore real estate agent "
        f"specializing in {agent.get('specialty') or 'landed, GCB, and ultra-luxury properties'}. "
        "If asked your name, you are Mary -- introduce yourself that way rather than as a "
        "generic assistant. Answer anything relevant to their work: property questions, "
        "calculations (unit conversions, land area, PSF, stamp duty, mortgage estimates), "
        "Singapore property regulations, current market conditions, and how to use the "
        "NestList app itself. Use web search whenever a property question needs current or "
        "Singapore-specific information rather than relying on your own knowledge -- "
        "property figures, rates, and rules change, and agents need accurate answers, "
        "not guesses. Be concise and direct; agents are often asking on the go."
        + NESTLIST_APP_HELP + listing_context
    )

    claude_messages = [{"role": m.role, "content": m.content} for m in req.messages]
    reply_text = ""
    sources = []

    try:
        # Web search is a server-side tool -- Anthropic runs the search and folds the
        # result into this same response, no client-side tool loop needed. The only
        # loop here handles the rare case where the server's own internal search
        # iteration hits its cap mid-turn (stop_reason "pause_turn") and needs one
        # more request to finish, per Anthropic's documented resume pattern.
        for _ in range(3):
            response = await create_claude_message(
                model="claude-opus-5",
                max_tokens=1024,
                system=system_prompt,
                messages=claude_messages,
                tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 5}],
            )
            for block in response.content:
                if block.type == "text":
                    reply_text += block.text
                elif block.type == "web_search_tool_result" and isinstance(block.content, list):
                    for result in block.content:
                        sources.append({"title": getattr(result, "title", None), "url": getattr(result, "url", None)})
            if response.stop_reason != "pause_turn":
                break
            claude_messages = claude_messages + [{"role": "assistant", "content": response.content}]
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Assistant is temporarily unavailable: {e}")

    return {"reply": reply_text.strip(), "sources": sources}
