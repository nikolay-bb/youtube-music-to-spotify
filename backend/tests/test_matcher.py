"""Tests for the scoring logic.

These are the tests that matter. If the matcher is wrong, the app silently puts
the wrong songs in your playlists, and no other test would notice.

Each case is a real-world shape of problem, not an invented one.
"""

from __future__ import annotations

import pytest

from app.matcher import (
    build_search_queries,
    clean_title,
    decide,
    normalise,
    pick_best,
    score_candidate,
)
from app.models import SpotifyCandidate, YtTrack

MATCH = 0.86
REVIEW = 0.70


def yt(title, artists=(), album=None, seconds=None) -> YtTrack:
    return YtTrack(
        video_id="v1",
        title=title,
        artists=list(artists),
        album=album,
        duration_seconds=seconds,
    )


def sp(title, artists=(), album=None, seconds=None, track_id="s1") -> SpotifyCandidate:
    return SpotifyCandidate(
        id=track_id,
        uri=f"spotify:track:{track_id}",
        title=title,
        artists=list(artists),
        album=album,
        duration_seconds=seconds,
    )


# ---------------------------------------------------------------------------
# Title cleaning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Blinding Lights (Official Video)", "Blinding Lights"),
        ("Blinding Lights (Official Music Video)", "Blinding Lights"),
        ("Bad Guy (Official Audio)", "Bad Guy"),
        ("Levitating (Lyrics)", "Levitating"),
        ("Levitating [Lyric Video]", "Levitating"),
        ("Dynamite (Official Visualizer)", "Dynamite"),
        ("Something [HD]", "Something"),
        ("Plain Title", "Plain Title"),
    ],
)
def test_clean_title_strips_youtube_noise(raw, expected):
    cleaned, _ = clean_title(raw)
    assert cleaned == expected


@pytest.mark.parametrize(
    ("raw", "title", "featured"),
    [
        ("Stay (feat. Justin Bieber)", "Stay", ["Justin Bieber"]),
        ("Stay ft. Justin Bieber", "Stay", ["Justin Bieber"]),
        ("One Dance feat. Wizkid, Kyla", "One Dance", ["Wizkid", "Kyla"]),
        ("Song [featuring Someone]", "Song", ["Someone"]),
    ],
)
def test_clean_title_extracts_featured_artists(raw, title, featured):
    cleaned, feats = clean_title(raw)
    assert cleaned == title
    assert feats == featured


def test_normalise_keeps_non_latin_letters():
    """Cyrillic and CJK titles must survive; stripping them would zero the score."""
    assert normalise("Привет, мир!") == "привет мир"
    assert normalise("夜に駆ける") == "夜に駆ける"
    assert normalise("Café — Déjà Vu!") == "café déjà vu"


# ---------------------------------------------------------------------------
# Scoring: pairs that must match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("track", "candidate", "why"),
    [
        (
            yt("Blinding Lights (Official Video)", ["The Weeknd"], seconds=200),
            sp("Blinding Lights", ["The Weeknd"], seconds=200),
            "identical once the noise is stripped",
        ),
        (
            yt("Stay", ["The Kid LAROI", "Justin Bieber"], seconds=141),
            sp("STAY (with Justin Bieber)", ["The Kid LAROI", "Justin Bieber"], seconds=141),
            "different capitalisation and a 'with' credit",
        ),
        (
            yt("One Dance feat. Wizkid", ["Drake"], seconds=173),
            sp("One Dance", ["Drake", "Wizkid", "Kyla"], seconds=173),
            "featured artist in the title on one side, artist list on the other",
        ),
        (
            yt("Levitating", ["Dua Lipa"], seconds=203),
            sp("Levitating", ["Dua Lipa"], seconds=206),
            "three seconds apart is the same recording",
        ),
        (
            yt("夜に駆ける", ["YOASOBI"], seconds=261),
            sp("夜に駆ける", ["YOASOBI"], seconds=261),
            "non-Latin titles must score normally",
        ),
        (
            yt("Bohemian Rhapsody", ["Queen"], album="A Night at the Opera", seconds=354),
            sp("Bohemian Rhapsody", ["Queen"], album="A Night at the Opera", seconds=355),
            "album agreement adds a small bonus",
        ),
    ],
)
def test_true_pairs_score_above_the_match_threshold(track, candidate, why):
    assert score_candidate(track, candidate) >= MATCH, why


