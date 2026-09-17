# Spec: Reliable Subsonic track matching (fallback matching + stable persisted state)

> **Completed 2026-09-16.** Built via #4 (core fix), #5 (Dockerfile fix), and #6 (repair
> script safety fix, after live deployment verification caught a second instance of the
> app-import-triggers-a-second-scheduler incident class). Live-verified against production:
> the "Rock" playlist went from 1 matched track to 361 on reimport after deploy, the app
> restarted cleanly against the pre-existing (pre-migration-format) cache file, and the
> one-time duplicate-relation repair ran cleanly, deleting 1,234 duplicate rows across 1,206
> pairs with no scheduler side effect. All acceptance criteria (C1-C8, C-final) confirmed.

## Goal
Make Spotisub's Spotify→Subsonic track matching (a) actually use its existing text-based
fallback when MBID matching fails, and (b) keep its own persisted "is this track matched"
state consistent with what's actually pushed to the target Subsonic server's playlist, across
repeated reimports of the same playlist.

## Context
Spotisub (this repo, `J-MaFf/spotisub`, a fork of `blastbeng/spotisub`) mirrors a user's
Spotify playlists into Subsonic-API playlists (deployed target: Navidrome) containing only
tracks that already exist in the local library. This spec fixes two confirmed bugs in the
matching pipeline, both isolated to files in this repo. Implement against the `dev` branch
(the branch the live deployment builds from — see `docker-compose.yml`'s comment history in
the consuming `homelab` repo; not relevant to this repo's own build/test process).

**Verified: MBID-first matching has no working fallback.**
`spotisub/helpers/subsonic_helper.py::match_with_subsonic_track()` tries
`get_subsonic_track_via_mbid()` first: it converts the Spotify track's ISRC to MusicBrainz
recording MBIDs via `musicbrainz_helper.get_mbids_from_isrc(isrc)`, then looks those MBIDs up
in `subsonic_cache.song_mbid_dict` — a cache built by `build_subsonic_cache()` in the same
file, which indexes the Subsonic library **only** by `song["musicBrainzId"]` from the
`search2` response. Live scale on the deployed instance: of 15,812 total songs, only 9,997
carry an MBID and are indexed at all.

If the MBID lookup fails, `get_subsonic_track_via_string_compare()` (normalized exact
title+artist comparison, via `utils.compare_track_metadata`/`compare_strings`) is supposed to
run as a fallback — but only when `TEXT_COMAPRE_MATCHING_ENABLED` (env var,
`constants.py:38`) is `"1"`. Its default (`constants.py:63`,
`TEXT_COMAPRE_MATCHING_ENABLED_DEFAULT_VALUE = "0"`) means the fallback **never runs** unless
the deployer opts in — and the reference deployment (`homelab` repo's
`docker-compose.yml`) does not set it. Confirmed false negatives caused by this on the live
deployment: Bassnectar – Vava Voom / The Matrix / Wildstyle Method / Mind Tricks / Ping Pong /
Timestretch / Shampion Chip / Encore, and 9 of 10 "matched" tracks in a "Rock" playlist
(Seven Nation Army, Hooked on a Feeling, Sultans of Swing, Carry On Wayward Son, Stranglehold,
Gimme Shelter, Simple Man, Hotel California, Dust in the Wind) — all confirmed present in the
Subsonic server's library with populated MusicBrainz recording IDs and complete
artist/album/title tags, all logged `not found in your music library`.
`get_subsonic_track_via_string_compare()` itself was manually verified against this data to
produce no false positives on clearly-absent content — the matching logic is sound, it's
simply unreachable by default.

**Verified: the persisted relation table is keyed wrong, so re-runs silently go stale
instead of updating.**
`spotisub/database.py::insert_playlist_relation()` (line 632) decides insert-vs-update by
calling `select_playlist_relation()` (line 730), whose `WHERE` clause matches on
`subsonic_song_id`, `subsonic_artist_id`, `spotify_song_uuid`, **and**
`playlist_info_uuid` — i.e. it requires the *outcome* of the match to already be identical to
find "the same" row. When a track that matched successfully on a prior run (real
`subsonic_song_id`) fails to re-match on a later run (`write_playlist()` calls
`insert_song(playlist_info, None, ...)` in that case), the lookup for
`subsonic_song_id IS NULL` naturally finds nothing (the existing row has a real, non-null
`subsonic_song_id`), so the code takes the `INSERT` branch and creates a **second row** for
the same `(playlist_info_uuid, spotify_song_uuid)` pair instead of updating the first. Net
effect: the old row's stale-but-real `subsonic_song_id` survives untouched forever, so
`subsonic_spotify_relation` keeps reporting the track as matched long after it's stopped being
in the actual pushed playlist. Live evidence: after a full reimport of the "Rock" playlist,
`subsonic_spotify_relation` still listed 10 matched tracks (each with a `subsonic_song_id`
that is real and currently exists in the Subsonic library), but the *actual* Subsonic
playlist that run produced contained only 1 song. Scale of pre-existing damage on the live
deployment: `SELECT COUNT(*), COUNT(DISTINCT playlist_info_uuid||'|'||spotify_song_uuid) FROM
subsonic_spotify_relation` → 6,769 total rows, 5,797 distinct `(playlist, song)` pairs → 972
duplicate rows (~14%) already accumulated. A playlist with no reimport history (freshly
rebuilt after an unrelated tracking-row wipe) showed perfect 1:1 consistency between the
relation table and the real Subsonic playlist (27 matched, 27 present) — confirming this is
specifically a repeat-run staleness bug, not present on a playlist's first-ever import.

**Not in scope / not touched by this spec:** the container orchestration, `docker-compose.yml`
env var overrides in the consuming `homelab` repo, and any change to how the *initial* match
attempt is chosen (MBID-first is correct and stays first).

## Deliverable
Code changes in this repo (`J-MaFf/spotisub`, branch `dev` or a feature branch off it) to:
1. `spotisub/helpers/subsonic_helper.py`
2. `spotisub/database.py`
3. A one-time data-repair path (script or startup migration) for existing installations'
   already-duplicated `subsonic_spotify_relation` rows.
4. Test coverage (this repo has no existing test suite directory as of this spec — add one
   under `tests/` using `pytest`, importable the same way the app itself imports
   `spotisub.helpers.subsonic_helper` / `spotisub.database`; mock the Subsonic/Spotify/
   MusicBrainz network calls, do not require a live server).

## Requirements

- R1. `get_subsonic_track_via_string_compare()` fallback matching runs whenever
  `get_subsonic_track_via_mbid()` returns `None` for a track that has *any* usable metadata to
  compare (title + primary artist name both non-empty), regardless of the
  `TEXT_COMAPRE_MATCHING_ENABLED` environment variable's value.
  [verify: read `match_with_subsonic_track()` — the `os.environ.get(constants.TEXT_COMAPRE_MATCHING_ENABLED, ...)` gate around the string-compare call is removed or bypassed for this path; a unit test with `TEXT_COMAPRE_MATCHING_ENABLED` unset/`"0"` and a track whose MBID lookup misses but whose title+artist exactly matches a `subsonic_tracks_dict` entry results in a non-`None` `matched_track`.]

- R2. `TEXT_COMAPRE_MATCHING_ENABLED` is removed from `constants.py` and every place that reads
  it (`match_with_subsonic_track` and any other reference), since R1 makes it permanently
  effectively `"1"` and a dead toggle is worse than no toggle.
  [verify: `grep -r TEXT_COMAPRE_MATCHING_ENABLED spotisub/` returns no results after the change.]

- R3. String-compare fallback matching is indexed, not a linear scan of the whole cache per
  track. Extend `SubsonicCache`/`build_subsonic_cache()` (or add a sibling structure built
  alongside it) with a dict keyed by each song's normalized `(title, artist)` compare-variants
  (reuse `utils.generate_compare_array`), so `get_subsonic_track_via_string_compare()` looks up
  candidate matches by key instead of iterating `subsonic_tracks_dict.values()` for every
  track. The existing narrowing logic in `compare_track_metadata` / exclusion-word checks stays
  in place for whatever candidate set the index returns.
  [verify: a full reimport of a 500+ track playlist (e.g. the live "Rock" playlist, 564
  tracks) against a Subsonic library of ~15,000 songs completes in under 10 minutes wall-clock
  (current observed baseline for this playlist size, pre-fix, is ~3.5 minutes with the
  fallback disabled — this bounds how much the new fallback path is allowed to add); a code
  review confirms no per-track loop iterates the full song cache.]

- R4. `select_playlist_relation()` (and thus `insert_playlist_relation()`'s insert-vs-update
  decision) matches an existing row by `(spotify_song_uuid, playlist_info_uuid)` only —
  **not** by `subsonic_song_id`/`subsonic_artist_id`. A row's match outcome (found vs.
  not-found, or a changed matched track) always updates the single existing row for that
  `(playlist, spotify song)` pair rather than creating a new one.
  [verify: a unit test inserts a relation with a real `subsonic_song_id`, then calls
  `insert_playlist_relation` again for the same `(spotify_song_uuid, playlist_info_uuid)` with
  `subsonic_song_id=None` — asserts exactly one row exists afterward for that pair, and its
  `subsonic_song_id` is `None` (reflects the latest run, not the stale prior value).]

