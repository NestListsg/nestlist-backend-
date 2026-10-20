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
