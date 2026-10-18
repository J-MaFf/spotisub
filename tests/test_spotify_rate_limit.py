"""Tests for how Spotisub handles Spotify's dev-mode /v1/tracks quota.

Live symptom (J-MaFf/homelab#823): every playlist track was fetched again
with GET /v1/tracks/{id}, because the playlist_items fields request had no
external_ids, so has_isrc() always failed. That used up the shared app's daily
/v1/tracks quota a minute after it reset. Spotify then answered with a 429
whose Retry-After was up to 24 h. urllib3 slept 6 h of it inside the request
and re-sent it with the old, expired token: a 401 every 6 h.

Covered here:
- the playlist_items fields request asks for external_ids, so a catalog
  track needs no lookup;
- a 429 with a long Retry-After is handed back at once, with its headers,
  while a short one is still waited out and retried;
- a 429 on a lookup skips that kind of lookup until Retry-After has passed.

The HTTP tests run spotipy against a throwaway local server, never Spotify.
"""
import http.server
import json
import threading
import time

import pytest
import spotipy
from expiringdict import ExpiringDict
from spotipy.exceptions import SpotifyException

from spotisub import generator
from spotisub.helpers import spotipy_helper
from spotisub.helpers import subsonic_helper

from tests.helpers import make_spotify_track


# ---------------------------------------------------------------------------
# Playlist tracks carry their ISRC, so they need no /v1/tracks lookup.
# ---------------------------------------------------------------------------

class _FakeSpForPlaylistItems:
    def __init__(self, items):
        self.items = items
        self.fields = None

    def playlist_items(self, playlist_id, offset=0, fields=None, limit=50,
                       additional_types=None):
        self.fields = fields
        return {"items": self.items if offset == 0 else []}


class _SpotifyCallNotExpected:
    def __getattr__(self, name):
        raise AssertionError(f"unexpected Spotify API access: {name}")


