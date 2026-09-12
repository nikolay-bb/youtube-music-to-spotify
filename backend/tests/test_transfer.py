"""Tests for the transfer engine and the Spotify request loop.

The matcher decides what is right. This file checks the things that would ruin
a real run even with perfect matching: adding a song twice, giving up the first
time Spotify says "slow down", or losing the report when the process stops.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from app import transfer as engine, ytmusic
from app.config import Settings
from app.matcher import build_result
from app.models import (
    Job,
    JobCounters,
    MatchResult,
    PlaylistProgress,
    SpotifyCandidate,
    TransferRequest,
    YtTrack,
)
from app.spotify import (
    SpotifyCancelled,
    SpotifyClient,
    SpotifyError,
    SpotifyQuotaExhausted,
)

SETTINGS = Settings(match_threshold=0.86, review_threshold=0.70, spotify_request_delay_ms=0)


def yt(title, artists=("Artist",), seconds=200, video_id="v1") -> YtTrack:
    return YtTrack(video_id=video_id, title=title, artists=list(artists), duration_seconds=seconds)


def sp(title, artists=("Artist",), seconds=200, track_id="s1") -> SpotifyCandidate:
    return SpotifyCandidate(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        title=title,
        artists=list(artists),
        duration_seconds=seconds,
    )


@pytest.fixture(autouse=True)
def isolate_job_files(tmp_path, monkeypatch):
    """Keep test jobs out of the real backend/data folder."""
    monkeypatch.setattr(engine, "JOBS_DIR", tmp_path)
    monkeypatch.setattr(engine, "ensure_data_dirs", lambda: None)
    monkeypatch.setattr(engine, "store", engine.JobStore())
    return tmp_path


# ---------------------------------------------------------------------------
# A stand-in for the Spotify client, recording what it was asked to do.
# ---------------------------------------------------------------------------


class FakeSpotify:
    def __init__(self, results=None, existing=None, playlist=None):
        self._results = results or {}
        self._existing = set(existing or [])
        self._playlist = playlist
        self.searches: list[str] = []
        self.added: list[str] = []
        self.created: list[str] = []

    async def search_tracks(self, query, limit=10):
        self.searches.append(query)
        return list(self._results.get(query, []))

    async def find_playlist_by_name(self, name):
        return self._playlist

    async def create_playlist(self, name, description=""):
        self.created.append(name)
        return {"id": "new-playlist", "external_urls": {"spotify": "https://open.spotify.com/x"}}

    async def get_playlist_track_ids(self, playlist_id):
        return set(self._existing)

    async def add_tracks(self, playlist_id, uris):
        self.added.extend(uris)
        return len(uris)


def matched_result(track, candidate) -> MatchResult:
    return build_result(track, candidate, 1.0, 0.86, 0.70, playlist_name="Test")


# ---------------------------------------------------------------------------
# Never add the same song twice.
# ---------------------------------------------------------------------------


async def test_songs_already_in_the_playlist_are_not_added_again():
    """This is what makes a second run safe: it should add nothing."""
    fake = FakeSpotify(existing={"s1", "s2"}, playlist={"id": "p1", "external_urls": {}})
    job = Job(id="j1", dry_run=False)
    progress = PlaylistProgress(source_id="src", source_title="Test")
    matched = [
        matched_result(yt("A", video_id="a"), sp("A", track_id="s1")),
        matched_result(yt("B", video_id="b"), sp("B", track_id="s2")),
    ]

    await engine._write_playlist(fake, job, progress, "Test", matched)

    assert fake.added == []
    assert progress.added == 0
    assert progress.skipped_duplicates == 2


async def test_only_the_missing_songs_are_added():
    fake = FakeSpotify(existing={"s1"}, playlist={"id": "p1", "external_urls": {}})
    job = Job(id="j1", dry_run=False)
    progress = PlaylistProgress(source_id="src", source_title="Test")
    matched = [
        matched_result(yt("A", video_id="a"), sp("A", track_id="s1")),
        matched_result(yt("B", video_id="b"), sp("B", track_id="s2")),
    ]

    await engine._write_playlist(fake, job, progress, "Test", matched)

    assert fake.added == ["spotify:track:s2"]
    assert progress.added == 1
    assert progress.skipped_duplicates == 1


async def test_the_same_song_matched_twice_is_only_added_once():
    """Two YouTube entries can resolve to one Spotify track."""
    fake = FakeSpotify(playlist=None)
    job = Job(id="j1", dry_run=False)
    progress = PlaylistProgress(source_id="src", source_title="Test")
    matched = [
        matched_result(yt("A", video_id="a"), sp("A", track_id="same")),
        matched_result(yt("A again", video_id="b"), sp("A", track_id="same")),
    ]

    await engine._write_playlist(fake, job, progress, "Test", matched)

    assert fake.added == ["spotify:track:same"]
    assert progress.skipped_duplicates == 1


async def test_a_new_playlist_is_created_when_none_exists():
    fake = FakeSpotify(playlist=None)
    job = Job(id="j1", dry_run=False)
    progress = PlaylistProgress(source_id="src", source_title="Fresh")

    await engine._write_playlist(
        fake, job, progress, "Fresh", [matched_result(yt("A"), sp("A", track_id="s9"))]
    )

    assert fake.created == ["Fresh"]
    assert progress.spotify_playlist_id == "new-playlist"


# ---------------------------------------------------------------------------
# Searching stops as soon as it is confident.
# ---------------------------------------------------------------------------


async def test_a_confident_first_query_costs_only_one_search():
    """Rate limits are tight, so an easy song must not cost three API calls."""
    track = yt("Blinding Lights", ["The Weeknd"], seconds=200)
    exact = 'track:"Blinding Lights" artist:"The Weeknd"'
    fake = FakeSpotify({exact: [sp("Blinding Lights", ["The Weeknd"], seconds=200)]})

    result = await engine.match_track(fake, track, "Test", SETTINGS, JobCounters())

    assert result.verdict == "matched"
    assert fake.searches == [exact]


async def test_a_hard_song_falls_through_to_the_looser_queries():
    track = yt("Some Song", ["Some Artist"], seconds=200)
    loose = "Some Song"
    fake = FakeSpotify({loose: [sp("Some Song", ["Some Artist"], seconds=200)]})

    result = await engine.match_track(fake, track, "Test", SETTINGS, JobCounters())

    assert result.verdict == "matched"
    assert len(fake.searches) == 3


async def test_a_song_with_no_results_anywhere_is_reported_not_found():
    counters = JobCounters()
    result = await engine.match_track(FakeSpotify({}), yt("Obscure"), "Test", SETTINGS, counters)
    assert result.verdict == "not_found"
    assert result.best is None
    # Every search attempt was tallied, even though none found anything.
    assert counters.searches == 3


async def test_a_weak_match_is_held_back_rather_than_added():
    """The middle band is the whole point: report it, never add it."""
    track = yt("Alone", ["Artist"], seconds=200)
    fake = FakeSpotify(
        {q: [sp("Alone Together", ["Artist"], seconds=200)] for q in
         ['track:"Alone" artist:"Artist"', "Alone Artist", "Alone"]}
    )

    result = await engine.match_track(fake, track, "Test", SETTINGS, JobCounters())

    assert result.verdict == "low_confidence"
    assert result.best is not None


# ---------------------------------------------------------------------------
# The job file on disk.
# ---------------------------------------------------------------------------


def test_a_job_survives_the_process_that_made_it(isolate_job_files):
    """Progress is written to disk, so a crash or a closed browser loses nothing."""
    store = engine.store
    job = store.create(dry_run=True)
    job.counters.processed = 7
    job.results.append(matched_result(yt("A"), sp("A")))
    store.save(job)

    # A fresh store, as if the server had restarted.
    reloaded = engine.JobStore().get(job.id)

    assert reloaded is not None
    assert reloaded.counters.processed == 7
    assert len(reloaded.results) == 1
    assert reloaded.results[0].source.title == "A"


def test_an_unknown_job_id_returns_nothing():
    assert engine.JobStore().get("does-not-exist") is None


def test_cancelling_marks_the_job_and_cancelling_twice_is_harmless():
    store = engine.store
    job = store.create(dry_run=True)

    assert store.cancel(job.id) is True
    assert store.is_cancelled(job.id) is True
    assert store.cancel("no-such-job") is False


async def test_a_cancelled_job_stops_and_is_recorded_as_cancelled(monkeypatch):
    store = engine.store
    job = store.create(dry_run=True)
    store.cancel(job.id)

    async def get_tracks(_playlist_id, raise_if_cancelled=None):
        return "Road Trip", [yt("Blinding Lights", video_id="v1")]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: spotify_service_with_hits())

    await engine.run_job(job.id, TransferRequest(playlist_ids=["PL1"], dry_run=True))

    assert store.get(job.id).status == "cancelled"


# ---------------------------------------------------------------------------
# The CSV report.
# ---------------------------------------------------------------------------


def test_the_report_can_be_narrowed_to_just_the_problems():
    job = Job(id="j1")
    job.results = [
        matched_result(yt("Fine"), sp("Fine")),
        build_result(yt("Unsure"), sp("Unsure-ish"), 0.75, 0.86, 0.70),
        build_result(yt("Missing"), None, 0.0, 0.86, 0.70),
    ]

    full = engine.results_to_csv(job)
    problems = engine.results_to_csv(job, only_problems=True)

    assert len(full.strip().splitlines()) == 4  # header plus three rows
    assert len(problems.strip().splitlines()) == 3  # header plus two rows
    assert "Fine" not in problems
    assert "Missing" in problems


def test_the_report_starts_with_a_header_row():
    header = engine.results_to_csv(Job(id="j1")).strip()
    assert header.startswith("playlist,verdict,score,youtube_title")


# ---------------------------------------------------------------------------
# The Spotify request loop: rate limits and outages.
# ---------------------------------------------------------------------------


def make_client(monkeypatch, handler) -> SpotifyClient:
    """A SpotifyClient wired to a fake network and a token that never expires.

    Backoff sleeps are made instant. The point of these tests is that the retry
    decisions are right, not that the clock works, and a quota backoff waits
    minutes on purpose.
    """
    real_sleep = asyncio.sleep

    async def instant(_seconds: float) -> None:
        await real_sleep(0)

    monkeypatch.setattr("app.spotify.asyncio.sleep", instant)
    monkeypatch.setattr(
        "app.spotify.get_settings",
        lambda: Settings(
            spotify_client_id="id",
            spotify_client_secret="secret",
            spotify_request_delay_ms=0,
        ),
    )
    monkeypatch.setattr("app.spotify.load_token", lambda: None)

    client = SpotifyClient()
    client._token = {"access_token": "test-token", "expires_at": time.time() + 3600}
    client._client = httpx.AsyncClient(
        base_url="https://api.spotify.com/v1",
        transport=httpx.MockTransport(handler),
    )
    return client


async def test_a_rate_limit_is_obeyed_and_the_request_succeeds(monkeypatch):
    """429 must be waited out, not treated as a failure."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": {"message": "API rate limit exceeded"}},
            )
        return httpx.Response(200, json={"id": "me"})

    client = make_client(monkeypatch, handler)
    result = await client._request("GET", "/me")
    await client.close()

    assert result == {"id": "me"}
    assert calls["n"] == 2


