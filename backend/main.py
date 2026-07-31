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
import time
from datetime import datetime, timedelta, date
from PIL import Image as PILImage, ImageEnhance, ImageOps
import poster_renderer
import ura_market_pulse

app = FastAPI()

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
        client = anthropic.Anthropic(api_key=primary_key)
        return client.messages.create(**kwargs)
    except anthropic.AuthenticationError:
        backup_key = os.environ.get("ANTHROPIC_API_KEY_BACKUP", "")
        if not backup_key:
            raise
        await send_telegram_alert("⚠️ <b>NestList Alert</b>\n\nPrimary Anthropic API key failed — automatically switched to the backup key, agents are unaffected.\n\nPlease check/replace the primary key in Railway when convenient (no rush).")
        client = anthropic.Anthropic(api_key=backup_key)
        return client.messages.create(**kwargs)

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

def _agent_response(agent) -> dict:
    out = {k: v for k, v in agent.items() if k != "password_hash"}
    out["can_use_instagram_beta"] = _can_use_instagram_beta(agent)
    out["can_use_facebook_beta"] = _can_use_facebook_beta(agent)
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
    return result.data or []

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

    prompt = f"""You are {agent['name']} from {agent['agency']}, a specialist in {agent['specialty']}.
Your tone: {agent.get('tone', 'Warm & Conversational')}
You emphasise: {agent.get('emphasis', 'Lifestyle & Prestige')}
Your signature phrase: "{agent.get('signature', 'Where your next chapter begins.')}"

Write a premium property listing for:
- Type: {req.property_type}
- Location: {req.location}
- Land size: {req.land_size:,} sqft
- Built-up: {req.built_up:,} sqft
- Bedrooms: {req.bedrooms}
- Bathrooms: {req.bathrooms}
- Price: SGD {_format_price_millions(req.price)}
- Features: {req.features}

Write:
1. A compelling headline
2. Three paragraphs in your personal voice
3. A warm call to action
4. End with: {agent['name']} | {agent['agency']} Specialist"""

    response = await create_claude_message(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    listing_text = response.content[0].text.strip().replace('**', '').replace('---', '').replace('# ', '').strip()

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

    return {
        "compliance": {"passed": passed, "warnings": warnings, "issues": issues},
        "listing": saved.data[0]
    }

@app.post("/api/listings/{listing_id}/upload-images")
async def upload_listing_images(listing_id: str, request: Request, agent=Depends(get_current_agent)):
    try:
        body = await request.json()
        images = body.get("images", [])

        if not images:
            raise HTTPException(status_code=400, detail="No images provided")

        if len(images) > 15:
            images = images[:15]

        supabase = get_db()
        image_urls = []

        for i, img in enumerate(images):
            image_data = img.get("image_data")
            img_bytes = base64.b64decode(image_data)
            pil_img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
            pil_img.thumbnail((1920, 1920))
            pil_img = _auto_enhance_photo(pil_img)

            buffer = io.BytesIO()
            pil_img.save(buffer, format="JPEG", quality=80)
            buffer.seek(0)
            compressed = buffer.read()

            filename = f"{listing_id}/{i}_{listing_id[:8]}.jpg"
            supabase.storage.from_("listings-images").upload(
                filename,
                compressed,
                {"content-type": "image/jpeg", "upsert": "true"}
            )

            url = supabase.storage.from_("listings-images").get_public_url(filename)
            image_urls.append(url)

        supabase.table("listings").update({"images": image_urls}).eq("id", listing_id).eq("agent_id", agent["id"]).execute()

        return {"success": True, "image_urls": image_urls}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/api/listings/{listing_id}/images/{image_index}")
def delete_listing_image(listing_id: str, image_index: int, agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    images = result.data[0].get("images") or []
    if image_index < 0 or image_index >= len(images):
        raise HTTPException(status_code=400, detail="Invalid image index")
    images.pop(image_index)
    get_db().table("listings").update({"images": images}).eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    return {"images": images}

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
        data={"url": listing["poster_url"], "caption": caption, "access_token": fb_token}
    )
    data = response.json()
    if "id" in data:
        return {"success": True, "post_id": data["id"]}

    err = data.get("error", {})
    if err.get("code") == 190:
        _clear_facebook_connection()
        raise HTTPException(status_code=401, detail="Your Facebook connection has expired. Please reconnect in My Profile.")
    raise HTTPException(status_code=400, detail=err.get("message", "Failed to post to Facebook"))

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

    district_match = re.search(r"district\s*\d+", str(listing.get("location") or ""), re.IGNORECASE)
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

def _wait_for_ig_container_ready(container_id: str, page_token: str, max_attempts: int = 10, delay_seconds: float = 1.5) -> bool:
    # Instagram fetches/processes the image asynchronously after the container is
    # created — publishing before that finishes fails with "Media ID is not
    # available", so poll status_code until it reports FINISHED.
    for _ in range(max_attempts):
        status_res = requests.get(
            f"https://graph.facebook.com/v25.0/{container_id}",
            params={"fields": "status_code", "access_token": page_token},
        )
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
    })
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
    })
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

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
    })
    d1 = r1.json()
    if "access_token" not in d1:
        raise HTTPException(status_code=400, detail=f"Instagram connect failed: {d1.get('error', {}).get('message', 'unknown error')}")
    short_lived_token = d1["access_token"]

    r2 = requests.get("https://graph.facebook.com/v25.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_lived_token,
    })
    d2 = r2.json()
    if "access_token" not in d2:
        raise HTTPException(status_code=400, detail=f"Instagram connect failed: {d2.get('error', {}).get('message', 'unknown error')}")
    long_lived_user_token = d2["access_token"]

    r3 = requests.get("https://graph.facebook.com/v25.0/me/accounts", params={"access_token": long_lived_user_token})
    d3 = r3.json()
    if not d3.get("data"):
        raise HTTPException(status_code=400, detail="No Facebook Page found for this account. Instagram connect requires a Facebook Page linked to your Instagram professional account.")
    page = d3["data"][0]
    page_id, page_name, page_token = page["id"], page.get("name"), page["access_token"]

    r4 = requests.get(f"https://graph.facebook.com/v25.0/{page_id}", params={
        "fields": "instagram_business_account", "access_token": page_token,
    })
    d4 = r4.json()
    ig_account = d4.get("instagram_business_account")
    if not ig_account:
        raise HTTPException(status_code=400, detail="Your Facebook Page isn't linked to an Instagram professional account yet. Link it in the Instagram app under Settings > Account, then try again.")
    ig_user_id = ig_account["id"]

    r5 = requests.get(f"https://graph.facebook.com/v25.0/{ig_user_id}", params={
        "fields": "username", "access_token": page_token,
    })
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
    })
    d1 = r1.json()
    if "access_token" not in d1:
        raise HTTPException(status_code=400, detail=f"Facebook connect failed: {d1.get('error', {}).get('message', 'unknown error')}")
    short_lived_token = d1["access_token"]

    r2 = requests.get("https://graph.facebook.com/v25.0/oauth/access_token", params={
        "grant_type": "fb_exchange_token", "client_id": app_id,
        "client_secret": app_secret, "fb_exchange_token": short_lived_token,
    })
    d2 = r2.json()
    if "access_token" not in d2:
        raise HTTPException(status_code=400, detail=f"Facebook connect failed: {d2.get('error', {}).get('message', 'unknown error')}")
    long_lived_user_token = d2["access_token"]

    r3 = requests.get("https://graph.facebook.com/v25.0/me/accounts", params={"access_token": long_lived_user_token})
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
    })
    ig_account = r4.json().get("instagram_business_account")
    if ig_account:
        ig_user_id = ig_account["id"]
        r5 = requests.get(f"https://graph.facebook.com/v25.0/{ig_user_id}", params={
            "fields": "username", "access_token": page_token,
        })
        update_payload["instagram_business_account_id"] = ig_user_id
        update_payload["instagram_username"] = r5.json().get("username", "")
        update_payload["instagram_connected_at"] = datetime.utcnow().isoformat()

    get_db().table("agents").update(update_payload).eq("id", agent["id"]).execute()

    return {"success": True, "page_name": page_name}

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "NestList Prestige API"}

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
        params={"fields": "instagram_business_account", "access_token": fb_token}
    )
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
        }
    )
    exchange_data = exchange_response.json()
    if "access_token" not in exchange_data:
        raise HTTPException(status_code=400, detail=f"User token exchange failed: {exchange_data.get('error', {}).get('message', 'unknown error')}")
    long_lived_user_token = exchange_data["access_token"]

    accounts_response = requests.get(
        "https://graph.facebook.com/v25.0/me/accounts",
        params={"access_token": long_lived_user_token}
    )
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
        "price": listing["price"],
        "content": listing["content"],
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
    try:
        result = get_db().table("listings").insert(_seller_lead_payload(req, agent["id"])).execute()
        return result.data[0]
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DIAGNOSTIC: {type(e).__name__}: {e}")

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
