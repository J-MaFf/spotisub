"""One-time repair for duplicate subsonic_spotify_relation rows.

Background: before the fix in specs/archive/reliable-track-matching.md (R4),
insert_playlist_relation()/select_playlist_relation() matched an existing row
by its match *outcome* (subsonic_song_id, subsonic_artist_id) instead of by
identity (spotify_song_uuid, playlist_info_uuid). A track that matched on one
run and then stopped matching on a later run (or vice versa) could never find
its own prior row, so a second row got inserted for the same
(playlist_info_uuid, spotify_song_uuid) pair instead of updating the first.
Installations that reimported playlists more than once before that fix
shipped can have accumulated many such duplicate pairs.

This script finds every duplicate (playlist_info_uuid, spotify_song_uuid)
pair and deletes all but one row for it, keeping the most-recently-inserted
row (SQLite rowid) for each pair -- this table has no timestamp column, and
rowid is monotonically increasing for ordinary inserts on SQLite's default
rowid tables, so it's a reasonable insertion-order proxy.

IMPORTANT: this script deliberately uses only Python's standard-library
`sqlite3` module and imports NOTHING from the `spotisub` package (nor from
the sibling `config` module, which itself pulls in `apscheduler`). Importing
`spotisub` or any of its submodules (e.g. `from spotisub import database`)
runs spotisub/__init__.py, which creates the Flask app and starts a second,
independent APScheduler instance -- a real, previously-hit-in-production
incident: that second scheduler immediately fires real jobs
(scan_library -> scan_user_playlists, etc.) concurrently against the same
live Spotify account and the same live Navidrome/SQLite database as the
actually-running app, corrupting playlist state via concurrent SQLite
writers. This script must be safe to run via
`docker exec spotisub python3 repair_duplicate_relations.py` while the real
app is running, so it stays fully standalone with zero non-stdlib
dependencies and zero app imports -- just direct SQL against the same
SQLite file the app uses.

Usage:
    python3 repair_duplicate_relations.py          # dry run, prints a report
    python3 repair_duplicate_relations.py --apply  # actually deletes the losing rows
"""
import argparse
import sqlite3
import sys
from os.path import dirname, join

RELATION_TABLE = "subsonic_spotify_relation"

# Matches config.Config.SQLALCHEMY_DATABASE_URI's layout (sqlite:///<repo
# root>/cache/spotisub.db) without importing config.py, so this script has
# no dependency on anything beyond the Python standard library.
DEFAULT_DB_PATH = join(dirname(__file__), "cache", "spotisub.db")


def find_duplicate_groups(conn):
    """Returns {(playlist_info_uuid, spotify_song_uuid): [row, ...]} for
    every pair with more than one row, each row ordered oldest-to-newest by
    rowid. Each row is a sqlite3.Row with rowid/uuid/spotify_song_uuid/
    playlist_info_uuid fields.
    """
    rows = conn.execute(
        f"SELECT rowid, uuid, subsonic_song_id, subsonic_artist_id, "
        f"spotify_song_uuid, playlist_info_uuid "
        f"FROM {RELATION_TABLE} "
        f"ORDER BY playlist_info_uuid, spotify_song_uuid, rowid"
    ).fetchall()

    groups = {}
    for row in rows:
        key = (row["playlist_info_uuid"], row["spotify_song_uuid"])
        groups.setdefault(key, []).append(row)

    return {key: group_rows for key,
            group_rows in groups.items() if len(group_rows) > 1}


def plan_repair(conn):
    """Decide, for every duplicate pair, which row to keep (highest rowid --
    most recently inserted) and which to delete. Returns a list of dicts:
    {"playlist_info_uuid", "spotify_song_uuid", "keep_uuid", "delete_uuids"}.
    Read-only -- does not modify the database.
    """
    groups = find_duplicate_groups(conn)
    plan = []
    for (playlist_info_uuid, spotify_song_uuid), rows in groups.items():
        keep = max(rows, key=lambda r: r["rowid"])
        plan.append({
            "playlist_info_uuid": playlist_info_uuid,
            "spotify_song_uuid": spotify_song_uuid,
            "keep_uuid": keep["uuid"],
            "delete_uuids": [r["uuid"] for r in rows if r["uuid"] != keep["uuid"]],
        })
    return plan


def apply_repair(conn, plan):
    all_delete_uuids = [u for item in plan for u in item["delete_uuids"]]
    if not all_delete_uuids:
        return
    placeholders = ", ".join("?" for _ in all_delete_uuids)
    conn.execute(
        f"DELETE FROM {RELATION_TABLE} WHERE uuid IN ({placeholders})",
        all_delete_uuids)
    conn.commit()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually delete the superseded duplicate rows. Without this "
             "flag, only a report is printed (dry run) and nothing is "
             "changed in the database.")
    parser.add_argument(
        "--db-path", default=DEFAULT_DB_PATH,
        help=f"Path to the SQLite database file (default: {DEFAULT_DB_PATH}).")
    args = parser.parse_args(argv)

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    try:
        plan = plan_repair(conn)

        if not plan:
            print("No duplicate subsonic_spotify_relation rows found. Nothing to do.")
            return 0

        total_deleted = 0
        for item in plan:
            total_deleted += len(item["delete_uuids"])
            verb = "Deleting" if args.apply else "Would delete"
            print(
                f"playlist_info_uuid={item['playlist_info_uuid']} "
                f"spotify_song_uuid={item['spotify_song_uuid']}: "
                f"keeping row {item['keep_uuid']}; "
                f"{verb} {len(item['delete_uuids'])} row(s): "
                f"{item['delete_uuids']}")

        if args.apply:
            apply_repair(conn, plan)
    finally:
        conn.close()

    print(
        f"\n{'Deleted' if args.apply else 'Would delete'} "
        f"{total_deleted} duplicate row(s) across {len(plan)} pair(s).")
    if not args.apply:
        print("Dry run only -- nothing was changed. Re-run with --apply to "
              "actually delete these rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
