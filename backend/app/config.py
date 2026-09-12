"""Application settings, read once from the .env file at the repository root."""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/config.py -> backend/app -> backend -> repository root
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent.parent

# Everything secret or disposable lives here. The whole folder is gitignored.
DATA_DIR = BACKEND_ROOT / "data"
JOBS_DIR = DATA_DIR / "jobs"
YTM_OAUTH_FILE = DATA_DIR / "oauth.json"
SPOTIFY_TOKEN_FILE = DATA_DIR / "spotify_token.json"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Spotify application credentials.
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    # Must be 127.0.0.1 and not localhost. Spotify rejects the localhost hostname.
    spotify_redirect_uri: str = "http://127.0.0.1:8000/api/auth/spotify/callback"

    # Google OAuth client for the YouTube Data API. Use application type
    # "Web application" and register the redirect URI below on it.
    ytm_client_id: str = ""
    ytm_client_secret: str = ""
    # Must match the Authorised redirect URI on the Google client exactly.
    ytm_redirect_uri: str = "http://127.0.0.1:8000/api/auth/youtube/callback"

    # Score at or above which a candidate is accepted without asking.
    match_threshold: float = 0.86
    # Score below which a candidate is discarded as not found.
    review_threshold: float = 0.70

    # Spotify Development Mode has low rate limits, so keep the pressure gentle.
    # How many search queries to try per song before giving up. The first is
    # precise and answers most songs; each further rung costs another request
    # against a daily quota of roughly 1,000.
    max_queries_per_song: int = 3
    spotify_max_concurrency: int = 1
    spotify_request_delay_ms: int = 400

    frontend_url: str = "http://127.0.0.1:3000"

    @property
    def spotify_configured(self) -> bool:
        return bool(self.spotify_client_id and self.spotify_client_secret)

    @property
    def ytm_keys_present(self) -> bool:
        """The client pair is in .env. Says nothing about whether a login exists."""
        return bool(self.ytm_client_id and self.ytm_client_secret)

    @property
    def ytm_configured(self) -> bool:
        """Needs the OAuth client pair and the saved token."""
        return self.ytm_keys_present and YTM_OAUTH_FILE.exists()


@lru_cache
def get_settings() -> Settings:
    """Cached so the .env file is parsed once per process."""
    return Settings()


def ensure_data_dirs() -> None:
    JOBS_DIR.mkdir(parents=True, exist_ok=True)


# The scopes we ask Spotify for. Deliberately minimal:
# we only create playlists and add songs to them, and read playlists to avoid
# adding duplicates. No library scopes, because liked songs go to a playlist.
SPOTIFY_SCOPES = "playlist-modify-private playlist-modify-public playlist-read-private"
