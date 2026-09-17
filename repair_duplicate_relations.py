"""One-time repair for duplicate subsonic_spotify_relation rows.

Background: before the fix in specs/reliable-track-matching.md (R4),
insert_playlist_relation()/select_playlist_relation() matched an existing row
by its match *outcome* (subsonic_song_id, subsonic_artist_id) instead of by
identity (spotify_song_uuid, playlist_info_uuid). A track that matched on one
run and then stopped matching on a later run (or vice versa) could never find
its own prior row, so a second row got inserted for the same
(playlist_info_uuid, spotify_song_uuid) pair instead of updating the first.
Installations that reimported playlists more than once before that fix
shipped can have accumulated many such duplicate pairs.

This script finds every duplicate (playlist_info_uuid, spotify_song_uuid)
pair and deletes all but one row for it, preferring (in order):
  1. the row whose subsonic_song_id is currently confirmed present in the
     live Subsonic library AND in that playlist's currently-pushed contents
     (requires reaching the Subsonic server -- skipped, with a warning, if it
     can't be reached, or via --skip-live-check);
  2. otherwise, the most-recently-inserted row (SQLite rowid).

Usage:
    python3 repair_duplicate_relations.py                  # dry run, prints a report
    python3 repair_duplicate_relations.py --apply           # actually deletes the losing rows
    python3 repair_duplicate_relations.py --skip-live-check # don't contact Subsonic; use rowid only
"""
import argparse
import sys
from os.path import dirname, join
from dotenv import load_dotenv

dotenv_path = join(dirname(__file__), '.env')
load_dotenv(dotenv_path)

from spotisub import database  # noqa: E402  (must follow load_dotenv)


def _collect_live_song_ids_by_playlist():
    """Best-effort: ask the live Subsonic server which songs are actually in
    each spotisub-managed playlist right now, so the repair can prefer the
    duplicate row whose subsonic_song_id matches reality. Returns None if the
    Subsonic server can't be reached, in which case the repair falls back to
    keeping the most-recently-inserted row for every duplicate pair.
    """
    try:
        from spotisub.helpers import subsonic_helper
        subsonic_helper.check_pysonic_connection()
    except Exception as e:
        print(
            f"Could not reach the Subsonic server ({e}); "
            "falling back to most-recently-inserted for all duplicates.")
        return None

    live_ids_by_playlist = {}
    all_playlists, _ = database.select_all_playlists()
    for playlist in all_playlists:
        subsonic_playlist_id = playlist.get("subsonic_playlist_id")
        playlist_info_uuid = playlist.get("uuid")
        if not subsonic_playlist_id or not playlist_info_uuid:
            continue
        try:
            song_ids = subsonic_helper.get_playlist_songs_ids_by_id(
                subsonic_playlist_id)
        except Exception as e:
            print(
                f"Could not read playlist {subsonic_playlist_id!r} "
                f"from Subsonic ({e}); skipping live check for it.")
            continue
        live_ids_by_playlist[playlist_info_uuid] = set(song_ids)
    return live_ids_by_playlist


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
        "--skip-live-check", action="store_true",
        help="Don't contact the Subsonic server; always keep the "
             "most-recently-inserted row for each duplicate pair.")
    args = parser.parse_args(argv)

    live_ids_by_playlist = None
    if not args.skip_live_check:
        live_ids_by_playlist = _collect_live_song_ids_by_playlist()

    plan = database.repair_duplicate_playlist_relations(
        live_song_ids_by_playlist=live_ids_by_playlist,
        dry_run=not args.apply)

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

    print(
        f"\n{'Deleted' if args.apply else 'Would delete'} "
        f"{total_deleted} duplicate row(s) across {len(plan)} pair(s).")
    if not args.apply:
        print("Dry run only -- nothing was changed. Re-run with --apply to "
              "actually delete these rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
