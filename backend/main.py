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
from datetime import datetime, timedelta, date
from PIL import Image as PILImage

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

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(monitor_api_key())
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

class ProfilePhotoRequest(BaseModel):
    image_data: str

class TokenExchangeRequest(BaseModel):
    user_token: str

class PublicEnquiryRequest(BaseModel):
    listing_id: str
    client_name: str
    phone: str = ""
    email: str = ""
    message: str = ""
    website: str = ""  # honeypot field, must stay empty

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
    return {"token": token, "agent": {k: v for k, v in agent.items() if k != "password_hash"}}

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
    return {"token": token, "agent": {k: v for k, v in agent.items() if k != "password_hash"}}

# ================================
# LISTINGS ROUTES
# ================================
@app.get("/api/listings")
def get_listings(agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("*").eq("agent_id", agent["id"]).order("created_at", desc=True).execute()
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
- Price: SGD {req.price}
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
def post_to_facebook(listing_id: str, agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = result.data[0]

    fb_token = os.environ.get("FB_PAGE_ACCESS_TOKEN", "")
    fb_page_id = os.environ.get("FB_PAGE_ID", "")

    post_message = f"""NEW LISTING | {listing['property_type']}
{listing['location']}
SGD {listing['price']}

{listing['content'][:800]}...

Contact us at nestlist.sg to find out more!

#NestList #NestListPrestige #Singapore #SingaporeProperty #GCB #LandedProperty #PropertySG #RealEstate"""

    image_urls = listing.get("images") or []

    if image_urls:
        media_ids = []
        for url in image_urls[:5]:
            photo_response = requests.post(
                f"https://graph.facebook.com/v25.0/{fb_page_id}/photos",
                data={
                    "url": url,
                    "published": "false",
                    "access_token": fb_token
                }
            )
            photo_data = photo_response.json()
            if "id" in photo_data:
                media_ids.append({"media_fbid": photo_data["id"]})

        post_data = {
            "message": post_message,
            "access_token": fb_token
        }
        for i, media in enumerate(media_ids):
            post_data[f"attached_media[{i}]"] = json.dumps(media)

        response = requests.post(
            f"https://graph.facebook.com/v25.0/{fb_page_id}/feed",
            data=post_data
        )
    else:
        response = requests.post(
            f"https://graph.facebook.com/v25.0/{fb_page_id}/feed",
            data={"message": post_message, "access_token": fb_token}
        )

    data = response.json()
    if "id" in data:
        return {"success": True, "post_id": data["id"]}
    else:
        raise HTTPException(status_code=400, detail=data.get("error", {}).get("message", "Unknown error"))

def _to_number(value) -> float:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return 0

# ================================
# POSTER GENERATION (Placid.app)
# ================================
POSTER_TEMPLATES = [
    {"id": "editorial", "name": "Editorial", "placid_uuid": "djmsagsiw2i7f", "thumbnail_url": ""},
]

def _template_uuid_for(agent) -> str:
    template_id = agent.get("poster_template_id")
    for t in POSTER_TEMPLATES:
        if t["id"] == template_id:
            return t["placid_uuid"]
    return os.environ.get("PLACID_TEMPLATE_UUID", "")

@app.get("/api/poster-templates")
def get_poster_templates(agent=Depends(get_current_agent)):
    return [{"id": t["id"], "name": t["name"], "thumbnail_url": t["thumbnail_url"]} for t in POSTER_TEMPLATES]

@app.post("/api/listings/{listing_id}/generate-poster")
async def generate_poster(listing_id: str, photo_index: int = 0, agent=Depends(get_current_agent)):
    result = get_db().table("listings").select("*").eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    if not result.data:
        raise HTTPException(status_code=404, detail="Listing not found")
    listing = result.data[0]

    images = listing.get("images") or []
    if not images:
        raise HTTPException(status_code=400, detail="Upload at least one photo before generating a poster")
    if photo_index < 0 or photo_index >= len(images):
        photo_index = 0

    placid_token = os.environ.get("PLACID_API_TOKEN", "")
    template_uuid = _template_uuid_for(agent)
    if not placid_token or not template_uuid:
        raise HTTPException(status_code=503, detail="Poster generation is not configured yet")

    price_num = _to_number(listing.get("price"))
    built_up_num = _to_number(listing.get("built_up"))
    price_psf = round(price_num / built_up_num) if built_up_num > 0 else 0
    bar_color = agent.get("poster_color") or "#1a1a5c"
    bedrooms_match = re.search(r"\d+", str(listing.get("bedrooms") or ""))
    bathrooms_match = re.search(r"\d+", str(listing.get("bathrooms") or ""))
    bedrooms_val = bedrooms_match.group(0) if bedrooms_match else ""
    bathrooms_val = bathrooms_match.group(0) if bathrooms_match else ""

    layers = {
        "photo": {"image": images[photo_index]},
        "title": {"text": listing.get("property_type") or listing.get("location", "")},
        "price": {"text": f"SGD {listing['price']}"},
        "rooms": {"text": f"{bedrooms_val} Rooms" if bedrooms_val else ""},
        "baths": {"text": f"{bathrooms_val} Baths" if bathrooms_val else ""},
        "size": {"text": f"{built_up_num:,.0f} sqft" if built_up_num else ""},
        "price_psf": {"text": f"SGD {price_psf:,} psf" if price_psf else ""},
        "agent_name": {"text": agent["name"], "text_color": "#F8F4EC"},
        "agency": {"text": agent.get("agency", ""), "text_color": "#F8F4EC"},
        "agent_phone": {"text": agent.get("contact", ""), "text_color": "#F8F4EC"},
        "agent-bar": {"background_color": bar_color},
    }
    if agent.get("photo_url"):
        layers["agent_photo"] = {"image": agent["photo_url"]}

    async with httpx.AsyncClient() as client:
        create_response = await client.post(
            "https://api.placid.app/api/rest/images",
            headers={"Authorization": f"Bearer {placid_token}"},
            json={"template_uuid": template_uuid, "layers": layers},
            timeout=20
        )
        create_data = create_response.json()
        image_id = create_data.get("id")
        if not image_id:
            raise HTTPException(status_code=502, detail="Poster generation failed to start")

        poster_url = None
        for _ in range(15):
            await asyncio.sleep(2)
            poll_response = await client.get(
                f"https://api.placid.app/api/rest/images/{image_id}",
                headers={"Authorization": f"Bearer {placid_token}"},
                timeout=20
            )
            poll_data = poll_response.json()
            if poll_data.get("status") == "finished" and poll_data.get("image_url"):
                poster_url = poll_data["image_url"]
                break
            if poll_data.get("status") == "error":
                raise HTTPException(status_code=502, detail="Placid failed to render the poster")

    if not poster_url:
        raise HTTPException(status_code=504, detail="Poster is taking longer than expected — please try again shortly")

    get_db().table("listings").update({"poster_url": poster_url}).eq("id", listing_id).eq("agent_id", agent["id"]).execute()

    return {"poster_url": poster_url}

# ================================
# PROFILE ROUTE
# ================================
@app.put("/api/profile")
def update_profile(req: ProfileUpdate, agent=Depends(get_current_agent)):
    get_db().table("agents").update(req.dict()).eq("id", agent["id"]).execute()
    result = get_db().table("agents").select("*").eq("id", agent["id"]).execute()
    agent = result.data[0]
    return {k: v for k, v in agent.items() if k != "password_hash"}

@app.get("/api/profile")
def get_profile(agent=Depends(get_current_agent)):
    return {k: v for k, v in agent.items() if k != "password_hash"}

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
        photo_url = supabase.storage.from_("listings-images").get_public_url(filename)

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
  "property_type": "one of: Good Class Bungalow (GCB), Landed Bungalow, Semi-Detached, Terrace House, Penthouse, Ultra Luxury Investment Property, HDB Flat, Condominium",
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
        "last_updated": "Jan 2026"
    }

@app.put("/api/market-pulse")
async def update_market_pulse(request: Request, agent=Depends(get_current_agent)):
    if agent["email"] != "leesbjane@gmail.com":
        raise HTTPException(status_code=403, detail="Not authorised")
    body = await request.json()
    body["last_updated"] = date.today().strftime("%b %Y")
    get_db().table("market_pulse").upsert({"id": 1, **body}).execute()
    return {"success": True}

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

def _rate_limited(ip: str, limit: int = 5, window_seconds: int = 3600) -> bool:
    now = datetime.utcnow()
    hits = [t for t in _public_enquiry_hits.get(ip, []) if (now - t).total_seconds() < window_seconds]
    hits.append(now)
    _public_enquiry_hits[ip] = hits
    return len(hits) > limit

def _is_valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
        return True
    except (ValueError, AttributeError, TypeError):
        return False

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
    if _rate_limited(client_ip):
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

    agent_chat_result = get_db().table("agents").select("telegram_chat_id").eq("id", listing["agent_id"]).execute()
    agent_chat_id = agent_chat_result.data[0].get("telegram_chat_id") if agent_chat_result.data else None

    score_emoji = {"Hot": "🔥", "Warm": "🌤️", "Cold": "❄️"}.get(lead_score, "")
    await send_telegram_alert(
        f"{score_emoji} <b>New Lead: {lead_score}</b>\n\n"
        f"<b>{req.client_name}</b>\n"
        f"Listing: {listing['property_type']} — {listing['location']}\n"
        f"Phone: {req.phone or 'not provided'}\n"
        f"Email: {req.email or 'not provided'}\n"
        f"Summary: {ai_summary or message[:200]}",
        chat_id=agent_chat_id
    )

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
    get_db().table("enquiries").delete().eq("id", enquiry_id).eq("agent_id", agent["id"]).execute()
    return {"success": True}

@app.delete("/api/listings/{listing_id}")
def delete_listing(listing_id: str, agent=Depends(get_current_agent)):
    get_db().table("listings").delete().eq("id", listing_id).eq("agent_id", agent["id"]).execute()
    return {"success": True}

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