async def test_a_server_error_is_retried(monkeypatch):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": True})

    client = make_client(monkeypatch, handler)
    result = await client._request("GET", "/me")
    await client.close()

    assert result == {"ok": True}
    assert calls["n"] == 3


async def test_a_forbidden_response_explains_the_premium_and_allowlist_rules(monkeypatch):
    """403 is the error people actually hit, so it must not be cryptic."""
    client = make_client(monkeypatch, lambda request: httpx.Response(403, json={}))

    with pytest.raises(SpotifyError) as caught:
        await client._request("GET", "/me")
    await client.close()

    message = str(caught.value).lower()
    assert "allowlist" in message
    assert "premium" in message


async def test_repeated_rate_limits_become_a_quota_stop(monkeypatch):
    """A permanently throttled account must stop loudly and early, not hang.

    Spotify signals a spent daily quota on some days only with endless 429s and
    short waits. Three in a row, from a client this gentle, mean today is done,
    so the job must stop with the quota message rather than retry for an hour
    to reach the same answer.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "5"},
            json={"error": {"message": "Too many requests"}},
        )

    client = make_client(monkeypatch, handler)
    with pytest.raises(SpotifyQuotaExhausted) as caught:
        await client._request("GET", "/me")
    await client.close()

    # Stopped on the third refusal, not after eight.
    assert calls["n"] == 3
    # A five-second Retry-After is no basis for a countdown.
    assert caught.value.resets_at is None


async def test_two_rate_limits_then_success_is_still_waited_out(monkeypatch):
    """A burst limit that clears after a couple of waits is not the quota."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] <= 2:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"ok": True})

    client = make_client(monkeypatch, handler)
    result = await client._request("GET", "/me")
    await client.close()

    assert result == {"ok": True}
    assert calls["n"] == 3


