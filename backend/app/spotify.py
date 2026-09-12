"""Spotify Web API client: login, search, and writing playlists.

Written against the API as it stands after the February/March 2026 changes.
The parts that differ from older tutorials:

* Adding songs is ``POST /playlists/{id}/items``. The old ``/tracks`` path is
  deprecated.
* In playlist responses the ``tracks`` field is now ``items`` and each row's
  ``track`` field is now ``item``. We read both spellings so the app keeps
  working whichever one the server sends.
* Search returns at most 10 results per call. It used to allow 50.
* Track objects no longer carry ``popularity`` or ``external_ids`` (ISRC).

The client secret lives only here, on the server. The browser never sees it.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import secrets
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import httpx

from .config import SPOTIFY_SCOPES, SPOTIFY_TOKEN_FILE, ensure_data_dirs, get_settings
from .errors import describe_oauth_error, describe_transport_error, error_fields
from .models import SpotifyCandidate

log = logging.getLogger(__name__)

AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

# Search allows at most 10 results per call since February 2026.
SEARCH_LIMIT = 10
# One request may add at most 100 songs to a playlist.
ADD_BATCH_SIZE = 100
# Give up on a single request after this many attempts.
MAX_ATTEMPTS = 8
# A request that gets no answer at all (no network, a dropped connection, a
# timeout) is retried this many times with short waits. After that the run
# stops with a plain reason, rather than marking every remaining song as an
# error while the Wi-Fi is off.
MAX_TRANSPORT_FAILURES = 4
# Spotify refuses playlist names longer than this with a bare 400.
MAX_PLAYLIST_NAME_LENGTH = 100
# A 429 whose Retry-After is at least this long is the daily quota, whatever
# Spotify calls it in the error body. A burst limit clears inside its short
# window and never asks for a wait of minutes, so a big Retry-After means
# today's allowance is gone: stop at once and say when to come back.
QUOTA_RETRY_AFTER_SECONDS = 300
# This app sends one request at a time with a gap between them, so a burst
# limit clears after one short wait. If Spotify still says "slow down" this
# many times in a row, the daily allowance is gone for practical purposes:
# stop with the quota message rather than retry into the same wall for an hour.
MAX_CONSECUTIVE_RATE_LIMITS = 3
# Refresh the access token this many seconds before it actually expires.
EXPIRY_MARGIN_SECONDS = 60


class SpotifyError(RuntimeError):
    """Raised when Spotify is not set up, or refuses a request for good."""


class SpotifyAuthRequired(SpotifyError):
    """Raised when there is no usable token and the user must log in again."""


class SpotifyQuotaExhausted(SpotifyError):
    """The daily quota is spent.

    Separate from an ordinary rate limit because the answer is different. A
    burst limit clears in seconds and is worth waiting out. A daily quota is
    hours away, so waiting inside the job only strands it: stop, keep the
    results, and say when to come back.
    """

    def __init__(self, message: str, resets_at: datetime | None = None) -> None:
        super().__init__(message)
        self.resets_at = resets_at


class SpotifyCancelled(SpotifyError):
    """Raised when the user stops the job while a request is waiting to retry."""


# ---------------------------------------------------------------------------
# Token storage. A small JSON file in backend/data, which is gitignored.
# ---------------------------------------------------------------------------


def load_token() -> dict[str, Any] | None:
    if not SPOTIFY_TOKEN_FILE.exists():
        return None
    try:
        return json.loads(SPOTIFY_TOKEN_FILE.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("Spotify token file is unreadable, ignoring it: %s", exc)
        return None


def save_token(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    """Store a token response, working out its absolute expiry time.

    A refresh response often omits ``refresh_token``. When that happens the old
    refresh token stays valid, so we carry it forward instead of losing it.
    """
    ensure_data_dirs()
    token = dict(payload)
    token["expires_at"] = time.time() + float(payload.get("expires_in", 3600))
    if not token.get("refresh_token") and previous:
        token["refresh_token"] = previous.get("refresh_token")
    SPOTIFY_TOKEN_FILE.write_text(json.dumps(token, indent=2))
    SPOTIFY_TOKEN_FILE.chmod(0o600)
    return token


def clear_token() -> None:
    SPOTIFY_TOKEN_FILE.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# The login round trip.
# ---------------------------------------------------------------------------

# Random values we have handed out but not yet seen come back. This guards
# against another site tricking the browser into completing a login (CSRF).
_pending_states: set[str] = set()


def build_login_url() -> str:
    settings = get_settings()
    if not settings.spotify_configured:
        raise SpotifyError("SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are missing from .env")

    state = secrets.token_urlsafe(16)
    _pending_states.add(state)

    query = urlencode(
        {
            "client_id": settings.spotify_client_id,
            "response_type": "code",
            "redirect_uri": settings.spotify_redirect_uri,
            "scope": SPOTIFY_SCOPES,
            "state": state,
            # Always show the consent screen, so switching account is possible.
            "show_dialog": "true",
        }
    )
    return f"{AUTH_URL}?{query}"


def _basic_auth_header() -> str:
    settings = get_settings()
    raw = f"{settings.spotify_client_id}:{settings.spotify_client_secret}".encode()
    return "Basic " + base64.b64encode(raw).decode()


async def exchange_code(code: str, state: str | None) -> dict[str, Any]:
    """Turn the one-time code from the redirect into a usable token."""
    if state is None or state not in _pending_states:
        raise SpotifyError(
            "The login state did not match. Start the login again from the app."
        )
    _pending_states.discard(state)

    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                TOKEN_URL,
                headers={"Authorization": _basic_auth_header()},
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": settings.spotify_redirect_uri,
                },
            )
    except httpx.TransportError as exc:
        raise SpotifyError(describe_transport_error(exc, "Spotify")) from exc

    if response.status_code != 200:
        code_name, description = error_fields(response)
        raise SpotifyError(
            describe_oauth_error(
                "Spotify", response.status_code, code_name, description, settings.spotify_redirect_uri
            )
        )
    try:
        return save_token(response.json())
    except ValueError as exc:
        raise SpotifyError(describe_transport_error(exc, "Spotify")) from exc


async def refresh_token(token: dict[str, Any]) -> dict[str, Any]:
    refresh = token.get("refresh_token")
    if not refresh:
        raise SpotifyAuthRequired("No refresh token stored. Connect Spotify again.")

    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                TOKEN_URL,
                headers={"Authorization": _basic_auth_header()},
                data={"grant_type": "refresh_token", "refresh_token": refresh},
            )
    except httpx.TransportError as exc:
        raise SpotifyError(describe_transport_error(exc, "Spotify")) from exc

    if response.status_code != 200:
        code_name, description = error_fields(response)
        message = describe_oauth_error(
            "Spotify", response.status_code, code_name, description, settings.spotify_redirect_uri
        )
        # A wrong secret in .env is not fixed by logging in again, so it must
        # not be reported as "connect again".
        if code_name in {"invalid_client", "unauthorized_client"}:
            raise SpotifyError(message)
        raise SpotifyAuthRequired(message)
    try:
        return save_token(response.json(), previous=token)
    except ValueError as exc:
        raise SpotifyError(describe_transport_error(exc, "Spotify")) from exc


# ---------------------------------------------------------------------------
# The API client.
# ---------------------------------------------------------------------------


class SpotifyClient:
    """Talks to the Spotify API, politely.

    Development Mode has low rate limits, so requests are capped to a couple at
    a time with a short gap between them, and a 429 response is obeyed rather
    than fought.

    Use it as an async context manager::

        async with SpotifyClient() as spotify:
            await spotify.search_tracks("...")
    """

    def __init__(
        self,
        on_rate_limit: "Callable[[float, str], None] | None" = None,
        should_cancel: "Callable[[], bool] | None" = None,
    ) -> None:
        # Called when a request is about to sit out a rate limit, so the
        # caller can show the wait instead of appearing to freeze.
        self._on_rate_limit = on_rate_limit
        # Checked while waiting, so Stop works during a long backoff.
        self._should_cancel = should_cancel
        settings = get_settings()
        self._settings = settings
        self._token = load_token()
        self._semaphore = asyncio.Semaphore(max(1, settings.spotify_max_concurrency))
        self._delay = max(0, settings.spotify_request_delay_ms) / 1000
        self._refresh_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(base_url=API_BASE, timeout=30)

    async def __aenter__(self) -> "SpotifyClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self._client.aclose()

    async def close(self) -> None:
        await self._client.aclose()

    # -- authentication -----------------------------------------------------

    async def _access_token(self) -> str:
        if not self._token:
            raise SpotifyAuthRequired("Spotify is not connected. Click 'Connect Spotify'.")

        # One coroutine refreshes; the rest wait for it rather than all firing
        # their own refresh request at the same moment.
        async with self._refresh_lock:
            expires_at = float(self._token.get("expires_at", 0))
            if time.time() >= expires_at - EXPIRY_MARGIN_SECONDS:
                log.info("Spotify access token expired, refreshing")
                self._token = await refresh_token(self._token)

        return self._token["access_token"]

    # -- the request loop ---------------------------------------------------

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Send one request, handling rate limits, outages and expiry.

        Holding the semaphore across the retry sleeps is deliberate: when
        Spotify tells us to slow down, everything slows down.
        """
        last_error = ""
        consecutive_rate_limits = 0
        transport_failures = 0

        async with self._semaphore:
            for attempt in range(MAX_ATTEMPTS):
                token = await self._access_token()
                try:
                    response = await self._client.request(
                        method,
                        path,
                        params=params,
                        json=json_body,
                        headers={"Authorization": f"Bearer {token}"},
                    )
                except httpx.TransportError as exc:
                    # No answer at all: the network is down, the connection
                    # dropped, or Spotify took too long. A few short retries
                    # cover a blip; after that the honest answer is to stop
                    # and say so, not to record a thousand "errors".
                    transport_failures += 1
                    last_error = describe_transport_error(exc, "Spotify")
                    if transport_failures >= MAX_TRANSPORT_FAILURES:
                        raise SpotifyError(
                            f"{last_error} The transfer stopped here so that nothing is "
                            "marked as not found by mistake. Every song matched so far "
                            "is saved - press Resume once you are back online."
                        ) from exc
                    wait = 2**transport_failures
                    log.warning(
                        "Spotify unreachable (%s). Retrying in %ss (%d/%d)",
                        exc.__class__.__name__,
                        wait,
                        transport_failures,
                        MAX_TRANSPORT_FAILURES,
                    )
                    await self._sleep(wait)
                    continue

                # Too many requests. Spotify tells us exactly how long to wait.
                if response.status_code == 429:
                    consecutive_rate_limits += 1
                    reason = _error_reason(response)
                    raw_wait = _raw_retry_after(response)

                    # A daily quota does not free up in minutes. Retrying only
                    # spends half an hour to reach the same answer, so stop at
                    # the first one and let the caller save what it has.
                    # Spotify names the quota in the error body on some days and
                    # only in a huge Retry-After on others, so trust either.
                    if "QUOTA" in reason.upper() or (raw_wait or 0) >= QUOTA_RETRY_AFTER_SECONDS:
                        resets_at = _quota_reset_at(response)
                        log.warning(
                            "Spotify daily quota exhausted on %s (reason=%r, retry-after=%s)",
                            path,
                            reason,
                            raw_wait,
                        )
                        raise SpotifyQuotaExhausted(
                            "Spotify's daily limit for this app is used up, so the "
                            "transfer stopped here. Everything matched so far is "
                            "saved, and your Spotify playlists are untouched. The "
                            "limit resets about once a day - come back then and "
                            "press Resume to carry on.",
                            resets_at,
                        )

                    # Three refusals in a row, despite this app sending one
                    # polite request at a time, is not a passing rush: today's
                    # allowance is gone. Stop now with the same clear message.
                    if consecutive_rate_limits >= MAX_CONSECUTIVE_RATE_LIMITS:
                        resets_at = _quota_reset_at(response)
                        log.warning(
                            "Spotify rate-limited %s %d times in a row; treating it as the daily quota",
                            path,
                            consecutive_rate_limits,
                        )
                        raise SpotifyQuotaExhausted(
                            "Spotify kept refusing this app's requests with 'slow "
                            "down'. That is the daily rate limit, not a passing "
                            "rush. Everything matched so far is saved, and your "
                            "Spotify playlists are untouched. The limit resets "
                            "about once a day - come back then and press Resume "
                            "to carry on.",
                            resets_at,
                        )

                    wait = _retry_after_seconds(response)
                    log.warning(
                        "Spotify rate limit (%s). Waiting %.0fs before retry %d/%d of %s",
                        reason or "rate limited",
                        wait,
                        attempt + 1,
                        MAX_ATTEMPTS,
                        path,
                    )
                    if self._on_rate_limit:
                        self._on_rate_limit(wait, reason or "rate limited")
                    await self._sleep(wait)
                    last_error = f"429 {reason}"
                    continue

                # Anything other than a 429 means the burst, if there was one,
                # has cleared.
                consecutive_rate_limits = 0

                # The token expired between our check and the request.
                if response.status_code == 401:
                    if attempt < MAX_ATTEMPTS - 1:
                        async with self._refresh_lock:
                            self._token = await refresh_token(self._token or {})
                        last_error = "401 unauthorised"
                        continue
                    raise SpotifyAuthRequired(
                        "Spotify no longer accepts this login, even after refreshing "
                        "it. Press Connect Spotify again."
                    )

                # Spotify's own problem. Back off and try again.
                if response.status_code >= 500:
                    wait = 2**attempt
                    log.warning("Spotify returned %s, retrying in %ss", response.status_code, wait)
                    await self._sleep(wait)
                    last_error = f"{response.status_code} server error"
                    continue

                if response.status_code >= 400:
                    raise SpotifyError(_explain_refusal(method, path, response))

                # Be polite even when things go well.
                if self._delay:
                    await asyncio.sleep(self._delay)

                if not response.content:
                    return {}
                try:
                    return response.json()
                except ValueError as exc:
                    raise SpotifyError(describe_transport_error(exc, "Spotify")) from exc

        raise SpotifyError(
            f"Gave up on {method} {path} after {MAX_ATTEMPTS} attempts "
            f"({last_error}). This is almost always Spotify's daily rate limit. "
            "Come back later and press Resume - every song matched so far is saved."
        )

    async def _sleep(self, seconds: float) -> None:
        """Wait, but notice a Stop press within a second of it happening.

        A single long sleep would swallow the cancel flag for its whole
        duration, which is how a ten-minute backoff turns into a Stop button
        that appears to do nothing.
        """
        remaining = seconds
        while remaining > 0:
            if self._should_cancel and self._should_cancel():
                raise SpotifyCancelled("Stopped while waiting for Spotify.")
            step = min(1.0, remaining)
            await asyncio.sleep(step)
            remaining -= step

    # -- the operations we actually need ------------------------------------

    async def current_user(self) -> dict[str, Any]:
        return await self._request("GET", "/me")

    async def search_tracks(self, query: str, limit: int = SEARCH_LIMIT) -> list[SpotifyCandidate]:
        """Search for songs and return them as candidates, unscored."""
        payload = await self._request(
            "GET",
            "/search",
            params={"q": query, "type": "track", "limit": min(limit, SEARCH_LIMIT)},
        )
        items = (payload.get("tracks") or {}).get("items") or []
        return [c for c in (_to_candidate(item) for item in items) if c is not None]

    async def find_playlist_by_name(self, name: str) -> dict[str, Any] | None:
        """Look for a playlist we already made, so repeat runs reuse it."""
        wanted = name.strip().lower()
        path: str | None = "/me/playlists"
        params: dict[str, Any] | None = {"limit": 50}

        while path:
            payload = await self._request("GET", path, params=params)
            for playlist in payload.get("items") or []:
                if (playlist.get("name") or "").strip().lower() == wanted:
                    return playlist
            path = _next_path(payload)
            params = None
        return None

    async def create_playlist(self, name: str, description: str = "") -> dict[str, Any]:
        """Create a private playlist owned by the logged-in user.

        The name is trimmed to Spotify's limit, because a YouTube playlist can
        be called anything and a bare 400 half-way through a run helps nobody.
        """
        return await self._request(
            "POST",
            "/me/playlists",
            json_body={
                "name": name[:MAX_PLAYLIST_NAME_LENGTH],
                "public": False,
                "description": description,
            },
        )

    async def get_playlist_track_ids(self, playlist_id: str) -> set[str]:
        """Every track id already in a playlist, so we never add a duplicate."""
        found: set[str] = set()
        path: str | None = f"/playlists/{playlist_id}/items"
        params: dict[str, Any] | None = {"limit": 50}

        while path:
            payload = await self._request("GET", path, params=params)
            for row in payload.get("items") or []:
                # 2026 renamed this field from "track" to "item"; accept either.
                entry = row.get("item") or row.get("track") or {}
                track_id = entry.get("id")
                if track_id:
                    found.add(track_id)
            path = _next_path(payload)
            params = None
        return found

    async def add_tracks(self, playlist_id: str, uris: list[str]) -> int:
        """Add songs in batches of 100. Returns how many were sent."""
        added = 0
        for start in range(0, len(uris), ADD_BATCH_SIZE):
            batch = uris[start : start + ADD_BATCH_SIZE]
            await self._request(
                "POST",
                f"/playlists/{playlist_id}/items",
                json_body={"uris": batch},
            )
            added += len(batch)
        return added


