"""Integration-style test for R5: reimporting the same playlist twice, with
an unchanged Subsonic library and unchanged Spotify tracks, must produce
byte-identical pushed-playlist membership both times, and
subsonic_spotify_relation must contain exactly one row per
(playlist_info_uuid, spotify_song_uuid) pair -- not a growing pile of
duplicates (the bug fixed by R4).

See specs/reliable-track-matching.md.
"""
from spotisub import database
from spotisub.helpers import subsonic_helper

from tests.helpers import FakePysonicClient, make_spotify_track, make_subsonic_song, sample_playlist_info


def _relation_row_counts():
    """Return (total_rows, distinct_pairs) for subsonic_spotify_relation --
    equal iff there are no duplicate (playlist_info_uuid, spotify_song_uuid)
    pairs."""
    with database.dbms.db_engine.connect() as conn:
        rows = conn.execute(
            database.dbms.subsonic_spotify_relation.select()).fetchall()
        conn.close()
    total = len(rows)
    distinct_pairs = len({(r.playlist_info_uuid, r.spotify_song_uuid) for r in rows})
    return total, distinct_pairs


def test_reimporting_unchanged_playlist_twice_is_idempotent(monkeypatch):
    library_songs = [
        make_subsonic_song("s-vava-voom", "Vava Voom", "Bassnectar", "Vava Voom"),
        make_subsonic_song("s-encore", "Encore", "Bassnectar", "Cozza Frenzy"),
        make_subsonic_song("s-timestretch", "Timestretch", "Bassnectar", "Timestretch"),
    ]
    fake_client = FakePysonicClient(songs=library_songs)
    monkeypatch.setattr(
        subsonic_helper, "check_pysonic_connection", lambda: fake_client)

    spotify_tracks = [
        make_spotify_track("Vava Voom", "Bassnectar", album_name="Vava Voom",
                           track_id="sp-vava-voom"),
        make_spotify_track("Encore", "Bassnectar", album_name="Cozza Frenzy",
                           track_id="sp-encore"),
        make_spotify_track("Timestretch", "Bassnectar", album_name="Timestretch",
                           track_id="sp-timestretch"),
        # Genuinely absent from the fake library -- should never match.
        make_spotify_track("Nonexistent Song", "Nobody At All",
                           album_name="Nothing", track_id="sp-absent"),
    ]
    results = {"tracks": spotify_tracks}
    playlist_info = sample_playlist_info(
        name="Rock", uuid="rock-playlist-uuid",
        spotify_uri="spotify:playlist:rock", import_arg="spotify:playlist:rock")

    subsonic_helper.write_playlist(None, dict(playlist_info), results)

    assert len(fake_client.playlists) == 1
    (pl_id_1, pl_data_1), = fake_client.playlists.items()
    song_ids_run_1 = sorted(entry["id"] for entry in pl_data_1["entry"])
    assert song_ids_run_1 == sorted(
        ["s-vava-voom", "s-encore", "s-timestretch"])

    total_1, distinct_1 = _relation_row_counts()
    assert total_1 == distinct_1, "no duplicates should exist after the first import"

    # Reimport the exact same playlist again: unchanged Subsonic library,
    # unchanged Spotify tracks.
    subsonic_helper.write_playlist(None, dict(playlist_info), results)

    assert len(fake_client.playlists) == 1, (
        "the second run must reuse the existing Subsonic playlist, not "
        "create a second one")
    (pl_id_2, pl_data_2), = fake_client.playlists.items()
    assert pl_id_2 == pl_id_1
    song_ids_run_2 = sorted(entry["id"] for entry in pl_data_2["entry"])

    assert song_ids_run_2 == song_ids_run_1, (
        "pushed playlist membership must be byte-identical across repeated "
        "reimports of an unchanged playlist")

    total_2, distinct_2 = _relation_row_counts()
    assert total_2 == distinct_2, (
        f"subsonic_spotify_relation has {total_2} rows but only "
        f"{distinct_2} distinct (playlist, song) pairs after a second, "
        f"unchanged reimport -- duplicate rows were created")
    assert total_2 == total_1, (
        "row count must not grow on a no-op reimport")
