"""Tests for pruning playlist_info rows no longer present in the user's
Spotify account.

See specs/prune-stale-user-playlists.md.
"""
import pytest

from spotisub import database
from spotisub import generator
from spotisub.helpers import spotipy_helper
from spotisub.helpers import subsonic_helper

from tests.helpers import FakePysonicClient, FakeSpotipyClient, sample_playlist_info


def _seed_playlist(name, subsonic_playlist_id=None):
    """Create a playlist_info row via the real insert path and return the
    row as actually persisted (database.create_playlist()/
    insert_playlist_type() always assigns a fresh uuid on insert -- the
    "uuid" key in the input dict is only used to look an *existing* row up,
    never to set a new row's uuid -- so callers must use the returned row's
    .uuid, not any uuid they passed in)."""
    slug = name.lower().replace(" ", "-")
    playlist_info = sample_playlist_info(
        name=name,
        spotify_uri=f"spotify:playlist:{slug}",
        import_arg=f"spotify:playlist:{slug}")
    if subsonic_playlist_id is not None:
        playlist_info["subsonic_playlist_id"] = subsonic_playlist_id
    row = database.create_playlist(playlist_info)
    assert row is not None
    return row


def _relation_playlist_uuids():
    with database.dbms.db_engine.connect() as conn:
        rows = conn.execute(
            database.dbms.subsonic_spotify_relation.select()).fetchall()
        conn.close()
    return {row.playlist_info_uuid for row in rows}


def _set_ignored(playlist_uuid, ignored=1):
    with database.dbms.db_engine.connect() as conn:
        conn.execute(database.dbms.playlist_info.update().where(
            database.dbms.playlist_info.c.uuid == playlist_uuid
        ).values(ignored=ignored))
        conn.commit()
        conn.close()


# ---------------------------------------------------------------------------
# R1: scan_user_playlists() accumulates and returns the complete set of
# current Spotify playlist names once the full paginated fetch succeeds; a
# mid-fetch failure propagates and produces no usable (partial) list.
# ---------------------------------------------------------------------------

def test_scan_user_playlists_accumulates_names_across_pages(monkeypatch):
    page1 = [{"name": f"Playlist {i}", "uri": f"spotify:playlist:{i}"}
             for i in range(50)]
    page2 = [{"name": f"Playlist {i}", "uri": f"spotify:playlist:{i}"}
             for i in range(50, 110)]
    fake_sp = FakeSpotipyClient(pages=[page1, page2])
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    result = generator.scan_user_playlists()

    expected = {f"Playlist {i}" for i in range(110)}
    assert result == expected


def test_scan_user_playlists_propagates_mid_fetch_failure(monkeypatch):
    fake_sp = FakeSpotipyClient(
        pages=[[{"name": "Page One Playlist", "uri": "spotify:playlist:p1"}]],
        fail_on_page=2)
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    with pytest.raises(Exception):
        generator.scan_user_playlists()


# ---------------------------------------------------------------------------
# R2/R5: the pruning step is sequenced inside scan_library()'s existing try,
# right after scan_user_playlists() -- a mid-fetch failure there must
# prevent pruning from ever running, with zero rows deleted as a result.
# ---------------------------------------------------------------------------

def test_scan_library_never_prunes_on_mid_fetch_failure(monkeypatch):
    prune_calls = []
    monkeypatch.setattr(
        subsonic_helper, "prune_stale_user_playlists",
        lambda names: prune_calls.append(names))
    monkeypatch.setattr(generator, "scan_user_saved_tracks", lambda: None)

    # A stale row that pruning *would* remove if it ever ran.
    stale_row = _seed_playlist("Stale Playlist")

    fake_sp = FakeSpotipyClient(
        pages=[[{"name": "Page One Playlist", "uri": "spotify:playlist:p1"}]],
        fail_on_page=2)
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    with pytest.raises(Exception):
        generator.scan_library()

    assert prune_calls == [], (
        "pruning must not run/be reachable when the R1 fetch raised")
    assert database.select_playlist_info_by_uuid(stale_row.uuid) is not None, (
        "zero playlist_info rows must be deleted when the fetch failed")


# ---------------------------------------------------------------------------
# R3: delete_playlist_info_by_uuid() deletes a playlist_info row and its
# subsonic_spotify_relation rows, whether subsonic_playlist_id is NULL or
# set, and reports whether a Subsonic-side delete is needed.
# ---------------------------------------------------------------------------