async def test_search_results_are_converted_into_candidates(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tracks": {
                    "items": [
                        {
                            "id": "abc",
                            "uri": "spotify:track:abc",
                            "name": "Song",
                            "artists": [{"name": "Someone"}],
                            "album": {"name": "Record"},
                            "duration_ms": 200_000,
                            "external_urls": {"spotify": "https://open.spotify.com/track/abc"},
                        },
                        {"not": "a track"},
                    ]
                }
            },
        )

    client = make_client(monkeypatch, handler)
    candidates = await client.search_tracks("anything")
    await client.close()

    assert len(candidates) == 1
    assert candidates[0].title == "Song"
    assert candidates[0].duration_seconds == 200
    assert candidates[0].album == "Record"


# ---------------------------------------------------------------------------
# The whole pipeline, with both services faked.
#
# This is the test that proves the parts fit together: reading a library,
# matching every song, counting the verdicts, and writing the result. It runs
# the real run_job, so a mistake in the wiring shows up here.
# ---------------------------------------------------------------------------


class FakeSpotifyService(FakeSpotify):
    """FakeSpotify plus the bits run_job needs: a context manager and /me."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return None

    async def current_user(self):
        return {"id": "tester", "display_name": "Tester"}


@pytest.fixture
def fake_library(monkeypatch):
    """A two-playlist library where one song matches and one does not."""
    library = {
        "PL1": (
            "Road Trip",
            [
                yt("Blinding Lights", ["The Weeknd"], seconds=200, video_id="v1"),
                yt("Totally Obscure Demo Tape", ["Nobody"], seconds=999, video_id="v2"),
            ],
        ),
        "LM": ("Liked songs", [yt("Levitating", ["Dua Lipa"], seconds=203, video_id="v3")]),
    }

    async def fake_get_tracks(playlist_id, raise_if_cancelled=None):
        return library[playlist_id]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", fake_get_tracks)
    return library


def spotify_service_with_hits() -> FakeSpotifyService:
    """Search results good enough to match two of the three songs."""
    hits = {
        'track:"Blinding Lights" artist:"The Weeknd"': [
            sp("Blinding Lights", ["The Weeknd"], seconds=200, track_id="t1")
        ],
        'track:"Levitating" artist:"Dua Lipa"': [
            sp("Levitating", ["Dua Lipa"], seconds=203, track_id="t3")
        ],
    }
    return FakeSpotifyService(results=hits, playlist=None)


async def test_a_dry_run_reports_everything_and_writes_nothing(monkeypatch, fake_library):
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=True)
    await engine.run_job(
        job.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=True)
    )

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    assert finished.counters.total == 3
    assert finished.counters.processed == 3
    assert finished.counters.matched == 2
    assert finished.counters.not_found == 1
    # The whole point of a dry run:
    assert service.added == []
    assert service.created == []
    assert finished.counters.added == 0


async def test_a_live_run_creates_the_playlists_and_adds_the_matches(monkeypatch, fake_library):
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=False)
    await engine.run_job(
        job.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=False)
    )

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    assert finished.counters.added == 2
    assert set(service.added) == {"spotify:track:t1", "spotify:track:t3"}
    # Liked songs become a normal playlist, not Spotify's built-in Liked Songs.
    assert set(service.created) == {"Road Trip", engine.LIKED_PLAYLIST_NAME}


async def test_running_the_same_transfer_twice_adds_nothing_the_second_time(
    monkeypatch, fake_library
):
    """The idempotency promise, tested end to end."""
    first = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: first)
    job1 = engine.store.create(dry_run=False)
    await engine.run_job(
        job1.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=False)
    )
    assert engine.store.get(job1.id).counters.added == 2

    # Second run: the playlists now exist and already hold those songs.
    second = spotify_service_with_hits()
    second._playlist = {"id": "p1", "external_urls": {"spotify": "https://open.spotify.com/p1"}}
    second._existing = {"t1", "t3"}
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: second)

    job2 = engine.store.create(dry_run=False)
    await engine.run_job(
        job2.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=False)
    )

    finished = engine.store.get(job2.id)
    assert finished.counters.added == 0
    assert finished.counters.skipped_duplicates == 2
    assert second.added == []
    assert second.created == []


async def test_per_playlist_progress_is_tracked_separately(monkeypatch, fake_library):
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=True)
    await engine.run_job(
        job.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=True)
    )

    finished = engine.store.get(job.id)
    by_id = {p.source_id: p for p in finished.playlists}
    assert by_id["PL1"].total == 2 and by_id["PL1"].processed == 2
    assert by_id["LM"].total == 1 and by_id["LM"].processed == 1
    # The liked playlist is renamed on the way through.
    assert by_id["LM"].source_title == engine.LIKED_PLAYLIST_NAME


async def test_a_failure_partway_through_is_recorded_not_swallowed(monkeypatch, fake_library):
    class BrokenSpotify(FakeSpotifyService):
        async def current_user(self):
            raise SpotifyError("Spotify returned 403 Forbidden.")

    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: BrokenSpotify())

    job = engine.store.create(dry_run=True)
    await engine.run_job(job.id, TransferRequest(playlist_ids=["PL1"], dry_run=True))

    finished = engine.store.get(job.id)
    assert finished.status == "failed"
    assert "403" in finished.error


# ---------------------------------------------------------------------------
# Reading the library must survive playlists that vanish or blip.
#
# A twenty-playlist run was once stopped dead by one 404 from YouTube. The
# answer is not to retry harder but to fail smaller: skip what is gone, say
# so, and finish the rest.
# ---------------------------------------------------------------------------


async def test_a_missing_playlist_is_skipped_not_fatal(monkeypatch):
    """One deleted playlist must not kill a run that covers the rest."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    library = {
        "PL1": ("Road Trip", [yt("Blinding Lights", ["The Weeknd"], seconds=200, video_id="v1")]),
    }

    async def get_tracks(playlist_id, raise_if_cancelled=None):
        if playlist_id == "GONE":
            raise ytmusic.PlaylistNotFound(
                "Playlist GONE is no longer available on YouTube Music."
            )
        return library[playlist_id]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)

    job = engine.store.create(dry_run=True)
    await engine.run_job(
        job.id, TransferRequest(playlist_ids=["PL1", "GONE"], dry_run=True)
    )

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    # Only the readable playlist counts towards the work.
    assert finished.counters.total == 1
    assert finished.counters.processed == 1
    # The user is told what was skipped, in plain words.
    assert len(finished.warnings) == 1
    assert "GONE" in finished.warnings[0]


