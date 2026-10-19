"""Tests for surviving a slow Spotify response (J-MaFf/homelab#852).

Live symptom: on 2026-10-18 at 20:16 CDT both scheduled import jobs ran
together, and each one's thread died on a 5 s read timeout from
api.spotify.com. spotipy's session never retries reads (read=False), and
nothing between spotipy and the job thread caught requests' ReadTimeout.
saved_tracks lost that run. user_playlists died before it picked its next
playlist, so the following run imported the same one again.

Covered here:
- the session retries a read timeout, and the client reads for 10 s;
- a Spotify call that still fails skips the saved-tracks write instead of
  killing the thread;
- a failed playlist listing stops paging but still moves the job on to the
  next playlist.

The HTTP test runs spotipy against a throwaway local server, never Spotify.
"""
import http.server
import json
import threading
import time
import types

import requests
import spotipy

from spotisub import generator
from spotisub import database
from spotisub.helpers import spotipy_helper
from spotisub.helpers import subsonic_helper


# ---------------------------------------------------------------------------
# The session retries a read timeout.
# ---------------------------------------------------------------------------

class _SlowFirstResponseServer:
    """Answers every GET with 200, but stalls the first one for `stall`
    seconds, longer than the client's read timeout."""

    def __init__(self, stall):
        self.requests = 0
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.requests += 1
                if server.requests == 1:
                    time.sleep(stall)
                payload = json.dumps({"items": [], "total": 0}).encode()
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the client gave up on the stalled request

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_read_timeout_is_retried():
    with _SlowFirstResponseServer(stall=2) as server:
        sp = spotipy.Spotify(
            auth="test-token",
            requests_session=spotipy_helper.build_requests_session(),
            requests_timeout=0.5)
        sp.prefix = f"http://127.0.0.1:{server.httpd.server_port}/v1/"

        result = sp.current_user_saved_tracks(limit=50)

    assert result == {"items": [], "total": 0}
    assert server.requests == 2


def test_create_sp_client_reads_for_10_seconds():
    assert spotipy_helper.create_sp_client().requests_timeout == 10


# ---------------------------------------------------------------------------
# The job threads survive a Spotify call that still fails.
# ---------------------------------------------------------------------------

_TIMEOUT = requests.exceptions.ReadTimeout(
    "HTTPSConnectionPool(host='api.spotify.com', port=443): "
    "Read timed out. (read timeout=10)")


class _FakeScheduler:
    def __init__(self):
        self.modified = []

    def get_job(self, *args, **kwargs):
        return object()

    def modify_job(self, *args, **kwargs):
        self.modified.append(kwargs)

    def remove_job(self, *args, **kwargs):
        raise AssertionError("the job should not be removed")


def _playlist_info_row(uuid, name, import_arg=None):
    return types.SimpleNamespace(uuid=uuid, subsonic_playlist_name=name,
                                 import_arg=import_arg)


def test_saved_tracks_timeout_skips_the_write_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(database, "select_playlist_info_by_uuid",
                        lambda uuid: _playlist_info_row(uuid, "Saved Tracks"))
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: object())

    def raise_timeout(result, offset_tracks=0):
        raise _TIMEOUT
    monkeypatch.setattr(generator, "get_user_saved_tracks_playlist",
                        raise_timeout)
    writes = []
    monkeypatch.setattr(subsonic_helper, "write_playlist",
                        lambda sp, info, result: writes.append(info))

    generator.get_user_saved_tracks_run("saved-uuid")

    assert writes == []
    assert "Skipping your saved tracks this run" in caplog.text
    assert "Read timed out" in caplog.text


class _FakeSpForPlaylistListing:
    """One page with one playlist, then a read timeout on the next page."""

    def __init__(self):
        self.offsets = []

    def current_user_playlists(self, limit=50, offset=0):
        self.offsets.append(offset)
        if offset == 0:
            return {"items": [{"name": "Naruto", "uri": "spotify:playlist:n"}]}
        raise _TIMEOUT


def test_playlist_listing_timeout_still_picks_the_next_playlist(monkeypatch,
                                                                caplog):
    fake_sp = _FakeSpForPlaylistListing()
    scheduler = _FakeScheduler()
    monkeypatch.setattr(generator, "scheduler", scheduler)
    monkeypatch.setattr(database, "select_playlist_info_by_uuid",
                        lambda uuid: _playlist_info_row(uuid, "Naruto",
                                                        import_arg="Naruto"))
    monkeypatch.setattr(subsonic_helper, "is_ignored_playlist",
                        lambda playlist_info: False)
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)
    monkeypatch.setattr(generator, "get_playlist_tracks",
                        lambda item, result: result)
    writes = []
    monkeypatch.setattr(subsonic_helper, "write_playlist",
                        lambda sp, info, result: writes.append(info["name"]))
    monkeypatch.setattr(generator, "pick_user_playlist",
                        lambda: types.SimpleNamespace(uuid="next-uuid"))

    generator.get_user_playlists_run("naruto-uuid")

    assert fake_sp.offsets == [0, 50]
    assert writes == ["Naruto"]
    assert {"args": ["next-uuid"], "id": generator.constants.JOB_UP_ID} in (
        scheduler.modified)
    assert "Could not list your playlists (offset 50)" in caplog.text


def test_playlist_import_timeout_skips_that_playlist(monkeypatch, caplog):
    fake_sp = _FakeSpForPlaylistListing()
    monkeypatch.setattr(generator, "scheduler", _FakeScheduler())
    monkeypatch.setattr(database, "select_playlist_info_by_uuid",
                        lambda uuid: _playlist_info_row(uuid, "Naruto",
                                                        import_arg="Naruto"))
    monkeypatch.setattr(subsonic_helper, "is_ignored_playlist",
                        lambda playlist_info: False)
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    def raise_timeout(item, result):
        raise _TIMEOUT
    monkeypatch.setattr(generator, "get_playlist_tracks", raise_timeout)
    writes = []
    monkeypatch.setattr(subsonic_helper, "write_playlist",
                        lambda sp, info, result: writes.append(info["name"]))
    monkeypatch.setattr(generator, "pick_user_playlist",
                        lambda: types.SimpleNamespace(uuid="next-uuid"))

    generator.get_user_playlists_run("naruto-uuid")

    assert writes == []
    assert "Skipping playlist Naruto" in caplog.text
