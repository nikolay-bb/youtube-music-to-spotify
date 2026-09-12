"""One-click YouTube login, using Google's ordinary web OAuth flow.

Why this exists alongside login_youtube.py
-------------------------------------------
The device flow (a code you type on another page) was needed when this app
used ytmusicapi, which required a "TVs and Limited Input devices" client. That
dependency is gone, so YouTube can use the same kind of login as Spotify: click
a button, approve, done.

Setting it up costs one extra field when creating the Google client - the
redirect URI - and removes the code-typing from every login after that. Both
services then work the same way, which is one idea to learn instead of two.

The client secret stays on the server. The browser never sees it.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import YTM_OAUTH_FILE, ensure_data_dirs, get_settings
from .errors import describe_oauth_error, describe_transport_error, error_fields

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
SCOPE = "https://www.googleapis.com/auth/youtube.readonly"


class GoogleAuthError(RuntimeError):
    """Raised when the login cannot start or cannot be completed."""


# Random values handed out but not yet seen come back. Guards against another
# site tricking the browser into completing a login (CSRF).
_pending_states: set[str] = set()


def build_login_url() -> str:
    settings = get_settings()
    if not (settings.ytm_client_id and settings.ytm_client_secret):
        raise GoogleAuthError("YTM_CLIENT_ID and YTM_CLIENT_SECRET are missing from .env")

    state = secrets.token_urlsafe(16)
    _pending_states.add(state)

    query = urlencode(
        {
            "client_id": settings.ytm_client_id,
            "redirect_uri": settings.ytm_redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "state": state,
            # Both are required to be given a refresh token. Without them Google
            # returns only a one-hour access token and the app cannot come back
            # tomorrow to finish a large library.
            "access_type": "offline",
            "prompt": "consent",
        }
    )
    return f"{AUTH_URL}?{query}"


async def exchange_code(code: str, state: str | None) -> dict[str, Any]:
    """Turn the one-time code from the redirect into a stored refresh token."""
    if state is None or state not in _pending_states:
        raise GoogleAuthError("The login state did not match. Start the login again.")
    _pending_states.discard(state)

    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                TOKEN_URL,
                data={
                    "code": code,
                    "client_id": settings.ytm_client_id,
                    "client_secret": settings.ytm_client_secret,
                    "redirect_uri": settings.ytm_redirect_uri,
                    "grant_type": "authorization_code",
                },
            )
    except httpx.TransportError as exc:
        raise GoogleAuthError(describe_transport_error(exc, "Google")) from exc

    if response.status_code != 200:
        code_name, description = error_fields(response)
        raise GoogleAuthError(
            describe_oauth_error(
                "Google", response.status_code, code_name, description, settings.ytm_redirect_uri
            )
        )

    try:
        payload = response.json()
    except ValueError as exc:
        raise GoogleAuthError(describe_transport_error(exc, "Google")) from exc
    if not payload.get("refresh_token"):
        raise GoogleAuthError(
            "Google did not return a refresh token. Remove this app at "
            "https://myaccount.google.com/permissions and connect again."
        )

    ensure_data_dirs()
    YTM_OAUTH_FILE.write_text(json.dumps(payload, indent=2))
    YTM_OAUTH_FILE.chmod(0o600)
    return payload


def clear_token() -> None:
    YTM_OAUTH_FILE.unlink(missing_ok=True)
