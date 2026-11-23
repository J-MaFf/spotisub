"""Tests for the playlist-ownership marker: Spotisub marks every Subsonic
playlist it creates with constants.MANAGED_PLAYLIST_MARKER in the playlist's
comment, and never adopts, rewrites or deletes a playlist without that mark
-- even one that has exactly the name Spotisub would give the playlist
(J-MaFf/homelab#1038).

Test names carry the letter of the case they cover:
  (a) a newly created playlist gets the marker comment
  (b) an unmanaged same-name playlist is left untouched, with a warning
  (c) prune leaves an unmanaged same-name playlist alone, deletes a managed one
  (d) the ignored-playlist path leaves an unmanaged same-name playlist alone
  (e) the zero-songs path leaves an unmanaged playlist alone
  (f) with PLAYLIST_PREFIX='', a managed playlist named exactly like the
      Spotify playlist is found and updated
"""
import logging

import pytest

from spotisub import constants
from spotisub import database
from spotisub.helpers import subsonic_helper

from tests.helpers import (FakePysonicClient, make_spotify_track,
                           make_subsonic_song, sample_playlist_info)

MARKER = constants.MANAGED_PLAYLIST_MARKER

LIBRARY = [
    make_subsonic_song("s-vava-voom", "Vava Voom", "Bassnectar", "Vava Voom"),
    make_subsonic_song("s-encore", "Encore", "Bassnectar", "Cozza Frenzy"),
]

MATCHING_TRACKS = [
    make_spotify_track("Vava Voom", "Bassnectar", album_name="Vava Voom",
                       track_id="sp-vava-voom"),
    make_spotify_track("Encore", "Bassnectar", album_name="Cozza Frenzy",
                       track_id="sp-encore"),
]

UNMATCHED_TRACKS = [
    make_spotify_track("Nonexistent Song", "Nobody At All",
                       album_name="Nothing", track_id="sp-absent"),
]


@pytest.fixture
def default_prefix(monkeypatch):
    monkeypatch.delenv(constants.PLAYLIST_PREFIX, raising=False)


@pytest.fixture
def empty_prefix(monkeypatch):
    monkeypatch.setenv(constants.PLAYLIST_PREFIX, "")


def _fake_client(monkeypatch, **kwargs):
    fake_client = FakePysonicClient(songs=LIBRARY, **kwargs)
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)
    return fake_client


def _seed_row(name, ignored=0):
    slug = name.lower().replace(" ", "-")
    row = database.create_playlist(sample_playlist_info(
        name=name, spotify_uri=f"spotify:playlist:{slug}", import_arg=name))
    assert row is not None
    if ignored:
        database.update_ignored_playlist(row.uuid, ignored)
    return database.select_playlist_info_by_uuid(row.uuid)


def _playlist_info(name):
    slug = name.lower().replace(" ", "-")
    return sample_playlist_info(
        name=name, uuid=f"{slug}-uuid",
        spotify_uri=f"spotify:playlist:{slug}",
        import_arg=f"spotify:playlist:{slug}")


def _calls_on(fake_client, playlist_id):
    return [call for call in fake_client.calls if call[1] == playlist_id]


def _song_ids(fake_client, playlist_id):
    return sorted(e["id"] for e in fake_client.playlists[playlist_id]["entry"])


# --------------------------------------------------------------------------
# (a)
# --------------------------------------------------------------------------

def test_is_managed_playlist_checks_the_comment_for_the_marker():
    assert MARKER == "spotisub:managed"
    assert subsonic_helper.is_managed_playlist(
        {"id": "1", "name": "chill", "comment": MARKER})
    assert subsonic_helper.is_managed_playlist(
        {"id": "1", "name": "chill", "comment": "mine; " + MARKER + " ok"})
    assert not subsonic_helper.is_managed_playlist({"id": "1", "name": "chill"})
    assert not subsonic_helper.is_managed_playlist(
        {"id": "1", "name": "chill", "comment": ""})
    assert not subsonic_helper.is_managed_playlist(
        {"id": "1", "name": "chill", "comment": "spotisub"})
    assert not subsonic_helper.is_managed_playlist(None)


@pytest.mark.parametrize("return_created_playlist", [False, True],
                         ids=["lookup-new-id", "id-from-create-response"])