# ---------------------------------------------------------------------------
# Small helpers.
# ---------------------------------------------------------------------------


def _explain_refusal(method: str, path: str, response: httpx.Response) -> str:
    """One sentence for a 4xx, naming the likely cause and the fix.

    Spotify's own message is quoted when it has one, but never on its own:
    ``{"error":{"status":404,"message":"Invalid base62 id"}}`` tells a
    first-time user nothing.
    """
    status = response.status_code
    _reason, message = error_fields(response)
    said = f' Spotify said: "{message}".' if message else ""

    if status == 403:
        return (
            "Spotify returned 403 Forbidden. The usual causes are: your account is "
            "not on the app's allowlist under User Management in the developer "
            "dashboard, or the app owner does not have Premium, which Development "
            f"Mode has required since February 2026.{said}"
        )
    if status == 404:
        return (
            "Spotify says that no longer exists. If you deleted a playlist on "
            "Spotify while the transfer was running, run it again and the playlist "
            f"will be recreated.{said}"
        )
    if status == 400:
        return (
            f"Spotify rejected {method} {path} as invalid. This is a fault in the "
            f"app rather than in your setup - please report it with this message.{said}"
        )
    return f"Spotify rejected {method} {path} with HTTP {status}.{said or ' No reason was given.'}"


