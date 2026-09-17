"""Tests for the Subsonic string-compare fallback matcher and the removal of
the TEXT_COMAPRE_MATCHING_ENABLED toggle.

Covers specs/reliable-track-matching.md R1, R2, R3, R7, R8.
"""
import subprocess
from pathlib import Path

from spotisub import constants
from spotisub import utils
from spotisub.classes import SubsonicCache
from spotisub.helpers import subsonic_helper

from tests.helpers import (
    FakePysonicClient,
    NoLinearScanDict,
    build_cache_for_songs,
    comparison_helper_for,
    make_spotify_track,
    make_subsonic_song,
    sample_playlist_info,
)


# ---------------------------------------------------------------------------
# R2: the dead TEXT_COMAPRE_MATCHING_ENABLED toggle is gone entirely.
# ---------------------------------------------------------------------------

def test_text_compare_matching_enabled_constant_removed():
    assert not hasattr(constants, "TEXT_COMAPRE_MATCHING_ENABLED")
    assert not hasattr(constants, "TEXT_COMAPRE_MATCHING_ENABLED_DEFAULT_VALUE")


def test_grep_finds_no_remaining_references():
    repo_root = Path(__file__).resolve().parent.parent
    result = subprocess.run(
        ["grep", "-r", "TEXT_COMAPRE_MATCHING_ENABLED", str(repo_root / "spotisub")],
        capture_output=True, text=True)
    # grep exits 1 when it finds no matches -- that's the passing case here.
    assert result.returncode == 1, (
        f"expected zero matches under spotisub/, grep found:\n{result.stdout}")


# ---------------------------------------------------------------------------
# R1 + R3: get_subsonic_track_via_string_compare() itself -- runs regardless
# of the (now nonexistent) env var, via an indexed lookup, not a linear scan.
# ---------------------------------------------------------------------------

def test_string_compare_matches_via_index_not_linear_scan():
    subsonic_song = make_subsonic_song(
        "s2", "Sultans of Swing", "Dire Straits", "Dire Straits")
    compare_dict = NoLinearScanDict()
    for variant in utils.generate_compare_array("Sultans of Swing"):
        compare_dict.setdefault(variant, []).append(subsonic_song)

    track = make_spotify_track("Sultans of Swing", "Dire Straits")
    helper = comparison_helper_for(track)

    matched = subsonic_helper.get_subsonic_track_via_string_compare(
        helper, compare_dict)

    assert matched is not None
    assert matched["id"] == "s2"


def test_string_compare_returns_none_when_index_has_no_candidates():
    helper = comparison_helper_for(
        make_spotify_track("Some Song", "Some Artist"))
    matched = subsonic_helper.get_subsonic_track_via_string_compare(
        helper, NoLinearScanDict())
    assert matched is None


# ---------------------------------------------------------------------------
# R1: match_with_subsonic_track() runs the string-compare fallback whenever
# MBID matching misses (or is skipped, e.g. no ISRC) and title+artist are
# both present -- regardless of any environment variable.
# ---------------------------------------------------------------------------

def _run_match(monkeypatch, track, subsonic_songs, old_song_ids=None,
              env_value=None, env_unset=True):
    """Exercise the real match_with_subsonic_track() end to end (including
    its database.insert_song()/createPlaylist() side effects), with the
    Subsonic network boundary faked out."""
    if env_unset:
        monkeypatch.delenv("TEXT_COMAPRE_MATCHING_ENABLED", raising=False)
    elif env_value is not None:
        monkeypatch.setenv("TEXT_COMAPRE_MATCHING_ENABLED", env_value)

    fake_client = FakePysonicClient(songs=subsonic_songs)
    playlist_info = sample_playlist_info()
    playlist_info["subsonic_playlist_id"] = "pl-1"
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    cache = build_cache_for_songs(subsonic_songs)
    helper = comparison_helper_for(track)

    result = subsonic_helper.match_with_subsonic_track(
        helper, playlist_info, old_song_ids or [], cache)
    return result, fake_client


def test_match_falls_back_to_string_compare_when_env_var_unset(monkeypatch):
    subsonic_songs = [
        make_subsonic_song("s1", "Vava Voom", "Bassnectar", "Vava Voom")]
    # No ISRC -> get_subsonic_track_via_mbid() is skipped entirely, so this
    # exercises exactly the "MBID lookup misses/unavailable" fallback path.
    track = make_spotify_track("Vava Voom", "Bassnectar", album_name="Vava Voom")

    result, _ = _run_match(monkeypatch, track, subsonic_songs, env_unset=True)

    assert result.found is True
    assert "s1" in result.song_ids


def test_match_falls_back_to_string_compare_when_env_var_explicitly_disabled(monkeypatch):
    subsonic_songs = [
        make_subsonic_song("s3", "Hotel California", "Eagles", "Hotel California")]
    track = make_spotify_track(
        "Hotel California", "Eagles", album_name="Hotel California")

    result, _ = _run_match(
        monkeypatch, track, subsonic_songs, env_unset=False, env_value="0")

    assert result.found is True
    assert "s3" in result.song_ids


