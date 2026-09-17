"""Tests for specs/local-file-track-matching.md (R1-R7).

Covers matching a Spotify "local file" playlist track (Spotify returns
`id: null`, `is_local: true` for a track the user manually imported into
their own Spotify library rather than something from Spotify's catalog)
against the Subsonic library by title/artist, instead of unconditionally
skipping it with zero attempt.

Nothing here talks to a real Spotify/Subsonic server -- see tests/helpers.py
for the fakes used (FakePysonicClient, make_local_spotify_track) and
tests/conftest.py for how the rest of the third-party/network surface is
stubbed out.
"""
import logging
import time

from spotisub import database
from spotisub import generator
from spotisub.helpers import spotipy_helper
from spotisub.helpers import subsonic_helper

from tests.helpers import (
    FakePysonicClient,
    make_local_spotify_track,
    make_subsonic_song,
    sample_playlist_info,
)


# ---------------------------------------------------------------------------
# Fakes local to this test module.
# ---------------------------------------------------------------------------

class _FakeSpForPlaylistItems:
    """Fake spotipy client covering only sp.playlist_items(), capturing the
    kwargs it was called with -- one page of items per call, consumed in
    order (mirrors FakeSpotipyClient's approach in tests/helpers.py)."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def playlist_items(self, playlist_id, offset=0, fields=None, limit=50,
                       additional_types=None):
        self.calls.append(dict(
            playlist_id=playlist_id, offset=offset, fields=fields,
            limit=limit, additional_types=additional_types))
        page_index = offset // limit
        if page_index >= len(self.pages):
            return {"items": []}
        return {"items": list(self.pages[page_index])}


class _FakeSpForSavedTracks:
    """Fake spotipy client covering only sp.current_user_saved_tracks()."""

    def __init__(self, pages):
        self.pages = list(pages)

    def current_user_saved_tracks(self, offset=0, limit=50):
        page_index = offset // limit
        if page_index >= len(self.pages):
            return {"items": []}
        return {"items": list(self.pages[page_index])}


class _SpotifyCallNotExpected:
    """Fails the test if any attribute/method is used -- proves
    add_missing_values_to_track() makes no Spotify API call for a
    local-file track (there's no catalog id to fetch a replacement
    object with)."""

    def __getattr__(self, name):
        raise AssertionError(
            f"unexpected Spotify API access for a local-file track: {name}")


# ---------------------------------------------------------------------------
# R1: get_playlist_tracks()'s sp.playlist_items() fields request is widened
# to also include items.item.uri, items.item.album, items.item.is_local,
# without dropping the four fields already relied upon elsewhere.
# ---------------------------------------------------------------------------

def test_r1_fields_request_widened_for_local_file_support(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake_sp = _FakeSpForPlaylistItems(pages=[[]])
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    generator.get_playlist_tracks(
        {"id": "playlist-1", "name": "Test Playlist"}, {"tracks": []})

    assert len(fake_sp.calls) == 1
    fields = fake_sp.calls[0]["fields"]
    for required_field in (
            "items.item.id", "items.item.name", "items.item.artists",
            "items.item.type", "items.item.uri", "items.item.album",
            "items.item.is_local"):
        assert required_field in fields, (
            f"{required_field} missing from widened fields string: {fields}")


# ---------------------------------------------------------------------------
# R2: add_missing_values_to_track() treats a track as a local file when
# track.get("id") is None -- no catalog fetch (there's no catalog id to
# fetch with); pass through as given if it has a usable uri, otherwise log
# a warning and return None (not raise).
# ---------------------------------------------------------------------------

def test_r2_local_file_track_passes_through_unmodified_with_no_spotify_call():
    track = make_local_spotify_track(
        "Battle! (Gym Leader)", "Junichi Masuda",
        album_name="Pokemon Battle Themes")

    result = subsonic_helper.add_missing_values_to_track(
        _SpotifyCallNotExpected(), dict(track))

    assert result == track


def test_r2_local_file_track_missing_uri_logs_warning_and_returns_none(caplog):
    track = make_local_spotify_track(
        "Completely Untagged Track", "Unknown Artist", uri="")

    caplog.set_level(logging.WARNING)
    result = subsonic_helper.add_missing_values_to_track(
        _SpotifyCallNotExpected(), track)

    assert result is None
    assert "Completely Untagged Track" in caplog.text


# ---------------------------------------------------------------------------
# R3: any track with an empty artists list is skipped with a warning naming
# the track, not an IndexError that crashes the reimport thread -- checked
# at all three reachable call sites: subsonic_helper.write_playlist() (the
# minimum required site) and both generator.py ingestion functions, which
# this spec's investigation found are *also* reachable for local-file
# tracks (playlist ingestion and Liked-Songs ingestion respectively).
# ---------------------------------------------------------------------------

def test_r3_write_playlist_skips_empty_artists_and_keeps_processing_others(
        monkeypatch, caplog):
    subsonic_songs = [
        make_subsonic_song("s1", "Battle! (Gym Leader)", "Junichi Masuda",
                            "Pokemon Battle Themes")]
    fake_client = FakePysonicClient(songs=subsonic_songs)
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    no_artists_track = make_local_spotify_track(
        "Completely Untagged Track", "Irrelevant Artist")
    no_artists_track["artists"] = []

    matching_track = make_local_spotify_track(
        "Battle! (Gym Leader)", "Junichi Masuda",
        album_name="Pokemon Battle Themes")

    results = {"tracks": [no_artists_track, matching_track]}
    playlist_info = sample_playlist_info(name="Battle heaters")

    caplog.set_level(logging.WARNING)
    subsonic_helper.write_playlist(None, dict(playlist_info), results)

    assert "Completely Untagged Track" in caplog.text

    (pl_data,) = fake_client.playlists.values()
    song_ids = [entry["id"] for entry in pl_data["entry"]]
    assert song_ids == ["s1"], (
        "the matching track after the bad one must still be found and "
        "added -- one bad track must not abort the whole playlist run")


def test_r3_get_playlist_tracks_skips_empty_artists_track(monkeypatch, caplog):
    """generator.get_playlist_tracks() ingests raw playlist items directly
    -- exactly the code path a local-file playlist (this spec's whole
    scenario) goes through -- so it's reachable and must be guarded too."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    no_artists_item = {"item": {
        "id": None, "name": "Untagged", "is_local": True,
        "uri": "spotify:local:x", "artists": [], "type": "track"}}
    ok_item = {"item": {
        "id": None, "name": "Battle! (Gym Leader)", "is_local": True,
        "uri": "spotify:local:junichi-masuda:pokemon:battle:210",
        "artists": [{"name": "Junichi Masuda"}], "type": "track"}}
    fake_sp = _FakeSpForPlaylistItems(pages=[[no_artists_item, ok_item]])
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    caplog.set_level(logging.WARNING)
    result = generator.get_playlist_tracks(
        {"id": "playlist-1", "name": "Battle heaters"}, {"tracks": []})

    assert len(result["tracks"]) == 1
    assert result["tracks"][0]["name"] == "Battle! (Gym Leader)"
    assert "Untagged" in caplog.text


