import base64
import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

router = APIRouter(prefix="/google", tags=["Google AI Hub"])

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.getenv(
    "GOOGLE_REDIRECT_URI",
    "https://callahanentertainment.onrender.com/google/oauth/callback",
).strip()
GOOGLE_OAUTH_STATE_SECRET = os.getenv(
    "GOOGLE_OAUTH_STATE_SECRET",
    os.getenv("BRIDGE_TOKEN", ""),
).strip()

GOOGLE_SCOPES = (
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "https://www.googleapis.com/auth/adwords",
    "https://www.googleapis.com/auth/business.manage",
)


def _require_config():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        raise HTTPException(
            status_code=500,
            detail="GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET must be configured in Render",
        )
    if not GOOGLE_OAUTH_STATE_SECRET:
        raise HTTPException(status_code=500, detail="OAuth state secret is not configured")


def _make_state() -> str:
    stamp = str(int(time.time()))
    sig = hmac.new(
        GOOGLE_OAUTH_STATE_SECRET.encode(),
        stamp.encode(),
        hashlib.sha256,
    ).digest()
    token = base64.urlsafe_b64encode(sig).decode().rstrip("=")
    return f"{stamp}.{token}"


@router.get("/oauth/start", include_in_schema=False)
async def google_oauth_start_public():
    """Start Google OAuth without putting a long-lived bridge secret in the URL."""
    _require_config()
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": _make_state(),
    }
    return RedirectResponse(
        "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)
    )
