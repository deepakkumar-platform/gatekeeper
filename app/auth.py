"""
Authentication for the Gatekeeper gateway.

Apps authenticate by sending their key in ONE of three headers (checked in
this order):

    api-key: <key>                  Azure-OpenAI convention; what the
                                    Azure OpenAI SDK / many MS tools send.
    Authorization: Bearer <key>     Standard OpenAI / OAuth2 convention; what
                                    the stock `openai` SDK sends.
    X-API-Key: <key>                Plain convenience for curl / scripts.

Supporting all three means any common client works without custom wiring:
the OpenAI SDK (Bearer), the Azure OpenAI SDK (api-key), and ad-hoc curl
(X-API-Key) all authenticate against the same gateway.

The `api-key` header is also registered as an OpenAPI security scheme, so the
"Authorize" button on the interactive /docs page works for browser testing.

A missing, malformed, unknown, or deactivated key results in 401. The key is
never logged in plaintext; only the resolved app name appears in logs.
"""

from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

from .storage import AppRecord, lookup_by_plaintext

# Registering this as a Security dependency does two things:
#   1. Makes the `api-key` header appear in the OpenAPI spec, so the /docs
#      "Authorize" button lets you paste a key and test interactively.
#   2. Hands us the header value (or None) without raising on absence
#      (auto_error=False), so we can fall back to the other two headers.
_api_key_scheme = APIKeyHeader(name="api-key", auto_error=False)


def _extract_key(request: Request) -> Optional[str]:
    """Pull the key from Authorization: Bearer or X-API-Key (api-key is handled
    by the Security scheme and passed into authenticate separately)."""
    auth = request.headers.get("authorization")
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() == "bearer" and token:
            return token.strip()
    x_api_key = request.headers.get("x-api-key")
    if x_api_key:
        return x_api_key.strip()
    return None


def authenticate(
    request: Request,
    api_key: Optional[str] = Security(_api_key_scheme),
) -> AppRecord:
    """FastAPI dependency. Returns the AppRecord or raises 401.

    `api_key` is the value of the `api-key` header (via the Security scheme);
    if absent, we fall back to Authorization: Bearer or X-API-Key.
    """
    plaintext = (api_key.strip() if api_key else None) or _extract_key(request)
    if not plaintext:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing API key (send 'api-key: <key>' or 'Authorization: Bearer <key>')",
            headers={"WWW-Authenticate": "Bearer"},
        )

    app = lookup_by_plaintext(plaintext)
    if app is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid API key",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not app.active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API key is deactivated",
        )
    return app