# ---------------------------------------------------------------------------
# R7: genuinely-absent tracks are still reported not-found, with no
# regression against a real known-absent artist from the live evidence
# (AlienPark, confirmed absent from Lidarr entirely).
# ---------------------------------------------------------------------------

def test_absent_track_is_not_found(monkeypatch):
    subsonic_songs = [
        make_subsonic_song("s1", "Vava Voom", "Bassnectar", "Vava Voom")]
    track = make_spotify_track(
        "Some Totally Different Song", "AlienPark", album_name="Nonexistent")

    result, fake_client = _run_match(monkeypatch, track, subsonic_songs)

    assert result.found is False
    assert result.song_ids == []
    assert fake_client.playlists == {} or all(
        not data["entry"] for data in fake_client.playlists.values())


# ---------------------------------------------------------------------------
# R8: two distinct songs sharing a normalized title+artist (same track
# released on two different albums) are disambiguated by album when
# possible, and still resolve to *some* match (not an error) when it isn't.
# ---------------------------------------------------------------------------

def test_disambiguates_same_title_artist_by_album():
    song_album_a = make_subsonic_song(
        "song-a", "Encore", "Bassnectar", "Album A")
    song_album_b = make_subsonic_song(
        "song-b", "Encore", "Bassnectar", "Album B")
    cache = build_cache_for_songs([song_album_a, song_album_b])

    track = make_spotify_track("Encore", "Bassnectar", album_name="Album B")
    helper = comparison_helper_for(track)

    matched = subsonic_helper.get_subsonic_track_via_string_compare(
        helper, cache.song_compare_dict)

    assert matched is not None
    assert matched["id"] == "song-b"


def test_disambiguation_falls_back_to_a_match_without_album_info():
    song_album_a = make_subsonic_song(
        "song-a", "Encore", "Bassnectar", "Album A")
    song_album_b = make_subsonic_song(
        "song-b", "Encore", "Bassnectar", "Album B")
    cache = build_cache_for_songs([song_album_a, song_album_b])

    # No album on the Spotify side at all -- disambiguation isn't possible,
    # but a match (not an error/None) must still be returned.
    track = make_spotify_track("Encore", "Bassnectar")
    helper = comparison_helper_for(track)

    matched = subsonic_helper.get_subsonic_track_via_string_compare(
        helper, cache.song_compare_dict)

    assert matched is not None
    assert matched["id"] in ("song-a", "song-b")


# ---------------------------------------------------------------------------
# C-final regression: the exact-dict-key candidate index must not lose
# matches the old full linear-scan compare_strings()/compare() supported --
# specifically, a Subsonic-side title carrying a bracketed/parenthetical
# qualifier suffix (e.g. "[Live]", "(Remaster)") that the Spotify-side title
# doesn't have. compare_strings() does substring matching, so the old linear
# scan found these; the indexed candidate lookup only works if
# generate_compare_array() produces a shared key for both sides (see
# constants.SPLIT_TOKENS), so this proves that still holds for both bracket
# styles.
# ---------------------------------------------------------------------------

def test_bracket_qualifier_suffix_still_resolves_via_index():
    subsonic_song = make_subsonic_song(
        "s-live", "Come As You Are [Live]", "Nirvana", "Live at the Paramount")
    cache = build_cache_for_songs([subsonic_song])

    track = make_spotify_track("Come As You Are", "Nirvana", album_name="Nevermind")
    helper = comparison_helper_for(track)

    matched = subsonic_helper.get_subsonic_track_via_string_compare(
        helper, cache.song_compare_dict)

    assert matched is not None
    assert matched["id"] == "s-live"


def test_parenthetical_qualifier_suffix_still_resolves_via_index():
    subsonic_song = make_subsonic_song(
        "s-remaster", "Come As You Are (Remaster)", "Nirvana", "Nevermind (Remastered)")
    cache = build_cache_for_songs([subsonic_song])

    track = make_spotify_track("Come As You Are", "Nirvana", album_name="Nevermind")
    helper = comparison_helper_for(track)

    matched = subsonic_helper.get_subsonic_track_via_string_compare(
        helper, cache.song_compare_dict)

    assert matched is not None
    assert matched["id"] == "s-remaster"


def test_bracket_qualifier_suffix_resolves_end_to_end_via_match_with_subsonic_track(monkeypatch):
    subsonic_songs = [
        make_subsonic_song(
            "s-live", "Come As You Are [Live]", "Nirvana", "Live at the Paramount")]
    # No ISRC -> MBID matching is skipped, exercising the string-compare
    # fallback path exactly as write_playlist() would for a track with no
    # usable ISRC/MBID.
    track = make_spotify_track(
        "Come As You Are", "Nirvana", album_name="Nevermind")

    result, fake_client = _run_match(monkeypatch, track, subsonic_songs)

    assert result.found is True
    assert "s-live" in result.song_ids
