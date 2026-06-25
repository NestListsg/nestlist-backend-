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
from datetime import datetime, timedelta, date
from PIL import Image as PILImage

app = FastAPI()

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
    payload = {"agent_id": agent_id, "exp": datetime.utcnow() + timedelta(days=7)}
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")

def get_current_agent(credentials: HTTPAuthorizationCredentials = Depends(security)):
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=["HS256"])
        agent_id = payload["agent_id"]
        result = get_db().table("agents").select("*").eq("id", agent_id).execute()
        if not result.data:
            raise HTTPException(status_code=401, detail="Agent not found")
        return result.data[0]
    except:
        raise HTTPException(status_code=401, detail="Invalid token")

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
def generate_listing(req: ListingRequest, agent=Depends(get_current_agent)):
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

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
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
- Price: SGD {req.price}
- Features: {req.features}

Write:
1. A compelling headline
2. Three paragraphs in your personal voice
3. A warm call to action
4. End with: {agent['name']} | {agent['agency']} Specialist"""

    claude = anthropic.Anthropic(api_key=api_key)
    response = claude.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}]
    )
    listing_text = response.content[0].text

    saved = get_db().table("listings").insert({
        "agent_id": agent["id"],
        "location": req.location,
        "price": req.price,
        "property_type": req.property_type,
        "content": listing_text,
        "land_size": req.land_size,
        "built_up": req.built_up,
        "bedrooms": req.bedrooms,
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
    response = requests.post(
        f"https://graph.facebook.com/v25.0/{fb_page_id}/feed",
        data={"message": post_message, "access_token": fb_token}
    )
    data = response.json()
    if "id" in data:
        return {"success": True, "post_id": data["id"]}
    else:
        raise HTTPException(status_code=400, detail=data.get("error", {}).get("message", "Unknown error"))

# ================================
# PROFILE ROUTE
# ================================
@app.put("/api/profile")
def update_profile(req: ProfileUpdate, agent=Depends(get_current_agent)):
    get_db().table("agents").update(req.dict()).eq("id", agent["id"]).execute()
    result = get_db().table("agents").select("*").eq("id", agent["id"]).execute()
    agent = result.data[0]
    return {k: v for k, v in agent.items() if k != "password_hash"}

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "NestList Prestige API"}

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

        client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

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
  "bedrooms": "e.g. 4 bedrooms, 3 bathrooms",
  "price": "e.g. 25,000,000",
  "features": "special features as comma separated text",
  "plot_width": number in metres or 0,
  "plot_depth": number in metres or 0,
  "storeys": number or 0,
  "site_coverage": number as percentage or 0
}
Return only valid JSON, nothing else."""
        })

        message = client.messages.create(
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
