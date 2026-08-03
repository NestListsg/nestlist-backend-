"""Autoenhance.ai integration -- real AI photo enhancement (exposure/color
correction, HDR-style tone mapping, optional sky replacement) as a drop-in
upgrade over the local PIL-based _auto_enhance_photo in main.py.

Degrades gracefully: returns None if AUTOENHANCE_API_KEY isn't set, or if
the call fails for any reason (network, timeout, API error) -- callers
should fall back to local enhancement on None so one bad photo (or the
service being briefly down) never breaks an upload. Same pattern as
URA_ACCESS_KEY / RESEND_API_KEY elsewhere in this backend.

API reference: https://docs.autoenhance.ai/ (v3). Flow per image:
  1. POST /images/           -> register image, get a signed upload_url
  2. PUT  <upload_url>       -> raw JPEG bytes go straight to their S3 bucket
  3. GET  /images/{id}       -> poll until "enhanced": true
  4. GET  /images/{id}/enhanced -> full-resolution enhanced JPEG bytes
"""
import os
import time
import uuid
import httpx

BASE_URL = "https://api.autoenhance.ai/v3"
POLL_INTERVAL_SECONDS = 2
POLL_TIMEOUT_SECONDS = 60

# Sky replacement is available (restage.sky: CLEAR / LOW_CLOUD / etc.) but
# left off by default -- swapping every exterior photo's sky automatically
# risks the "artificial, not trustworthy" look Feature 1 was built to avoid.
# Flip this if Jane wants it on.
SKY_REPLACEMENT = None  # e.g. "LOW_CLOUD" to enable


def _headers(access_key: str) -> dict:
    return {"x-api-key": access_key}


def enhance_image(image_bytes: bytes) -> bytes | None:
    """Runs one photo through Autoenhance.ai and returns the enhanced JPEG
    bytes on success, or None (not configured, or the call failed) --
    caller should fall back to local enhancement in that case."""
    access_key = os.environ.get("AUTOENHANCE_API_KEY", "")
    if not access_key:
        return None

    try:
        with httpx.Client(timeout=30) as client:
            create_body = {
                "image_name": f"{uuid.uuid4().hex}.jpg",
                "content_type": "image/jpeg",
            }
            if SKY_REPLACEMENT:
                create_body["restage"] = {"sky": SKY_REPLACEMENT}

            create_resp = client.post(
                f"{BASE_URL}/images/",
                headers={**_headers(access_key), "Content-Type": "application/json"},
                json=create_body,
            )
            create_resp.raise_for_status()
            create_data = create_resp.json()
            image_id = create_data["image_id"]
            upload_url = create_data.get("upload_url") or create_data.get("s3PutObjectUrl")
            if not upload_url:
                return None

            upload_resp = client.put(
                upload_url,
                headers={"Content-Type": "image/jpeg"},
                content=image_bytes,
            )
            upload_resp.raise_for_status()

            deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
            enhanced_ready = False
            while time.monotonic() < deadline:
                time.sleep(POLL_INTERVAL_SECONDS)
                status_resp = client.get(f"{BASE_URL}/images/{image_id}", headers=_headers(access_key))
                status_resp.raise_for_status()
                status_data = status_resp.json()
                if status_data.get("error"):
                    return None
                if status_data.get("enhanced"):
                    enhanced_ready = True
                    break
            if not enhanced_ready:
                return None

            download_resp = client.get(
                f"{BASE_URL}/images/{image_id}/enhanced",
                headers=_headers(access_key),
                params={"preview": "false", "format": "jpeg", "visible_disclosure": "false"},
            )
            download_resp.raise_for_status()
            return download_resp.content
    except Exception:
        return None