def _retry_after_seconds(response: httpx.Response) -> float:
    """Read the Retry-After header, with a sane floor and ceiling."""
    raw = response.headers.get("Retry-After", "")
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = 5.0
    # Add a second of headroom; never sleep longer than ten minutes.
    return min(max(seconds + 1, 1.0), 600.0)


def _raw_retry_after(response: httpx.Response) -> float | None:
    """The Retry-After header exactly as Spotify sent it, uncapped.

    None when the header is missing or is not a number. The capped version
    above is for ordinary backoff; decisions about the daily quota need the
    real value, which can be hours.
    """
    try:
        return float(response.headers.get("Retry-After", ""))
    except (TypeError, ValueError):
        return None


def _quota_reset_at(response: httpx.Response) -> datetime | None:
    """When the daily quota comes back, read from the raw Retry-After header.

    Spotify often sends a uselessly short value on a quota error, so anything
    under a minute is treated as "no idea" rather than shown to the user as a
    countdown that expires into the same error.
    """
    seconds = _raw_retry_after(response)
    if seconds is None or seconds < 60:
        return None
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


def _error_reason(response: httpx.Response) -> str:
    """Pull the machine-readable reason out of an error body, if there is one.

    Since July 2026 a 429 carries {"error": {"reason": "QUOTA_EXCEEDED"}}.
    """
    try:
        body = response.json()
    except (json.JSONDecodeError, ValueError):
        return ""
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("reason") or error.get("message") or "")
    return str(error or "")


