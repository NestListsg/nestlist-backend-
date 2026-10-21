"""Cloudinary integration -- free-tier photo enhancement (auto color/contrast/
brightness correction), triggered per-photo by the agent from My Listings
rather than automatically on upload (Jane's call: agents pick which photos
need it, not a blanket setting).

Degrades gracefully: returns None if CLOUDINARY_* env vars aren't set, or if
the call fails for any reason -- same pattern as autoenhance.py /
ura_market_pulse.py elsewhere in this backend.

Uses Cloudinary's plain REST API directly (signed upload + URL-based
transformation fetch) -- no SDK dependency, consistent with the rest of this
codebase's third-party integrations.
"""
import hashlib
import os
import time
import uuid
import httpx

UPLOAD_URL_TEMPLATE = "https://api.cloudinary.com/v1_1/{cloud_name}/image/upload"
DELIVERY_URL_TEMPLATE = "https://res.cloudinary.com/{cloud_name}/image/upload/e_improve/{public_id}.jpg"
DESTROY_URL_TEMPLATE = "https://api.cloudinary.com/v1_1/{cloud_name}/image/destroy"
USAGE_URL_TEMPLATE = "https://api.cloudinary.com/v1_1/{cloud_name}/usage"


def fetch_usage() -> dict:
    """Return live Cloudinary account usage (credits used/limit, plan name, and
    the transformations/storage/bandwidth/objects breakdown).

    Calls Cloudinary's Admin API usage endpoint directly over HTTPS with Basic
    auth (api_key:api_secret) -- this is exactly what the cloudinary SDK's
    api.usage() does under the hood, done here without adding the SDK as a
    dependency this codebase deliberately avoids (see module docstring).

    Never raises: returns {"ok": True, "usage": <raw Cloudinary JSON>} on success,
    or {"ok": False, "error": "<plain reason>"} on any failure (not configured,
    network error, auth rejected, non-200) so the caller can surface a clean JSON
    error instead of a 500. Reuses the same CLOUDINARY_* env vars as enhance_image
    and never returns or logs the secret itself."""
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
    api_key = os.environ.get("CLOUDINARY_API_KEY", "")
    api_secret = os.environ.get("CLOUDINARY_API_SECRET", "")
    if not (cloud_name and api_key and api_secret):
        return {"ok": False, "error": "Cloudinary is not configured on this server (CLOUDINARY_* env vars are not all set)."}

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                USAGE_URL_TEMPLATE.format(cloud_name=cloud_name),
                auth=(api_key, api_secret),
            )
        if resp.status_code != 200:
            # Deliberately do NOT echo the response body -- it can carry request
            # context; the status code alone is enough to diagnose (401/403 =
            # wrong/rotated key, 420/429 = rate limited, 5xx = Cloudinary down).
            return {"ok": False, "error": f"Cloudinary usage request failed (HTTP {resp.status_code})."}
        return {"ok": True, "usage": resp.json()}
    except Exception as e:
        return {"ok": False, "error": f"Could not reach Cloudinary ({type(e).__name__})."}


def _delete_uploaded_copy(client, cloud_name, api_key, api_secret, public_id):
    """Remove the copy this enhance left on Cloudinary so it stops counting
    against the free credit tier (the capacity audit flagged this as a path to a
    +US$99/mo overage -- every Enhance was leaving a stored original behind).

    Strictly best-effort: the enhanced JPEG has already been downloaded and
    returned by the time this runs, so a cleanup failure must never lose an
    agent's enhanced photo -- it is swallowed. Cloudinary's destroy removes the
    stored original together with its derived (e_improve) version."""
    try:
        timestamp = int(time.time())
        to_sign = f"public_id={public_id}&timestamp={timestamp}"
        signature = hashlib.sha1((to_sign + api_secret).encode("utf-8")).hexdigest()
        client.post(
            DESTROY_URL_TEMPLATE.format(cloud_name=cloud_name),
            data={
                "public_id": public_id,
                "timestamp": timestamp,
                "api_key": api_key,
                "signature": signature,
            },
        )
    except Exception:
        pass


def enhance_image(image_bytes: bytes) -> bytes | None:
    """Runs one photo through Cloudinary's e_improve auto-enhancement and
    returns the enhanced JPEG bytes on success, or None (not configured, or
    the call failed) -- caller should keep the original photo in that case."""
    cloud_name = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
    api_key = os.environ.get("CLOUDINARY_API_KEY", "")
    api_secret = os.environ.get("CLOUDINARY_API_SECRET", "")
    if not (cloud_name and api_key and api_secret):
        return None

    try:
        timestamp = int(time.time())
        to_sign = f"timestamp={timestamp}"
        signature = hashlib.sha1((to_sign + api_secret).encode("utf-8")).hexdigest()

        with httpx.Client(timeout=30) as client:
            upload_resp = client.post(
                UPLOAD_URL_TEMPLATE.format(cloud_name=cloud_name),
                files={"file": (f"{uuid.uuid4().hex}.jpg", image_bytes, "image/jpeg")},
                data={"timestamp": timestamp, "api_key": api_key, "signature": signature},
            )
            upload_resp.raise_for_status()
            public_id = upload_resp.json()["public_id"]

            delivery_resp = client.get(
                DELIVERY_URL_TEMPLATE.format(cloud_name=cloud_name, public_id=public_id)
            )
            delivery_resp.raise_for_status()
            enhanced = delivery_resp.content

            # We have the enhanced bytes in hand; the stored copy has done its job.
            # Delete it now so it doesn't accumulate on the free tier. Best-effort:
            # never let cleanup cost us the photo we just produced.
            _delete_uploaded_copy(client, cloud_name, api_key, api_secret, public_id)
            return enhanced
    except Exception:
        return None
