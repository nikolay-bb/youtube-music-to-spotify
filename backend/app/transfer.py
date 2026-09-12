"""The transfer engine: read from YouTube Music, match, write to Spotify.

One "job" is one run. A job is kept in memory while it runs and written to
``backend/data/jobs/<id>.json`` as it goes, so closing the browser, or the
server crashing, never loses the report.

Everything the engine writes to Spotify is safe to repeat: it reuses a playlist
of the same name instead of making a second one, and skips any song already in
it. Running the same transfer twice adds nothing the second time.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone

from . import ytmusic
from .config import JOBS_DIR, Settings, ensure_data_dirs, get_settings
from .errors import describe_transport_error
from .matcher import build_result, build_search_queries, pick_best
from .models import (
    Job,
    JobCounters,
    JobSummary,
    MatchResult,
    PlaylistProgress,
    SpotifyCandidate,
    TransferRequest,
    YtTrack,
)
from .spotify import (
    SpotifyCancelled,
    SpotifyClient,
    SpotifyError,
    SpotifyQuotaExhausted,
)
from .ytmusic import LIKED_SONGS_ID

log = logging.getLogger(__name__)

# What the liked-songs playlist is called on the Spotify side. It is a normal
# playlist, not Spotify's built-in "Liked Songs", so it is easy to review and
# easy to delete if the matching disappoints.
LIKED_PLAYLIST_NAME = "YouTube Music Likes"

# The one search made before a run starts, to check the day's quota is open.
# Any query works; this one is short and always has results.
PREFLIGHT_QUERY = "abba"

# Write the job file to disk after this many songs.
SAVE_EVERY = 10

# Stop the run after this many songs in a row end in an "error" verdict. One
# odd song failing is normal. Five in a row means Spotify, the network or the
# app is broken, and the honest answer is to stop with that reason rather
# than race through the library marking every song as an error.
MAX_CONSECUTIVE_ERRORS = 5


class JobCancelled(Exception):
    """Raised inside the engine when the user presses Cancel."""


def _summarise(job: Job) -> JobSummary:
    """Strip a job down to what the recent-transfers list shows."""
    return JobSummary(
        id=job.id,
        status=job.status,
        dry_run=job.dry_run,
        created_at=job.created_at,
        updated_at=job.updated_at,
        finished_at=job.finished_at,
        error=job.error,
        counters=job.counters,
        playlist_titles=[p.source_title for p in job.playlists],
    )


class JobStore:
    """Holds running jobs in memory and mirrors every job to disk."""

    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancelled: set[str] = set()

    def create(self, dry_run: bool, request: TransferRequest | None = None) -> Job:
        ensure_data_dirs()
        job = Job(id=uuid.uuid4().hex[:12], dry_run=dry_run, request=request)
        self._jobs[job.id] = job
        self.save(job)
        return job

    def get(self, job_id: str) -> Job | None:
        """Look in memory first, then fall back to the file on disk."""
        if job_id in self._jobs:
            return self._jobs[job_id]

        path = JOBS_DIR / f"{job_id}.json"
        if not path.exists():
            return None
        try:
            job = Job.model_validate_json(path.read_text())
        except (OSError, ValueError) as exc:
            log.warning("Job file %s is unreadable: %s", path, exc)
            return None

        if self._reconcile(job):
            self.save(job)
        self._jobs[job_id] = job
        return job

    def _reconcile(self, job: Job) -> bool:
        """Correct a status that outlived the process running it.

        A job only runs inside this process, so one loaded from disk as
        "running" means the server was restarted or crashed mid-run. Left
        alone that status is permanent, and Resume refuses to touch a job it
        believes is still going. Returns True when something was changed.
        """
        if job.status not in {"running", "pending"} or job.id in self._tasks:
            return False

        job.status = "failed"
        job.error = (
            "The transfer stopped because the app was restarted. Nothing is "
            "lost - press Resume to carry on from where it left off."
        )
        job.notice = None
        job.waiting_until = None
        log.info("Job %s was marked running but nothing is running it", job.id)
        return True

    def save(self, job: Job) -> None:
        ensure_data_dirs()
        job.touch()
        (JOBS_DIR / f"{job.id}.json").write_text(job.model_dump_json(indent=2))

    def list_summaries(self, limit: int = 20) -> list[JobSummary]:
        """Every job on disk, newest first, without the results.

        Reads the folder rather than memory, so jobs from an earlier run of the
        server still appear. Without this a restart hides finished work behind
        a URL nobody wrote down.
        """
        ensure_data_dirs()
        summaries: dict[str, JobSummary] = {}

        # A job still in memory is fresher than its file, which is only written
        # every SAVE_EVERY songs, so let memory win.
        for job in self._jobs.values():
            summaries[job.id] = _summarise(job)

        for path in sorted(JOBS_DIR.glob("*.json")):
            if path.stem in summaries:
                continue
            try:
                job = Job.model_validate_json(path.read_text())
            except (OSError, ValueError) as exc:
                # One corrupt file must not hide every other transfer.
                log.warning("Job file %s is unreadable: %s", path, exc)
                continue
            # A job left as "running" by a restart must not be listed as live.
            if self._reconcile(job):
                self.save(job)
            summaries[path.stem] = _summarise(job)

        newest_first = sorted(summaries.values(), key=lambda s: s.updated_at, reverse=True)
        return newest_first[:limit]

    def cancel(self, job_id: str) -> bool:
        """Ask a running job to stop at the next song."""
        job = self.get(job_id)
        if job is None or job.status not in {"pending", "running"}:
            return False
        self._cancelled.add(job_id)
        return True

    def clear_cancelled(self, job_id: str) -> None:
        """Forget a stop from an earlier run, at the moment a new one starts.

        Called by the routes rather than by ``run_job`` itself: a stop pressed
        in the instant before the task starts must still be honoured, so the
        run itself must never wipe the flag.
        """
        self._cancelled.discard(job_id)

    def is_cancelled(self, job_id: str) -> bool:
        return job_id in self._cancelled

    def register_task(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._tasks[job_id] = task
        task.add_done_callback(lambda _t: self._tasks.pop(job_id, None))


store = JobStore()


# ---------------------------------------------------------------------------
# Matching one song.
# ---------------------------------------------------------------------------


async def match_track(
    spotify: SpotifyClient,
    track: YtTrack,
    playlist_name: str,
    settings: Settings,
    counters: JobCounters,
) -> MatchResult:
    """Try each search query until one is confidently right.

    Stopping early matters: Spotify's Development Mode rate limits are low, and
    most songs are found by the first, most precise query. Only the awkward ones
    cost a second or third call.
    """
    best_overall: SpotifyCandidate | None = None
    best_score = 0.0

    try:
        for query in build_search_queries(track)[: settings.max_queries_per_song]:
            candidates = await spotify.search_tracks(query)
            counters.searches += 1
            candidate, score = pick_best(track, candidates)

            if candidate is not None and score > best_score:
                best_overall, best_score = candidate, score

            if best_score >= settings.match_threshold:
                break
    except SpotifyError:
        raise
    except Exception as exc:  # noqa: BLE001 - one bad song must not kill the run
        log.exception("Search failed for %s", track.label())
        return MatchResult(
            source=track,
            verdict="error",
            reason=f"Search failed. {describe_transport_error(exc, 'Spotify')}",
            playlist_name=playlist_name,
        )

    return build_result(
        track,
        best_overall,
        best_score,
        settings.match_threshold,
        settings.review_threshold,
        playlist_name=playlist_name,
    )


def _chunks(items: list[YtTrack], size: int) -> list[list[YtTrack]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


# ---------------------------------------------------------------------------
# Running a whole job.
# ---------------------------------------------------------------------------


async def run_job(job_id: str, request: TransferRequest, resume: bool = False) -> None:
    """Execute one transfer. Never raises: failures are recorded on the job.

    With ``resume`` set, songs already in ``job.results`` are skipped and their
    verdicts kept. That matters because Spotify's Development Mode quota is easy
    to exhaust part-way through a large library, and re-searching a thousand
    songs that were already matched would simply exhaust it again.
    """
    settings = get_settings()
    job = store.get(job_id)
    if job is None:
        log.error("Job %s vanished before it started", job_id)
        return

    # Songs already dealt with on an earlier attempt, per playlist.
    done: Counter[tuple[str, str]] = Counter()
    if resume:
        # Rows that ended in an error - a dropped connection, a crash on one
        # odd title - deserve another go, so they are dropped here and
        # searched again. Everything that reached a real verdict is kept.
        failed = sum(1 for r in job.results if r.verdict == "error")
        if failed:
            job.results = [r for r in job.results if r.verdict != "error"]
            log.info("Resuming job %s: %d songs that hit an error will be searched again", job_id, failed)
        # The counters are rebuilt from the rows that are kept, so a drift
        # left by an earlier attempt cannot make the progress bar lie.
        _recount(job)
        done = _already_done(job)
        job.error = None
        log.info("Resuming job %s, skipping %d songs already processed", job_id, len(job.results))
    else:
        job.results.clear()
        job.playlists.clear()
        job.counters = JobCounters()

    # Spotify may already have told us when the quota comes back. That answer
    # is kept while it is still in the future, so it can be checked before
    # the flag is cleared for a fresh start.
    pending_reset = job.quota_resets_at
    job.status = "running"
    # Last run's stop reason must not linger on this one.
    job.quota_resets_at = None
    store.save(job)

    def note_rate_limit(seconds: float, reason: str) -> None:
        minutes = seconds / 60
        pretty = f"{minutes:.0f} minutes" if minutes >= 1 else f"{seconds:.0f} seconds"
        job.notice = (
            f"Spotify says {reason}. Waiting {pretty} before trying again. "
            "Nothing is lost - this carries on by itself."
        )
        job.waiting_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        store.save(job)

    def _raise_if_stopped() -> None:
        _raise_if_cancelled(job_id)

    try:
        # If the quota is known to be spent until a moment still in the
        # future, do not even start. Pressing Resume on a spent quota used to
        # look like a run in progress that died a minute later; this answers
        # at once, without a single request.
        if pending_reset and pending_reset > datetime.now(timezone.utc):
            raise SpotifyQuotaExhausted(
                "Spotify's daily limit is still used up, so this run was not "
                "started. Nothing is lost. Come back at the time shown and "
                "press Resume again.",
                pending_reset,
            )

        async with SpotifyClient(
            on_rate_limit=note_rate_limit,
            should_cancel=lambda: store.is_cancelled(job_id),
        ) as spotify:
            # Fail fast and clearly if Spotify is not usable, rather than
            # discovering it after twenty minutes of searching.
            await spotify.current_user()

            # One cheap search before reading the library, so a spent quota
            # is caught within a second of pressing Resume. Costs one search
            # from the day's allowance when all is well.
            await spotify.search_tracks(PREFLIGHT_QUERY)

            # Liked songs go last. It is usually far bigger than every named
            # playlist put together, so running it first would spend the whole
            # daily search quota and leave the named ones untouched.
            targets = [pid for pid in request.playlist_ids if pid != LIKED_SONGS_ID]
            if request.include_liked or LIKED_SONGS_ID in request.playlist_ids:
                targets.append(LIKED_SONGS_ID)
            if not targets:
                raise SpotifyError("No playlists were selected.")

            # Read everything from YouTube Music first, so the progress bar has
            # a real total from the very first update.
            sources: list[tuple[str, str, list[YtTrack]]] = []
            for number, playlist_id in enumerate(targets, start=1):
                # Stop is checked between playlists and between pages, so a
                # press is never swallowed by a long read.
                _raise_if_cancelled(job_id)
                job.activity = (
                    f"Reading playlist {number} of {len(targets)} from YouTube Music"
                )
                store.save(job)

                # One unreadable playlist must not kill a run that covers
                # twenty. A missing one is skipped with a warning on the job;
                # a transient failure gets one more try; anything still
                # failing is a real problem and stops the run.
                title: str | None = None
                tracks: list[YtTrack] = []
                for attempt in (1, 2):
                    try:
                        title, tracks = await ytmusic.get_playlist_tracks(
                            playlist_id, raise_if_cancelled=_raise_if_stopped
                        )
                        break
                    except ytmusic.PlaylistNotFound:
                        known = next(
                            (p.source_title for p in job.playlists if p.source_id == playlist_id),
                            playlist_id,
                        )
                        warning = (
                            f"'{known}' could not be read from YouTube Music, so it was "
                            "skipped. It looks deleted or unavailable there. Everything "
                            "else carried on."
                        )
                        if warning not in job.warnings:
                            job.warnings.append(warning)
                        log.warning("Skipping playlist %s (%s): not found", playlist_id, known)
                        store.save(job)
                        break
                    except ytmusic.YouTubeMusicError as exc:
                        if attempt == 2:
                            raise
                        log.warning(
                            "Reading %s failed once (%s); trying one more time",
                            playlist_id,
                            exc,
                        )

                if title is None:
                    continue

                if playlist_id == LIKED_SONGS_ID:
                    title = LIKED_PLAYLIST_NAME
                sources.append((playlist_id, title, tracks))
                known_progress = next(
                    (p for p in job.playlists if p.source_id == playlist_id), None
                )
                if known_progress is None:
                    job.playlists.append(
                        PlaylistProgress(
                            source_id=playlist_id, source_title=title, total=len(tracks)
                        )
                    )
                else:
                    # The playlist may have changed on YouTube since the first
                    # read, and "33 left" must mean 33 songs that exist now.
                    known_progress.total = len(tracks)

            # Honour a Stop pressed while the library was being read, and
            # say so clearly when nothing readable was found at all.
            _raise_if_cancelled(job_id)
            if not sources:
                raise SpotifyError(
                    "None of the selected playlists could be read from YouTube Music."
                )

            job.counters.total = sum(len(tracks) for _, _, tracks in sources)
            store.save(job)

            for playlist_id, title, tracks in sources:
                _raise_if_cancelled(job_id)
                progress = next(p for p in job.playlists if p.source_id == playlist_id)
                job.activity = f"Starting '{title}'"
                store.save(job)
                await _process_playlist(
                    spotify, job, progress, title, tracks, settings, request, done
                )

            job.status = "completed"

    except (JobCancelled, SpotifyCancelled):
        job.status = "cancelled"
        log.info("Job %s cancelled by the user", job_id)
    except SpotifyQuotaExhausted as exc:
        # Not a failure of the job, just the end of today's allowance.
        job.status = "failed"
        job.error = str(exc)
        job.quota_resets_at = exc.resets_at
        log.warning("Job %s stopped on the daily quota", job_id)
    except Exception as exc:  # noqa: BLE001 - the UI must show why it stopped
        job.status = "failed"
        job.error = str(exc)
        log.exception("Job %s failed", job_id)

    job.notice = None
    job.waiting_until = None
    job.activity = None
    job.finished_at = datetime.now(timezone.utc)
    store.save(job)


def _raise_if_cancelled(job_id: str) -> None:
    if store.is_cancelled(job_id):
        raise JobCancelled


async def _process_playlist(
    spotify: SpotifyClient,
    job: Job,
    progress: PlaylistProgress,
    title: str,
    tracks: list[YtTrack],
    settings: Settings,
    request: TransferRequest,
    done: Counter[tuple[str, str]],
) -> None:
    """Match every song in one playlist, then write the winners to Spotify."""
    # Keep the verdicts an earlier attempt already reached for this playlist.
    matched: list[MatchResult] = [
        r
        for r in job.results
        if r.playlist_name == title and r.verdict == "matched" and r.best is not None
    ]

    # Skip the copies of each song that this playlist already has a row for.
    # It is a count, not a set, because YouTube lets one video appear twice
    # in a playlist and both copies count towards the total.
    pending: list[YtTrack] = []
    seen: Counter[str] = Counter()
    for track in tracks:
        seen[track.video_id] += 1
        if seen[track.video_id] > done[(title, track.video_id)]:
            pending.append(track)
    if len(pending) < len(tracks):
        log.info("'%s': %d of %d already done", title, len(tracks) - len(pending), len(tracks))

    # A song answered in another playlist gets the same answer here without
    # another search. Matching is about the song, not the playlist, and
    # Spotify's daily quota is the scarce thing. A real library overlaps a
    # lot: most of Gym is in Liked music too.
    known: dict[str, MatchResult] = {}
    for previous in job.results:
        if previous.verdict != "error":
            known.setdefault(previous.source.video_id, previous)

    # Search a couple of songs at a time. The client's own semaphore is the real
    # brake; this just keeps enough work in flight to reach it.
    batch_size = max(1, settings.spotify_max_concurrency)

    for chunk in _chunks(pending, batch_size):
        _raise_if_cancelled(job.id)

        results: list[MatchResult] = []
        to_search: list[YtTrack] = []
        for track in chunk:
            previous = known.get(track.video_id)
            if previous is None:
                to_search.append(track)
            else:
                results.append(
                    previous.model_copy(update={"source": track, "playlist_name": title})
                )

        if to_search:
            # Named here rather than counted, so the line changes every song
            # even when a playlist is small enough that the percentage does
            # not move.
            job.activity = f"Searching Spotify for '{to_search[0].title}' — {title}"
            results.extend(
                await asyncio.gather(
                    *(
                        match_track(spotify, track, title, settings, job.counters)
                        for track in to_search
                    )
                )
            )

        if job.notice:
            job.notice = None
            job.waiting_until = None

        for result in results:
            job.results.append(result)
            _count(job.counters, result)
            progress.processed += 1
            if result.verdict == "matched" and result.best is not None:
                matched.append(result)
            if result.verdict != "error":
                known.setdefault(result.source.video_id, result)

        # A run of errors is not bad luck, it is a broken connection or a
        # broken app. Stop with the last reason while the report still means
        # something; Resume searches these songs again.
        tail = job.results[-MAX_CONSECUTIVE_ERRORS:]
        if len(tail) == MAX_CONSECUTIVE_ERRORS and all(r.verdict == "error" for r in tail):
            store.save(job)
            raise SpotifyError(
                f"{MAX_CONSECUTIVE_ERRORS} songs in a row failed to search, so the run "
                "stopped rather than mark the whole library as errors. The last "
                f"failure was: {tail[-1].reason} Fix that, then press Resume and those "
                "songs will be searched again."
            )

        if progress.processed % SAVE_EVERY < batch_size:
            store.save(job)

    store.save(job)

    if not matched:
        log.info("Nothing confidently matched in %s, so no playlist was written", title)
        return

    if request.dry_run:
        log.info("Dry run: would have added %d songs to '%s'", len(matched), title)
        return

    await _write_playlist(spotify, job, progress, title, matched)


async def _write_playlist(
    spotify: SpotifyClient,
    job: Job,
    progress: PlaylistProgress,
    title: str,
    matched: list[MatchResult],
) -> None:
    """Create or reuse the Spotify playlist, then add the songs that are missing."""
    job.activity = f"Opening '{title}' on Spotify"
    store.save(job)
    playlist = await spotify.find_playlist_by_name(title)
    if playlist is None:
        stamp = datetime.now(timezone.utc).strftime("%d %B %Y")
        playlist = await spotify.create_playlist(
            title,
            description=f"Imported from YouTube Music on {stamp}."[:300],
        )
        existing: set[str] = set()
        log.info("Created Spotify playlist '%s'", title)
    else:
        existing = await spotify.get_playlist_track_ids(playlist["id"])
        log.info("Reusing Spotify playlist '%s' (%d songs already in it)", title, len(existing))

    progress.spotify_playlist_id = playlist.get("id")
    progress.spotify_playlist_url = (playlist.get("external_urls") or {}).get("spotify")

    # Skip anything already there, and anything the matcher picked twice.
    uris: list[str] = []
    seen: set[str] = set(existing)
    duplicates = 0
    for result in matched:
        candidate = result.best
        if candidate is None:
            continue
        if candidate.id in seen:
            duplicates += 1
            continue
        seen.add(candidate.id)
        uris.append(candidate.uri)

    if uris:
        job.activity = f"Adding {len(uris)} songs to '{title}' on Spotify"
        store.save(job)
    added = await spotify.add_tracks(playlist["id"], uris) if uris else 0

    # Per playlist: "added" builds up across runs (a song cannot be added
    # twice, so it never double counts) and "already there" is the latest
    # check. The job-level numbers are sums of those, never running totals:
    # a resume re-checks every playlist, and "already in your playlists" was
    # growing past the size of the library.
    progress.added += added
    progress.skipped_duplicates = duplicates
    playlists = job.playlists if any(p is progress for p in job.playlists) else [*job.playlists, progress]
    job.counters.added = sum(p.added for p in playlists)
    job.counters.skipped_duplicates = sum(p.skipped_duplicates for p in playlists)
    store.save(job)
    log.info("Added %d songs to '%s' (%d already there)", added, title, duplicates)


def _count(counters: JobCounters, result: MatchResult) -> None:
    counters.processed += 1
    if result.verdict == "matched":
        counters.matched += 1
    elif result.verdict == "low_confidence":
        counters.low_confidence += 1
    elif result.verdict == "not_found":
        counters.not_found += 1
    else:
        counters.errors += 1


def _already_done(job: Job) -> Counter[tuple[str, str]]:
    """How many result rows each (playlist, video) pair already has.

    Keyed by the pair, not the video alone. A song that sits in both Gym and
    Liked music must get a row for each, or Gym stays one short for ever and
    Resume reports itself finished at 96%. That is exactly what happened on
    a real library before this was keyed per playlist.
    """
    return Counter((r.playlist_name, r.source.video_id) for r in job.results)


def _recount(job: Job) -> None:
    """Rebuild the verdict counters and per-playlist progress from the results.

    Used on resume after error rows are dropped, so the numbers agree with
    what is actually kept. The write-side counters (added, duplicates,
    searches) are not derived from results and are left alone.
    """
    counters = job.counters
    counters.processed = 0
    counters.matched = counters.low_confidence = counters.not_found = counters.errors = 0
    for result in job.results:
        _count(counters, result)
    for progress in job.playlists:
        progress.processed = sum(
            1 for r in job.results if r.playlist_name == progress.source_title
        )


async def apply_matches(job_id: str) -> None:
    """Write a finished dry run's matches to Spotify without searching again.

    A dry run has already worked out which Spotify track each song maps to.
    Turning that into real playlists needs only the playlist writes - roughly
    one request per hundred songs - instead of repeating one search per song.
    On a Development Mode quota that is the difference between finishing and
    stopping half way.
    """
    job = store.get(job_id)
    if job is None:
        log.error("Job %s not found", job_id)
        return

    def note_rate_limit(seconds: float, reason: str) -> None:
        job.notice = f"Spotify says {reason}. Waiting {seconds / 60:.0f} minutes."
        job.waiting_until = datetime.now(timezone.utc) + timedelta(seconds=seconds)
        store.save(job)

    job.status = "running"
    job.error = None
    job.quota_resets_at = None
    store.save(job)

    try:
        async with SpotifyClient(
            on_rate_limit=note_rate_limit,
            should_cancel=lambda: store.is_cancelled(job_id),
        ) as spotify:
            await spotify.current_user()

            # Group the matches by the playlist they came from.
            by_playlist: dict[str, list[MatchResult]] = {}
            for result in job.results:
                if result.verdict == "matched" and result.best is not None:
                    by_playlist.setdefault(result.playlist_name, []).append(result)

            for title, matched in by_playlist.items():
                _raise_if_cancelled(job_id)
                progress = next(
                    (p for p in job.playlists if p.source_title == title), None
                )
                if progress is None:
                    progress = PlaylistProgress(
                        source_id=title, source_title=title, total=len(matched)
                    )
                    job.playlists.append(progress)
                await _write_playlist(spotify, job, progress, title, matched)

        job.dry_run = False
        job.status = "completed"

    except (JobCancelled, SpotifyCancelled):
        job.status = "cancelled"
    except SpotifyQuotaExhausted as exc:
        job.status = "failed"
        job.error = str(exc)
        job.quota_resets_at = exc.resets_at
        log.warning("Applying job %s stopped on the daily quota", job_id)
    except Exception as exc:  # noqa: BLE001 - the UI must show why it stopped
        job.status = "failed"
        job.error = str(exc)
        log.exception("Applying job %s failed", job_id)

    job.notice = None
    job.waiting_until = None
    job.activity = None
    job.finished_at = datetime.now(timezone.utc)
    store.save(job)


# ---------------------------------------------------------------------------
# The downloadable report.
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "playlist",
    "verdict",
    "score",
    "youtube_title",
    "youtube_artists",
    "youtube_duration_seconds",
    "spotify_title",
    "spotify_artists",
    "spotify_duration_seconds",
    "spotify_url",
    "reason",
]


def results_to_csv(job: Job, only_problems: bool = False) -> str:
    """Flatten a job's results into a spreadsheet.

    ``only_problems`` keeps just the rows that need a human: the ones held back
    for low confidence, the ones not found, and any errors.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)

    for result in job.results:
        if only_problems and result.verdict == "matched":
            continue
        best = result.best
        writer.writerow(
            [
                result.playlist_name,
                result.verdict,
                f"{result.score:.3f}",
                result.source.title,
                ", ".join(result.source.artists),
                result.source.duration_seconds or "",
                best.title if best else "",
                ", ".join(best.artists) if best else "",
                (best.duration_seconds or "") if best else "",
                (best.url or "") if best else "",
                result.reason,
            ]
        )

    return buffer.getvalue()
