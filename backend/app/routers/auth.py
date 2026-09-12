"""Connection status and the Spotify login round trip."""

from __future__ import annotations

import logging
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException
from fastapi.responses import RedirectResponse

from .. import spotify, youtube_auth, ytmusic
from ..config import get_settings
from ..errors import describe_consent_error
from ..models import AuthStatus

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


@router.get("/status", response_model=AuthStatus)
async def status() -> AuthStatus:
    """What the first screen shows: which side is connected, and what is missing."""
    settings = get_settings()
    yt_ok, yt_detail = await ytmusic.check_connection()
    sp_ok, sp_detail, sp_user = await spotify.check_connection()
    return AuthStatus(
        youtube_connected=yt_ok,
        youtube_detail=yt_detail,
        spotify_connected=sp_ok,
        spotify_detail=sp_detail,
        spotify_user=sp_user,
        youtube_configured=settings.ytm_keys_present,
        spotify_configured=settings.spotify_configured,
    )


@router.get("/spotify/login")
async def spotify_login() -> RedirectResponse:
    """Send the browser to Spotify's consent screen."""
    try:
        return RedirectResponse(spotify.build_login_url())
    except spotify.SpotifyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/spotify/callback")
async def spotify_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Where Spotify sends the browser back to.

    This URL must be registered in the Spotify dashboard exactly as
    ``http://127.0.0.1:8000/api/auth/spotify/callback``. Spotify rejects
    ``localhost`` as a hostname, so the IP address is not optional.

    The outcome is passed back to the UI in the query string rather than shown
    here, because the user should end up in the app, not on a bare JSON page.
    """
    frontend = get_settings().frontend_url.rstrip("/")

    if error:
        message = describe_consent_error("Spotify", error)
        return RedirectResponse(f"{frontend}/?{urlencode({'spotify_error': message})}")
    if not code:
        return RedirectResponse(
            f"{frontend}/?{urlencode({'spotify_error': 'Spotify sent the browser back without a login code. Press Connect Spotify again.'})}"
        )

    try:
        await spotify.exchange_code(code, state)
    except spotify.SpotifyError as exc:
        return RedirectResponse(f"{frontend}/?{urlencode({'spotify_error': str(exc)})}")

    return RedirectResponse(f"{frontend}/?spotify=connected")


@router.get("/youtube/login")
async def youtube_login() -> RedirectResponse:
    """Send the browser to Google's consent screen."""
    try:
        return RedirectResponse(youtube_auth.build_login_url())
    except youtube_auth.GoogleAuthError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/youtube/callback")
async def youtube_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Where Google sends the browser back to.

    This URL must be registered on the Google client as an Authorised redirect
    URI, exactly as written in .env.
    """
    frontend = get_settings().frontend_url.rstrip("/")

    if error:
        message = describe_consent_error("Google", error)
        return RedirectResponse(f"{frontend}/?{urlencode({'youtube_error': message})}")
    if not code:
        return RedirectResponse(
            f"{frontend}/?{urlencode({'youtube_error': 'Google sent the browser back without a login code. Press Connect YouTube again.'})}"
        )

    try:
        await youtube_auth.exchange_code(code, state)
    except youtube_auth.GoogleAuthError as exc:
        return RedirectResponse(f"{frontend}/?{urlencode({'youtube_error': str(exc)})}")

    return RedirectResponse(f"{frontend}/?youtube=connected")


@router.post("/youtube/logout")
async def youtube_logout() -> dict[str, bool]:
    """Delete the stored YouTube token."""
    youtube_auth.clear_token()
    return {"ok": True}


@router.post("/spotify/logout")
async def spotify_logout() -> dict[str, bool]:
    """Delete the stored token. Useful when switching accounts."""
    spotify.clear_token()
    return {"ok": True}