# ---------------------------------------------------------------------------
# Scoring: pairs that must NOT match
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("track", "candidate", "why"),
    [
        (
            yt("Blinding Lights", ["The Weeknd"], seconds=200),
            sp("Blinding Lights - Chromatics Remix", ["The Weeknd"], seconds=245),
            "a remix is a different recording",
        ),
        (
            yt("Bohemian Rhapsody", ["Queen"], seconds=354),
            sp("Bohemian Rhapsody - Live Aid", ["Queen"], seconds=280),
            "a live version is a different recording",
        ),
        (
            yt("Shape of You", ["Ed Sheeran"], seconds=233),
            sp("Shape of You (Karaoke Version)", ["Sing Karaoke"], seconds=233),
            "karaoke tracks are a common trap",
        ),
        (
            yt("Yesterday", ["The Beatles"], seconds=125),
            sp("Yesterday", ["Some Tribute Band"], seconds=125),
            "same title, wrong artist",
        ),
        (
            yt("Hello", ["Adele"], seconds=295),
            sp("Hello", ["Adele"], seconds=180),
            "same title and artist, but nearly two minutes shorter",
        ),
    ],
)
def test_false_pairs_score_below_the_match_threshold(track, candidate, why):
    assert score_candidate(track, candidate) < MATCH, why


def test_a_remix_loses_to_the_studio_version():
    """The point of the version penalty: the right one must win, not just score well."""
    track = yt("Blinding Lights", ["The Weeknd"], seconds=200)
    studio = sp("Blinding Lights", ["The Weeknd"], seconds=200, track_id="studio")
    remix = sp("Blinding Lights - Chromatics Remix", ["The Weeknd"], seconds=245, track_id="remix")

    best, score = pick_best(track, [remix, studio])
    assert best is not None and best.id == "studio"
    assert score >= MATCH


def test_missing_duration_is_treated_as_unknown_not_as_a_mismatch():
    """A song with no reported length should still match on title and artist."""
    track = yt("Karma Police", ["Radiohead"], seconds=None)
    candidate = sp("Karma Police", ["Radiohead"], seconds=261)
    assert score_candidate(track, candidate) >= MATCH


def test_score_is_always_within_zero_and_one():
    track = yt("Live Remix Cover", ["A"], album="X", seconds=100)
    candidate = sp("Live Remix Cover", ["A"], album="X", seconds=100)
    assert 0.0 <= score_candidate(track, candidate) <= 1.0


# ---------------------------------------------------------------------------
# Picking and deciding
# ---------------------------------------------------------------------------


def test_pick_best_returns_nothing_for_an_empty_search():
    best, score = pick_best(yt("Anything", ["Someone"]), [])
    assert best is None
    assert score == 0.0


def test_pick_best_breaks_ties_on_closest_duration():
    track = yt("Same Song", ["Artist"], seconds=200)
    near = sp("Same Song", ["Artist"], seconds=200, track_id="near")
    far = sp("Same Song", ["Artist"], seconds=209, track_id="far")

    best, _ = pick_best(track, [far, near])
    assert best is not None and best.id == "near"


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0.95, "matched"),
        (0.86, "matched"),
        (0.80, "low_confidence"),
        (0.70, "low_confidence"),
        (0.69, "not_found"),
        (0.0, "not_found"),
    ],
)
def test_decide_uses_both_thresholds(score, expected):
    assert decide(score, MATCH, REVIEW) == expected


# ---------------------------------------------------------------------------
# Search queries
# ---------------------------------------------------------------------------


def test_queries_go_from_precise_to_loose():
    queries = build_search_queries(yt("Blinding Lights (Official Video)", ["The Weeknd"]))
    assert queries[0] == 'track:"Blinding Lights" artist:"The Weeknd"'
    assert queries[1] == "Blinding Lights The Weeknd"
    assert queries[2] == "Blinding Lights"


