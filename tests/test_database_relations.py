"""Tests for subsonic_spotify_relation identity (R4) and the one-time
duplicate-row repair path (R6).

See specs/reliable-track-matching.md.
"""
import uuid as uuid_module

from spotisub import database


def _relation_rows_for(pl_info_uuid, spotify_song_uuid):
    with database.dbms.db_engine.connect() as conn:
        rows = conn.execute(database.dbms.subsonic_spotify_relation.select().where(
            database.dbms.subsonic_spotify_relation.c.playlist_info_uuid == pl_info_uuid,
            database.dbms.subsonic_spotify_relation.c.spotify_song_uuid == spotify_song_uuid,
        )).fetchall()
        conn.close()
    return rows


def _insert_raw_relation(pl_info_uuid, spotify_song_uuid, subsonic_song_id,
                         subsonic_artist_id=None, row_uuid=None):
    """Insert a subsonic_spotify_relation row directly, bypassing
    insert_playlist_relation()'s dedup logic -- used to set up pre-existing
    duplicate-row fixtures like the ones described in the spec's Context
    (accumulated before the R4 fix shipped)."""
    row_uuid = row_uuid or uuid_module.uuid4().hex
    with database.dbms.db_engine.connect() as conn:
        conn.execute(database.dbms.subsonic_spotify_relation.insert().values(
            uuid=row_uuid,
            subsonic_song_id=subsonic_song_id,
            subsonic_artist_id=subsonic_artist_id,
            spotify_song_uuid=spotify_song_uuid,
            playlist_info_uuid=pl_info_uuid,
        ))
        conn.commit()
        conn.close()
    return row_uuid


# ---------------------------------------------------------------------------
# R4: insert_playlist_relation()/select_playlist_relation() key by
# (spotify_song_uuid, playlist_info_uuid) only -- a changed match outcome
# updates the existing row instead of inserting a second one.
# ---------------------------------------------------------------------------

def test_insert_playlist_relation_updates_existing_row_in_place():
    pl_uuid = "playlist-1"
    song_uuid = "song-1"

    with database.dbms.db_engine.connect() as conn:
        first = database.insert_playlist_relation(
            conn, "real-subsonic-song-id", "real-artist-id",
            {"name": "irrelevant"}, song_uuid, pl_uuid)
        conn.commit()
        conn.close()

    assert first is not None
    assert first.subsonic_song_id == "real-subsonic-song-id"

    rows = _relation_rows_for(pl_uuid, song_uuid)
    assert len(rows) == 1

    # Track stops matching on a later reimport (write_playlist calls
    # insert_song(playlist_info, None, ...) in that case).
    with database.dbms.db_engine.connect() as conn:
        database.insert_playlist_relation(
            conn, None, None, {"name": "irrelevant"}, song_uuid, pl_uuid)
        conn.commit()
        conn.close()

    rows = _relation_rows_for(pl_uuid, song_uuid)
    assert len(rows) == 1, (
        f"expected the existing row to be updated in place, found "
        f"{len(rows)} rows for the same (playlist, song) pair")
    assert rows[0].uuid == first.uuid
    assert rows[0].subsonic_song_id is None


def test_insert_playlist_relation_does_not_key_on_subsonic_song_id():
    """Guards specifically against the original bug: matching by the
    match *outcome* (subsonic_song_id/subsonic_artist_id) instead of by
    (spotify_song_uuid, playlist_info_uuid) identity."""
    pl_uuid = "playlist-2"
    song_uuid = "song-2"

    with database.dbms.db_engine.connect() as conn:
        database.insert_playlist_relation(
            conn, "song-id-A", "artist-id-A", {}, song_uuid, pl_uuid)
        conn.commit()
        conn.close()

    # Re-matched to a *different* subsonic song on a later run (e.g. the
    # library was retagged) -- still the same (playlist, spotify song) pair.
    with database.dbms.db_engine.connect() as conn:
        database.insert_playlist_relation(
            conn, "song-id-B", "artist-id-B", {}, song_uuid, pl_uuid)
        conn.commit()
        conn.close()

    rows = _relation_rows_for(pl_uuid, song_uuid)
    assert len(rows) == 1
    assert rows[0].subsonic_song_id == "song-id-B"


def test_insert_playlist_relation_creates_new_row_for_different_pairs():
    with database.dbms.db_engine.connect() as conn:
        database.insert_playlist_relation(
            conn, "s1", "a1", {}, "song-x", "playlist-x")
        database.insert_playlist_relation(
            conn, "s2", "a2", {}, "song-y", "playlist-x")
        conn.commit()
        conn.close()

    assert len(_relation_rows_for("playlist-x", "song-x")) == 1
    assert len(_relation_rows_for("playlist-x", "song-y")) == 1


# ---------------------------------------------------------------------------
# R6: one-time repair path de-duplicates pre-existing duplicate rows.
# ---------------------------------------------------------------------------

def test_repair_dry_run_reports_without_deleting():
    pl_uuid = "playlist-dup"
    song_uuid = "song-dup"
    stale_uuid = _insert_raw_relation(pl_uuid, song_uuid, "real-song-id")
    fresh_uuid = _insert_raw_relation(pl_uuid, song_uuid, None)

    plan = database.repair_duplicate_playlist_relations(dry_run=True)

    assert len(_relation_rows_for(pl_uuid, song_uuid)) == 2, (
        "dry run must not delete anything")

    matching = [p for p in plan
                if p["playlist_info_uuid"] == pl_uuid
                and p["spotify_song_uuid"] == song_uuid]
    assert len(matching) == 1
    item = matching[0]
    # No live-Subsonic info supplied -> falls back to most-recently-inserted.
    assert item["keep_uuid"] == fresh_uuid
    assert item["delete_uuids"] == [stale_uuid]


def test_repair_applies_and_collapses_to_one_row():
    pl_uuid = "playlist-dup-2"
    song_uuid = "song-dup-2"
    _insert_raw_relation(pl_uuid, song_uuid, "real-song-id")
    fresh_uuid = _insert_raw_relation(pl_uuid, song_uuid, None)

    database.repair_duplicate_playlist_relations(dry_run=False)

    rows = _relation_rows_for(pl_uuid, song_uuid)
    assert len(rows) == 1
    assert rows[0].uuid == fresh_uuid


def test_repair_prefers_row_confirmed_live_over_most_recent():
    pl_uuid = "playlist-dup-3"
    song_uuid = "song-dup-3"
    live_uuid = _insert_raw_relation(pl_uuid, song_uuid, "still-live-song-id")
    # Inserted after the live one, but its subsonic_song_id is stale/gone.
    _insert_raw_relation(pl_uuid, song_uuid, "stale-deleted-song-id")

    live_song_ids_by_playlist = {pl_uuid: {"still-live-song-id"}}

    database.repair_duplicate_playlist_relations(
        live_song_ids_by_playlist=live_song_ids_by_playlist, dry_run=False)

    rows = _relation_rows_for(pl_uuid, song_uuid)
    assert len(rows) == 1
    assert rows[0].uuid == live_uuid
    assert rows[0].subsonic_song_id == "still-live-song-id"


def test_repair_leaves_non_duplicate_pairs_untouched():
    _insert_raw_relation("playlist-solo", "song-solo", "some-id")

    database.repair_duplicate_playlist_relations(dry_run=False)

    assert len(_relation_rows_for("playlist-solo", "song-solo")) == 1


def test_repair_reports_nothing_when_no_duplicates_exist():
    plan = database.repair_duplicate_playlist_relations(dry_run=True)
    assert plan == []
