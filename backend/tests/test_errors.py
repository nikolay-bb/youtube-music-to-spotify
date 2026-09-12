"""Every failure the two APIs can throw must come out as a sentence.

The matcher tests prove the app picks the right song. These prove that when
Spotify, YouTube, Google or the network refuse, the message that reaches the
screen says what happened and what to do - never a status code on its own,
never a JSON blob.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest

from app import spotify as spotify_module
from app import transfer as engine
from app import ytmusic
from app.config import Settings
from app.errors import describe_consent_error, describe_oauth_error, error_fields
from app.matcher import build_result
from app.models import SpotifyCandidate, TransferRequest, YtTrack
from app.spotify import SpotifyAuthRequired, SpotifyClient, SpotifyError

REDIRECT = "http://127.0.0.1:8000/api/auth/spotify/callback"


def yt(title, artists=("Artist",), seconds=200, video_id="v1") -> YtTrack:
    return YtTrack(video_id=video_id, title=title, artists=list(artists), duration_seconds=seconds)


def sp(title, artists=("Artist",), seconds=200, track_id="s1") -> SpotifyCandidate:
    return SpotifyCandidate(
        id=track_id, uri=f"spotify:track:{track_id}", title=title,
        artists=list(artists), duration_seconds=seconds,
    )


@pytest.fixture(autouse=True)
def isolate_job_files(tmp_path, monkeypatch):
    """Keep test jobs out of the real backend/data folder."""
    monkeypatch.setattr(engine, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(engine, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(engine, "store", engine.JobStore())
    return tmp_path


def make_spotify_client(monkeypatch, handler) -> SpotifyClient:
    """A SpotifyClient on a fake network with instant backoff sleeps."""
    real_sleep = asyncio.sleep

    async def instant(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("app.spotify.asyncio.sleep", instant)
    monkeypatch.setattr(
        "app.spotify.get_settings",
        lambda: Settings(
            spotify_client_id="id",
            spotify_client_secret="secret",
            spotify_redirect_uri=REDIRECT,
            spotify_request_delay_ms=0,
        ),
    )
    monkeypatch.setattr("app.spotify.load_token", lambda: None)

    client = SpotifyClient()
    client._token = {"access_token": "test-token", "expires_at": time.time() + 3600}
    client._client = httpx.AsyncClient(
        base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
    )
    return client


def fake_network(monkeypatch, module, handler) -> None:
    """Point a module's httpx.AsyncClient at a fake transport."""
    real = httpx.AsyncClient

    def make(**kwargs):
        return real(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", make)


# ---------------------------------------------------------------------------
# Spotify: the network, and refusals.
# ---------------------------------------------------------------------------


async def test_a_dead_network_is_retried_a_few_times_then_explained(monkeypatch):
    """Wi-Fi off must not turn into a thousand 'error' rows in ten seconds."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("nodename nor servname provided")

    client = make_spotify_client(monkeypatch, handler)
    with pytest.raises(SpotifyError) as caught:
        await client._request("GET", "/me")
    await client.close()

    message = str(caught.value)
    assert calls["n"] == spotify_module.MAX_TRANSPORT_FAILURES
    assert "Could not connect to Spotify" in message
    assert "Resume" in message
    # The raw exception text never reaches the user.
    assert "nodename" not in message


async def test_a_single_network_blip_is_survived(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"id": "me"})

    client = make_spotify_client(monkeypatch, handler)
    result = await client._request("GET", "/me")
    await client.close()

    assert result == {"id": "me"}
    assert calls["n"] == 2


async def test_a_404_is_explained_and_quotes_spotify(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"status": 404, "message": "Invalid base62 id"}})

    client = make_spotify_client(monkeypatch, handler)
    with pytest.raises(SpotifyError) as caught:
        await client._request("GET", "/playlists/nope")
    await client.close()

    message = str(caught.value)
    assert "no longer exists" in message
    assert "Invalid base62 id" in message
    # Not the old form: a status code and a slice of raw JSON.
    assert '{"error"' not in message


async def test_a_400_is_owned_as_an_app_fault_not_a_setup_problem(monkeypatch):
    client = make_spotify_client(
        monkeypatch, lambda request: httpx.Response(400, json={"error": {"message": "Bad uri"}})
    )
    with pytest.raises(SpotifyError) as caught:
        await client._request("POST", "/playlists/x/items")
    await client.close()

    assert "fault in the app" in str(caught.value)
    assert "Bad uri" in str(caught.value)


async def test_a_401_that_survives_a_refresh_asks_for_a_new_login(monkeypatch):
    async def fake_refresh(token):
        return {"access_token": "still-bad", "expires_at": time.time() + 3600}

    monkeypatch.setattr("app.spotify.refresh_token", fake_refresh)
    client = make_spotify_client(monkeypatch, lambda request: httpx.Response(401, json={}))

    with pytest.raises(SpotifyAuthRequired) as caught:
        await client._request("GET", "/me")
    await client.close()

    assert "Connect Spotify" in str(caught.value)


async def test_a_success_that_is_not_json_is_reported_not_crashed(monkeypatch):
    """A captive portal or a proxy can answer 200 with an HTML page."""
    client = make_spotify_client(
        monkeypatch,
        lambda request: httpx.Response(200, text="<html>Sign in to the hotel Wi-Fi</html>"),
    )
    with pytest.raises(SpotifyError) as caught:
        await client._request("GET", "/me")
    await client.close()

    assert "not JSON" in str(caught.value)


async def test_the_spotify_login_names_a_redirect_uri_mismatch(monkeypatch):
    """The number one setup mistake must be named, with the exact string to check."""
    monkeypatch.setattr(
        "app.spotify.get_settings",
        lambda: Settings(spotify_client_id="id", spotify_client_secret="s", spotify_redirect_uri=REDIRECT),
    )
    fake_network(
        monkeypatch,
        spotify_module,
        lambda request: httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Invalid redirect URI"}
        ),
    )
    spotify_module._pending_states.add("state-1")

    with pytest.raises(SpotifyError) as caught:
        await spotify_module.exchange_code("code", "state-1")

    message = str(caught.value)
    assert "redirect URI" in message
    assert REDIRECT in message
    assert "localhost" in message


async def test_a_wrong_spotify_secret_is_not_reported_as_log_in_again(monkeypatch):
    """Logging in again cannot fix .env, so the message must point at .env."""
    monkeypatch.setattr(
        "app.spotify.get_settings",
        lambda: Settings(spotify_client_id="id", spotify_client_secret="wrong", spotify_redirect_uri=REDIRECT),
    )
    fake_network(
        monkeypatch,
        spotify_module,
        lambda request: httpx.Response(400, json={"error": "invalid_client"}),
    )

    with pytest.raises(SpotifyError) as caught:
        await spotify_module.refresh_token({"refresh_token": "r"})

    assert not isinstance(caught.value, SpotifyAuthRequired)
    assert ".env" in str(caught.value)


async def test_a_revoked_spotify_token_asks_for_a_new_login(monkeypatch):
    monkeypatch.setattr(
        "app.spotify.get_settings",
        lambda: Settings(spotify_client_id="id", spotify_client_secret="s", spotify_redirect_uri=REDIRECT),
    )
    fake_network(
        monkeypatch,
        spotify_module,
        lambda request: httpx.Response(
            400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"}
        ),
    )

    with pytest.raises(SpotifyAuthRequired) as caught:
        await spotify_module.refresh_token({"refresh_token": "r"})

    assert "revoked" in str(caught.value)
    assert "Start the login again" in str(caught.value)


# ---------------------------------------------------------------------------
# YouTube and Google.
# ---------------------------------------------------------------------------


@pytest.fixture
def youtube_login(monkeypatch):
    """A working, cached YouTube token, so _get goes straight to the API."""
    async def fake_token(_client):
        return "token"

    monkeypatch.setattr(ytmusic, "_get_access_token", fake_token)

    real_sleep = asyncio.sleep

    async def instant(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("app.ytmusic.asyncio.sleep", instant)


async def test_youtube_401_asks_for_a_new_login(monkeypatch, youtube_login):
    fake_network(monkeypatch, ytmusic, lambda request: httpx.Response(401, json={}))

    ok, message = await ytmusic.check_connection()

    assert ok is False
    assert "Connect YouTube" in message


async def test_youtube_quota_403_says_when_it_comes_back(monkeypatch, youtube_login):
    fake_network(
        monkeypatch,
        ytmusic,
        lambda request: httpx.Response(
            403,
            json={"error": {"code": 403, "message": "quota exceeded", "errors": [{"reason": "quotaExceeded"}]}},
        ),
    )

    ok, message = await ytmusic.check_connection()

    assert ok is False
    assert "quota" in message
    assert "midnight" in message


async def test_youtube_api_not_enabled_403_says_how_to_enable_it(monkeypatch, youtube_login):
    fake_network(
        monkeypatch,
        ytmusic,
        lambda request: httpx.Response(
            403,
            json={"error": {"message": "YouTube Data API v3 has not been used in project 1 before", "errors": [{"reason": "accessNotConfigured"}]}},
        ),
    )

    ok, message = await ytmusic.check_connection()

    assert ok is False
    assert "not enabled" in message
    assert "Enable" in message


async def test_a_youtube_outage_is_retried_then_succeeds(monkeypatch, youtube_login):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"items": [{"snippet": {"title": "Me"}}]})

    fake_network(monkeypatch, ytmusic, handler)

    ok, message = await ytmusic.check_connection()

    assert ok is True
    assert calls["n"] == 3


async def test_a_dead_network_reading_youtube_is_explained(monkeypatch, youtube_login):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    fake_network(monkeypatch, ytmusic, handler)

    ok, message = await ytmusic.check_connection()

    assert ok is False
    assert "Could not connect to YouTube" in message
    assert "boom" not in message


@pytest.fixture
def stored_google_login(tmp_path, monkeypatch):
    """A saved refresh token and a client pair, with no cached access token."""
    path = tmp_path / "oauth.json"
    path.write_text(json.dumps({"refresh_token": "r"}))
    monkeypatch.setattr(ytmusic, "YTM_OAUTH_FILE", path)
    monkeypatch.setattr(ytmusic, "_access_token", None)
    monkeypatch.setattr(ytmusic, "_expires_at", 0.0)
    monkeypatch.setattr(
        ytmusic, "get_settings", lambda: Settings(ytm_client_id="id", ytm_client_secret="secret")
    )


async def test_a_wrong_google_secret_points_at_env_not_at_logging_in(stored_google_login):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(401, json={"error": "invalid_client", "error_description": "Unauthorized"})
        )
    )
    with pytest.raises(ytmusic.YouTubeMusicError) as caught:
        await ytmusic._get_access_token(client)
    await client.aclose()

    assert not isinstance(caught.value, ytmusic.YouTubeAuthRequired)
    assert "YTM_CLIENT_SECRET" in str(caught.value)


async def test_an_expired_google_login_asks_to_connect_again(stored_google_login):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token has been expired or revoked."})
        )
    )
    with pytest.raises(ytmusic.YouTubeAuthRequired) as caught:
        await ytmusic._get_access_token(client)
    await client.aclose()

    assert "7 days" in str(caught.value)
    assert "Connect YouTube" in str(caught.value)


# ---------------------------------------------------------------------------
# The translation helpers on their own.
# ---------------------------------------------------------------------------


def test_error_fields_reads_all_three_body_shapes():
    api = httpx.Response(401, json={"error": {"status": 401, "message": "Expired", "reason": "NO_ACTIVE_DEVICE"}})
    oauth = httpx.Response(400, json={"error": "invalid_grant", "error_description": "Bad code"})
    google = httpx.Response(403, json={"error": {"message": "Quota", "errors": [{"reason": "quotaExceeded"}]}})
    junk = httpx.Response(502, text="<html>Bad gateway</html>")

    assert error_fields(api) == ("NO_ACTIVE_DEVICE", "Expired")
    assert error_fields(oauth) == ("invalid_grant", "Bad code")
    assert error_fields(google) == ("quotaExceeded", "Quota")
    assert error_fields(junk) == ("", "<html>Bad gateway</html>")


def test_the_consent_screen_cancel_is_explained():
    message = describe_consent_error("Spotify", "access_denied")
    assert "Cancel" in message
    assert "allowlist" in message


def test_an_unknown_consent_error_still_names_the_service_and_the_code():
    message = describe_consent_error("Google", "temporarily_unavailable")
    assert "Google" in message
    assert "temporarily_unavailable" in message


def test_googles_redirect_mismatch_code_is_recognised():
    message = describe_oauth_error("Google", 400, "redirect_uri_mismatch", "", "http://127.0.0.1:8000/x")
    assert "http://127.0.0.1:8000/x" in message


# ---------------------------------------------------------------------------
# The engine: a run of errors stops the job; Resume tries them again.
# ---------------------------------------------------------------------------


class FakeSpotifyService:
    def __init__(self, hits=None):
        self._hits = hits or {}
        self.searches: list[str] = []
        self.created: list[str] = []
        self.added: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def current_user(self):
        return {"id": "tester"}

    async def search_tracks(self, query, limit=10):
        self.searches.append(query)
        return list(self._hits.get(query, []))

    async def find_playlist_by_name(self, name):
        return None

    async def create_playlist(self, name, description=""):
        self.created.append(name)
        return {"id": "p", "external_urls": {}}

    async def get_playlist_track_ids(self, playlist_id):
        return set()

    async def add_tracks(self, playlist_id, uris):
        self.added.extend(uris)
        return len(uris)


class CrashingSearch(FakeSpotifyService):
    """Every song search blows up with something that is not a SpotifyError."""

    async def search_tracks(self, query, limit=10):
        if query == engine.PREFLIGHT_QUERY:
            return []
        raise RuntimeError("weird internal failure")


async def test_five_errors_in_a_row_stop_the_run_with_the_last_reason(monkeypatch):
    songs = [yt(f"Song {n}", video_id=f"v{n}") for n in range(12)]

    async def get_tracks(_playlist_id, raise_if_cancelled=None):
        return "Road Trip", songs

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: CrashingSearch())

    job = engine.store.create(dry_run=True)
    await engine.run_job(job.id, TransferRequest(playlist_ids=["PL1"], dry_run=True))

    finished = engine.store.get(job.id)
    assert finished.status == "failed"
    assert "in a row" in finished.error
    assert "weird internal failure" in finished.error
    # It stopped at the limit, not after all twelve.
    assert finished.counters.processed == engine.MAX_CONSECUTIVE_ERRORS
    assert finished.counters.errors == engine.MAX_CONSECUTIVE_ERRORS


async def test_resume_searches_the_error_rows_again(monkeypatch):
    """An 'error' verdict is a failure of the moment, not a fact about the song."""
    songs = [
        yt("Blinding Lights", ["The Weeknd"], video_id="v1"),
        yt("Levitating", ["Dua Lipa"], seconds=203, video_id="v2"),
    ]

    async def get_tracks(_playlist_id, raise_if_cancelled=None):
        return "Road Trip", songs

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)
    service = FakeSpotifyService(
        {'track:"Levitating" artist:"Dua Lipa"': [sp("Levitating", ["Dua Lipa"], seconds=203, track_id="t2")]}
    )
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    request = TransferRequest(playlist_ids=["PL1"], dry_run=True)
    job = engine.store.create(dry_run=True, request=request)
    # An earlier attempt: song 1 matched, song 2 hit an error, then it died.
    job.results = [
        build_result(songs[0], sp("Blinding Lights", ["The Weeknd"], track_id="t1"), 1.0, 0.86, 0.70, playlist_name="Road Trip"),
        build_result(songs[1], None, 0.0, 0.86, 0.70, playlist_name="Road Trip"),
    ]
    job.results[1].verdict = "error"
    job.results[1].reason = "Search failed. Could not connect to Spotify."
    job.playlists.append(engine.PlaylistProgress(source_id="PL1", source_title="Road Trip", total=2, processed=2))
    job.counters.total = 2
    job.counters.processed = 2
    job.counters.matched = 1
    job.counters.errors = 1
    job.status = "failed"
    engine.store.save(job)

    await engine.run_job(job.id, request, resume=True)

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    # Song 2 was searched again and this time matched; song 1 was not touched.
    assert any("Levitating" in s for s in service.searches)
    assert not any("Blinding Lights" in s for s in service.searches)
    assert finished.counters.errors == 0
    assert finished.counters.matched == 2
    assert finished.counters.processed == 2
    assert finished.playlists[0].processed == 2


# ---------------------------------------------------------------------------
# The HTTP layer: every failure is {"detail": "<sentence>"}, with CORS headers.
# ---------------------------------------------------------------------------


def test_a_validation_failure_is_a_sentence_not_a_list():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        response = client.post("/api/transfer", json={"playlist_ids": "not-a-list"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert "playlist_ids" in detail


def test_an_unexpected_crash_says_what_and_keeps_cors_headers():
    """A crash used to arrive as 'cannot reach the backend'. Now it says why."""
    from fastapi.testclient import TestClient

    from app.main import app

    async def boom() -> dict[str, str]:
        raise KeyError("missing_thing")

    app.add_api_route("/api/_test_boom", boom, methods=["GET"])
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/_test_boom", headers={"Origin": "http://127.0.0.1:3000"})
    finally:
        app.router.routes[:] = [r for r in app.router.routes if getattr(r, "path", "") != "/api/_test_boom"]

    assert response.status_code == 500
    assert "KeyError" in response.json()["detail"]
    assert "missing_thing" in response.json()["detail"]
    assert response.headers.get("access-control-allow-origin") == "http://127.0.0.1:3000"