async def test_a_transient_read_failure_gets_one_more_try(monkeypatch):
    """A single blip from the YouTube API must not stop the run."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    attempts = {"n": 0}

    async def get_tracks(playlist_id, raise_if_cancelled=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ytmusic.YouTubeMusicError("YouTube rejected videos: 500 server error")
        return "Road Trip", [yt("Blinding Lights", ["The Weeknd"], seconds=200, video_id="v1")]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)

    job = engine.store.create(dry_run=True)
    await engine.run_job(job.id, TransferRequest(playlist_ids=["PL1"], dry_run=True))

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    assert attempts["n"] == 2
    assert finished.warnings == []


async def test_a_playlist_that_still_will_not_read_stops_the_job(monkeypatch):
    """Skipping is for missing playlists. A real outage must still fail loudly."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    async def get_tracks(playlist_id, raise_if_cancelled=None):
        raise ytmusic.YouTubeMusicError("Could not refresh the YouTube login (400).")

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)

    job = engine.store.create(dry_run=True)
    await engine.run_job(job.id, TransferRequest(playlist_ids=["PL1"], dry_run=True))

    finished = engine.store.get(job.id)
    assert finished.status == "failed"
    assert "YouTube" in finished.error


async def test_a_404_reading_playlist_items_becomes_the_friendly_error(monkeypatch):
    """The API's JSON blob must become plain words the UI can show.

    YouTube sometimes still lists a playlist but refuses to read its contents -
    usually a moment after it was deleted. That half-dead state is exactly the
    one that used to put raw JSON on the user's screen.
    """
    async def fake_token(_client):
        return "token"

    monkeypatch.setattr(ytmusic, "_get_access_token", fake_token)

    def handler(request: httpx.Request) -> httpx.Response:
        if "playlistItems" in str(request.url):
            return httpx.Response(
                404,
                json={
                    "error": {
                        "code": 404,
                        "message": "The playlist identified with the request's "
                        "playlistId parameter cannot be found.",
                    }
                },
            )
        return httpx.Response(
            200, json={"items": [{"id": "GONE", "snippet": {"title": "Deleted"}}]}
        )

    # Capture the real class before the patch replaces it, or make_client
    # would call itself.
    real_client = httpx.AsyncClient

    def make_client(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(ytmusic.httpx, "AsyncClient", make_client)

    with pytest.raises(ytmusic.PlaylistNotFound) as caught:
        await ytmusic.get_playlist_tracks("GONE")

    assert "no longer available" in str(caught.value)


# ---------------------------------------------------------------------------
# Resuming a job that stopped part-way.
#
# Spotify's Development Mode quota runs out on a large library, so this is not
# an edge case - it is the normal path for anyone with more than a few hundred
# songs. Losing the work already done would make the tool useless.
# ---------------------------------------------------------------------------


async def test_resume_skips_songs_already_processed(monkeypatch, fake_library):
    """The whole point: a second attempt must not re-search what is already done."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    request = TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=True)
    job = engine.store.create(dry_run=True, request=request)
    await engine.run_job(job.id, request)

    first_pass_searches = len(service.searches)
    assert engine.store.get(job.id).counters.processed == 3
    assert first_pass_searches > 0

    # Resume a completed job: everything is done, so only the one pre-flight
    # quota check is searched.
    service.searches.clear()
    await engine.run_job(job.id, request, resume=True)

    assert service.searches == [engine.PREFLIGHT_QUERY]
    assert engine.store.get(job.id).counters.processed == 3


async def test_resume_continues_from_where_it_stopped(monkeypatch, fake_library):
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    request = TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=True)
    job = engine.store.create(dry_run=True, request=request)

    # Pretend an earlier attempt got through one song, then died.
    job.results.append(
        build_result(
            yt("Blinding Lights", ["The Weeknd"], video_id="v1"),
            sp("x"),
            1.0,
            0.86,
            0.70,
            playlist_name="Road Trip",
        )
    )
    job.status = "failed"
    job.error = "429 QUOTA_EXCEEDED"
    engine.store.save(job)

    await engine.run_job(job.id, request, resume=True)

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    assert finished.error is None
    # Three songs in the fixture library, one already done, so two were searched.
    assert {r.source.video_id for r in finished.results} == {"v1", "v2", "v3"}
    assert not any("Blinding Lights" in s for s in service.searches)


async def test_a_fresh_run_does_not_inherit_an_old_attempt(monkeypatch, fake_library):
    """Without resume, previous results must be cleared rather than doubled up."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    request = TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=True)
    job = engine.store.create(dry_run=True, request=request)
    await engine.run_job(job.id, request)
    await engine.run_job(job.id, request)  # same job, no resume

    finished = engine.store.get(job.id)
    assert finished.counters.processed == 3
    assert len(finished.results) == 3
    assert len(finished.playlists) == 2


async def test_a_quota_failure_keeps_the_results_it_had(monkeypatch, fake_library):
    """A job that dies must leave its report on disk, or resume has nothing to use."""
    class QuotaSpotify(FakeSpotifyService):
        def __init__(self):
            super().__init__(results={}, playlist=None)
            self.calls = 0

        async def search_tracks(self, query, limit=10):
            # Call 1 is the pre-flight quota check, then a song costs up to
            # three queries. This lets the first song finish and kills the
            # quota during the second - which is what a real mid-library
            # quota failure looks like.
            self.calls += 1
            if self.calls > 4:
                raise SpotifyError("Gave up on GET /search. Last error: 429 QUOTA_EXCEEDED")
            return []

    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: QuotaSpotify())

    request = TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=True)
    job = engine.store.create(dry_run=True, request=request)
    await engine.run_job(job.id, request)

    finished = engine.store.get(job.id)
    assert finished.status == "failed"
    assert "QUOTA_EXCEEDED" in finished.error
    # The song it managed before dying is still recorded, so a resume has
    # something to skip rather than starting from nothing.
    assert finished.counters.processed == 1
    assert len(finished.results) == 1
    assert finished.request is not None  # so the resume endpoint can replay it


