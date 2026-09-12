"""Read playlists and liked music from YouTube, using the official Data API v3.

Why the official API and not ytmusicapi
---------------------------------------
ytmusicapi talks to YouTube Music's private endpoints. Those endpoints reject
OAuth tokens issued to a self-made Google Cloud client:

    WEB_REMIX + Bearer token  ->  HTTP 400 INVALID_ARGUMENT

The cause is a client mismatch. The OAuth token is issued to a TV client, but
ytmusicapi always sends ``clientName: WEB_REMIX``. Sending ``TVHTML5`` instead
does return HTTP 200, but the TV surface answers with ``tvBrowseRenderer``,
a completely different shape that ytmusicapi cannot parse. Its other login
method, browser cookies, works but means copying request headers by hand and
is marked deprecated upstream.

The official YouTube Data API has none of those problems. It accepts the same
OAuth token, it is documented and stable, and YouTube Music playlists are
ordinary YouTube playlists underneath - including "Liked Music", which is the
playlist with the id ``LM``.

What we give up, and why it does not matter
-------------------------------------------
The Data API describes videos rather than songs, so there is no album name.
Album agreement was only a 0.05 bonus in the matcher, so its loss is small.
Everything that carries real weight is still here:

* **title**   - the song name
* **artist**  - from the channel. YouTube auto-generates an "<Artist> - Topic"
  channel for licensed music, so the artist comes out clean.
* **duration** - from contentDetails, which the matcher relies on to reject
  remixes and live takes.

Quota cost is negligible: one unit per 50 items, against a daily 10,000.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable
from typing import Any

import httpx

from .config import YTM_OAUTH_FILE, get_settings
from .errors import describe_transport_error, error_fields
from .models import PlaylistSummary, YtTrack

log = logging.getLogger(__name__)

API_BASE = "https://www.googleapis.com/youtube/v3"
TOKEN_URL = "https://oauth2.googleapis.com/token"

# YouTube Music's "Liked Music" playlist. Distinct from "LL", which is the
# thumbs-up list for ordinary YouTube videos.
LIKED_SONGS_ID = "LM"

# The Data API returns at most 50 items per page.
PAGE_SIZE = 50

# categoryId 10 is Music. Playlists often hold podcasts or tutorials too, and
# there is no point searching Spotify for a two-hour conference talk.
MUSIC_CATEGORY_ID = "10"

# Rows the API returns for content that no longer exists.
DEAD_VIDEO_TITLES = {"Deleted video", "Private video", "[Deleted video]", "[Private video]"}

# One request is tried this many times when YouTube is down or unreachable,
# with a short wait between goes. The quota is not touched by a failed call.
MAX_ATTEMPTS = 4

_ISO_DURATION = re.compile(
    r"P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?"
)


class YouTubeMusicError(RuntimeError):
    """Raised when YouTube is not set up, or refuses the request."""


class PlaylistNotFound(YouTubeMusicError):
    """Raised when a playlist has been deleted or is unavailable.

    Separate from other failures because the answer is different: one missing
    playlist must not stop a transfer that covers the rest of the library, so
    the caller skips it and carries on.
    """


class YouTubeAuthRequired(YouTubeMusicError):
    """The saved login no longer works and the user must press Connect again."""


class YouTubeNotFound(YouTubeMusicError):
    """YouTube answered 404: the thing asked for is not there any more."""


# ---------------------------------------------------------------------------
# Token handling.
# ---------------------------------------------------------------------------

# Cached access token, so a whole transfer does not refresh on every call.
_access_token: str | None = None
_expires_at: float = 0.0


async def _get_access_token(client: httpx.AsyncClient) -> str:
    """Return a live access token, refreshing it when it is close to expiry."""
    global _access_token, _expires_at

    if _access_token and time.time() < _expires_at - 60:
        return _access_token

    settings = get_settings()
    if not YTM_OAUTH_FILE.exists():
        raise YouTubeMusicError(
            "No YouTube login found. Press Connect YouTube in the app."
        )
    if not (settings.ytm_client_id and settings.ytm_client_secret):
        raise YouTubeMusicError("YTM_CLIENT_ID and YTM_CLIENT_SECRET are missing from .env")

    try:
        stored = json.loads(YTM_OAUTH_FILE.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise YouTubeMusicError(f"Could not read {YTM_OAUTH_FILE.name}: {exc}") from exc

    refresh = stored.get("refresh_token")
    if not refresh:
        raise YouTubeMusicError(
            f"{YTM_OAUTH_FILE.name} has no refresh token. Press Connect YouTube in the app again."
        )

    try:
        response = await client.post(
            TOKEN_URL,
            data={
                "client_id": settings.ytm_client_id,
                "client_secret": settings.ytm_client_secret,
                "refresh_token": refresh,
                "grant_type": "refresh_token",
            },
        )
    except httpx.TransportError as exc:
        raise YouTubeMusicError(describe_transport_error(exc, "Google")) from exc

    if response.status_code != 200:
        code, description = error_fields(response)
        said = f' Google said: "{description}".' if description else ""
        if code in {"invalid_client", "unauthorized_client"}:
            # Logging in again cannot fix a wrong secret, so say what can.
            raise YouTubeMusicError(
                "Google does not recognise YTM_CLIENT_ID or YTM_CLIENT_SECRET in "
                ".env. Copy both again from Credentials in the Google Cloud "
                f"Console, save .env, and restart the app.{said}"
            )
        if code == "invalid_grant":
            # A Google project in "Testing" status issues refresh tokens that
            # die after 7 days, which is the usual reason to land here.
            raise YouTubeAuthRequired(
                "The saved YouTube login has expired or was revoked. Google "
                "expires these after 7 days while the project is in Testing. "
                f"Press Connect YouTube in the app again.{said}"
            )
        raise YouTubeAuthRequired(
            f"Could not refresh the YouTube login (HTTP {response.status_code}"
            f"{' ' + code if code else ''}).{said} Press Connect YouTube in the app again."
        )

    try:
        payload = response.json()
        _access_token = payload["access_token"]
    except (ValueError, KeyError, TypeError) as exc:
        raise YouTubeMusicError(
            "Google's login server answered without an access token. Try again in a "
            "minute; if it keeps happening, press Connect YouTube again."
        ) from exc
    _expires_at = time.time() + float(payload.get("expires_in", 3600))
    return _access_token


# ---------------------------------------------------------------------------
# Talking to the API.
# ---------------------------------------------------------------------------


async def _get(client: httpx.AsyncClient, path: str, params: dict[str, Any]) -> dict[str, Any]:
    """One GET against the Data API, retried through outages, explained on refusal."""
    global _access_token

    last_error = ""
    for attempt in range(MAX_ATTEMPTS):
        token = await _get_access_token(client)
        try:
            response = await client.get(
                f"{API_BASE}/{path}", params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.TransportError as exc:
            last_error = describe_transport_error(exc, "YouTube")
            if attempt == MAX_ATTEMPTS - 1:
                raise YouTubeMusicError(last_error) from exc
            wait = 2**attempt
            log.warning("YouTube unreachable (%s). Retrying in %ss", exc.__class__.__name__, wait)
            await asyncio.sleep(wait)
            continue

        # YouTube's own problem. Back off and try again.
        if response.status_code >= 500:
            last_error = f"YouTube returned HTTP {response.status_code}"
            if attempt == MAX_ATTEMPTS - 1:
                raise YouTubeMusicError(
                    f"{last_error} {MAX_ATTEMPTS} times in a row. It is having trouble; "
                    "try again in a few minutes."
                )
            wait = 2**attempt
            log.warning("%s, retrying in %ss", last_error, wait)
            await asyncio.sleep(wait)
            continue

        if response.status_code == 401:
            # The cached token is no good. Drop it so the next call refreshes,
            # and tell the user what happened in case that fails too.
            _access_token = None
            raise YouTubeAuthRequired(
                "YouTube no longer accepts the saved login. Press Connect YouTube "
                "in the app again."
            )

        if response.status_code == 403:
            raise YouTubeMusicError(_explain_forbidden(response))

        if response.status_code == 404:
            _reason, message = error_fields(response)
            raise YouTubeNotFound(
                f"YouTube has nothing at {path} any more."
                + (f' It said: "{message}".' if message else "")
            )

        if response.status_code >= 400:
            reason, message = error_fields(response)
            where = f" {reason}" if reason else ""
            raise YouTubeMusicError(
                f"YouTube rejected the request for {path} (HTTP {response.status_code}{where})."
                + (f' It said: "{message}".' if message else " No reason was given.")
            )

        try:
            return response.json()
        except ValueError as exc:
            raise YouTubeMusicError(describe_transport_error(exc, "YouTube")) from exc

    raise YouTubeMusicError(last_error or f"YouTube did not answer {path}.")


def _explain_forbidden(response: httpx.Response) -> str:
    """A 403 from the Data API has three common causes with three different fixes."""
    reason, message = error_fields(response)
    said = f' YouTube said: "{message}".' if message else ""

    if reason in {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}:
        return (
            "YouTube's daily API quota for your Google project is used up. It "
            "resets at midnight Pacific time. Try again tomorrow, or raise the "
            f"quota under APIs & Services in the Google Cloud Console.{said}"
        )
    if reason == "accessNotConfigured" or "has not been used" in message:
        return (
            "The YouTube Data API v3 is not enabled on your Google Cloud project. "
            "Open APIs & Services, search for YouTube Data API v3, press Enable, "
            f"then try again.{said}"
        )
    if reason in {"playlistForbidden", "playlistItemsNotAccessible", "forbidden"}:
        return (
            "YouTube will not let this account read that playlist. It may be "
            f"private and belong to someone else.{said}"
        )
    return (
        f"YouTube returned 403 Forbidden{' (' + reason + ')' if reason else ''}. "
        "The usual causes are a spent daily quota or the YouTube Data API v3 not "
        f"being enabled on the Google Cloud project.{said}"
    )


async def _paginate(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, Any],
    raise_if_cancelled: Callable[[], None] | None = None,
) -> list[dict[str, Any]]:
    """Follow nextPageToken until the whole list is collected.

    ``raise_if_cancelled`` is called before every page, so a Stop press is
    honoured inside a long read rather than after it.
    """
    items: list[dict[str, Any]] = []
    page_token: str | None = None

    while True:
        if raise_if_cancelled:
            raise_if_cancelled()
        page_params = {**params, "maxResults": PAGE_SIZE}
        if page_token:
            page_params["pageToken"] = page_token
        payload = await _get(client, path, page_params)
        items.extend(payload.get("items", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            return items


# ---------------------------------------------------------------------------
# Turning API rows into our models.
# ---------------------------------------------------------------------------


def parse_duration(iso: str | None) -> int | None:
    """Convert an ISO 8601 duration such as "PT4M1S" into seconds."""
    if not iso:
        return None
    match = _ISO_DURATION.fullmatch(iso)
    if not match:
        return None
    parts = {k: int(v) for k, v in match.groupdict(default="0").items()}
    total = (
        parts["days"] * 86400 + parts["hours"] * 3600 + parts["minutes"] * 60 + parts["seconds"]
    )
    return total or None


def artist_from_channel(channel: str | None) -> str:
    """Get the artist out of a channel name.

    YouTube creates an auto-generated channel per artist for licensed music,
    always named "<Artist> - Topic". When that suffix is present the channel is
    the artist. When it is absent the channel is a person or a label, so it is
    not treated as an artist name; the title usually carries "Artist - Song"
    instead, and the matcher pulls it apart from there.
    """
    if not channel:
        return ""
    if channel.endswith(" - Topic"):
        return channel[: -len(" - Topic")].strip()
    return ""


def _to_track(video: dict[str, Any]) -> YtTrack | None:
    """Convert one videos.list row into a track, or None if it is not usable."""
    snippet = video.get("snippet") or {}
    title = (snippet.get("title") or "").strip()
    if not title or title in DEAD_VIDEO_TITLES:
        return None

    # Skip anything that is not music: podcasts, tutorials, conference talks.
    if snippet.get("categoryId") != MUSIC_CATEGORY_ID:
        return None

    channel = snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle")
    artist = artist_from_channel(channel)

    return YtTrack(
        video_id=video.get("id") or "",
        title=title,
        artists=[artist] if artist else [],
        album=None,  # The Data API describes videos, so there is no album.
        duration_seconds=parse_duration((video.get("contentDetails") or {}).get("duration")),
    )


async def _hydrate(
    client: httpx.AsyncClient,
    video_ids: list[str],
    raise_if_cancelled: Callable[[], None] | None = None,
) -> list[YtTrack]:
    """Fetch full details for a list of video ids, 50 at a time."""
    tracks: list[YtTrack] = []
    for start in range(0, len(video_ids), PAGE_SIZE):
        if raise_if_cancelled:
            raise_if_cancelled()
        batch = video_ids[start : start + PAGE_SIZE]
        payload = await _get(
            client,
            "videos",
            {"part": "snippet,contentDetails", "id": ",".join(batch), "maxResults": PAGE_SIZE},
        )
        for video in payload.get("items", []):
            track = _to_track(video)
            if track is not None:
                tracks.append(track)
    return tracks


# ---------------------------------------------------------------------------
# The public interface. Unchanged from the ytmusicapi version, so the transfer
# engine and the routes did not need touching.
# ---------------------------------------------------------------------------


async def list_playlists() -> list[PlaylistSummary]:
    """Every playlist you own, plus a "Liked music" entry for the LM playlist."""
    async with httpx.AsyncClient(timeout=30) as client:
        raw = await _paginate(client, "playlists", {"part": "snippet,contentDetails", "mine": "true"})

        summaries = [
            PlaylistSummary(
                id=item["id"],
                title=(item.get("snippet", {}).get("title") or "Untitled").strip(),
                track_count=(item.get("contentDetails") or {}).get("itemCount"),
            )
            for item in raw
            if item.get("id")
        ]
        summaries.sort(key=lambda item: item.title.lower())

        liked_count = None
        try:
            payload = await _get(
                client,
                "playlistItems",
                {"part": "id", "playlistId": LIKED_SONGS_ID, "maxResults": 1},
            )
            liked_count = (payload.get("pageInfo") or {}).get("totalResults")
        except YouTubeMusicError as exc:
            log.warning("Could not count liked music: %s", exc)

        summaries.insert(
            0,
            PlaylistSummary(
                id=LIKED_SONGS_ID,
                title="Liked music",
                track_count=liked_count,
                is_liked_songs=True,
            ),
        )
        return summaries


async def get_playlist_tracks(
    playlist_id: str,
    raise_if_cancelled: Callable[[], None] | None = None,
) -> tuple[str, list[YtTrack]]:
    """Return the playlist title and every music track in it.

    ``raise_if_cancelled`` is called between pages so a Stop press is felt
    within a page, even while a large playlist is being read.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        if playlist_id == LIKED_SONGS_ID:
            title = "Liked music"
        else:
            payload = await _get(
                client, "playlists", {"part": "snippet", "id": playlist_id, "maxResults": 1}
            )
            items = payload.get("items") or []
            if not items:
                raise PlaylistNotFound(
                    f"Playlist {playlist_id} was not found on your account. "
                    "It may have been deleted."
                )
            title = (items[0].get("snippet", {}).get("title") or "Untitled playlist").strip()

        # YouTube sometimes lists a playlist but then refuses to read its
        # contents, usually a moment after it was deleted or made private.
        # Say so in plain words rather than passing the API's JSON through.
        try:
            rows = await _paginate(
                client,
                "playlistItems",
                {"part": "snippet", "playlistId": playlist_id},
                raise_if_cancelled,
            )
        except YouTubeNotFound as exc:
            raise PlaylistNotFound(
                f"Playlist {playlist_id} is no longer available on YouTube Music. "
                "It may have been deleted or made private."
            ) from exc

        video_ids = [
            vid
            for row in rows
            if (vid := (row.get("snippet", {}).get("resourceId") or {}).get("videoId"))
        ]

        tracks = await _hydrate(client, video_ids, raise_if_cancelled)

        dropped = len(video_ids) - len(tracks)
        if dropped:
            log.info("'%s': skipped %d non-music or unavailable items", title, dropped)

        return title, tracks


async def check_connection() -> tuple[bool, str]:
    """Used by the connect screen. Never raises; returns a message instead."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            payload = await _get(client, "channels", {"part": "snippet", "mine": "true"})
        items = payload.get("items") or []
        name = items[0]["snippet"]["title"] if items else "Connected"
        return True, f"Signed in as {name}"
    except YouTubeMusicError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001 - surface any failure as readable text
        return False, describe_transport_error(exc, "YouTube")
