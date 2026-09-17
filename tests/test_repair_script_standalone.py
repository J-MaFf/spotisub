"""Tests for the standalone repair_duplicate_relations.py CLI.

This script deliberately does not import anything from the `spotisub`
package (see its module docstring for why -- importing `spotisub` runs
spotisub/__init__.py, which bootstraps a second live Flask/APScheduler app).
These tests import it directly as a plain, dependency-free module to prove
that stays true and that its repair logic is correct on its own.
"""
import ast
import sqlite3
import subprocess
import sys
from os.path import dirname, join

SCRIPT_PATH = join(dirname(dirname(__file__)), "repair_duplicate_relations.py")


def _make_db(tmp_path, rows):
    db_path = tmp_path / "spotisub.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE subsonic_spotify_relation ("
        "uuid TEXT, subsonic_song_id TEXT, subsonic_artist_id TEXT, "
        "spotify_song_uuid TEXT, playlist_info_uuid TEXT, ignored INTEGER)")
    conn.executemany(
        "INSERT INTO subsonic_spotify_relation VALUES (?, ?, ?, ?, ?, 0)",
        rows)
    conn.commit()
    conn.close()
    return str(db_path)


def test_script_imports_nothing_from_spotisub_package():
    """Static guard: this script must never import the spotisub package (or
    config.py, which itself imports apscheduler and is a step closer to the
    same class of risk if config.py ever grows a side effect). Parses the
    actual AST rather than substring-matching the source, since the script's
    own module docstring mentions `from spotisub import database` by name as
    a cautionary example -- a raw substring check would false-positive on
    that prose.
    """
    source = open(SCRIPT_PATH, encoding="utf-8").read()
    tree = ast.parse(source, filename=SCRIPT_PATH)

    imported_roots = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported_roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    assert "spotisub" not in imported_roots
    assert "config" not in imported_roots


def test_dry_run_reports_without_deleting(tmp_path):
    db_path = _make_db(tmp_path, [
        ("u1", "real-song", "artist1", "song-A", "pl1"),
        ("u2", None, None, "song-A", "pl1"),
        ("u3", "other", "artist2", "song-B", "pl1"),
    ])
    result = subprocess.run(
        [sys.executable, SCRIPT_PATH, "--db-path", db_path],
        capture_output=True, text=True, check=True)
    assert "Would delete 1 duplicate row(s) across 1 pair(s)." in result.stdout
    assert "keeping row u2" in result.stdout

    conn = sqlite3.connect(db_path)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM subsonic_spotify_relation").fetchone()[0]
    conn.close()
    assert remaining == 3, "dry run must not delete anything"


def test_apply_collapses_duplicates_to_most_recent_rowid(tmp_path):
    db_path = _make_db(tmp_path, [
        ("u1", "real-song", "artist1", "song-A", "pl1"),
        ("u2", None, None, "song-A", "pl1"),
        ("u3", "other", "artist2", "song-B", "pl1"),
    ])
    result = subprocess.run(
        [sys.executable, SCRIPT_PATH, "--apply", "--db-path", db_path],
        capture_output=True, text=True, check=True)
    assert "Deleted 1 duplicate row(s) across 1 pair(s)." in result.stdout

    conn = sqlite3.connect(db_path)
    remaining = conn.execute(
        "SELECT uuid, spotify_song_uuid FROM subsonic_spotify_relation "
        "ORDER BY uuid").fetchall()
    conn.close()
    # u1 (lower rowid, for song-A) was deleted; u2 (higher rowid, same pair)
    # and u3 (a different pair entirely) remain.
    assert remaining == [("u2", "song-A"), ("u3", "song-B")]


def test_no_duplicates_reports_nothing_to_do(tmp_path):
    db_path = _make_db(tmp_path, [
        ("u1", "real-song", "artist1", "song-A", "pl1"),
        ("u3", "other", "artist2", "song-B", "pl1"),
    ])
    result = subprocess.run(
        [sys.executable, SCRIPT_PATH, "--db-path", db_path],
        capture_output=True, text=True, check=True)
    assert "No duplicate subsonic_spotify_relation rows found" in result.stdout

    conn = sqlite3.connect(db_path)
    remaining = conn.execute(
        "SELECT COUNT(*) FROM subsonic_spotify_relation").fetchone()[0]
    conn.close()
    assert remaining == 2
