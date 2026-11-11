"""Admin access.

Two ways in, checked in this order:

1. Firebase Authentication (recommended for people). The site signs you in with Google through
   Firebase and sends the Firebase ID token as `Authorization: Bearer <token>`. The API checks the
   token's signature with Google's public keys, that it was issued for FIREBASE_PROJECT_ID, and
   that the email is verified and listed in ADMIN_EMAILS (or the user has the custom claim
   `admin: true`). No service-account file is needed just to verify tokens.
2. ADMIN_API_TOKEN (for scripts and as a fallback). A long random shared secret.

Firebase only handles *who you are*. Paper data still lives in PostgreSQL (DATABASE_URL).
"""
from __future__ import annotations

import hmac
import os
import time
from typing import Any

from fastapi import HTTPException

_cache: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_SECONDS = 300


def firebase_project_id() -> str:
    return os.environ.get("FIREBASE_PROJECT_ID", "").strip()


def admin_emails() -> set[str]:
    return {email.strip().lower() for email in os.environ.get("ADMIN_EMAILS", "").split(",") if email.strip()}


def verify_firebase_token(token: str) -> dict[str, Any] | None:
    """Decoded claims for a valid Firebase ID token, or None if it isn't one."""
    project_id = firebase_project_id()
    if not project_id or token.count(".") != 2:
        return None
    cached = _cache.get(token)
    if cached and cached[0] > time.time():
        return cached[1]
    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError:  # google-auth not installed: Firebase login disabled
        return None
    try:
        claims = id_token.verify_firebase_token(token, google_requests.Request(), audience=project_id)
    except Exception:  # expired, wrong project, bad signature, ...
        return None
    if not claims or claims.get("iss") != f"https://securetoken.google.com/{project_id}":
        return None
    expires = min(float(claims.get("exp", 0)), time.time() + _CACHE_SECONDS)
    _cache[token] = (expires, claims)
    if len(_cache) > 500:
        for key, (expiry, _) in list(_cache.items()):
            if expiry <= time.time():
                _cache.pop(key, None)
    return claims


def is_admin_claims(claims: dict[str, Any]) -> bool:
    if claims.get("admin") is True:
        return True
    email = (claims.get("email") or "").lower()
    return bool(email and claims.get("email_verified") and email in admin_emails())


def require_admin(authorization: str | None) -> dict[str, Any]:
    """Raise 401/403/503 unless the request carries admin credentials. Returns who it was."""
    provided = authorization.removeprefix("Bearer ").strip() if authorization else ""
    expected = os.environ.get("ADMIN_API_TOKEN", "")
    if not expected and not firebase_project_id():
        raise HTTPException(
            status_code=503,
            detail="Admin actions are disabled until FIREBASE_PROJECT_ID (with ADMIN_EMAILS) "
            "or ADMIN_API_TOKEN is configured.",
        )
    if expected and provided and hmac.compare_digest(provided, expected):
        return {"method": "token"}
    claims = verify_firebase_token(provided) if provided else None
    if claims is not None:
        if is_admin_claims(claims):
            return {"method": "firebase", "email": claims.get("email"), "uid": claims.get("user_id")}
        raise HTTPException(status_code=403, detail="This Google account is not on the admin list (ADMIN_EMAILS).")
    raise HTTPException(status_code=401, detail="Sign in with an admin account or provide the admin token.")
