from __future__ import annotations

import hmac
import os

from fastapi import HTTPException


def require_admin(authorization: str | None) -> None:
    expected = os.environ.get("ADMIN_API_TOKEN", "")
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Admin actions are disabled until ADMIN_API_TOKEN is configured.",
        )
    provided = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not hmac.compare_digest(provided, expected):
        raise HTTPException(status_code=401, detail="A valid admin bearer token is required.")
