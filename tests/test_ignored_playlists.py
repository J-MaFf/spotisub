"""Tests for ignored user playlists: an ignored playlist is skipped before any
Spotify fetch or Subsonic write, ignoring one deletes its Subsonic copy, and
the ignored flag survives that copy being gone.
"""
from spotisub import database
from spotisub import generator
from spotisub.helpers import spotipy_helper
from spotisub.helpers import subsonic_helper

from tests.helpers import FakePysonicClient, make_spotify_track, sample_playlist_info


def _seed_playlist(name, subsonic_playlist_id=None, ignored=0):
    slug = name.lower().replace(" ", "-")
    playlist_info = sample_playlist_info(
        name=name,
        spotify_uri=f"spotify:playlist:{slug}",
        import_arg=name)
    if subsonic_playlist_id is not None:
        playlist_info["subsonic_playlist_id"] = subsonic_playlist_id
    row = database.create_playlist(playlist_info)
    assert row is not None
    if ignored:
        database.update_ignored_playlist(row.uuid, ignored)
    return database.select_playlist_info_by_uuid(row.uuid)


def _fake_client(monkeypatch):
    fake_client = FakePysonicClient()
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)
    return fake_client


def _live_copy(fake_client, name):
    """Create a Subsonic playlist named like Spotisub's copy of `name` and
    return its id."""
    fake_client.createPlaylist(name="Spotisub - " + name, songIds=["song-1"])
    return max(fake_client.playlists.keys(), key=int)


def _playlist_names(fake_client):
    return {data["name"] for data in fake_client.playlists.values()}


def test_write_playlist_skips_ignored_playlist_and_deletes_its_copy(monkeypatch):
    fake_client = _fake_client(monkeypatch)
    live_id = _live_copy(fake_client, "Raspy")
    row = _seed_playlist("Raspy", subsonic_playlist_id=live_id, ignored=1)

    playlist_info = sample_playlist_info(name="Raspy", uuid=row.uuid)
    subsonic_helper.write_playlist(
        None, playlist_info,
        {"tracks": [make_spotify_track("Song", "Artist", album_name="Album")]})

    assert fake_client.playlists == {}
    kept = database.select_playlist_info_by_uuid(row.uuid)
    assert kept.ignored == 1
    assert kept.subsonic_playlist_id is None


def test_write_playlist_never_creates_a_copy_for_ignored_playlist(monkeypatch):
    fake_client = _fake_client(monkeypatch)
    row = _seed_playlist("Raspy", ignored=1)

    subsonic_helper.write_playlist(
        None, sample_playlist_info(name="Raspy", uuid=row.uuid),
        {"tracks": [make_spotify_track("Song", "Artist", album_name="Album")]})

    assert fake_client.playlists == {}


def test_set_ignore_deletes_copy_and_flag_survives_deleted_playlist_cleanup(monkeypatch):
    fake_client = _fake_client(monkeypatch)
    live_id = _live_copy(fake_client, "Raspy")
    row = _seed_playlist("Raspy", subsonic_playlist_id=live_id)

    subsonic_helper.set_ignore("playlist", row.uuid, 1)

    assert "Spotisub - Raspy" not in _playlist_names(fake_client)

    # The 8-hourly cleanup drops playlist_info rows whose recorded Subsonic
    # playlist is gone; it must not take the ignored row with it.
    subsonic_helper.remove_subsonic_deleted_playlist()

    kept = database.select_playlist_info_by_uuid(row.uuid)
    assert kept is not None
    assert kept.ignored == 1


def test_set_ignore_zero_leaves_copy_alone(monkeypatch):
    fake_client = _fake_client(monkeypatch)
    live_id = _live_copy(fake_client, "Raspy")
    row = _seed_playlist("Raspy", subsonic_playlist_id=live_id, ignored=1)

    subsonic_helper.set_ignore("playlist", row.uuid, 0)

    assert "Spotisub - Raspy" in _playlist_names(fake_client)
    assert database.select_playlist_info_by_uuid(row.uuid).ignored == 0


def test_get_user_playlists_run_skips_spotify_for_ignored_playlist(monkeypatch):
    fake_client = _fake_client(monkeypatch)
    _live_copy(fake_client, "Raspy")
    row = _seed_playlist("Raspy", ignored=1)

    def no_spotify():
        raise AssertionError("an ignored playlist must not be fetched")
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", no_spotify)

    generator.get_user_playlists_run(row.uuid)

    assert fake_client.playlists == {}


def test_pick_user_playlist_leaves_out_ignored(monkeypatch):
    kept = _seed_playlist("Rock")
    _seed_playlist("Raspy", ignored=1)
    _seed_playlist("Songs we both like", ignored=1)

    offered = []
    monkeypatch.setattr(
        generator.random, "choice", lambda seq: offered.append(seq) or seq[0])

    picked = generator.pick_user_playlist()

    assert picked.uuid == kept.uuid
    assert [p.uuid for p in offered[0]] == [kept.uuid]


def test_pick_user_playlist_returns_none_when_all_ignored():
    _seed_playlist("Raspy", ignored=1)

    assert generator.pick_user_playlist() is None


def test_remove_ignored_playlists_only_removes_ignored_copies(monkeypatch):
    fake_client = _fake_client(monkeypatch)
    _live_copy(fake_client, "Raspy")
    _live_copy(fake_client, "Rock")
    _seed_playlist("Raspy", ignored=1)
    _seed_playlist("Rock")

    subsonic_helper.remove_ignored_playlists()

    assert _playlist_names(fake_client) == {"Spotisub - Rock"}
