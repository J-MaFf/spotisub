# Spec: Prune playlist_info rows for playlists no longer in the user's Spotify account

## Goal
When a Spotify playlist a user previously followed/owned is unfollowed or deleted, Spotisub's
daily discovery scan should remove its corresponding `playlist_info` row (and associated
tracking data and Navidrome playlist) instead of leaving it as permanent dead weight that the
random per-cycle reimport draw keeps wasting picks on forever.

## Context
Spotisub (this repo, `J-MaFf/spotisub`, fork of `blastbeng/spotisub`, branch `dev`) discovers
the user's Spotify playlists via `scan_user_playlists()` in `spotisub/generator.py`, called
once daily by the `scan_library` job (`spotisub/generator.py:800-818`). This discovery only
ever *adds* rows to the `playlist_info` table (type `"user_playlists"`, `constants.JOB_UP_ID`)
— there is no existing code path that removes a row when its Spotify-side playlist disappears
(the user unfollows or deletes it). The only existing cleanup job,
`remove_subsonic_deleted_playlist()` (`spotisub/helpers/subsonic_helper.py:948-984`), checks
the *opposite* direction (a playlist deleted from Navidrome/Subsonic) and is unrelated.

Live impact this is fixing: the recurring `user_playlists` reimport job
(`init_user_playlists()`/`get_user_playlists_run()`, `spotisub/generator.py`) picks one random
row from *every* `playlist_info` row of type `"user_playlists"` every `PLAYLIST_GEN_SCHED`
hours (default 3), regardless of `ignored` status (`select_playlist_info_by_type()`,
`spotisub/database.py:523-541`, has no `WHERE ignored = 0` filter). On the live deployment this
pool had grown to 45 playlists — including several the user never actually curates (Spotify
auto-created placeholders, playlists merely followed, one literally named "uTorrent") — so the
playlists the user actually cares about were getting picked far less often than a 15-playlist
pool would allow. The user has now cleaned up their Spotify account (unfollowed the playlists
they don't want tracked), but confirmed live that `playlist_info` still has all 45 rows
unchanged — because, as this spec establishes, nothing currently prunes them.

**Verified: matching key convention.** `insert_playlist_type()`
(`spotisub/database.py:343-387`), the function `scan_user_playlists()` uses via
`subsonic_helper.generate_playlist()` -> `database.create_playlist()` to resolve "does this
playlist already exist," looks up an existing row for a `type == "user_playlists"` insert by
**exact match on `subsonic_playlist_name`** (`select_playlist_info_by_name_with_conn()`,
`spotisub/database.py:452-472`, `WHERE subsonic_playlist_name == name`) — not by
`spotify_playlist_uri` (that column is stored but never used as a lookup key anywhere in
`database.py`; no `select_playlist_info_by_spotify_uri`-style function exists). Pruning must
use the same key (`subsonic_playlist_name` exact match against the live Spotify playlist's
`item['name'].strip()`) to stay consistent — a Spotify-side rename is a known, pre-existing,
accepted quirk under this convention (the app already doesn't handle renames cleanly anywhere)
and fixing that is explicitly out of scope here.

**Verified: `scan_user_playlists()` has no accumulator.** It is a bare self-recursing function
(`spotisub/generator.py:86-105`) with no return value — each recursive call only sees its own
page of `sp.current_user_playlists(limit=50, offset=...)` results, calls
`subsonic_helper.generate_playlist()` per item, and recurses if the page had any items, stopping
only when a page returns zero items. There is no point in the existing call graph where the
full accumulated list of current Spotify playlist names exists in one place. This must change
(see R1) — pruning needs the complete list, and needs to know the fetch fully succeeded before
acting on it.

**Verified: failure containment falls out "for free" from placement.** `scan_library()`
(`spotisub/generator.py:800-818`) calls `scan_user_playlists()` inside a `try` that logs and
swallows `EOFError` (non-interactive OAuth failure) but re-raises everything else after
logging. `scan_user_playlists()` itself has no internal try/except, so a `spotipy.SpotifyException`
raised partway through fetching page N propagates straight out. As long as the pruning step is
sequenced *after* the full, successful completion of the accumulating fetch — still inside
`scan_library()`'s existing `try` block — a mid-fetch failure naturally raises before pruning
ever runs, with zero new error-handling code needed for that guarantee. (If R1's accumulator
is implemented as a second, independent pass rather than reusing the existing recursion, that
second pass needs this same "only prune after a fully successful complete fetch" discipline
applied locally to itself.)

**Verified: no existing primitive deletes a `playlist_info` row with a NULL `subsonic_playlist_id`.**
`delete_playlist_relation_by_id(playlist_id)` (`spotisub/database.py:592-607`) is keyed by
*Subsonic* playlist id, not `playlist_info.uuid` — it looks the row up via
`select_playlist_info_by_subsonic_id_with_conn()` (`WHERE subsonic_playlist_id == playlist_id`)
and is a silent no-op if nothing matches. A `playlist_info` row that was discovered but never
successfully reimported yet (or whose reimport never got far enough to create a Subsonic
playlist) has `subsonic_playlist_id IS NULL`, and SQL `NULL == NULL` never matches, so this
function cannot delete such a row today. A new deletion path keyed by `playlist_info.uuid` is
required (R3).

**Verified: exact Subsonic-delete error-handling pattern to mirror**, from `write_playlist()`'s
own "zero songs matched" cleanup (`spotisub/helpers/subsonic_helper.py:531-546`):
```python
elif len(song_ids) == 0:
    try:
        try:
            check_pysonic_connection().deletePlaylist(
                playlist_info["subsonic_playlist_id"])
            logging.info('(%s) Fail! No songs found for playlist %s', ...)
        except DataNotFoundError:
            raise  # Don't retry on DataNotFoundError
        except Exception as e:
            logging.debug('(%s) Deleting playlist failed', ..., str(e))
            time.sleep(1)
    except DataNotFoundError:
        pass
```
One `deletePlaylist()` attempt; `DataNotFoundError` (already gone from Subsonic) is swallowed
with no log; any other exception is logged at `debug` and followed by `time.sleep(1)` with no
actual retry despite the comment; the whole block never raises out to its caller. The new
pruning deletion must follow this same shape when it has a non-null `subsonic_playlist_id` to
delete, so behavior stays consistent with the rest of the app (unlike that existing code, the
new pruning path should still log at `INFO` *before* attempting deletion, naming which playlist
is being pruned and why — see R4 — since silently vanishing playlists with zero trace would be
a worse debugging experience than this spec is trying to fix).

**Verified: no coupling with `ignored`.** `ignored` is set only via the `/ignore/...` dashboard
route (`spotisub/routes.py:548-562`) and read only by `write_playlist()` to skip *writing
content* to an ignored playlist (`spotisub/helpers/subsonic_helper.py:440-444`) — nothing in
the discovery or existing deletion paths reads or reacts to it. Pruning must key purely off
Spotify-side presence, independent of `ignored`: a still-present ignored playlist is not
pruned; a gone playlist is pruned whether or not it was ignored.

**Verified: no fake Spotipy client exists in `tests/`.** `tests/helpers.py`/`tests/conftest.py`
(from the archived `specs/archive/reliable-track-matching.md` PR) provide `FakePysonicClient`
(including a working `deletePlaylist()`/`getPlaylist()` pair that raises `DataNotFoundError`
correctly, `tests/helpers.py:126-158`), `sample_playlist_info()`, and autouse `isolated_database`/
`isolated_cache_dir` fixtures — all reusable. Nothing simulates `sp.current_user_playlists()`;
a new fake Spotipy-client stand-in (paginated `current_user_playlists(limit=, offset=)` ->
`{'items': [...]}`) is needed, monkeypatched the same way the existing fixtures monkeypatch
other module-level state.

## Deliverable
Code changes in `spotisub/generator.py`, `spotisub/database.py`, and
`spotisub/helpers/subsonic_helper.py`, plus a new `tests/test_playlist_pruning.py` (and a small
addition to `tests/helpers.py` for the new Spotipy-client fake), on a branch off `dev`.

## Requirements

- R1. `scan_user_playlists()` (or a wrapper around it) accumulates and returns the complete set
  of current Spotify playlist names (`item['name'].strip()` for every non-null, non-empty-name
  item across every page) once the full paginated fetch completes without raising. If the fetch
  raises partway through, no accumulated (partial) list is used for pruning — the exception
  propagates exactly as it does today (see R2's placement requirement for how this is enforced).
  [verify: a unit test with a 2-page fake Spotipy client (100+ playlists) asserts the returned
  set contains every item's stripped name across both pages; a second test makes the fake raise
  on page 2 and asserts no pruning occurs (see R5) and the exception propagates.]

- R2. A new pruning step runs inside `scan_library()`'s existing `try` block, sequenced
  immediately after the now-returning `scan_user_playlists()` call, using the set from R1. It
  must not run, and must not be reachable, if the R1 fetch raised.
  [verify: code inspection confirms placement inside the same `try`, after the call, not in a
  separate `except`/`finally`; the R1 partial-failure test doubles as verification here since a
  raised exception must prevent any pruning call from executing.]

- R3. A new `database` function (e.g. `delete_playlist_info_by_uuid(uuid)`) deletes a
  `playlist_info` row and its associated `subsonic_spotify_relation` rows by `playlist_info.uuid`
  — working correctly whether `subsonic_playlist_id` is `NULL` or set (unlike the existing
  `delete_playlist_relation_by_id()`, which cannot address a `NULL`-subsonic-id row). Returns
  enough information (e.g. the row it deleted, or `None` if the uuid didn't exist) for the
  caller to know whether a Subsonic-side delete is also needed.
  [verify: a unit test creates a `playlist_info` row with `subsonic_playlist_id=None` and one
  or more `subsonic_spotify_relation` rows pointing at it, calls the new function, and asserts
  both the `playlist_info` row and all its relation rows are gone; a second test does the same
  with a non-null `subsonic_playlist_id` set, confirming the function still works and reports
  that a Subsonic delete is needed.]

- R4. The pruning step: for every `playlist_info` row of type `"user_playlists"`
  (`constants.JOB_UP_ID`) whose `subsonic_playlist_name` is not in the R1 set, log at `INFO`
  naming the playlist being pruned and why (no longer present in the user's Spotify account),
  then delete it via R3, then — only if the deleted row had a non-null `subsonic_playlist_id`
  — attempt exactly one Subsonic `deletePlaylist()` call following the exact error-handling
  shape quoted in Context from `write_playlist()`'s zero-songs branch (swallow
  `DataNotFoundError` silently, log any other exception at `debug` + `sleep(1)`, never
  propagate). A row whose name is still present in the R1 set is left untouched regardless of
  its `ignored` value.
  [verify: a unit test seeds three `playlist_info` rows (one present in the fake current-Spotify
  list, one absent with `subsonic_playlist_id=None`, one absent with a real
  `subsonic_playlist_id` also present in a `FakePysonicClient`), runs the pruning step, and
  asserts: the present row survives untouched; both absent rows and their relation rows are
  gone from `playlist_info`/`subsonic_spotify_relation`; the `FakePysonicClient`'s playlist for
  the third row is also gone; an `INFO` log line was emitted per pruned row.]

- R5. Pruning never runs against a partial/incomplete fetch. If R1's fetch fails for any reason
  before completing, zero `playlist_info` rows are deleted that run.
  [verify: covered by R1's second test — assert the database is unchanged (row count identical
  before/after) when the fake Spotipy client raises mid-fetch.]

- R6. `ignored` status has no bearing on whether a row is pruned — only Spotify-side presence
  by name (R4) does.
  [verify: a unit test seeds an `ignored=1` row absent from the fake current-Spotify list and
  asserts it is pruned exactly like a non-ignored absent row; a second seeds an `ignored=1` row
  present in the list and asserts it survives.]

- R7. The random per-cycle draw (`select_playlist_info_by_type()`,
  `spotisub/database.py:523-541`) is unaffected in shape by this spec — it will simply see a
  smaller `user_playlists` row set once pruning has run, with no code change needed there. This
  requirement exists only to make explicit that this spec deliberately does *not* also add an
  `ignored`-exclusion filter to the random draw (a separately-identified, different improvement,
  already discussed and explicitly deferred by the user in favor of this pruning fix) — do not
  add one.
  [verify: `select_playlist_info_by_type()` is unmodified by this PR's diff.]

## Out of scope
- Filtering the random reimport draw to exclude `ignored` playlists (a distinct, previously
  discussed improvement the user deferred in favor of this fix — do not implement it here).
- Handling Spotify-side playlist renames as anything other than "old name pruned, new name
  discovered as a fresh row" (the existing, accepted, pre-existing behavior/quirk).
- Pruning any `playlist_info` type other than `"user_playlists"` (`saved_tracks`,
  `artist_top_tracks`, `artist_recommendations` are untouched).
- Changing `PLAYLIST_GEN_SCHED`, the random-selection mechanism itself, or anything about how
  often `scan_library` runs.
- Retrying a failed Subsonic `deletePlaylist()` call beyond the single attempt the existing
  `write_playlist()` pattern already makes (matching it exactly, not improving it).

## Constraints
- Python, matching this repo's existing style and the conventions established in
  `specs/archive/reliable-track-matching.md`'s PRs (module-level functions, `logging` for
  diagnostics, SQLAlchemy `core` for `database.py` queries, no new runtime dependencies).
- Must not require a live Spotify/Subsonic server to run the test suite — mock both, following
  the existing `FakePysonicClient` pattern in `tests/helpers.py` for the Subsonic side and
  adding an equivalent minimal fake for the Spotipy side.
- Backwards compatible with SQLite (the only DB engine this repo ships against).
- This is the same live production deployment `specs/archive/reliable-track-matching.md` was
  verified against (CT 207, `spotisub-fork:playlist-items-fix`, built from this repo's `dev`
  branch) — after merge, the same rebuild-and-verify process applies (rebuild the image from
  `dev`, redeploy, and confirm live that the next `scan_library` run actually prunes the
  playlists the user already unfollowed on Spotify's side).

## Acceptance rubric
- C1 (from R1): PASS iff the two-page-fetch test and the mid-fetch-failure test both pass, with
  citable evidence (test output) for each.
- C2 (from R2): PASS iff code inspection confirms the pruning call is sequenced inside
  `scan_library()`'s existing `try`, after the (now list-returning) `scan_user_playlists()`
  call, not reachable from any except/finally branch.
- C3 (from R3): PASS iff both `delete_playlist_info_by_uuid` unit tests (null and non-null
  `subsonic_playlist_id`) pass.
- C4 (from R4): PASS iff the three-row pruning test passes, confirming the present row
  survives, both absent rows and their relations are deleted, the Subsonic-side playlist is
  also deleted for the row that had one, and an INFO log line is present per pruned row.
- C5 (from R5): PASS iff the mid-fetch-failure test (shared with C1) shows zero rows deleted.
- C6 (from R6): PASS iff both ignored-status tests (pruned-when-absent, kept-when-present) pass.
- C7 (from R7): PASS iff `git diff` shows `select_playlist_info_by_type()` unchanged.
- C-final (production, verified by the orchestrator post-merge, not by the generator/evaluator
  loop — same live-deployment-gate pattern as `specs/archive/reliable-track-matching.md`):
  PASS iff, after rebuilding and redeploying the live image and manually triggering (or waiting
  for) the next `scan_library` run, the playlists the user already unfollowed on Spotify are
  confirmed gone from `playlist_info` (and their Navidrome playlists, if any existed, gone from
  Navidrome too), while playlists still followed are unaffected.

## Open questions
(none — all resolved above)
