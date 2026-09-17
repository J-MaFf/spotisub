"""Shared test factories and a fake Subsonic client used across the
reliable-track-matching test suite (see specs/archive/reliable-track-matching.md).

Nothing here talks to a real Subsonic/Spotify/MusicBrainz server -- the fake
client below is an in-memory stand-in for exactly the `libsonic.Connection`
surface spotisub.helpers.subsonic_helper actually calls.
"""
from libsonic.errors import DataNotFoundError

from spotisub import utils
from spotisub.classes import ComparisonHelper, SubsonicCache


def make_spotify_track(name, artist_name, album_name=None, isrc=None,
                       track_id="spotify-track-id", artist_id="spotify-artist-id"):
    """Build a minimal Spotify track dict shaped like what write_playlist()
    passes around (see spotisub.helpers.subsonic_helper.write_playlist)."""
    track = {
        "name": name,
        "id": track_id,
        "uri": f"spotify:track:{track_id}",
        "artists": [{"name": artist_name, "uri": f"spotify:artist:{artist_id}"}],
    }
    if album_name is not None:
        track["album"] = {
            "name": album_name,
            "uri": f"spotify:album:{album_name.lower().replace(' ', '-')}"}
    if isrc is not None:
        track["external_ids"] = {"isrc": isrc}
    return track


def make_subsonic_song(song_id, title, artist, album, artist_id=None,
                       music_brainz_id=None):
    """Build a minimal Subsonic search2 song dict."""
    song = {
        "id": song_id,
        "title": title,
        "artist": artist,
        "album": album,
    }
    if artist_id is not None:
        song["artistId"] = artist_id
    if music_brainz_id is not None:
        song["musicBrainzId"] = music_brainz_id
    return song


def build_cache_for_songs(songs):
    """Build a SubsonicCache exactly the way build_subsonic_cache() would for
    a fixed, known list of Subsonic songs -- without any network call."""
    song_mbid_dict = {}
    song_compare_dict = {}
    for song in songs:
        if "musicBrainzId" in song:
            song_mbid_dict[song["musicBrainzId"]] = song
        title = song.get("title")
        if title:
            for variant in utils.generate_compare_array(title):
                song_compare_dict.setdefault(variant, []).append(song)
    return SubsonicCache(len(songs), song_mbid_dict, song_compare_dict)


def comparison_helper_for(track):
    return ComparisonHelper(
        track=track,
        artist_spotify=track["artists"][0],
        found=False,
        excluded=False,
        song_ids=[],
        track_helper=[])


def sample_playlist_info(name="Test Playlist", uuid="playlist-uuid-1",
                         spotify_uri="spotify:playlist:test",
                         import_arg="spotify:playlist:test",
                         playlist_type="user_playlists", prefix="Spotisub - "):
    return {
        "uuid": uuid,
        "name": name,
        "spotify_uri": spotify_uri,
        "type": playlist_type,
        "import_arg": import_arg,
        "prefix": prefix,
    }


class NoLinearScanDict(dict):
    """A dict that fails the test if anything calls .values() on it.

    Used to prove the string-compare fallback matcher looks candidates up by
    key (via generate_compare_array()) instead of scanning the whole
    Subsonic library per track (specs/archive/reliable-track-matching.md R3).
    """

    def values(self):  # pragma: no cover - only hit on regression
        raise AssertionError(
            "subsonic_compare_dict.values() was called: expected an "
            "indexed lookup via .get(variant), not a linear scan of the "
            "whole cache.")


class FakePysonicClient:
    """In-memory stand-in for libsonic.Connection, covering exactly the
    surface spotisub.helpers.subsonic_helper calls: ping, search2 (paged),
    getPlaylists, createPlaylist (both "new, empty" and "replace contents"
    forms), getPlaylist, deletePlaylist.
    """

    def __init__(self, songs=None):
        self.songs = list(songs or [])
        self.playlists = {}  # id -> {"name": str, "entry": [{"id": ...}, ...]}
        self._next_playlist_id = 1

    def ping(self):
        return True

    def search2(self, query, songCount=500, songOffset=0, artistCount=0, albumCount=0):
        if songOffset < 0 or songOffset >= len(self.songs):
            return {"searchResult2": {}}
        page = self.songs[songOffset:songOffset + songCount]
        if not page:
            return {"searchResult2": {}}
        return {"searchResult2": {"song": page}}

    def getPlaylists(self):
        if not self.playlists:
            return {"playlists": {}}
        playlist_list = [
            {"id": pid, "name": data["name"]}
            for pid, data in self.playlists.items()]
        return {"playlists": {"playlist": playlist_list}}

    def createPlaylist(self, name=None, playlistId=None, songIds=None):
        song_ids = songIds or []
        if playlistId is None:
            new_id = str(self._next_playlist_id)
            self._next_playlist_id += 1
            self.playlists[new_id] = {
                "name": name,
                "entry": [{"id": sid} for sid in song_ids]}
            return {}
        if playlistId not in self.playlists:
            self.playlists[playlistId] = {"name": name or "", "entry": []}
        self.playlists[playlistId]["entry"] = [
            {"id": sid} for sid in song_ids]
        return {}

    def getPlaylist(self, key):
        if key not in self.playlists:
            raise DataNotFoundError("no such playlist: " + str(key))
        data = self.playlists[key]
        return {"playlist": {"name": data["name"], "entry": list(data["entry"])}}

    def deletePlaylist(self, key):
        if key not in self.playlists:
            raise DataNotFoundError("no such playlist: " + str(key))
        del self.playlists[key]


class FakeSpotipyClient:
    """In-memory stand-in for a spotipy.Spotify client, covering exactly the
    surface generator.scan_user_playlists() calls: paginated
    current_user_playlists(limit=, offset=) -> {'items': [...]}.

    `pages` is a list of pages, each page a list of item dicts (each with at
    least a "name" and "uri" key) or None entries (mirroring a null item
    Spotify can return). `limit` must match the REQUEST_LIMIT
    scan_user_playlists() calls with (50) -- pages are consumed one per call
    regardless of the requested limit/offset, purely by call order, which is
    enough to simulate a multi-page fetch without reimplementing real
    pagination math.

    If `fail_on_page` is set (1-indexed), the call that would fetch that
    page raises `raise_with` instead (default a generic Exception), to
    simulate a mid-fetch failure (spotipy.SpotifyException in production).
    """

    def __init__(self, pages, fail_on_page=None, raise_with=None):
        self.pages = list(pages)
        self.fail_on_page = fail_on_page
        self.raise_with = raise_with or Exception("simulated Spotify API failure")
        self._call_count = 0

    def current_user_playlists(self, limit=50, offset=0):
        self._call_count += 1
        if self.fail_on_page is not None and self._call_count == self.fail_on_page:
            raise self.raise_with
        page_index = self._call_count - 1
        if page_index >= len(self.pages):
            return {"items": []}
        return {"items": list(self.pages[page_index])}