def test_r3_get_user_saved_tracks_playlist_skips_empty_artists_track(
        monkeypatch, caplog):
    """generator.get_user_saved_tracks_playlist() ingests Liked Songs, where
    a local file can also appear (per the spec's Context) -- reachable and
    must be guarded too."""
    monkeypatch.setattr(time, "sleep", lambda s: None)
    no_artists_item = {"track": {
        "name": "Untagged", "id": None, "is_local": True,
        "uri": "spotify:local:x", "artists": []}}
    ok_item = {"track": {
        "name": "Battle! (Gym Leader)", "id": None, "is_local": True,
        "uri": "spotify:local:y", "artists": [{"name": "Junichi Masuda"}]}}
    fake_sp = _FakeSpForSavedTracks(pages=[[no_artists_item, ok_item]])
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    caplog.set_level(logging.WARNING)
    result = generator.get_user_saved_tracks_playlist({"tracks": []})

    assert len(result["tracks"]) == 1
    assert result["tracks"][0]["name"] == "Battle! (Gym Leader)"
    assert "Untagged" in caplog.text


# ---------------------------------------------------------------------------
# R4: match_with_subsonic_track()/get_subsonic_track_via_string_compare()
# need no changes -- a local-file track that clears R2/R3 flows through
# them completely unmodified and is found via the existing string-compare
# fallback.
# ---------------------------------------------------------------------------

def test_r4_local_file_track_matches_via_existing_string_compare_fallback(
        monkeypatch):
    subsonic_songs = [
        make_subsonic_song("s1", "Battle! (Gym Leader)", "Junichi Masuda",
                            "Pokemon Battle Themes")]
    fake_client = FakePysonicClient(songs=subsonic_songs)
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    local_track = make_local_spotify_track(
        "Battle! (Gym Leader)", "Junichi Masuda",
        album_name="Pokemon Battle Themes")
    results = {"tracks": [local_track]}
    playlist_info = sample_playlist_info(name="Battle heaters")

    subsonic_helper.write_playlist(None, dict(playlist_info), results)

    (pl_data,) = fake_client.playlists.values()
    song_ids = [entry["id"] for entry in pl_data["entry"]]
    assert song_ids == ["s1"]


