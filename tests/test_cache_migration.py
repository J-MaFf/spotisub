"""Tests for loading a Subsonic cache pickle written by a pre-fix version of
this app (specs/archive/reliable-track-matching.md C-final).

spotisub.classes.SubsonicCache was extended from 2 required fields
(total_song_count, song_mbid_dict) to 3 (adding song_compare_dict). The
consuming deployment (see this repo's docker-compose.yml / the homelab
repo's) bind-mounts ./cache as a persistent volume, so an already-built,
2-field pickle can legitimately be sitting at cache/subsonic_cache.pkl the
moment this version of the app starts up. Unpickling that file under the
current 3-field NamedTuple definition raises
`TypeError: SubsonicCache.__new__() missing 1 required positional argument:
'song_compare_dict'` -- load_subsonic_cache_from_file() must not let that (or
any other stale/corrupt-cache load failure) propagate and crash app startup;
it should log the situation and fall through to a fresh build.
"""
import logging
import os
import pickle
from typing import NamedTuple

from spotisub import classes, constants
from spotisub.classes import SubsonicCache
from spotisub.helpers import subsonic_helper

from tests.helpers import FakePysonicClient, make_subsonic_song


def _write_old_format_cache_pickle(path):
    """Write a pickle to `path` that is byte-for-byte what a pre-fix
    deployment would have produced: an instance of a 2-field
    `spotisub.classes.SubsonicCache`.

    The pickler verifies (at dump time, in modern CPython) that
    `getattr(sys.modules[obj.__class__.__module__], obj.__class__.__qualname__)`
    really is `obj.__class__` -- so `spotisub.classes.SubsonicCache` is
    temporarily swapped for the old 2-field shape for just the dump call,
    then restored. `spotisub.helpers.subsonic_helper` already holds its own
    `SubsonicCache` name bound to the real (current) class from its
    `from spotisub.classes import ... SubsonicCache` at import time, so this
    swap does not affect the loader under test -- only what gets embedded in
    the pickle file, exactly mirroring what a real stale file on disk
    contains.
    """
    class OldSubsonicCache(NamedTuple):
        total_song_count: int
        song_mbid_dict: dict

    OldSubsonicCache.__module__ = "spotisub.classes"
    OldSubsonicCache.__qualname__ = "SubsonicCache"

    old_cache = OldSubsonicCache(5, {"some-mbid": {"id": "old-song-1"}})
    original_cls = classes.SubsonicCache
    classes.SubsonicCache = OldSubsonicCache
    try:
        with open(path, "wb") as f:
            pickle.dump(old_cache, f)
    finally:
        classes.SubsonicCache = original_cls


def test_old_format_pickle_actually_raises_typeerror_on_direct_load(tmp_path):
    """Sanity check that this test fixture really reproduces the reported
    failure mode (pickle.load() alone, with no protective handling)."""
    path = tmp_path / "old_cache.pkl"
    _write_old_format_cache_pickle(str(path))

    with open(path, "rb") as f:
        try:
            pickle.load(f)
        except TypeError as exc:
            assert "song_compare_dict" in str(exc)
        else:
            raise AssertionError(
                "expected unpickling a 2-field SubsonicCache under the "
                "current 3-field definition to raise TypeError")


def test_loader_falls_back_to_rebuild_on_stale_cache_file(monkeypatch, caplog):
    """The actual fix: load_subsonic_cache_from_file() must not raise for
    this scenario, and must return a valid, fully-populated SubsonicCache
    obtained by rebuilding from the (fake) Subsonic server, rather than
    crashing app startup."""
    path = os.path.join(constants.CACHE_DIR, constants.SUBSONIC_CACHE_FILENAME)
    _write_old_format_cache_pickle(path)

    fake_client = FakePysonicClient(songs=[
        make_subsonic_song("s1", "Sultans of Swing", "Dire Straits",
                            "Dire Straits", music_brainz_id="mbid-1"),
        make_subsonic_song("s2", "Seven Nation Army", "The White Stripes",
                            "Elephant", music_brainz_id="mbid-2"),
    ])
    monkeypatch.setattr(subsonic_helper, "check_pysonic_connection",
                         lambda: fake_client)

    with caplog.at_level(logging.WARNING):
        cache = subsonic_helper.load_subsonic_cache_from_file()

    # Must not have raised, and must be a real, fully-populated SubsonicCache
    # (i.e. it fell through to build_subsonic_cache() rather than returning
    # a half-migrated/garbage object).
    assert isinstance(cache, SubsonicCache)
    assert cache.total_song_count == 2
    assert cache.song_mbid_dict["mbid-1"]["id"] == "s1"
    assert cache.song_mbid_dict["mbid-2"]["id"] == "s2"
    assert "Sultans of Swing" in cache.song_compare_dict or any(
        "sultans" in variant.lower() for variant in cache.song_compare_dict)

    # Diagnosable in production logs: something clearly says a stale/
    # incompatible cache was found and rebuilt.
    assert any(
        "stale" in record.message.lower()
        or "incompatible" in record.message.lower()
        for record in caplog.records)

    # The stale pickle on disk must have been overwritten by the rebuild
    # (build_subsonic_cache() calls save_cache_object_to_file()), so a
    # second load doesn't need to migrate anything again.
    with open(path, "rb") as f:
        reloaded = pickle.load(f)
    assert isinstance(reloaded, SubsonicCache)
    assert reloaded.total_song_count == 2


def test_missing_cache_file_still_returns_empty_cache_without_rebuilding(monkeypatch):
    """No behavior change for the ordinary 'no cache file yet' case: it
    should still return the cheap empty default rather than triggering a
    network-hitting rebuild at every cold start."""
    path = os.path.join(constants.CACHE_DIR, constants.SUBSONIC_CACHE_FILENAME)
    assert not os.path.exists(path)

    def _fail_if_called():
        raise AssertionError(
            "build_subsonic_cache() should not run for a simply-missing "
            "cache file -- only for a present-but-stale/corrupt one")

    monkeypatch.setattr(subsonic_helper, "build_subsonic_cache",
                         lambda: _fail_if_called())

    cache = subsonic_helper.load_subsonic_cache_from_file()
    assert cache == SubsonicCache(0, {}, {})