# ---------------------------------------------------------------------------
# Stop must work during a backoff.
#
# A rate-limit wait can be ten minutes. If the cancel flag is only read between
# songs, pressing Stop does nothing for ten minutes and the app looks broken.
# ---------------------------------------------------------------------------


async def test_stop_interrupts_a_rate_limit_wait(monkeypatch):
    """The Stop button must be felt within a second, not after the backoff."""
    cancelled = {"yes": False}

    def handler(request: httpx.Request) -> httpx.Response:
        # Always rate limited with a wait under the quota threshold, so the
        # only way out is pressing Stop.
        return httpx.Response(
            429,
            headers={"Retry-After": "60"},
            json={"error": {"message": "API rate limit exceeded"}},
        )

    monkeypatch.setattr(
        "app.spotify.get_settings",
        lambda: Settings(
            spotify_client_id="id", spotify_client_secret="secret", spotify_request_delay_ms=0
        ),
    )
    monkeypatch.setattr("app.spotify.load_token", lambda: None)

    client = SpotifyClient(should_cancel=lambda: cancelled["yes"])
    client._token = {"access_token": "t", "expires_at": time.time() + 3600}
    client._client = httpx.AsyncClient(
        base_url="https://api.spotify.com/v1", transport=httpx.MockTransport(handler)
    )

    # Press Stop almost immediately.
    async def press_stop():
        await asyncio.sleep(0.05)
        cancelled["yes"] = True

    asyncio.create_task(press_stop())

    with pytest.raises(SpotifyCancelled):
        await client._request("GET", "/me")
    await client.close()


async def test_a_wait_that_is_not_cancelled_still_retries(monkeypatch):
    """The interruptible sleep must not break ordinary retrying."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={})
        return httpx.Response(200, json={"ok": True})

    client = make_client(monkeypatch, handler)
    result = await client._request("GET", "/me")
    await client.close()

    assert result == {"ok": True}
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Playlist order, and finding a job again after a restart.
# ---------------------------------------------------------------------------


async def test_liked_songs_are_transferred_last(monkeypatch, fake_library):
    """Liked songs is usually bigger than every named playlist put together.

    Running it first spends the whole daily search quota on it and leaves the
    named playlists at zero, which is exactly what happened in real use.
    """
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=False)
    await engine.run_job(
        job.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=False)
    )

    assert service.created == ["Road Trip", engine.LIKED_PLAYLIST_NAME]


async def test_liked_songs_go_last_even_when_asked_for_first(monkeypatch, fake_library):
    """The UI can send LM inside playlist_ids. Order it the same way regardless."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=False)
    await engine.run_job(
        job.id,
        TransferRequest(playlist_ids=["LM", "PL1"], include_liked=False, dry_run=False),
    )

    assert service.created == ["Road Trip", engine.LIKED_PLAYLIST_NAME]


