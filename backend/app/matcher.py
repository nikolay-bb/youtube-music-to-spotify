"""Decide whether a Spotify search result is really the same song.

Spotify removed ISRC codes from track objects in February 2026. An ISRC is a
unique identifier for one specific recording, and it would have made this file
almost unnecessary. Without it we compare three weaker signals - title, artist
and duration - and subtract points for the specific ways a search result can
look right while being wrong.

The scoring runs in two parts:

1. A weighted average of the signals we have, giving a base score.
2. Penalties for definite disqualifiers: a remix or live take, a tribute or
   karaoke act, a wildly different length, or an artist who simply is not the
   same person.

The penalties matter more than the average. Most wrong results score highly on
title and artist, and are only caught by a penalty.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz

from .models import MatchResult, SpotifyCandidate, Verdict, YtTrack

# ---------------------------------------------------------------------------
# Weights for the base score. They add up to 1.0. If a signal is missing for a
# given pair - Spotify not reporting a duration, say - its weight is removed and
# the rest are rescaled, so an unknown never counts as a mismatch.
# ---------------------------------------------------------------------------
TITLE_WEIGHT = 0.50
ARTIST_WEIGHT = 0.35
DURATION_WEIGHT = 0.15

ALBUM_BONUS = 0.05

# Penalties. Each one marks a reason to believe the recordings differ.
VERSION_PENALTY = 0.15
DURATION_MISMATCH_PENALTY = 0.20
ARTIST_MISMATCH_PENALTY = 0.20
IMPOSTOR_PENALTY = 0.25

# Two recordings of the same song are normally seconds apart. Past this many
# seconds the duration signal scores zero.
DURATION_TOLERANCE_SECONDS = 10
# Past this many seconds apart they are almost certainly different recordings,
# so a penalty applies on top of the zero.
DURATION_HARD_LIMIT_SECONDS = 25
# Artist similarity below this means it is a different act, not a spelling slip.
ARTIST_MISMATCH_LIMIT = 0.45

# Stylised spellings artists use. Applied only between two word characters, so
# "Ke$ha" becomes "kesha" while a trailing "Panic!" simply loses the mark.
STYLISED_CHARACTERS = {"$": "s", "!": "i", "@": "a"}

# Rubbish that appears in YouTube titles but never in Spotify titles, plus
# edition markers that do not change the recording.
NOISE_PATTERNS = [
    r"\(\s*official\s+(music\s+)?video\s*\)",
    r"\(\s*official\s+audio\s*\)",
    r"\(\s*official\s+lyric[s]?\s*(video)?\s*\)",
    r"\(\s*official\s+visuali[sz]er\s*\)",
    r"\(\s*official\s*\)",
    r"\(\s*lyric[s]?\s*(video)?\s*\)",
    r"\(\s*audio\s*\)",
    r"\(\s*visuali[sz]er\s*\)",
    r"\(\s*music\s+video\s*\)",
    r"\(\s*video\s*\)",
    r"\(\s*explicit\s*\)",
    r"\[\s*official\s+(music\s+)?video\s*\]",
    r"\[\s*lyric[s]?\s*(video)?\s*\]",
    r"\[\s*audio\s*\]",
    r"[\[\(]\s*(hd|hq|4k|1080p|720p)\s*[\]\)]",
    r"\bofficial\s+m/?v\b",
    r"\bm/v\b",
    # Edition markers: same recording, different packaging.
    r"[\[\(]\s*(\d{4}\s+)?remaster(ed)?(\s+\d{4})?\s*[\]\)]",
    r"[\[\(]\s*(album|single)\s+version\s*[\]\)]",
    r"[\[\(]\s*bonus\s+track\s*[\]\)]",
    r"[\[\(]\s*deluxe(\s+edition)?\s*[\]\)]",
]

# Words meaning "this is a different recording of that song".
VERSION_KEYWORDS = [
    "remix",
    "live",
    "karaoke",
    "cover",
    "acoustic",
    "instrumental",
    "sped up",
    "spedup",
    "slowed",
    "nightcore",
    "mashup",
    "bootleg",
    "demo",
    "reverb",
    "extended",
    "acapella",
]

# Words in an artist name that mean "not the original act". Spotify search is
# full of these, and they score deceptively well on title.
IMPOSTOR_MARKERS = [
    "karaoke",
    "tribute",
    "cover",
    "covers",
    "made famous by",
    "in the style of",
    "backing track",
    "instrumental",
]

# "(feat. X)" and "(with X)" in brackets, anywhere in the title.
_FEAT_INLINE_PATTERN = re.compile(
    r"[\(\[]\s*(?:feat\.?|ft\.?|featuring|with)\s+([^)\]]+?)\s*[\)\]]",
    re.IGNORECASE,
)
# A trailing credit with no brackets. "with" is deliberately absent here: it
# would eat the end of ordinary titles such as "Dancing With Myself".
_FEAT_TRAILING_PATTERN = re.compile(
    r"\s(?:feat\.?|ft\.?|featuring)\s+(.+)$",
    re.IGNORECASE,
)
_STYLISED_PATTERN = re.compile(r"(?<=\w)([$!@])(?=\w)")


def normalise(text: str) -> str:
    """Lower-case, undo stylised spellings, drop punctuation, collapse spaces.

    ``\\w`` is unicode aware in Python, so Cyrillic, Greek and CJK titles keep
    their letters instead of being wiped out.
    """
    if not text:
        return ""
    text = text.lower()
    text = _STYLISED_PATTERN.sub(lambda m: STYLISED_CHARACTERS[m.group(1)], text)
    text = re.sub(r"[^\w\s]", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _strip_dash_suffix(title: str) -> str:
    """Remove Spotify's " - Live Aid" style suffix.

    Spotify puts version and edition information after a spaced dash:
    "Bohemian Rhapsody - Live Aid", "Come Together - Remastered 2009",
    "Sunflower - Spider-Man: Into the Spider-Verse". The suffix is metadata, not
    part of the song's name, so it is always removed.

    Applied to Spotify titles only. A YouTube title is sometimes typed as
    "Artist - Song", where cutting at the dash would throw the song away.
    """
    head = re.split(r"\s+[-\u2013\u2014]\s+", title)[0].strip()
    # Refuse to reduce a title to almost nothing, which would match everything.
    return head if len(head) >= 2 else title


def _strip_artist_prefix(title: str, artist: str) -> str:
    """Remove a leading "Artist - " from a YouTube title.

    Some YouTube Music entries, especially user uploads, carry the video title
    rather than the song name. Cutting at the dash blindly would be dangerous,
    but here we already know who the artist is, so the prefix can be removed
    only when it really is the artist's name.
    """
    if not artist:
        return title
    parts = re.split(r"\s+[-\u2013\u2014]\s+", title, maxsplit=1)
    if len(parts) != 2:
        return title
    prefix, remainder = parts
    if remainder.strip() and _title_similarity(prefix, artist) >= 0.85:
        return remainder.strip()
    return title


def clean_title(
    title: str,
    strip_dash_suffix: bool = False,
    known_artist: str = "",
) -> tuple[str, list[str]]:
    """Strip noise and pull featured artists out of a title.

    Returns the cleaned title and any artists found in a "feat." part, so a song
    credited to one artist on YouTube and three on Spotify still matches.

    Set ``strip_dash_suffix`` for Spotify titles only. See ``_strip_dash_suffix``.
    Pass ``known_artist`` for YouTube titles, so an "Artist - Song" prefix can be
    removed safely. See ``_strip_artist_prefix``.
    """
    if not title:
        return "", []

    featured: list[str] = []

    for match in _FEAT_INLINE_PATTERN.finditer(title):
        featured.extend(_split_artists(match.group(1)))
    title = _FEAT_INLINE_PATTERN.sub(" ", title)

    trailing = _FEAT_TRAILING_PATTERN.search(title)
    if trailing:
        featured.extend(_split_artists(trailing.group(1)))
        title = title[: trailing.start()]

    for pattern in NOISE_PATTERNS:
        title = re.sub(pattern, " ", title, flags=re.IGNORECASE)

    if strip_dash_suffix:
        title = _strip_dash_suffix(title)
    elif known_artist:
        title = _strip_artist_prefix(title, known_artist)

    title = re.sub(r"\s+", " ", title).strip(" -–—|·")
    return title.strip(), [a for a in featured if a]


def _split_artists(blob: str) -> list[str]:
    """Turn "A, B & C" into ["A", "B", "C"]."""
    parts = re.split(r"\s*(?:,|&|\band\b|\+)\s+", blob, flags=re.IGNORECASE)
    return [p.strip(" ()[]") for p in parts if p.strip(" ()[]")]


def _has_word(text: str, phrase: str) -> bool:
    """Whole-word search, so "live" does not match "Alive"."""
    return bool(re.search(rf"(?<!\w){re.escape(phrase)}(?!\w)", text))


def _version_tags(text: str) -> set[str]:
    lowered = (text or "").lower()
    return {word for word in VERSION_KEYWORDS if _has_word(lowered, word)}


def _impostor_tags(artists: list[str]) -> set[str]:
    """Markers of a tribute act, karaoke band or covers project."""
    joined = " ".join(artists).lower()
    return {marker for marker in IMPOSTOR_MARKERS if _has_word(joined, marker)}


def _title_similarity(a: str, b: str) -> float:
    """Strict similarity for titles, 0.0 to 1.0.

    ``token_sort_ratio`` ignores word order but not extra words, so
    "Yesterday" scores low against "Yesterday Once More". That strictness is
    only safe because both titles have already had their credit and version
    suffixes removed by ``clean_title``.
    """
    a, b = normalise(a), normalise(b)
    if not a or not b:
        return 0.0
    return fuzz.token_sort_ratio(a, b) / 100.0


def _artist_similarity(yt_artists: list[str], sp_artists: list[str]) -> float:
    """Best score across every pairing of the two artist lists.

    Collaborations list artists in different orders on the two services, so the
    best single pairing counts as well as the joined strings. ``token_sort_ratio``
    is used rather than ``token_set_ratio`` because the latter scores
    "ABBA" against "Abba Tribute Orchestra" as a perfect match.
    """
    if not yt_artists or not sp_artists:
        return 0.0
    joined = _title_similarity(", ".join(yt_artists), ", ".join(sp_artists))
    best_pair = max(_title_similarity(a, b) for a in yt_artists for b in sp_artists)
    return max(joined, best_pair)


def _duration_similarity(a: int | None, b: int | None) -> float | None:
    """1.0 for identical length, falling to 0.0 at the tolerance. None if unknown."""
    if not a or not b:
        return None
    return max(0.0, 1.0 - abs(a - b) / DURATION_TOLERANCE_SECONDS)


def score_candidate(track: YtTrack, candidate: SpotifyCandidate) -> float:
    """Score how likely it is that ``candidate`` is the same recording as ``track``."""
    yt_title, yt_featured = clean_title(track.title, known_artist=track.primary_artist)
    sp_title, sp_featured = clean_title(candidate.title, strip_dash_suffix=True)

    yt_artists = [a for a in [*track.artists, *yt_featured] if a]
    sp_artists = [a for a in [*candidate.artists, *sp_featured] if a]

    # --- base score: a weighted average of the signals we actually have -----
    parts: list[tuple[float, float]] = [(TITLE_WEIGHT, _title_similarity(yt_title, sp_title))]

    artist_score: float | None = None
    if yt_artists and sp_artists:
        artist_score = _artist_similarity(yt_artists, sp_artists)
        parts.append((ARTIST_WEIGHT, artist_score))

    duration_score = _duration_similarity(track.duration_seconds, candidate.duration_seconds)
    if duration_score is not None:
        parts.append((DURATION_WEIGHT, duration_score))

    total_weight = sum(weight for weight, _ in parts)
    score = sum(weight * value for weight, value in parts) / total_weight

    # Same album is weak evidence on its own, so it is only a small nudge.
    if track.album and candidate.album and _title_similarity(track.album, candidate.album) >= 0.90:
        score += ALBUM_BONUS

    # --- penalties: the reasons a good-looking result is still wrong --------

    # A remix, live take or karaoke version is a different recording. Compare the
    # raw titles, because clean_title has already removed the dash suffix that
    # usually carries the marker.
    if _version_tags(track.title) != _version_tags(candidate.title):
        score -= VERSION_PENALTY

    # A tribute band or karaoke act covering the right song at the right length.
    if _impostor_tags(sp_artists) - _impostor_tags(yt_artists):
        score -= IMPOSTOR_PENALTY

    # Same name, same artist, but minutes apart: an edit, a medley, or the wrong
    # track entirely.
    if (
        track.duration_seconds
        and candidate.duration_seconds
        and abs(track.duration_seconds - candidate.duration_seconds)
        > DURATION_HARD_LIMIT_SECONDS
    ):
        score -= DURATION_MISMATCH_PENALTY

    # A different act, not a spelling difference.
    if artist_score is not None and artist_score < ARTIST_MISMATCH_LIMIT:
        score -= ARTIST_MISMATCH_PENALTY

    return max(0.0, min(1.0, score))


def pick_best(
    track: YtTrack, candidates: list[SpotifyCandidate]
) -> tuple[SpotifyCandidate | None, float]:
    """Score every candidate and return the winner.

    Ties are broken by whichever length is closest to the YouTube track, since
    that is the signal least likely to be fooled by near-identical titles.
    """
    if not candidates:
        return None, 0.0

    for candidate in candidates:
        candidate.score = round(score_candidate(track, candidate), 4)

    def sort_key(candidate: SpotifyCandidate) -> tuple[float, float]:
        if track.duration_seconds and candidate.duration_seconds:
            closeness = -abs(track.duration_seconds - candidate.duration_seconds)
        else:
            closeness = -999.0
        return (candidate.score, closeness)

    best = max(candidates, key=sort_key)
    return best, best.score


def decide(score: float, match_threshold: float, review_threshold: float) -> Verdict:
    """Turn a score into an action.

    matched         -> add it, no questions asked
    low_confidence  -> probably right, but do not touch the playlist; report it
    not_found       -> no usable candidate
    """
    if score >= match_threshold:
        return "matched"
    if score >= review_threshold:
        return "low_confidence"
    return "not_found"


def build_search_queries(track: YtTrack) -> list[str]:
    """Spotify search queries to try, most precise first.

    The caller stops at the first query that yields a confident match, which
    keeps the number of API calls close to one per song. Search returns at most
    10 results per call since February 2026, so precision matters.
    """
    title, featured = clean_title(track.title, known_artist=track.primary_artist)
    title = title.replace('"', " ").strip()
    artist = (track.primary_artist or (featured[0] if featured else "")).replace('"', " ").strip()

    queries: list[str] = []
    if title and artist:
        queries.append(f'track:"{title}" artist:"{artist}"')
        queries.append(f"{title} {artist}")
    if title:
        queries.append(title)

    seen: set[str] = set()
    unique: list[str] = []
    for query in queries:
        if query not in seen:
            seen.add(query)
            unique.append(query)
    return unique


def build_result(
    track: YtTrack,
    candidate: SpotifyCandidate | None,
    score: float,
    match_threshold: float,
    review_threshold: float,
    playlist_name: str = "",
) -> MatchResult:
    """Wrap a scored candidate into the row that ends up in the report."""
    if candidate is None:
        return MatchResult(
            source=track,
            verdict="not_found",
            score=0.0,
            reason="Spotify returned no results for any query.",
            playlist_name=playlist_name,
        )

    verdict = decide(score, match_threshold, review_threshold)
    if verdict == "matched":
        reason = "Title, artist and length all agree."
    elif verdict == "low_confidence":
        reason = (
            f"Best guess scored {score:.2f}, below the {match_threshold:.2f} "
            "threshold. Not added. Check it by hand."
        )
    else:
        reason = f"Best guess scored only {score:.2f}. Treated as not found."

    return MatchResult(
        source=track,
        verdict=verdict,
        best=candidate,
        score=score,
        reason=reason,
        playlist_name=playlist_name,
    )
