"""Reading the YouTube Music library."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from .. import ytmusic
from ..errors import describe_transport_error
from ..models import PlaylistSummary

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("/playlists", response_model=list[PlaylistSummary])
async def playlists() -> list[PlaylistSummary]:
    """Every playlist in your YouTube Music library, plus a liked-songs entry.

    The status code tells the UI what kind of problem it is: 401 means "go
    and log in again", 400 is something to fix in the setup, 502 is YouTube
    or the network, and the detail is always a full sentence.
    """
    try:
        return await ytmusic.list_playlists()
    except ytmusic.YouTubeAuthRequired as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except ytmusic.YouTubeMusicError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - turn any library failure into a clear 502
        raise HTTPException(
            status_code=502, detail=describe_transport_error(exc, "YouTube")
        ) from exc