def test_recent_transfers_are_listed_newest_first(isolate_job_files):
    older = engine.store.create(dry_run=True)
    newer = engine.store.create(dry_run=False)

    summaries = engine.store.list_summaries()

    assert [s.id for s in summaries] == [newer.id, older.id]


def test_recent_transfers_are_read_from_disk_after_a_restart(isolate_job_files):
    """The whole point: a restart must not hide finished work."""
    job = engine.store.create(dry_run=False)
    job.playlists.append(PlaylistProgress(source_id="PL1", source_title="Gym", total=108))
    job.counters.total = 108
    job.counters.processed = 108
    engine.store.save(job)

    # A fresh store has an empty memory, exactly like a restarted server.
    restarted = engine.JobStore()
    summaries = restarted.list_summaries()

    assert len(summaries) == 1
    assert summaries[0].id == job.id
    assert summaries[0].playlist_titles == ["Gym"]
    assert summaries[0].counters.processed == 108


def test_an_unreadable_job_file_does_not_hide_the_others(isolate_job_files):
    good = engine.store.create(dry_run=True)
    (isolate_job_files / "broken.json").write_text("{ not json")

    summaries = engine.JobStore().list_summaries()

    assert [s.id for s in summaries] == [good.id]


def test_the_recent_list_is_capped(isolate_job_files):
    for _ in range(5):
        engine.store.create(dry_run=True)

    assert len(engine.store.list_summaries(limit=3)) == 3


# ---------------------------------------------------------------------------
# The daily quota stops the run at once, and says when to come back.
# ---------------------------------------------------------------------------


async def test_the_daily_quota_stops_at_once_instead_of_waiting(monkeypatch):
    """Retrying a daily quota spends half an hour reaching the same answer."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "3600"},
            json={"error": {"reason": "QUOTA_EXCEEDED"}},
        )

    client = make_client(monkeypatch, handler)
    with pytest.raises(SpotifyQuotaExhausted):
        await client._request("GET", "/me")
    await client.close()

    # One try, not eight.
    assert calls["n"] == 1


async def test_a_huge_retry_after_alone_is_treated_as_the_daily_quota(monkeypatch):
    """Some days Spotify never says QUOTA, only a Retry-After of hours.

    That must stop the job at once with a reset time, not burn eight attempts
    on a wait that ends in the same answer.
    """
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            429,
            headers={"Retry-After": "7200"},
            json={"error": {"message": "Too many requests"}},
        )

    client = make_client(monkeypatch, handler)
    with pytest.raises(SpotifyQuotaExhausted) as caught:
        await client._request("GET", "/me")
    await client.close()

    assert calls["n"] == 1
    resets_at = caught.value.resets_at
    assert resets_at is not None
    ahead = (resets_at - datetime.now(timezone.utc)).total_seconds()
    assert 7000 < ahead <= 7200


async def test_a_short_burst_limit_is_still_waited_out_not_called_quota(monkeypatch):
    """Only a long wait means quota. An ordinary burst limit must retry."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "30"},
                json={"error": {"message": "Too many requests"}},
            )
        return httpx.Response(200, json={"ok": True})

    client = make_client(monkeypatch, handler)
    result = await client._request("GET", "/me")
    await client.close()

    assert result == {"ok": True}
    assert calls["n"] == 2


async def test_the_quota_error_carries_when_it_resets(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "7200"},
            json={"error": {"reason": "QUOTA_EXCEEDED"}},
        )

    client = make_client(monkeypatch, handler)
    with pytest.raises(SpotifyQuotaExhausted) as caught:
        await client._request("GET", "/me")
    await client.close()

    resets_at = caught.value.resets_at
    assert resets_at is not None
    ahead = (resets_at - datetime.now(timezone.utc)).total_seconds()
    assert 7000 < ahead <= 7200


async def test_a_uselessly_short_retry_after_is_not_shown_as_a_countdown(monkeypatch):
    """Spotify often sends a few seconds on a quota error. That is not an answer.

    Counting down to it would just expire into the same error, so the UI is
    told nothing rather than something wrong.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "3"},
            json={"error": {"reason": "QUOTA_EXCEEDED"}},
        )

    client = make_client(monkeypatch, handler)
    with pytest.raises(SpotifyQuotaExhausted) as caught:
        await client._request("GET", "/me")
    await client.close()

    assert caught.value.resets_at is None


async def test_a_quota_stop_keeps_the_results_and_records_the_reset(
    monkeypatch, fake_library
):
    """End to end: the job stops, keeps what it had, and says when to return."""
    service = spotify_service_with_hits()
    resets_at = datetime.now(timezone.utc) + timedelta(hours=5)

    async def out_of_quota(_query, limit=10):
        raise SpotifyQuotaExhausted("daily limit used up", resets_at)

    service.search_tracks = out_of_quota
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=False)
    await engine.run_job(
        job.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=False)
    )

    stopped = engine.store.get(job.id)
    assert stopped.status == "failed"
    assert stopped.quota_resets_at == resets_at
    assert "daily limit" in stopped.error
    # Nothing was written to Spotify on the way out.
    assert service.added == []


async def test_resuming_clears_the_previous_quota_stop(monkeypatch, fake_library):
    """A stale reset time - one already in the past - must not block the next run."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=False)
    job.quota_resets_at = datetime.now(timezone.utc) - timedelta(hours=5)
    engine.store.save(job)

    await engine.run_job(
        job.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=False)
    )

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    assert finished.quota_resets_at is None