- R5. After R4 ships, running a full reimport twice in a row against an unchanged Subsonic
  library and unchanged Spotify playlist produces byte-identical membership in the actual
  pushed Subsonic playlist both times, and `subsonic_spotify_relation` contains exactly one
  row per `(playlist_info_uuid, spotify_song_uuid)` pair touched by that playlist, with
  `subsonic_song_id` matching what's actually in the pushed playlist.
  [verify: integration-style test (mocked Subsonic/Spotify clients) runs `write_playlist`
  twice with identical inputs; assert the resulting `song_ids` list is identical both times and
  the relation table has no duplicate `(playlist_info_uuid, spotify_song_uuid)` pairs.]

- R6. A one-time repair path de-duplicates existing `subsonic_spotify_relation` rows for
  installations upgrading from before this fix: for each `(playlist_info_uuid,
  spotify_song_uuid)` pair with more than one row, keep the row whose `subsonic_song_id`
  currently still exists in the live Subsonic library and is present in the currently-pushed
  playlist for that `playlist_info_uuid` (if determinable), otherwise keep the most-recently
  inserted row, and delete the rest.
  [verify: given a fixture DB with the duplicate pattern described in Context (two rows for
  the same pair, one with a real `subsonic_song_id` and one with `NULL`), running the repair
  leaves exactly one row for that pair; a dry-run/report mode exists so this can be checked
  against the live deployment's `spotisub.db` before applying.]

- R7. No behavior change to genuinely-absent tracks: a track whose title+artist does not match
  anything in the Subsonic library (after normalization) still reports "not found" and is not
  added to any playlist.
  [verify: a unit test with a track for an artist/title with no corresponding
  `subsonic_tracks_dict` entry asserts `match_with_subsonic_track` returns `found=False` and
  no relation row gets a non-null `subsonic_song_id`.]

- R8. No new false-positive matches: two distinct real songs that happen to share a normalized
  title+artist (e.g. same track title released on two different albums by the same artist) are
  not silently merged into one match — `get_subsonic_track_via_string_compare`'s existing
  "pick the first candidate" behavior is preserved or improved (e.g. prefer a candidate whose
  album also loosely matches when disambiguating), but must not regress to matching an
  obviously-wrong candidate when a better one exists in the same result set.
  [verify: a unit test provides two Subsonic library entries with identical normalized
  title+artist but different albums, and a Spotify track whose album metadata matches one of
  them — asserts the correct one is chosen when album disambiguation is possible, and that some
  match (not an error) is still returned when it isn't.]

## Out of scope
- Changing which matching strategy runs *first* (MBID-first stays first).
- Populating more Subsonic-side `musicBrainzId` tags, changing Navidrome/Subsonic-server-side
  MBID tagging, or anything about the audio files themselves.
- The `TEXT_COMAPRE_MATCHING_ENABLED`-driven artist/recommendation/top-tracks generators
  (`ARTIST_GEN_SCHED` etc.) — this spec only touches playlist and saved-tracks track matching.
- Any change to the `homelab` repo's `docker-compose.yml` or deployment config — this spec is
  scoped to this repo's code; once merged, the consuming deployment picks it up via its
  existing image-build pinning, which is a separate, already-documented process there.
- Retrying/backfilling already-missed Spotify API rate limits or MusicBrainz API
  availability — this spec assumes those calls succeed or fail as they currently do; it does
  not add retry/backoff logic beyond what already exists.
- A UI for resolving matching ambiguity (mentioned as a `TODO` in
  `get_subsonic_track_via_string_compare` already) — out of scope here.

## Constraints
- Python, matching this repo's existing style (see `spotisub/helpers/subsonic_helper.py` for
  conventions: module-level functions, `logging` for diagnostics, SQLAlchemy `core` (not ORM
  sessions) for `database.py` queries).
- No new runtime dependencies without strong justification — `musicbrainz_helper` and the
  Subsonic client (`pysonic`) are already in use; reuse them.
- Must not require a live Subsonic/Spotify/MusicBrainz server to run the test suite — mock
  network boundaries.
- Backwards compatible with SQLite (the only DB engine this repo currently ships against, per
  `dbms.db_engine`).

## Acceptance rubric
- C1 (from R1): PASS iff a code reader can confirm the `TEXT_COMAPRE_MATCHING_ENABLED`
  environment check no longer gates whether string-compare fallback runs, and the described
  unit test (MBID miss + exact title/artist match present) passes.
- C2 (from R2): PASS iff `grep -r TEXT_COMAPRE_MATCHING_ENABLED spotisub/` returns nothing.
- C3 (from R3): PASS iff the 564-track playlist reimport benchmark completes in under 10
  minutes AND a code reader confirms fallback matching uses an index/dict lookup rather than a
  full linear scan of `subsonic_tracks_dict.values()` per track.
- C4 (from R4): PASS iff the described unit test (insert real match, then insert a
  not-found outcome for the same pair) results in exactly one row, updated in place.
- C5 (from R5): PASS iff the double-run integration test produces identical `song_ids` output
  both runs and no duplicate `(playlist_info_uuid, spotify_song_uuid)` pairs afterward.
- C6 (from R6): PASS iff the repair-path test collapses the fixture's duplicate pair to one
  row using the documented precedence, and a dry-run mode exists that reports intended
  deletions without applying them.
- C7 (from R7): PASS iff the not-found unit test passes and no regression is observed against
  at least one real known-absent track from the live evidence in Context (e.g. AlienPark,
  confirmed absent from Lidarr entirely, must still show `found=False`).
- C8 (from R8): PASS iff the disambiguation unit test passes and no existing test (C1, C4, C5,
  C7) regresses.
- C-final: PASS iff a domain expert reviewing this artifact would accept it without
  substantive changes — specifically, iff re-running a real reimport of the live "Rock"
  playlist after this fix ships results in all 10 currently-tracked matches (not just 1)
  appearing in the actual Subsonic playlist, without introducing any new track that isn't
  genuinely in the library.

## Open questions
(none — all resolved above)