def test_delete_playlist_info_by_uuid_with_null_subsonic_id():
    row = _seed_playlist("Null Id Playlist")
    with database.dbms.db_engine.connect() as conn:
        database.insert_playlist_relation(
            conn, "song-id-a", "artist-id-a", {}, "song-uuid-a", row.uuid)
        conn.commit()
        conn.close()

    deleted = database.delete_playlist_info_by_uuid(row.uuid)

    assert deleted is not None
    assert deleted.subsonic_playlist_id is None
    assert database.select_playlist_info_by_uuid(row.uuid) is None
    assert row.uuid not in _relation_playlist_uuids()


def test_delete_playlist_info_by_uuid_with_real_subsonic_id():
    row = _seed_playlist("Real Id Playlist", subsonic_playlist_id="subsonic-playlist-1")
    with database.dbms.db_engine.connect() as conn:
        database.insert_playlist_relation(
            conn, "song-id-b", "artist-id-b", {}, "song-uuid-b", row.uuid)
        conn.commit()
        conn.close()

    deleted = database.delete_playlist_info_by_uuid(row.uuid)

    assert deleted is not None
    assert deleted.subsonic_playlist_id == "subsonic-playlist-1", (
        "the caller needs this to know a Subsonic-side delete is required")
    assert database.select_playlist_info_by_uuid(row.uuid) is None
    assert row.uuid not in _relation_playlist_uuids()


def test_delete_playlist_info_by_uuid_missing_uuid_is_noop():
    assert database.delete_playlist_info_by_uuid("does-not-exist") is None


# ---------------------------------------------------------------------------
# R4/R6: the pruning step itself -- present rows survive, absent rows (with
# or without a Subsonic playlist) are deleted from both playlist_info and
# subsonic_spotify_relation, the Subsonic-side playlist is also deleted when
# one exists, an INFO line is logged per pruned row, and `ignored` has no
# bearing on the prune-or-keep decision.
# ---------------------------------------------------------------------------

def test_prune_stale_user_playlists_three_row_scenario(monkeypatch, caplog):
    fake_client = FakePysonicClient()
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    # Row 1: present on Spotify -> must survive untouched.
    present_row = _seed_playlist("Present Playlist")

    # Row 2: absent from Spotify, subsonic_playlist_id is NULL (never
    # successfully reimported).
    absent_null_row = _seed_playlist("Absent Null Playlist")
    with database.dbms.db_engine.connect() as conn:
        database.insert_playlist_relation(
            conn, "song-id-c", "artist-id-c", {}, "song-uuid-c", absent_null_row.uuid)
        conn.commit()
        conn.close()

    # Row 3: absent from Spotify, has a real Subsonic playlist too.
    fake_client.createPlaylist(
        name="Spotisub - Absent Real Playlist", songIds=[])
    (real_subsonic_id,) = fake_client.playlists.keys()
    absent_real_row = _seed_playlist(
        "Absent Real Playlist", subsonic_playlist_id=real_subsonic_id)
    with database.dbms.db_engine.connect() as conn:
        database.insert_playlist_relation(
            conn, "song-id-d", "artist-id-d", {}, "song-uuid-d", absent_real_row.uuid)
        conn.commit()
        conn.close()

    current_names = {"Present Playlist"}

    caplog.set_level("INFO")
    subsonic_helper.prune_stale_user_playlists(current_names)

    assert database.select_playlist_info_by_uuid(present_row.uuid) is not None
    assert database.select_playlist_info_by_uuid(absent_null_row.uuid) is None
    assert database.select_playlist_info_by_uuid(absent_real_row.uuid) is None

    remaining = _relation_playlist_uuids()
    assert absent_null_row.uuid not in remaining
    assert absent_real_row.uuid not in remaining

    assert real_subsonic_id not in fake_client.playlists, (
        "the Subsonic-side playlist must also be deleted")

    assert "Absent Null Playlist" in caplog.text
    assert "Absent Real Playlist" in caplog.text
    assert "Present Playlist" not in caplog.text


def test_prune_stale_user_playlists_ignored_absent_row_is_pruned(monkeypatch):
    fake_client = FakePysonicClient()
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    ignored_absent_row = _seed_playlist("Ignored Absent Playlist")
    _set_ignored(ignored_absent_row.uuid, 1)

    subsonic_helper.prune_stale_user_playlists(set())

    assert database.select_playlist_info_by_uuid(ignored_absent_row.uuid) is None, (
        "ignored=1 must not save an otherwise-absent row from pruning")


def test_prune_stale_user_playlists_ignored_present_row_survives(monkeypatch):
    fake_client = FakePysonicClient()
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    ignored_present_row = _seed_playlist("Ignored Present Playlist")
    _set_ignored(ignored_present_row.uuid, 1)

    subsonic_helper.prune_stale_user_playlists({"Ignored Present Playlist"})

    assert database.select_playlist_info_by_uuid(ignored_present_row.uuid) is not None, (
        "ignored=1 must not cause pruning of a row still present on Spotify")