async def test_resume_with_the_quota_still_spent_does_not_start_the_run(monkeypatch, fake_library):
    """Pressing Resume while the quota is still spent must answer at once.

    The user must never watch a run start and die a minute later on a quota
    we were already told about. No request may be made at all.
    """
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=False)
    job.quota_resets_at = datetime.now(timezone.utc) + timedelta(hours=3)
    engine.store.save(job)

    service.searches.clear()
    await engine.run_job(
        job.id, TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=False)
    )

    stopped = engine.store.get(job.id)
    assert stopped.status == "failed"
    assert "not started" in stopped.error
    assert stopped.quota_resets_at is not None
    # Not a single search or playlist read happened.
    assert service.searches == []


async def test_a_spent_quota_is_detected_before_the_library_is_read(monkeypatch, fake_library):
    """The pre-flight search must catch a spent quota before any reading."""
    reads = {"n": 0}

    async def get_tracks(_playlist_id, raise_if_cancelled=None):
        reads["n"] += 1
        return "Road Trip", [yt("A")]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)

    class QuotaSpent(FakeSpotifyService):
        async def search_tracks(self, query, limit=10):
            raise SpotifyQuotaExhausted(
                "Spotify's daily limit for this app is used up.", None
            )

    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: QuotaSpent())

    job = engine.store.create(dry_run=True)
    await engine.run_job(job.id, TransferRequest(playlist_ids=["PL1"], dry_run=True))

    finished = engine.store.get(job.id)
    assert finished.status == "failed"
    assert "daily limit" in finished.error
    assert reads["n"] == 0  # the library was never read
    assert finished.counters.processed == 0


async def test_stop_is_felt_while_the_library_is_being_read(monkeypatch):
    """A Stop press during the YouTube read must be honoured mid-read.

    Reading a large playlist used to swallow the flag for its whole duration,
    which is exactly how a Stop button comes to look dead.
    """
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=True)

    async def get_tracks(playlist_id, raise_if_cancelled=None):
        # A large read in miniature: the user presses Stop half-way through,
        # and the next page check must feel it.
        engine.store.cancel(job.id)
        for _ in range(50):
            if raise_if_cancelled:
                raise_if_cancelled()
        return "Road Trip", [yt("A")]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)

    await engine.run_job(job.id, TransferRequest(playlist_ids=["PL1"], dry_run=True))

    finished = engine.store.get(job.id)
    assert finished.status == "cancelled"
    # Only the pre-flight search was spent; no song was ever searched.
    assert service.searches == [engine.PREFLIGHT_QUERY]


# ---------------------------------------------------------------------------
# A status that outlived the process that set it.
# ---------------------------------------------------------------------------


def test_a_job_left_running_by_a_restart_is_not_still_running(isolate_job_files):
    """A job only runs in memory, so "running" on disk after a restart is a lie.

    Left uncorrected it is permanent, and Resume refuses to touch a job it
    believes is still going - which is exactly how a transfer gets stuck.
    """
    job = engine.store.create(dry_run=False)
    job.status = "running"
    job.notice = "Waiting for Spotify."
    engine.store.save(job)

    restarted = engine.JobStore()
    recovered = restarted.get(job.id)

    assert recovered.status == "failed"
    assert "restarted" in recovered.error
    assert recovered.notice is None


def test_the_corrected_status_is_written_back_to_disk(isolate_job_files):
    job = engine.store.create(dry_run=False)
    job.status = "running"
    engine.store.save(job)

    engine.JobStore().get(job.id)

    on_disk = json.loads((isolate_job_files / f"{job.id}.json").read_text())
    assert on_disk["status"] == "failed"


def test_a_genuinely_running_job_is_left_alone(isolate_job_files):
    """The correction must only fire when no task is actually running."""

    async def check():
        job = engine.store.create(dry_run=False)
        job.status = "running"
        engine.store.save(job)

        async def forever():
            await asyncio.sleep(30)

        task = asyncio.create_task(forever())
        engine.store.register_task(job.id, task)
        engine.store._jobs.clear()  # force the disk path

        try:
            assert engine.store.get(job.id).status == "running"
        finally:
            task.cancel()

    asyncio.run(check())


def test_the_recent_list_does_not_show_a_dead_job_as_running(isolate_job_files):
    job = engine.store.create(dry_run=False)
    job.status = "running"
    engine.store.save(job)

    summaries = engine.JobStore().list_summaries()

    assert [s.status for s in summaries] == ["failed"]


# ---------------------------------------------------------------------------
# The same song in two playlists.
#
# A real library overlaps: most of Gym is in Liked music too. Resume once
# treated "searched anywhere" as "done for every playlist", so those playlists
# stayed a few songs short for ever and the run called itself finished at 96%.
# ---------------------------------------------------------------------------


@pytest.fixture
def overlapping_library(monkeypatch):
    """Two playlists that share one song."""
    shared = yt("Blinding Lights", ["The Weeknd"], seconds=200, video_id="v1")
    library = {
        "PL1": ("Road Trip", [shared, yt("Levitating", ["Dua Lipa"], seconds=203, video_id="v3")]),
        "PL2": ("Gym", [shared]),
    }

    async def fake_get_tracks(playlist_id, raise_if_cancelled=None):
        return library[playlist_id]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", fake_get_tracks)
    return library


def row(track, candidate, playlist_name) -> MatchResult:
    return build_result(track, candidate, 1.0, 0.86, 0.70, playlist_name=playlist_name)


