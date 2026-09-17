# Spec: Match Spotify "local file" playlist tracks against the local library

## Goal
Let Spotisub attempt to match a Spotify "local file" track (one the user manually imported
into their own Spotify library rather than something from Spotify's catalog) against the
Subsonic library by title/artist, instead of unconditionally skipping it with zero attempt —
so a playlist built from local files can still sync whatever of its tracks the user actually
owns.

## Context
Spotisub (this repo, `J-MaFf/spotisub`, fork of `blastbeng/spotisub`, branch `dev`) mirrors
Spotify playlists into Navidrome. Confirmed live on the production deployment: a playlist of
19 Spotify "local file" tracks (Pokémon battle-theme music the user had imported as local MP3s)
produced 19 `"track was set to None when adding missing values, skipping."` log lines and zero
attempted matches — even though several of those tracks (e.g. "Junichi Masuda - Battle! (Gym
Leader)") plausibly exist in the local Subsonic library too.

**Verified root cause chain** (all four steps below are load-bearing; fixing only the first is
not sufficient — confirmed by tracing exactly what each downstream function does with an
unmodified local-file track dict):

1. **`add_missing_values_to_track()`** (`spotisub/helpers/subsonic_helper.py:383-395`):
   ```python
   def add_missing_values_to_track(sp, track):
       """calls spotify if tracks has missing album or isrc or uri"""
       if "id" in track and track["id"] is not None:
           uri = 'spotify:track:' + track['id']
           if "album" not in track or not has_isrc(track):
               spotify_track = get_spotify_object_from_cache(sp, uri)
               if spotify_track is not None:
                   track = spotify_track
               time.sleep(0.25)
           if "uri" not in track:
               track["uri"] = uri
           return track
       return None
   ```
   Spotify returns `id: null` and `is_local: true` for a local-file playlist item — this
   function's guard is `False` for such a track, so it returns `None` unconditionally. The
   caller, `write_playlist()` (`spotisub/helpers/subsonic_helper.py:449-455`), treats `None` as
   "skip this track, don't even try to match it":
   ```python
   for track in results['tracks']:
       track = add_missing_values_to_track(sp, track)
       if track is None:
           logging.error(f'... track was set to None when adding missing values, skipping.')
           continue
   ```

2. **The `fields` restriction on the Spotify API call that produces these track dicts in the
   first place** makes this worse than just the `id` guard. `get_playlist_tracks()`
   (`spotisub/generator.py:567-572`) calls:
   ```python
   response_tracks = sp.playlist_items(
       item['id'],
       offset=offset_tracks,
       fields='items.item.id,items.item.name,items.item.artists,items.item.type,total',
       limit=50,
       additional_types=['track'])
   ```
   This means **every** track object passed into `write_playlist()` — local file or catalog
   track alike — only ever carries `id`, `name`, `artists`, `type` off this call. For a
   catalog track this is invisible because `add_missing_values_to_track()` immediately
   replaces the whole dict with a full `sp.track(uri)` object (which has everything). A local
   file has no catalog id to fetch a replacement from, so if step 1's guard is simply removed,
   the local-file track dict handed onward is still missing `uri`, `album`, and `is_local`
   entirely — none of which were ever requested. **This `fields` string must be widened** (add
   `items.item.uri`, `items.item.album`, `items.item.is_local`) for the rest of the chain to
   have anything to work with. (`get_user_saved_tracks_playlist()`,
   `spotisub/generator.py:529-552`, calls `sp.current_user_saved_tracks()` with no `fields`
   restriction at all, so a local file appearing in Liked Songs already carries the full object
   — that ingestion path is unaffected by this particular fix.)

3. **`has_isrc(track)`** (`spotisub/helpers/subsonic_helper.py:372-380`) already degrades
   cleanly to `False` for a track with no `external_ids` key (confirmed: `"external_ids" not in
   track` short-circuits the `or` chain before any further access) — a local file will never
   have `external_ids`, and this is not a landmine. **No change needed here.**

4. **`match_with_subsonic_track()`** (`spotisub/helpers/subsonic_helper.py:651-676`) already
   falls through cleanly to the string-compare fallback (added by
   `specs/archive/reliable-track-matching.md`) whenever `has_isrc()` is `False`:
   ```python
   matched_track = None
   if has_isrc(comparison_helper.track):
       matched_track = get_subsonic_track_via_mbid(...)
   if matched_track is None:
       track_name = (comparison_helper.track.get("name") or "").strip()
       artist_name = (comparison_helper.artist_spotify.get("name") or "").strip()
       if track_name != "" and artist_name != "":
           matched_track = get_subsonic_track_via_string_compare(...)
   ```
   This only needs `track.get("name")` and `artist_spotify.get("name")` (i.e.
   `track["artists"][0]["name"]`), both accessed via `.get()`/already-guarded, and both fields
   a local file reliably carries when it had any tags at all. **No change needed here either
   — the matching mechanism this spec depends on already exists and already works.** The whole
   fix is about getting a local-file track dict safely as far as this function, not changing
   what happens once it arrives.

5. **`insert_spotify_song()`** (`spotisub/database.py:1081-1118`) is where a naive "just delete
   the `return None` guard" fix breaks, confirmed by reading it fully:
   ```python
   def insert_spotify_song(conn, artist_spotify, track_spotify):
       ...
       song_db = select_spotify_song_by_uri(conn, track_spotify["uri"])
       ...
       if song_db is None:
           album = None
           if "album" in track_spotify:
               album = insert_spotify_album(conn, track_spotify["album"])
           if album is not None:
               ...
               stmt = insert(dbms.spotify_song).values(
                   uuid=str(uuid.uuid4().hex), album_uuid=album.uuid,
                   title=track_spotify["name"], spotify_uri=track_spotify["uri"])
   ```
   Line `track_spotify["uri"]` is a **bare, unguarded bracket access** — if a local track ever
   reaches this function without a `uri` key set, it raises `KeyError: 'uri'`, not a graceful
   skip. And the `if "album" in track_spotify:` gate means an album-less track silently never
   gets persisted (`album` stays `None`, the `insert` never runs, `insert_song()`
   (`spotisub/database.py:290-321`) rolls back and returns `None`) — no crash, but also no
   diagnostic trace of why a track that appeared to match still isn't in the database.
   `spotify_song`/`spotify_artist`/`spotify_album` are all keyed by `spotify_uri`
   (`String(500), unique=True`, `spotisub/database.py:88-184`) with no code anywhere assuming
   that string starts with `spotify:track:` — a `spotify:local:{artist}:{album}:{title}:
   {duration}` string works mechanically fine as this key, **provided it's actually present**.

6. **`track["artists"][0]`** is read in four places (`subsonic_helper.py:464`,
   `generator.py:543`, `generator.py:587`) with no guard against an empty `artists` list.
   Spotify does not formally guarantee a non-empty `artists` array for a completely untagged
   local file. This is a narrow, pre-existing risk (not local-file-specific in principle, but
   local files are the realistic way to hit it) — an `IndexError` here would currently crash
   the whole reimport thread for the playlist, not just skip one track.

**Explicitly out of scope / accepted limitations** (per Spotify's own local-file behavior,
confirmed via the Web API reference and corroborating reports, not exhaustively spec'd by
Spotify itself): if a local file was imported into Spotify with **no tags at all**, its
`name`/`album`/`artist` fields may come back as empty strings rather than being absent. This
spec requires such a track to be skipped cleanly with a clear log (not crash, not silently
vanish) — it does **not** require inventing synthetic non-empty metadata to force a match, and
does not need to solve a theoretical `spotify_album`/`spotify_artist` unique-constraint
collision between two different untagged local files sharing an empty-string URI component,
since the real Spotify-generated `uri` for a local file already includes a duration component
that Spotify computes, not something this spec constructs — if two such tracks did somehow
collide, that's outside this spec's scope to engineer around.

## Deliverable
Code changes in `spotisub/generator.py`, `spotisub/helpers/subsonic_helper.py`, and
`spotisub/database.py`, plus new/extended tests in `tests/test_matching.py` (or a new
`tests/test_local_file_tracks.py`) and a new local-file track factory in `tests/helpers.py`
(the existing `make_spotify_track()`, `tests/helpers.py:14-30`, always sets `id`/`uri` and has
no local-file variant), on a branch off `dev`.

## Requirements

- R1. `get_playlist_tracks()`'s Spotify API `fields` request
  (`spotisub/generator.py:570`) is widened to also include `items.item.uri`,
  `items.item.album`, and `items.item.is_local`, alongside the existing `id`, `name`,
  `artists`, `type`.
  [verify: a unit test mocking `sp.playlist_items()` asserts the `fields` argument passed to
  it contains all seven field names; a code review confirms the widened string doesn't drop
  any of the four fields already relied upon elsewhere.]

- R2. `add_missing_values_to_track()` treats a track as a local file when `track.get("id") is
  None` (matching the existing guard's condition, so no new detection logic needed beyond what
  already exists) — for such a track, it does not attempt the `get_spotify_object_from_cache`
  catalog fetch (there is no catalog id to fetch with), and instead returns the track
  essentially as given, provided it has a non-empty `uri`. If, even after R1, the track's `uri`
  is missing or empty, log a warning naming the track and return `None` (graceful skip,
  preserving today's "None means skip" contract for the caller) rather than raising.
  [verify: a unit test with a local-file track dict carrying a real `spotify:local:...` uri
  asserts it passes through with its fields intact and no Spotify API call is made; a second
  test with a local-file track missing `uri` asserts a warning is logged and `None` is
  returned, not an exception.]

- R3. Any track (local-file or catalog) with an empty `artists` list is skipped with a warning
  log naming the track, not an `IndexError` that crashes the reimport thread. Apply this guard
  at the point(s) that index `track["artists"][0]` without existing protection
  (`spotisub/helpers/subsonic_helper.py:464` at minimum; the two `generator.py` call sites are
  in scope only if they're reachable for playlist/saved-tracks ingestion of local files — verify
  which are and are not before touching each).
  [verify: a unit test with `track["artists"] = []` asserts the track is skipped with a logged
  warning and the surrounding loop (processing other tracks in the same playlist) continues
  normally, proving one bad track doesn't abort the whole run.]

- R4. A local-file track that clears R2/R3 flows into `match_with_subsonic_track()` and
  `get_subsonic_track_via_string_compare()` completely unmodified — this requirement exists to
  make explicit that no changes to either function are needed or wanted; they already fall
  through to the string-compare fallback correctly for any track with no ISRC.
  [verify: `git diff` shows no changes to `match_with_subsonic_track()` or
  `get_subsonic_track_via_string_compare()`; an integration test feeds a local-file track
  through the full `write_playlist()` path against a fake Subsonic library containing a
  title/artist match and asserts it's found and added to the playlist's `song_ids`.]

- R5. `insert_spotify_song()`'s `track_spotify["uri"]` access
  (`spotisub/database.py:1089`) is guarded (e.g. `.get("uri")`) so a track dict that somehow
  reaches this function without a `uri` key logs a clear warning and returns `None` (matching
  the existing "no album" case's silent-rollback *behavior*, but no longer *silent* — log
  before rolling back) instead of raising `KeyError`. The existing "no album" gate's *outcome*
  (track not persisted) is unchanged; only its diagnosability changes (add a warning log naming
  the track when the gate is hit).
  [verify: a unit test calls `insert_spotify_song()` with a track dict missing `uri` and
  asserts it returns `None` with a warning logged, not an exception; a second test with a track
  missing `album` asserts the existing rollback-and-return-`None` behavior still happens, now
  with a warning logged naming the track.]

- R6. A local-file track that matches and persists correctly is functionally indistinguishable
  from a catalog track for playlist-membership purposes: it appears in the real Subsonic
  playlist's pushed `song_ids`, and re-running the reimport is idempotent for it (no duplicate
  `subsonic_spotify_relation` row — reusing the guarantee `specs/archive/reliable-track-matching.md`
  R4/R5 already established for `insert_playlist_relation()`, which is keyed by
  `(spotify_song_uuid, playlist_info_uuid)` and doesn't care whether `spotify_song_uuid`'s
  underlying `spotify_uri` was a `spotify:track:...` or `spotify:local:...` string).
  [verify: an integration test runs `write_playlist()` twice in a row with the same local-file
  track input and asserts the second run produces identical `song_ids` output and no duplicate
  relation row, mirroring the existing
  `tests/test_write_playlist_integration.py::test_reimporting_unchanged_playlist_twice_is_idempotent`
  pattern for a local-file track specifically.]

- R7. Reproduce the exact live scenario as a test: a playlist of local-file tracks where some
  titles/artists match entries in a fake Subsonic library and others genuinely don't, asserting
  the matching ones are found and added while the non-matching ones are correctly reported
  "not found" (not silently dropped, not falsely matched).
  [verify: a test seeds a handful of local-file tracks resembling the live "Battle heaters from
  Gen 3 & 4" case (e.g. "Junichi Masuda - Battle! (Gym Leader)"), a fake Subsonic library
  containing some of those exact title/artist pairs and not others, runs the full
  `write_playlist()` path, and asserts the found/not-found split matches expectations exactly.]

## Out of scope
- Inventing synthetic metadata (placeholder album/artist names, synthesized URIs) for a local
  file that Spotify itself reports with empty-string tags — such a track is skipped cleanly,
  not force-matched.
- Any change to MBID-based matching, `get_subsonic_track_via_mbid()`, or the
  `TEXT_COMAPRE_MATCHING_ENABLED`-adjacent code already removed by
  `specs/archive/reliable-track-matching.md` — local files never have ISRCs, so that matching
  tier is never relevant to them; nothing there needs touching.
- Widening the `fields` string on any Spotify API call other than `get_playlist_tracks()`'s
  `sp.playlist_items()` (R1) — `get_user_saved_tracks_playlist()` already requests everything
  and needs no change (see Context).
- Handling a genuinely malformed Spotify API response (e.g. `is_local: true` but `id` is
  somehow *not* null) — the existing `id is None` signal is the one this spec relies on and
  documents; a belt-and-suspenders `is_local` check is not required if `id is None` alone is
  sufficient for correctness (R1 requests `is_local` in `fields` for future diagnosability, but
  R2's actual detection logic does not need to branch on it).

## Constraints
- Python, matching this repo's existing style and the conventions established across
  `specs/archive/reliable-track-matching.md` and `specs/prune-stale-user-playlists.md`'s PRs
  (module-level functions, `logging` for diagnostics, SQLAlchemy `core` for `database.py`
  queries, no new runtime dependencies).
- Must not require a live Spotify/Subsonic server to run the test suite — mock both; add a
  local-file track factory to `tests/helpers.py` following the existing `make_spotify_track()`
  convention (`tests/helpers.py:14-30`) rather than duplicating fixture logic per test file.
- Backwards compatible with SQLite (the only DB engine this repo ships against).
- This is the same live production deployment prior specs were verified against (CT 207,
  `spotisub-fork:playlist-items-fix`, built from this repo's `dev` branch) — after merge, the
  same rebuild-and-verify process applies: rebuild the image from `dev`, redeploy, and confirm
  live that reimporting a known local-file playlist (e.g. the live "Battle heaters from Gen 3 &
  4" playlist) now finds and syncs whatever of its tracks are actually in the library, instead
  of reporting zero matches unconditionally.

## Acceptance rubric
- C1 (from R1): PASS iff the `fields`-argument test passes and code review confirms no existing
  field name was dropped.
- C2 (from R2): PASS iff both the pass-through and missing-uri unit tests pass.
- C3 (from R3): PASS iff the empty-`artists` unit test passes, confirming a warning log and
  continued processing of subsequent tracks.
- C4 (from R4): PASS iff `git diff` shows `match_with_subsonic_track()` and
  `get_subsonic_track_via_string_compare()` unchanged, and the found-via-fallback integration
  test passes.
- C5 (from R5): PASS iff both `insert_spotify_song()` guard tests (missing `uri`, missing
  `album`) pass.
- C6 (from R6): PASS iff the double-run idempotency test for a local-file track passes.
- C7 (from R7): PASS iff the reproduced-live-scenario test passes with the exact expected
  found/not-found split.
- C-final (production, verified by the orchestrator post-merge, not by the generator/evaluator
  loop — same live-deployment-gate pattern as the two prior specs): PASS iff, after rebuilding
  and redeploying the live image and re-triggering a reimport of the live "Battle heaters from
  Gen 3 & 4" playlist (or another local-file-only playlist still tracked at that time), at least
  one track that is confirmed present in the local library is found and added to the actual
  Navidrome playlist, where previously all such tracks were unconditionally skipped.

## Open questions
(none — all resolved above)