# ---------------------------------------------------------------------------
# R5: insert_spotify_song()'s track_spotify["uri"] access is guarded
# (missing uri logs a warning and returns None instead of raising
# KeyError); the existing "no album" silent-rollback outcome is unchanged,
# now with a warning naming the track.
# ---------------------------------------------------------------------------

def test_r5_insert_spotify_song_missing_uri_logs_warning_and_returns_none(
        caplog):
    track_spotify = {
        "name": "No Uri Track",
        "album": {"name": "Some Album", "uri": "spotify:album:x"}}
    artist_spotify = {"name": "Some Artist", "uri": "spotify:artist:y"}

    caplog.set_level(logging.WARNING)
    with database.dbms.db_engine.connect() as conn:
        result = database.insert_spotify_song(
            conn, artist_spotify, track_spotify)
        conn.close()

    assert result is None
    assert "No Uri Track" in caplog.text


def test_r5_insert_spotify_song_missing_album_still_rolls_back_now_with_warning(
        caplog):
    track_spotify = {
        "name": "No Album Track",
        "uri": "spotify:local:someone:somealbum:no-album-track:200"}
    artist_spotify = {"name": "Some Artist", "uri": "spotify:artist:y"}

    caplog.set_level(logging.WARNING)
    with database.dbms.db_engine.connect() as conn:
        result = database.insert_spotify_song(
            conn, artist_spotify, track_spotify)
        conn.close()

    assert result is not None
    assert result["song_uuid"] is None
    assert "No Album Track" in caplog.text


def test_r5_insert_spotify_song_empty_album_dict_does_not_raise(caplog):
    """Round-2 regression: a track whose "album" key is present but holds
    an empty dict ({}) -- e.g. a local-file track Spotify reports with
    incomplete album metadata -- must not crash insert_spotify_album()
    with KeyError('uri'). It should be logged and left unpersisted, same
    outcome as the missing-"album"-key case above."""
    track_spotify = {
        "name": "Empty Album Dict Track",
        "uri": "spotify:local:someone:somealbum:empty-album-dict-track:200",
        "album": {}}
    artist_spotify = {"name": "Some Artist", "uri": "spotify:artist:y"}

    caplog.set_level(logging.WARNING)
    with database.dbms.db_engine.connect() as conn:
        result = database.insert_spotify_song(
            conn, artist_spotify, track_spotify)
        conn.close()

    assert result is not None
    assert result["song_uuid"] is None
    assert "Empty Album Dict Track" in caplog.text


def test_r5_insert_spotify_song_album_missing_name_does_not_raise(caplog):
    """Round-2 regression: a track whose "album" dict is present and has a
    "uri" but no "name" key must not crash insert_spotify_album() with
    KeyError('name'). It should be logged and left unpersisted, same
    outcome as the missing-"album"-key case above."""
    track_spotify = {
        "name": "Album Missing Name Track",
        "uri": "spotify:local:someone:somealbum:album-missing-name:200",
        "album": {"uri": "spotify:album:no-name"}}
    artist_spotify = {"name": "Some Artist", "uri": "spotify:artist:y"}

    caplog.set_level(logging.WARNING)
    with database.dbms.db_engine.connect() as conn:
        result = database.insert_spotify_song(
            conn, artist_spotify, track_spotify)
        conn.close()

    assert result is not None
    assert result["song_uuid"] is None
    assert "Album Missing Name Track" in caplog.text


def test_r5_insert_song_handles_missing_uri_gracefully_without_crashing():
    """The insert_song() caller must not itself crash now that
    insert_spotify_song() can return None (rather than a dict) -- confirms
    the R5 fix doesn't just move the KeyError one frame up the stack."""
    playlist_info = sample_playlist_info(name="No Uri Playlist")
    track_spotify = {"name": "No Uri Track"}
    artist_spotify = {"name": "Some Artist", "uri": "spotify:artist:y"}

    result = database.insert_song(
        playlist_info, None, artist_spotify, track_spotify)

    assert result is None


# ---------------------------------------------------------------------------
# R6: a local-file track that matches and persists correctly is
# functionally indistinguishable from a catalog track for
# playlist-membership/idempotency purposes.
# ---------------------------------------------------------------------------