def test_queries_survive_a_track_with_no_artist():
    queries = build_search_queries(yt("Mystery Song", []))
    assert queries == ["Mystery Song"]


def test_quotes_in_a_title_cannot_break_the_query_syntax():
    """An unescaped double quote would corrupt the track:"..." filter."""
    queries = build_search_queries(yt('The "Best" Song', ["Artist"]))
    assert all(q.count('"') % 2 == 0 for q in queries)
    assert '"Best"' not in queries[0]


# ---------------------------------------------------------------------------
# Traps found while tuning the scoring. Each of these once produced a wrong
# answer, so each one keeps its own test.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("track", "candidate", "why"),
    [
        (
            yt("Yesterday", ["The Beatles"], seconds=125),
            sp("Yesterday Once More", ["The Beatles"], seconds=125),
            "a short title is a subset of a longer, different song",
        ),
        (
            yt("Hello", ["Adele"], seconds=295),
            sp("Hello Goodbye", ["Adele"], seconds=295),
            "subset title again, same artist and length",
        ),
        (
            yt("Alone", ["Artist"], seconds=200),
            sp("Alone Together", ["Artist"], seconds=200),
            "subset title, both signals otherwise perfect",
        ),
        (
            yt("Dancing Queen", ["ABBA"], seconds=230),
            sp("Dancing Queen", ["Abba Tribute Orchestra"], seconds=230),
            "a tribute act with the right song at the right length",
        ),
        (
            yt("Smells Like Teen Spirit", ["Nirvana"], seconds=301),
            sp("Smells Like Teen Spirit", ["Nirvana Tribute Band"], seconds=301),
            "tribute band whose name contains the real artist",
        ),
        (
            yt("Someone Like You", ["Adele"], seconds=285),
            sp("Someone Like You", ["Adele Karaoke Band"], seconds=285),
            "karaoke act whose name contains the real artist",
        ),
    ],
)
def test_lookalike_results_are_rejected(track, candidate, why):
    assert score_candidate(track, candidate) < MATCH, why


@pytest.mark.parametrize(
    ("track", "candidate", "why"),
    [
        (
            yt("Sunflower", ["Post Malone", "Swae Lee"], seconds=158),
            sp("Sunflower - Spider-Man: Into the Spider-Verse", ["Post Malone", "Swae Lee"], seconds=158),
            "Spotify puts soundtrack information after a dash",
        ),
        (
            yt("Levitating", ["Dua Lipa"], seconds=203),
            sp("Levitating (feat. DaBaby)", ["Dua Lipa", "DaBaby"], seconds=203),
            "Spotify names the featured artist, YouTube does not",
        ),
        (
            yt("Come Together (Remastered 2009)", ["The Beatles"], seconds=259),
            sp("Come Together - Remastered 2009", ["The Beatles"], seconds=259),
            "a remaster is the same recording, written differently on each service",
        ),
        (
            yt("Tik Tok", ["Ke$ha"], seconds=200),
            sp("TiK ToK", ["Kesha"], seconds=200),
            "stylised spelling of an artist name",
        ),
        (
            yt("Ke$ha - Tik Tok", ["Ke$ha"], seconds=200),
            sp("TiK ToK", ["Kesha"], seconds=200),
            "YouTube title carrying an 'Artist - Song' prefix",
        ),
        (
            yt("Dancing With Myself", ["Billy Idol"], seconds=194),
            sp("Dancing With Myself", ["Billy Idol"], seconds=194),
            "'with' inside an ordinary title is not a featured-artist credit",
        ),
    ],
)
def test_awkward_but_genuine_pairs_still_match(track, candidate, why):
    assert score_candidate(track, candidate) >= MATCH, why


def test_dancing_with_myself_keeps_its_whole_title():
    """An unbracketed 'with' must not be mistaken for a credit and cut off."""
    cleaned, featured = clean_title("Dancing With Myself")
    assert cleaned == "Dancing With Myself"
    assert featured == []