def test_a_new_playlist_gets_marker_before_any_song_write(
        monkeypatch, default_prefix, return_created_playlist):
    fake_client = _fake_client(
        monkeypatch, return_created_playlist=return_created_playlist)

    subsonic_helper.write_playlist(
        None, _playlist_info("Rock"), {"tracks": MATCHING_TRACKS})

    (new_id, data), = fake_client.playlists.items()
    assert data["name"] == "Spotisub - Rock"
    assert data["comment"] == MARKER
    assert _song_ids(fake_client, new_id) == ["s-encore", "s-vava-voom"]

    calls = _calls_on(fake_client, new_id)
    # 1. created empty, 2. marked, 3+. songs written -- in that order.
    assert calls[0] == ("createPlaylist", new_id,
                        {"name": "Spotisub - Rock", "songIds": []})
    assert calls[1] == ("updatePlaylist", new_id, {"comment": MARKER})
    song_writes = calls[2:]
    assert song_writes, "songs must be written after the marker"
    assert all(name == "createPlaylist" and "songIds" in kwargs
               and "name" not in kwargs
               for name, _, kwargs in song_writes)
    assert [c[0] for c in fake_client.calls].count("updatePlaylist") == 1


def test_a_second_run_reuses_the_marked_playlist(monkeypatch, default_prefix):
    fake_client = _fake_client(monkeypatch)
    subsonic_helper.write_playlist(
        None, _playlist_info("Rock"), {"tracks": MATCHING_TRACKS})
    (first_id,) = fake_client.playlists.keys()
    fake_client.calls.clear()

    subsonic_helper.write_playlist(
        None, _playlist_info("Rock"), {"tracks": MATCHING_TRACKS})

    assert list(fake_client.playlists.keys()) == [first_id]
    assert all(call[0] == "createPlaylist" and "name" not in call[2]
               for call in fake_client.calls), (
        "an already-managed playlist must only get its songs rewritten, "
        "never re-created or re-marked")


# --------------------------------------------------------------------------
# (b)
# --------------------------------------------------------------------------

def test_b_unmanaged_same_name_playlist_is_left_untouched_with_warning(
        monkeypatch, default_prefix, caplog):
    fake_client = _fake_client(monkeypatch)
    hand_made_id = fake_client.add_playlist(
        "Spotisub - Rock", song_ids=["hand-picked-1", "hand-picked-2"])
    before = {pid: dict(data) for pid, data in fake_client.playlists.items()}

    caplog.set_level(logging.INFO)
    subsonic_helper.write_playlist(
        None, _playlist_info("Rock"), {"tracks": MATCHING_TRACKS})

    assert fake_client.calls == [], (
        "no createPlaylist, updatePlaylist or deletePlaylist call may be "
        "made for a name held by an unmanaged playlist")
    assert fake_client.playlists == before
    assert _song_ids(fake_client, hand_made_id) == [
        "hand-picked-1", "hand-picked-2"]

    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING
                and "not managed by Spotisub" in r.getMessage()]
    assert len(warnings) == 1
    assert "Spotisub - Rock" in warnings[0].getMessage()


# --------------------------------------------------------------------------
# (c)
# --------------------------------------------------------------------------

def test_c_prune_leaves_unmanaged_alone_and_deletes_managed(
        monkeypatch, default_prefix):
    fake_client = _fake_client(monkeypatch)
    # "Gone Mixed": a hand-made and a Spotisub-made playlist share the name.
    mixed_unmanaged_id = fake_client.add_playlist(
        "Spotisub - Gone Mixed", song_ids=["hand-1"])
    mixed_managed_id = fake_client.add_playlist(
        "Spotisub - Gone Mixed", song_ids=["s-encore"], comment=MARKER)
    # "Gone Hand Made": only a hand-made playlist has the name.
    hand_made_id = fake_client.add_playlist(
        "Spotisub - Gone Hand Made", song_ids=["hand-2"])
    mixed_row = _seed_row("Gone Mixed")
    hand_made_row = _seed_row("Gone Hand Made")

    subsonic_helper.prune_stale_user_playlists(set())

    # Both stale playlist_info rows are pruned from Spotisub's own DB...
    assert database.select_playlist_info_by_uuid(mixed_row.uuid) is None
    assert database.select_playlist_info_by_uuid(hand_made_row.uuid) is None
    # ...the managed Subsonic playlist is deleted...
    assert mixed_managed_id not in fake_client.playlists
    # ...and neither unmanaged one is.
    assert mixed_unmanaged_id in fake_client.playlists
    assert hand_made_id in fake_client.playlists
    deleted = [c[1] for c in fake_client.calls if c[0] == "deletePlaylist"]
    assert deleted == [mixed_managed_id]


# --------------------------------------------------------------------------
# (d)
# --------------------------------------------------------------------------