def _relation_row_counts():
    with database.dbms.db_engine.connect() as conn:
        rows = conn.execute(
            database.dbms.subsonic_spotify_relation.select()).fetchall()
        conn.close()
    total = len(rows)
    distinct_pairs = len(
        {(r.playlist_info_uuid, r.spotify_song_uuid) for r in rows})
    return total, distinct_pairs


def test_r6_reimporting_local_file_playlist_twice_is_idempotent(monkeypatch):
    subsonic_songs = [
        make_subsonic_song("s1", "Battle! (Gym Leader)", "Junichi Masuda",
                            "Pokemon Battle Themes")]
    fake_client = FakePysonicClient(songs=subsonic_songs)
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    local_track = make_local_spotify_track(
        "Battle! (Gym Leader)", "Junichi Masuda",
        album_name="Pokemon Battle Themes")
    results = {"tracks": [local_track]}
    playlist_info = sample_playlist_info(name="Battle heaters")

    subsonic_helper.write_playlist(None, dict(playlist_info), results)

    assert len(fake_client.playlists) == 1
    (pl_id_1, pl_data_1), = fake_client.playlists.items()
    song_ids_1 = sorted(entry["id"] for entry in pl_data_1["entry"])
    assert song_ids_1 == ["s1"]
    total_1, distinct_1 = _relation_row_counts()
    assert total_1 == distinct_1 == 1

    # Reimport the exact same local-file playlist again.
    subsonic_helper.write_playlist(None, dict(playlist_info), results)

    assert len(fake_client.playlists) == 1, (
        "the second run must reuse the existing Subsonic playlist")
    (pl_id_2, pl_data_2), = fake_client.playlists.items()
    assert pl_id_2 == pl_id_1
    song_ids_2 = sorted(entry["id"] for entry in pl_data_2["entry"])
    assert song_ids_2 == song_ids_1

    total_2, distinct_2 = _relation_row_counts()
    assert total_2 == distinct_2 == 1, (
        "no duplicate subsonic_spotify_relation row must be created on a "
        "no-op reimport of a local-file track")


# ---------------------------------------------------------------------------
# R7: reproduce the exact live scenario -- a playlist of local-file tracks
# (the "Battle heaters from Gen 3 & 4" Pokemon playlist from the live
# report) where some titles/artists match the Subsonic library and others
# genuinely don't, asserting the found/not-found split is exact.
# ---------------------------------------------------------------------------

def test_r7_battle_heaters_playlist_found_not_found_split_exact(monkeypatch):
    subsonic_songs = [
        make_subsonic_song(
            "s-gym-leader", "Battle! (Gym Leader)", "Junichi Masuda",
            "Pokemon Diamond & Pearl Super Music Collection"),
        make_subsonic_song(
            "s-champion", "Battle! (Champion)", "Go Ichinose",
            "Pokemon Ruby & Sapphire Super Music Collection"),
    ]
    fake_client = FakePysonicClient(songs=subsonic_songs)
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    not_found_track_name = (
        "Battle! (Wild Pokemon) - Totally Not In Library")
    tracks = [
        make_local_spotify_track(
            "Battle! (Gym Leader)", "Junichi Masuda",
            album_name="Pokemon Diamond & Pearl Super Music Collection"),
        make_local_spotify_track(
            "Battle! (Champion)", "Go Ichinose",
            album_name="Pokemon Ruby & Sapphire Super Music Collection"),
        make_local_spotify_track(
            not_found_track_name, "Unknown Composer",
            album_name="Nonexistent Compilation"),
    ]
    results = {"tracks": tracks}
    playlist_info = sample_playlist_info(name="Battle heaters from Gen 3 & 4")

    not_found_calls = []
    original_insert_song = database.insert_song

    def spy_insert_song(pl_info, subsonic_track, artist_spotify, track_spotify):
        if subsonic_track is None:
            not_found_calls.append(track_spotify["name"])
        return original_insert_song(
            pl_info, subsonic_track, artist_spotify, track_spotify)

    monkeypatch.setattr(database, "insert_song", spy_insert_song)

    subsonic_helper.write_playlist(None, dict(playlist_info), results)

    (pl_data,) = fake_client.playlists.values()
    found_ids = sorted(entry["id"] for entry in pl_data["entry"])
    assert found_ids == ["s-champion", "s-gym-leader"], (
        "the two tracks present in the fake library must be found and "
        "added, and the genuinely-absent third track must not be")

    # The genuinely-absent track must be reported "not found" (routed
    # through insert_song(playlist_info, None, ...)) -- not silently
    # dropped, and not falsely matched to either real song above.
    assert not_found_calls == [not_found_track_name]