def test_bracketed_with_is_still_treated_as_a_credit():
    cleaned, featured = clean_title("STAY (with Justin Bieber)")
    assert cleaned == "STAY"
    assert featured == ["Justin Bieber"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Bohemian Rhapsody - Live Aid", "Bohemian Rhapsody"),
        ("Come Together - Remastered 2009", "Come Together"),
        ("Sunflower - Spider-Man: Into the Spider-Verse", "Sunflower"),
        ("Blinding Lights - Chromatics Remix", "Blinding Lights"),
        ("No Suffix Here", "No Suffix Here"),
    ],
)
def test_spotify_dash_suffix_is_always_removed(raw, expected):
    """Spotify puts version and edition metadata after a spaced dash.

    It is removed unconditionally. A song name that genuinely contains " - "
    would lose its second half, which is the accepted cost: the alternative is
    an endless list of film names, years and producer credits to recognise.
    """
    cleaned, _ = clean_title(raw, strip_dash_suffix=True)
    assert cleaned == expected


def test_youtube_artist_prefix_is_cut_only_when_it_is_the_artist():
    """"Artist - Song" is common on YouTube, but cutting blindly loses the song."""
    # The prefix is the known artist, so it goes.
    cleaned, _ = clean_title("Queen - Bohemian Rhapsody", known_artist="Queen")
    assert cleaned == "Bohemian Rhapsody"

    # The prefix is part of the song name, so it stays.
    kept, _ = clean_title("Ashes - Remain", known_artist="Some Band")
    assert kept == "Ashes - Remain"

    # With no artist to check against, nothing is cut.
    untouched, _ = clean_title("Ashes - Remain")
    assert untouched == "Ashes - Remain"


@pytest.mark.parametrize(
    "raw",
    [
        "Come Together (Remastered 2009)",
        "Come Together (2009 Remaster)",
        "Come Together (Album Version)",
        "Come Together (Bonus Track)",
        "Come Together (Deluxe Edition)",
    ],
)
def test_edition_markers_are_stripped(raw):
    cleaned, _ = clean_title(raw)
    assert cleaned == "Come Together"


def test_stylised_artist_spellings_normalise_to_the_same_text():
    assert normalise("Ke$ha") == normalise("Kesha")
    assert normalise("P!nk") == normalise("Pink")
    assert normalise("A$AP Rocky") == normalise("ASAP Rocky")


# ---------------------------------------------------------------------------
# Reading YouTube's own metadata.
#
# The app uses the official YouTube Data API, which describes videos rather
# than songs. These two functions turn that into the title/artist/duration the
# matcher needs, so they carry a lot of weight.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("iso", "seconds"),
    [
        ("PT4M1S", 241),
        ("PT15M1S", 901),
        ("PT5M", 300),
        ("PT45S", 45),
        ("PT1H2M3S", 3723),
        ("PT2H", 7200),
        ("P1DT1H", 90000),
        (None, None),
        ("", None),
        ("nonsense", None),
        ("PT0S", None),
    ],
)
def test_iso_durations_convert_to_seconds(iso, seconds):
    from app.ytmusic import parse_duration

    assert parse_duration(iso) == seconds


@pytest.mark.parametrize(
    ("channel", "artist"),
    [
        # YouTube auto-generates "<Artist> - Topic" channels for licensed music.
        ("Frank Sinatra - Topic", "Frank Sinatra"),
        ("Joe Henderson - Topic", "Joe Henderson"),
        ("Twenty One Pilots - Topic", "Twenty One Pilots"),
        # A real channel is a person or label, not an artist credit. Returning
        # it would poison the artist score, so it is deliberately dropped and
        # the matcher falls back to the title.
        ("freeCodeCamp.org", ""),
        ("AAIF Live", ""),
        ("Some Guy's Channel", ""),
        # A channel that merely mentions the word must not be mistaken for one.
        ("Topic", ""),
        ("", ""),
        (None, ""),
    ],
)
def test_artist_is_taken_only_from_topic_channels(channel, artist):
    from app.ytmusic import artist_from_channel

    assert artist_from_channel(channel) == artist
