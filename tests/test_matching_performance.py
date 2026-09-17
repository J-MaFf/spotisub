"""Performance regression test for the string-compare fallback matcher.

Covers specs/archive/reliable-track-matching.md R3 / C3.

C3 requires evidence that fallback matching (get_subsonic_track_via_string_compare,
via the get_subsonic_track_via_mbid()-misses-then-falls-back-to-string-compare
path in match_with_subsonic_track()) is not an O(track_count * cache_size) scan.
This builds a synthetic Subsonic cache of ~15,000 songs (the live deployment's
approximate library size, see specs/archive/reliable-track-matching.md Context) and a
synthetic playlist of 564 tracks (the live "Rock" playlist's reported size) with
no ISRC, so every track is forced through the string-compare fallback -- no
MBID matching, no network I/O.

Two independent things are asserted so a regression to a linear scan is caught
even if wall-clock timing is noisy on a particular machine:

1. Wall-clock time for matching all 564 tracks against the 15,000-song cache
   stays well under a generous bound (5s). A true O(track_count * cache_size)
   scan here would mean up to 564 * 15,000 = 8,460,000 per-track candidate
   comparisons; even a cheap per-comparison cost would blow well past 5s at
   that volume, while an indexed O(1)-ish lookup handles it in a small
   fraction of a second.
2. The number of utils.compare_track_metadata() calls (the expensive
   narrowing step run over whatever candidate set the index returns) is
   counted directly and asserted to stay close to O(track_count), not
   anywhere near O(track_count * cache_size). This makes the test's pass/fail
   depend on the indexing behavior itself, not just on wall-clock variance.

This is NOT the live "full reimport completes in under 10 minutes" bound from
the spec (that bound includes real network calls to Subsonic/Spotify/
MusicBrainz that this in-process test deliberately does not make) -- it only
proves the matching algorithm itself scales with the playlist size, not with
the library size, which is the mechanism the 10-minute live bound depends on.
"""
import time

from spotisub import utils
from spotisub.helpers import subsonic_helper

from tests.helpers import (
    build_cache_for_songs,
    comparison_helper_for,
    make_spotify_track,
    make_subsonic_song,
)

NUM_SUBSONIC_SONGS = 15_000
NUM_PLAYLIST_TRACKS = 564

# A generous wall-clock bound for pure in-process matching (no network I/O).
# The spec's 10-minute live reimport bound includes network calls this test
# does not make -- this bound only needs to catch an accidental return to an
# O(track_count * cache_size) scan, which would be orders of magnitude slower
# than this even on slow CI hardware.
MAX_WALL_CLOCK_SECONDS = 5.0

# A full linear-scan-per-track implementation would run compare_track_metadata
# up to NUM_PLAYLIST_TRACKS * NUM_SUBSONIC_SONGS times. An indexed lookup
# should only run it against the small candidate set the index returns per
# track (here, exactly one candidate per track by construction) -- bound the
# expected call count at a generous constant-per-track multiplier so this
# fails loudly if the index ever degrades back into a broad scan.
MAX_COMPARISONS_PER_TRACK = 20


def _build_large_subsonic_library(num_songs):
    """Synthetic library of `num_songs` distinct songs, each with a unique
    normalized title+artist so every playlist track below has exactly one
    correct candidate -- isolates the index/lookup behavior from the
    disambiguation logic covered separately in test_matching.py."""
    songs = []
    for i in range(num_songs):
        songs.append(
            make_subsonic_song(
                f"song-{i}",
                f"Unique Song Title {i}",
                f"Unique Artist {i}",
                f"Unique Album {i}"))
    return songs


def _build_playlist_tracks(num_tracks, library_size):
    """`num_tracks` Spotify tracks, spread across the synthetic library, each
    with no ISRC (forcing the string-compare fallback path) and matching
    exactly one library entry by title+artist."""
    step = max(1, library_size // num_tracks)
    tracks = []
    for i in range(0, library_size, step):
        if len(tracks) >= num_tracks:
            break
        tracks.append(
            make_spotify_track(
                f"Unique Song Title {i}", f"Unique Artist {i}"))
    return tracks


def test_string_compare_matching_scales_with_track_count_not_cache_size(monkeypatch):
    songs = _build_large_subsonic_library(NUM_SUBSONIC_SONGS)
    cache = build_cache_for_songs(songs)

    tracks = _build_playlist_tracks(NUM_PLAYLIST_TRACKS, NUM_SUBSONIC_SONGS)
    assert len(tracks) == NUM_PLAYLIST_TRACKS

    call_count = {"n": 0}
    real_compare_track_metadata = utils.compare_track_metadata

    def counting_compare_track_metadata(*args, **kwargs):
        call_count["n"] += 1
        return real_compare_track_metadata(*args, **kwargs)

    # subsonic_helper calls utils.compare_track_metadata via `from spotisub
    # import utils; utils.compare_track_metadata(...)`, so patching the
    # attribute on the utils module is visible to it without needing to
    # touch subsonic_helper itself.
    monkeypatch.setattr(
        utils, "compare_track_metadata", counting_compare_track_metadata)

    matched_count = 0
    start = time.perf_counter()
    for track in tracks:
        helper = comparison_helper_for(track)
        matched = subsonic_helper.get_subsonic_track_via_string_compare(
            helper, cache.song_compare_dict)
        if matched is not None:
            matched_count += 1
    elapsed = time.perf_counter() - start

    assert matched_count == NUM_PLAYLIST_TRACKS, (
        f"expected all {NUM_PLAYLIST_TRACKS} synthetic tracks to match "
        f"their unique library counterpart, got {matched_count}")

    assert elapsed < MAX_WALL_CLOCK_SECONDS, (
        f"string-compare fallback matching took {elapsed:.3f}s for "
        f"{NUM_PLAYLIST_TRACKS} tracks against a {NUM_SUBSONIC_SONGS}-song "
        f"cache; expected well under {MAX_WALL_CLOCK_SECONDS}s for a "
        f"non-linear-scan indexed lookup with no network I/O")

    max_expected_calls = NUM_PLAYLIST_TRACKS * MAX_COMPARISONS_PER_TRACK
    naive_scan_calls = NUM_PLAYLIST_TRACKS * NUM_SUBSONIC_SONGS
    assert call_count["n"] < max_expected_calls, (
        f"utils.compare_track_metadata was called {call_count['n']} times "
        f"for {NUM_PLAYLIST_TRACKS} tracks -- expected fewer than "
        f"{max_expected_calls} (O(track_count) via an indexed candidate "
        f"lookup), not anywhere near the {naive_scan_calls} calls a full "
        f"O(track_count * cache_size) linear scan of the cache would make")