def _next_path(payload: dict[str, Any]) -> str | None:
    """Turn a paging object's absolute 'next' URL into a path for our client."""
    nxt = payload.get("next")
    if not nxt:
        return None
    return str(nxt).replace(API_BASE, "", 1)


def _to_candidate(item: dict[str, Any]) -> SpotifyCandidate | None:
    """Convert one Spotify track object into our model."""
    if not isinstance(item, dict) or not item.get("id"):
        return None

    album = item.get("album")
    album_name = album.get("name") if isinstance(album, dict) else None

    duration_ms = item.get("duration_ms")
    duration_seconds = round(duration_ms / 1000) if isinstance(duration_ms, (int, float)) else None

    return SpotifyCandidate(
        id=item["id"],
        uri=item.get("uri") or f"spotify:track:{item['id']}",
        title=item.get("name") or "",
        artists=[
            (artist.get("name") or "").strip()
            for artist in (item.get("artists") or [])
            if isinstance(artist, dict) and artist.get("name")
        ],
        album=album_name,
        duration_seconds=duration_seconds,
        url=(item.get("external_urls") or {}).get("spotify"),
    )


async def check_connection() -> tuple[bool, str, str | None]:
    """Used by the connect screen. Never raises; returns a message instead."""
    settings = get_settings()
    if not settings.spotify_configured:
        return False, "SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are missing from .env", None
    if load_token() is None:
        return False, "Not connected yet.", None

    try:
        async with SpotifyClient() as client:
            me = await client.current_user()
        name = me.get("display_name") or me.get("id") or "Connected"
        return True, f"Signed in as {name}", name
    except SpotifyError as exc:
        return False, str(exc), None
    except Exception as exc:  # noqa: BLE001 - surface any failure as readable text
        return False, describe_transport_error(exc, "Spotify"), None