async def test_a_song_in_two_playlists_is_counted_for_both_on_resume(
    monkeypatch, overlapping_library
):
    """The bug seen on a real library: 65 songs 'not searched yet' for ever."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    request = TransferRequest(playlist_ids=["PL1", "PL2"], dry_run=True)
    job = engine.store.create(dry_run=True, request=request)
    # An earlier attempt finished Road Trip, then died before Gym.
    weeknd = sp("Blinding Lights", ["The Weeknd"], track_id="t1")
    job.results = [
        row(yt("Blinding Lights", ["The Weeknd"], video_id="v1"), weeknd, "Road Trip"),
        row(yt("Levitating", ["Dua Lipa"], seconds=203, video_id="v3"), sp("Levitating", ["Dua Lipa"], seconds=203, track_id="t3"), "Road Trip"),
    ]
    job.playlists = [
        PlaylistProgress(source_id="PL1", source_title="Road Trip", total=2, processed=2),
        PlaylistProgress(source_id="PL2", source_title="Gym", total=1, processed=0),
    ]
    job.counters.total, job.counters.processed, job.counters.matched = 3, 2, 2
    job.status = "failed"
    engine.store.save(job)

    await engine.run_job(job.id, request, resume=True)

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    assert finished.counters.processed == finished.counters.total == 3
    gym = next(p for p in finished.playlists if p.source_id == "PL2")
    assert gym.processed == gym.total == 1
    assert [r.playlist_name for r in finished.results if r.source.video_id == "v1"] == [
        "Road Trip",
        "Gym",
    ]
    # And it cost nothing: the answer was already known from Road Trip.
    assert service.searches == [engine.PREFLIGHT_QUERY]


async def test_a_song_already_matched_elsewhere_is_not_searched_again(
    monkeypatch, overlapping_library
):
    """First run, no resume: the shared song is searched once and answered twice."""
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    job = engine.store.create(dry_run=True)
    await engine.run_job(job.id, TransferRequest(playlist_ids=["PL1", "PL2"], dry_run=True))

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    assert finished.counters.processed == finished.counters.total == 3
    assert finished.counters.matched == 3
    assert sum("Blinding Lights" in s for s in service.searches) == 1


async def test_the_same_song_twice_in_one_playlist_is_counted_twice(monkeypatch):
    """YouTube allows a duplicate inside a playlist. Both copies count."""
    twice = yt("Blinding Lights", ["The Weeknd"], seconds=200, video_id="v1")

    async def get_tracks(_playlist_id, raise_if_cancelled=None):
        return "Road Trip", [twice, twice]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    request = TransferRequest(playlist_ids=["PL1"], dry_run=True)
    job = engine.store.create(dry_run=True, request=request)
    # The first copy was done, then it died.
    job.results = [row(twice, sp("Blinding Lights", ["The Weeknd"], track_id="t1"), "Road Trip")]
    job.playlists = [PlaylistProgress(source_id="PL1", source_title="Road Trip", total=2, processed=1)]
    job.counters.total, job.counters.processed, job.counters.matched = 2, 1, 1
    job.status = "failed"
    engine.store.save(job)

    await engine.run_job(job.id, request, resume=True)

    finished = engine.store.get(job.id)
    assert finished.status == "completed"
    assert finished.counters.processed == finished.counters.total == 2
    assert service.searches == [engine.PREFLIGHT_QUERY]


async def test_already_there_does_not_grow_with_every_resume(monkeypatch, fake_library):
    """A resume re-checks every playlist. That must not inflate the counters.

    On a real library "already in your playlists" reached 5,661 after three
    resumes of a 1,754-song job, because each check was added to the last.
    """
    request = TransferRequest(playlist_ids=["PL1"], include_liked=True, dry_run=False)
    job = engine.store.create(dry_run=False, request=request)

    first = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: first)
    await engine.run_job(job.id, request)
    assert engine.store.get(job.id).counters.added == 2

    # Resume twice. The playlists now exist and hold both songs.
    for _ in range(2):
        again = spotify_service_with_hits()
        again._playlist = {"id": "p1", "external_urls": {}}
        again._existing = {"t1", "t3"}
        monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: again)
        await engine.run_job(job.id, request, resume=True)

    finished = engine.store.get(job.id)
    # Still the two this job added, and "already there" is the latest check,
    # not three checks added together.
    assert finished.counters.added == 2
    assert finished.counters.skipped_duplicates == 2
    assert finished.counters.skipped_duplicates <= finished.counters.matched


async def test_a_playlist_that_shrank_on_youtube_gets_its_new_total(monkeypatch):
    """'33 left' must mean 33 songs that exist now, not at the first read."""
    library = {"PL1": ("Road Trip", [yt("A", video_id="v1")])}

    async def get_tracks(playlist_id, raise_if_cancelled=None):
        return library[playlist_id]

    monkeypatch.setattr(engine.ytmusic, "get_playlist_tracks", get_tracks)
    service = spotify_service_with_hits()
    monkeypatch.setattr(engine, "SpotifyClient", lambda **_kw: service)

    request = TransferRequest(playlist_ids=["PL1"], dry_run=True)
    job = engine.store.create(dry_run=True, request=request)
    # The first read saw three songs; two were since removed on YouTube.
    job.playlists = [PlaylistProgress(source_id="PL1", source_title="Road Trip", total=3, processed=0)]
    job.counters.total = 3
    job.status = "failed"
    engine.store.save(job)

    await engine.run_job(job.id, request, resume=True)

    finished = engine.store.get(job.id)
    assert finished.playlists[0].total == 1
    assert finished.counters.processed == finished.counters.total == 1