def test_d_ignored_playlist_path_leaves_unmanaged_same_name_alone(
        monkeypatch, empty_prefix):
    """With PLAYLIST_PREFIX='' an ignored Spotify playlist "Raspy" maps to a
    Subsonic playlist named exactly "Raspy": a hand-made one must survive
    every route into remove_ignored_playlist()."""
    fake_client = _fake_client(monkeypatch)
    hand_made_id = fake_client.add_playlist("Raspy", song_ids=["hand-1"])
    row = _seed_row("Raspy", ignored=1)

    subsonic_helper.remove_ignored_playlists()
    subsonic_helper.remove_ignored_playlist(
        database.select_playlist_info_by_uuid(row.uuid))
    subsonic_helper.write_playlist(
        None, sample_playlist_info(name="Raspy", uuid=row.uuid),
        {"tracks": MATCHING_TRACKS})
    subsonic_helper.set_ignore("playlist", row.uuid, 1)

    assert hand_made_id in fake_client.playlists
    assert _song_ids(fake_client, hand_made_id) == ["hand-1"]
    assert not [c for c in fake_client.calls if c[0] == "deletePlaylist"]


def test_d_ignored_playlist_path_still_deletes_managed_copy(
        monkeypatch, empty_prefix):
    fake_client = _fake_client(monkeypatch)
    hand_made_id = fake_client.add_playlist("Raspy", song_ids=["hand-1"])
    managed_id = fake_client.add_playlist(
        "Raspy", song_ids=["s-encore"], comment=MARKER)
    _seed_row("Raspy", ignored=1)

    subsonic_helper.remove_ignored_playlists()

    assert managed_id not in fake_client.playlists
    assert hand_made_id in fake_client.playlists


# --------------------------------------------------------------------------
# (e)
# --------------------------------------------------------------------------

def test_e_zero_songs_path_leaves_unmanaged_playlist_alone(
        monkeypatch, default_prefix):
    fake_client = _fake_client(monkeypatch)
    hand_made_id = fake_client.add_playlist(
        "Spotisub - Rock", song_ids=["hand-1"])

    subsonic_helper.write_playlist(
        None, _playlist_info("Rock"), {"tracks": UNMATCHED_TRACKS})

    assert hand_made_id in fake_client.playlists
    assert _song_ids(fake_client, hand_made_id) == ["hand-1"]
    assert not [c for c in fake_client.calls if c[0] == "deletePlaylist"]


def test_e_zero_songs_path_spares_playlist_unmarked_mid_import(
        monkeypatch, default_prefix):
    """The id is looked up (managed) before the tracks are matched; if the
    marker is removed from the comment while the import runs, the
    zero-songs branch must not delete the playlist."""
    fake_client = _fake_client(monkeypatch)
    playlist_id = fake_client.add_playlist(
        "Spotisub - Rock", song_ids=["s-old"], comment=MARKER)

    def unmark_then_match_nothing(comparison_helper, *args, **kwargs):
        fake_client.playlists[playlist_id].pop("comment", None)
        return comparison_helper
    monkeypatch.setattr(
        subsonic_helper, "match_with_subsonic_track", unmark_then_match_nothing)

    subsonic_helper.write_playlist(
        None, _playlist_info("Rock"), {"tracks": UNMATCHED_TRACKS})

    assert playlist_id in fake_client.playlists
    assert not [c for c in fake_client.calls if c[0] == "deletePlaylist"]


def test_e_zero_songs_path_still_deletes_managed_playlist(
        monkeypatch, default_prefix):
    fake_client = _fake_client(monkeypatch)
    playlist_id = fake_client.add_playlist(
        "Spotisub - Rock", song_ids=["s-old"], comment=MARKER)

    subsonic_helper.write_playlist(
        None, _playlist_info("Rock"), {"tracks": UNMATCHED_TRACKS})

    assert playlist_id not in fake_client.playlists


# --------------------------------------------------------------------------
# (f)
# --------------------------------------------------------------------------

def test_f_empty_prefix_finds_and_updates_managed_playlist_by_plain_name(
        monkeypatch, empty_prefix):
    fake_client = _fake_client(monkeypatch)
    # The state after the in-place rename: plain Spotify name + marker.
    renamed_id = fake_client.add_playlist(
        "chill", song_ids=["s-old"], comment=MARKER)

    subsonic_helper.write_playlist(
        None, _playlist_info("chill"), {"tracks": MATCHING_TRACKS})

    assert list(fake_client.playlists.keys()) == [renamed_id], (
        "no new playlist may be created next to the renamed one")
    data = fake_client.playlists[renamed_id]
    assert data["name"] == "chill"
    assert data["comment"] == MARKER
    assert _song_ids(fake_client, renamed_id) == ["s-encore", "s-vava-voom"]
    assert not [c for c in fake_client.calls
                if c[0] in ("deletePlaylist", "updatePlaylist")]
    assert not [c for c in fake_client.calls
                if c[0] == "createPlaylist" and "name" in c[2]]