def test_playlist_items_fields_request_external_ids(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    fake_sp = _FakeSpForPlaylistItems(items=[])
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    generator.get_playlist_tracks(
        {"id": "playlist-1", "name": "Test Playlist"}, {"tracks": []})

    assert "items.item.external_ids" in fake_sp.fields.split(",")


def test_playlist_track_with_album_and_isrc_makes_no_lookup(monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    track = make_spotify_track(
        "Rumble", "Skrillex", isrc="GBAHT2300123", album_name="Quest for Fire")
    fake_sp = _FakeSpForPlaylistItems(items=[{"item": track}])
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: fake_sp)

    result = generator.get_playlist_tracks(
        {"id": "playlist-1", "name": "Test Playlist"}, {"tracks": []})

    assert result["tracks"] == [track]
    assert subsonic_helper.add_missing_values_to_track(
        _SpotifyCallNotExpected(), dict(track)) == track


# ---------------------------------------------------------------------------
# BoundedRetry: long Retry-After is not slept on; short one still retried.
# ---------------------------------------------------------------------------

class _ScriptedServer:
    """Serves one scripted (status, headers, body) per GET, repeating the
    last, and records the Authorization header of every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.authorizations = []
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                server.authorizations.append(self.headers.get("Authorization"))
                index = min(len(server.authorizations), len(server.responses)) - 1
                status, headers, body = server.responses[index]
                payload = json.dumps(body).encode()
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

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

    def client(self):
        sp = spotipy.Spotify(
            auth="test-token",
            requests_session=spotipy_helper.build_requests_session())
        sp.prefix = f"http://127.0.0.1:{self.httpd.server_port}/v1/"
        return sp


_QUOTA_429 = (429, {"Retry-After": "86334"},
              {"error": {"status": 429, "message": "API rate limit exceeded"}})


def test_long_retry_after_429_is_raised_at_once_with_its_headers():
    with _ScriptedServer([_QUOTA_429]) as server:
        started = time.monotonic()
        with pytest.raises(SpotifyException) as raised:
            server.client().track("spotify:track:3yNVfRQlUPViUh8O2V9SQn")
        elapsed = time.monotonic() - started

    assert raised.value.http_status == 429
    assert raised.value.headers["Retry-After"] == "86334"
    assert len(server.authorizations) == 1, "a long Retry-After was retried"
    assert elapsed < 5


def test_short_retry_after_429_is_waited_out_and_retried():
    ok = (200, {}, {"id": "3yNVfRQlUPViUh8O2V9SQn", "name": "Rumble"})
    with _ScriptedServer([(429, {"Retry-After": "1"}, {}), ok]) as server:
        result = server.client().track("spotify:track:3yNVfRQlUPViUh8O2V9SQn")

    assert result["name"] == "Rumble"
    assert len(server.authorizations) == 2


def test_create_sp_client_uses_bounded_retry():
    sp = spotipy_helper.create_sp_client()
    retry = sp._session.get_adapter("https://api.spotify.com").max_retries

    assert isinstance(retry, spotipy_helper.BoundedRetry)
    assert retry.raise_on_status is False


# ---------------------------------------------------------------------------
# A 429 on a lookup skips that kind of lookup until Retry-After has passed.
# ---------------------------------------------------------------------------

class _RecordingExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append(args[1])


class _FakeSpForLookups:
    def __init__(self, error):
        self.error = error
        self.calls = []

    def _lookup(self, uri):
        self.calls.append(uri)
        if self.error is not None:
            raise self.error
        return {"uri": uri}

    track = album = artist = playlist = _lookup


@pytest.fixture
def lookups(monkeypatch):
    monkeypatch.setattr(subsonic_helper, "spotify_cache",
                        ExpiringDict(max_len=100, max_age_seconds=600))
    monkeypatch.setattr(subsonic_helper, "spotify_lookup_blocked_until", {})
    monkeypatch.setattr(subsonic_helper, "save_cache_object_to_file",
                        lambda obj, filename: None)
    executor = _RecordingExecutor()
    monkeypatch.setattr(subsonic_helper, "cache_executor", executor)
    return executor


def _quota_error(headers):
    return SpotifyException(429, -1, "API rate limit exceeded", headers=headers)


def test_429_blocks_track_lookups_until_retry_after(lookups, monkeypatch):
    sp = _FakeSpForLookups(_quota_error({"Retry-After": "86334"}))
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)

    subsonic_helper.load_spotify_object_to_cache(sp, "spotify:track:a")
    assert sp.calls == ["spotify:track:a"]

    # New lookups aren't queued, and ones queued earlier make no call.
    assert subsonic_helper.get_spotify_object_from_cache(
        sp, "spotify:track:b") is None
    subsonic_helper.load_spotify_object_to_cache(sp, "spotify:track:c")
    assert lookups.submitted == []
    assert sp.calls == ["spotify:track:a"]

    # Other kinds of lookup are unaffected.
    subsonic_helper.get_spotify_object_from_cache(sp, "spotify:album:x")
    assert lookups.submitted == ["spotify:album:x"]

    # Once Retry-After has passed, track lookups resume.
    monkeypatch.setattr(time, "time", lambda: now + 86335)
    subsonic_helper.get_spotify_object_from_cache(sp, "spotify:track:b")
    assert lookups.submitted == ["spotify:album:x", "spotify:track:b"]


def test_429_without_retry_after_blocks_for_the_default(lookups, monkeypatch):
    sp = _FakeSpForLookups(_quota_error(None))
    now = time.time()
    monkeypatch.setattr(time, "time", lambda: now)

    subsonic_helper.load_spotify_object_to_cache(sp, "spotify:track:a")

    assert subsonic_helper.spotify_lookup_blocked_until["track"] == (
        now + subsonic_helper.DEFAULT_LOOKUP_BLOCK_SECONDS)


def test_other_spotify_errors_do_not_block(lookups):
    sp = _FakeSpForLookups(SpotifyException(404, -1, "not found"))

    subsonic_helper.load_spotify_object_to_cache(sp, "spotify:track:a")
    subsonic_helper.get_spotify_object_from_cache(sp, "spotify:track:b")

    assert subsonic_helper.spotify_lookup_blocked_until == {}
    assert lookups.submitted == ["spotify:track:b"]


def test_forced_lookup_while_blocked_returns_none(lookups, monkeypatch):
    monkeypatch.setattr(subsonic_helper, "spotify_lookup_blocked_until",
                        {"track": time.time() + 3600})
    sp = _FakeSpForLookups(None)

    assert subsonic_helper.get_spotify_object_from_cache(
        sp, "spotify:track:a", force=True) is None
    assert sp.calls == []
    # download_song() relies on that None instead of a KeyError.
    monkeypatch.setattr(spotipy_helper, "get_spotipy_client", lambda: sp)
    assert subsonic_helper.download_song(
        spotipy_helper, "spotify:track:a") is False


def test_forced_lookup_while_blocked_still_returns_a_cached_object(lookups,
                                                                   monkeypatch):
    monkeypatch.setattr(subsonic_helper, "spotify_lookup_blocked_until",
                        {"track": time.time() + 3600})
    subsonic_helper.spotify_cache["spotify:track:a"] = {"uri": "spotify:track:a"}

    assert subsonic_helper.get_spotify_object_from_cache(
        _SpotifyCallNotExpected(), "spotify:track:a", force=True) == {
            "uri": "spotify:track:a"}
