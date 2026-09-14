# AO3 Calibre Backfill Handoff

Read this before changing or running the project. This is working context only;
do not commit it.

## Project Purpose

`ao3Archiver` is a Python AO3 download/backfill helper. The current work adds a
safe pipeline for an existing Calibre library:

- Read existing EPUBs and map their prefaces to primary AO3 work IDs.
- Fetch AO3 categories, counters, words, chapters, and status into an external
  append-only cache.
- Calculate local EPUB Words/Gfog without rewriting EPUBs unless the new
  explicit portable-enrichment stage is approved.
- Populate Calibre custom columns through guarded `calibredb set_custom` calls.
- Preserve standard Calibre metadata, tags, identifiers, comments, covers,
  series, formats, and unrelated custom columns.

`ao3downloadernew` is a separate, dirty repository. Its existing menu option
`l` harvests AO3 links and optional metadata CSV; its normal download option
does not own Calibre or local metric writes. The future-download plan recommends
making `ao3Archiver` the canonical enrichment/sync owner.

## Repositories And Data

- Archiver: `/home/drifter/repos/ao3Archiver`
- Downloader: `/home/drifter/repos/ao3downloadernew`
- Calibre library: `/home/drifter/Calibre Library`
- Backfill artifacts: `/home/drifter/.local/share/ao3-calibre-backfill`
- Local credential file: `/home/drifter/repos/ao3Archiver/.env`

The `.env` file is ignored, mode `0600`, and its values must never be printed,
logged, persisted in JSON/cache output, or included in command arguments. The
backfill reads it only when `--use-env-credentials` is explicitly supplied. The
old `personal.ini` path is not used by the backfill.

## Current Implementation

### Authentication and network

- `ao3_backfill.py` uses the AO3 token-dispenser/login flow.
- Login validates authenticated cookies or logged-in/logout markers.
- Work requests use HTTPS, bounded same-origin redirects, persisted request
  scheduling, exact `Retry-After`, and one bounded reauthentication attempt.
- `--retry-failed-once` retries incomplete/unavailable records and unexpected
  HTML once, then stops on a second failure.
- Network requests remain sequential. No proxy/VPN/session rotation or workers
  are allowed.

### Cache and reports

- Cache: `/home/drifter/.local/share/ao3-calibre-backfill/ao3-cache.jsonl`
- Scan report: `/home/drifter/.local/share/ao3-calibre-backfill/scan.json`
- A pre-refresh cache snapshot exists at:
  `ao3-cache.before-refresh-20260911T203829Z.jsonl`
- A one-book-refresh snapshot exists at:
  `ao3-cache.before-one-book-refresh-20260911T230744Z.jsonl`
- The cache is append-only; repeated work IDs are expected. Validation resolves
  the latest record by work ID.
- Cache provenance binds the report/cache to the Calibre library path and UUID.

### Local metrics

- `local_metrics.py` implements the documented pure-Python compatibility profile.
- Newly calculated Gfog values are rounded to two decimals.
- The normal scan is bounded to the first 25 non-ambiguous work IDs.
- `calculate-missing-metrics` calculates only for mapped books whose local
  `#words` or `#gfog` cell is blank. Existing local values are preserved.
- `--metrics-all` exists but is intentionally expensive and should not be run
  casually over the whole EPUB library.

### Portable EPUB metadata

- `ao3_metadata.py` now has `enrich_epub_portable()`.
- It writes only namespaced `ao3:*` OPF metadata, including category, AO3
  counters/words, local Words, and rounded Gfog.
- It does not alter standard title, author, tags, identifiers, description,
  series, covers, or formats.
- `ao3_backfill.py enrich-epubs` backs up each original EPUB externally before
  atomic mutation and validates the portable metadata afterward.
- This stage has not been run in bulk.

### Calibre state

- Target library currently reports 14,831 books and 14,830 EPUBs. If another
  inventory is expected, verify the library path before running anything.
- `ao3_category` exists as `AO3 Category` / `text` and was verified through
  `calibredb` and read-only SQLite.
- One-book Calibre test completed for book `27876`, work `65995504`.
- Its eight AO3/category fields were written and read back through both
  Calibre and SQLite.
- Its existing local values were preserved: Calibre had `#words=30207` and
  `#gfog=9.23301492872045`, while the newly calculated values were
  `30080` and `8.45`.
- `#ao3_kudos:>100` returned one result after the test write.
- Calibre-Web was not running, so UI visibility has not been checked.
- The backup used before that write is stale by design. Create a new verified
  backup before any further Calibre write.

## Last Live Network State

The approved first-25 refresh successfully updated six works. Work `59117515`
returned a logged-in same-work chapter template without work-level statistics;
it is now cached as an expected `incomplete` record rather than an unexpected
HTML stop. A targeted retry was performed once after the parser fix.

The current long-running foreground fetch may be active. At handoff creation,
the observed process was:

```text
python -u /home/drifter/repos/ao3Archiver/ao3_backfill.py fetch ... --limit 14800
```

Always recheck with `pgrep -af 'ao3_backfill.py fetch'` before starting another
fetch. Never run two fetch processes.

## Safe Operational Order

1. Confirm the target library path and Calibre/Calibre-Web process state.
2. Confirm `.env` presence without printing values.
3. Use one foreground fetch process with `--use-env-credentials`,
   `--retry-failed-once`, `--continue-after-review`, `--delay 30`, and the
   appropriate limit. Use `python -u` plus `tee` for visible output.
4. Validate the cache. Do not write Calibre values until the cache is reviewed.
5. Run `calculate-missing-metrics` if the report has new blank local cells.
6. Create a fresh non-empty provenance-checked `metadata.db` backup.
7. Run `enrich-epubs` with `--approve-epub-write`, an external EPUB backup
   directory, and `--limit 1`; inspect the portable EPUB metadata.
8. Run the one-book Calibre `write` with `--approve-write` and
   `--write-local-metrics`; verify the readback.
9. Create another fresh database backup before bulk Calibre writes.
10. Run approved bulk `enrich-epubs` and/or `write` operations, then verify
    numeric search/sorting and Calibre-Web visibility.

## Verification Baseline

- Archiver suite last passed: `63 passed`.
- Downloader supported environment last passed: `422 passed`, 20 snapshots,
  one dependency deprecation warning.
- No commit, push, reset, clean, or checkout is authorized.
- Existing unrelated worktree changes in both repositories belong to the user
  and must be preserved.

## Future Download Plan

See `future-download-metadata-plan.md`. The intended architecture is:

- Keep `ao3downloadernew` as an optional link/CSV harvesting source.
- Make `ao3Archiver` the canonical future EPUB enrichment and Calibre handoff.
- For fresh downloads, atomically bake namespaced AO3/local metadata into the
  EPUB, then separately populate guarded Calibre custom columns.
- Do not use arbitrary EPUB metadata as an implicit substitute for Calibre
  custom-column writes.
