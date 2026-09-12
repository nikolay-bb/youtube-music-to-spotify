"""Data shapes shared by every layer of the app.

These are pydantic models, so they validate themselves, serialise straight to
JSON for the job files on disk, and give the FastAPI routes their response
schemas for free.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["matched", "low_confidence", "not_found", "error"]
JobStatus = Literal["pending", "running", "completed", "cancelled", "failed"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


class YtTrack(BaseModel):
    """One song as YouTube Music describes it."""

    video_id: str
    title: str
    artists: list[str] = Field(default_factory=list)
    album: str | None = None
    duration_seconds: int | None = None

    @property
    def primary_artist(self) -> str:
        return self.artists[0] if self.artists else ""

    def label(self) -> str:
        """Human readable one-liner, used in reports and logs."""
        return f"{', '.join(self.artists)} - {self.title}" if self.artists else self.title


class SpotifyCandidate(BaseModel):
    """One search result from Spotify, plus the score we gave it."""

    id: str
    uri: str
    title: str
    artists: list[str] = Field(default_factory=list)
    album: str | None = None
    duration_seconds: int | None = None
    url: str | None = None
    score: float = 0.0

    def label(self) -> str:
        return f"{', '.join(self.artists)} - {self.title}" if self.artists else self.title


class MatchResult(BaseModel):
    """What happened to one YouTube song."""

    source: YtTrack
    verdict: Verdict
    best: SpotifyCandidate | None = None
    score: float = 0.0
    # Why we ended up here, in plain words, so the report explains itself.
    reason: str = ""
    # Which playlist this row belongs to, for the CSV and the UI filters.
    playlist_name: str = ""


class PlaylistSummary(BaseModel):
    """One YouTube Music playlist as shown on the selection screen."""

    id: str
    title: str
    track_count: int | None = None
    # True for the synthetic "Liked songs" entry, which is not a real playlist.
    is_liked_songs: bool = False


class TransferRequest(BaseModel):
    """What the UI sends to start a job."""

    playlist_ids: list[str] = Field(default_factory=list)
    include_liked: bool = False
    # Search and score everything, but write nothing to Spotify.
    dry_run: bool = True


class PlaylistProgress(BaseModel):
    source_id: str
    source_title: str
    spotify_playlist_id: str | None = None
    spotify_playlist_url: str | None = None
    total: int = 0
    processed: int = 0
    added: int = 0
    skipped_duplicates: int = 0
    # Spotify search requests spent. The daily quota is on this number, not
    # on the number of songs, so it is the one worth watching.
    searches: int = 0


class JobCounters(BaseModel):
    total: int = 0
    processed: int = 0
    matched: int = 0
    low_confidence: int = 0
    not_found: int = 0
    errors: int = 0
    added: int = 0
    skipped_duplicates: int = 0
    # Spotify search requests spent. The daily quota is on this number, not
    # on the number of songs, so it is the one worth watching.
    searches: int = 0


class Job(BaseModel):
    """A whole transfer run. Serialised to backend/data/jobs/<id>.json."""

    id: str
    status: JobStatus = "pending"
    dry_run: bool = True
    # Kept so a failed job can be resumed without the UI re-sending anything.
    request: TransferRequest | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    # Set when status is "failed", so the UI can show the real reason.
    error: str | None = None
    # Things that went wrong without stopping the run, in plain words. The
    # usual case: a playlist that vanished from YouTube Music mid-transfer.
    warnings: list[str] = Field(default_factory=list)
    # What the job is doing when it is not visibly progressing, such as sitting
    # out a rate limit. Without this a long wait looks like a hang.
    notice: str | None = None
    # What it is working on right now, in words. The progress bar cannot move
    # while the library is being read from YouTube, and a still bar reads as a
    # hang, so this changes even when the count does not.
    activity: str | None = None
    # When a wait ends, as a UTC timestamp, so the UI can count down.
    waiting_until: datetime | None = None
    # Set when Spotify's daily quota stopped the run, so the UI can count down
    # to the moment Resume is worth pressing. None means Spotify did not say.
    quota_resets_at: datetime | None = None
    counters: JobCounters = Field(default_factory=JobCounters)
    playlists: list[PlaylistProgress] = Field(default_factory=list)
    results: list[MatchResult] = Field(default_factory=list)

    def touch(self) -> None:
        self.updated_at = _now()


class JobProgress(BaseModel):
    """Everything the transfer screen needs while a run goes on, minus the
    results.

    A finished job with its results is about a megabyte. The screen used to
    poll that every 1.5 seconds, which made the page heavy and every button
    feel slow; this answers in a few kilobytes instead. The results are
    fetched separately, far less often.
    """

    id: str
    status: JobStatus = "pending"
    dry_run: bool = True
    # Kept so the UI can decide whether Resume is possible.
    request: TransferRequest | None = None
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)
    finished_at: datetime | None = None
    error: str | None = None
    warnings: list[str] = Field(default_factory=list)
    notice: str | None = None
    activity: str | None = None
    waiting_until: datetime | None = None
    quota_resets_at: datetime | None = None
    counters: JobCounters = Field(default_factory=JobCounters)
    playlists: list[PlaylistProgress] = Field(default_factory=list)


class JobSummary(BaseModel):
    """One row in the recent-transfers list.

    Deliberately carries no ``results``. A finished job file is around a
    megabyte, almost all of it results, and the home screen only needs the
    headline numbers.
    """

    id: str
    status: JobStatus = "pending"
    dry_run: bool = True
    created_at: datetime
    updated_at: datetime
    finished_at: datetime | None = None
    error: str | None = None
    counters: JobCounters = Field(default_factory=JobCounters)
    # What the job was moving, so a row is recognisable at a glance.
    playlist_titles: list[str] = Field(default_factory=list)


class AuthStatus(BaseModel):
    """Answers the question the first screen asks: what is still missing?"""

    youtube_connected: bool = False
    youtube_detail: str = ""
    spotify_connected: bool = False
    spotify_detail: str = ""
    spotify_user: str | None = None
    # Whether the client keys are in .env at all. Without them the Connect
    # button can only lead to an error page, so the UI disables it and says
    # what to add instead.
    youtube_configured: bool = False
    spotify_configured: bool = False
