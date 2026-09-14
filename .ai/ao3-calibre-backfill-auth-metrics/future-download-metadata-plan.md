# Future Download Metadata Plan

> **Superseded** for everything concerning new downloads by
> `.ai/download-first-enrichment/plan.md`. That plan reflects the decision that
> the backfill is one-time and that Calibre columns are populated by
> drag-and-drop import rather than a guarded `calibredb` sync. The sections
> below remain accurate for the existing-library backfill only.

Status: planning only. This document does not authorize implementation,
network requests, Calibre writes, commits, or pushes.

## Objective

Make newly downloaded AO3 EPUBs portable and Calibre-ready without changing the
existing-library safety boundary:

- Freshly downloaded EPUBs may be enriched atomically after they pass EPUB
  validation.
- Existing Calibre-library EPUBs remain read-only for the backfill workflow.
- AO3 metadata and local metrics are embedded in the new EPUB for portability.
- Calibre custom columns are populated separately through explicit, guarded
  synchronization.
- AO3 words remain separate from local Words.
- Gfog is rounded to two decimal places everywhere it is newly calculated or
  serialized.

For the existing-library backfill, the newly approved portable-EPUB stage is a
separate explicit operation. It must not be folded into the existing Calibre
column writer implicitly.

## Current Boundaries

### `ao3Archiver`

- `download.py:136-172` already fetches AO3 metadata after an EPUB download.
- `ao3_metadata.py:392-411` already enriches a fresh EPUB by rewriting its OPF
  package atomically.
- `AO3Metadata` already contains AO3 counters/words and now contains category,
  but category is not currently serialized by the normal enrichment path.
- `local_metrics.py` calculates local Words/Gfog without rewriting an EPUB.
- `ao3_backfill.py` owns the stronger cache, backup, process, approval, and
  dual-readback gates.
- `calibre_sync.py` has a legacy seven-column EPUB sync path and a separate
  backfill-only category contract. The legacy path must not silently bypass the
  guarded backfill gates.

### `ao3downloadernew`

- Menu option `l` in `ao3downloader/main.py:88-90` invokes
  `actions/getlinks.py`.
- `actions/getlinks.py:21-46` optionally writes a metadata CSV. The current
  uncommitted work adds an engagement prompt for comments, kudos, bookmarks,
  and hits.
- `parse_soup.py:257-280` already emits full-work category and AO3 words.
- `parse_soup.py:310-352` emits listing metadata with the plural
  `categories` key.
- The normal menu option `a` download path saves EPUB bytes but does not persist
  parsed metadata, calculate local metrics, or write Calibre columns.
- Local Words/Gfog cannot be calculated in the listing-only CSV path because no
  downloaded EPUB contents are available there.

## Recommended Ownership

Make `ao3Archiver` the canonical future-book enrichment and Calibre handoff
implementation. Preserve `ao3downloadernew`'s existing dirty worktree and do
not make it directly own Calibre database writes.

Recommended future flow:

1. Harvest AO3 links and optional AO3 CSV metadata through the existing
   downloader menu when desired.
2. Download and validate the raw EPUB in the archiver-owned future-download
   path.
3. Fetch current AO3 work metadata through the authenticated AO3 session.
4. Calculate local EPUB Words/Gfog from the saved EPUB.
5. Enrich the fresh EPUB atomically with portable AO3/local metadata.
6. Import or match the book in Calibre.
7. Populate only approved custom columns through the guarded synchronization
   boundary.
8. Verify values through `calibredb`, read-only SQLite, numeric sorting/search,
   and Calibre-Web after an explicit UI refresh/rescan.

This preserves a single operational owner while allowing the downloader repo to
remain an optional link/CSV source until its future is explicitly consolidated.

## Metadata Contract

### Shared model fields

Extend the canonical AO3 model with explicit optional fields:

- `category: str | None` - ordered visible AO3 category text, for example
  `F/F, M/M`.
- `words: int | None` - AO3-reported work word count.
- `local_words: int | None` - local EPUB word count.
- `local_gfog: float | None` - local Gunning Fog value rounded to two decimals.
- Existing counters/status/chapters remain unchanged.

Keep AO3 words and local Words distinct in names, cache records, EPUB metadata,
and Calibre targets.

### EPUB OPF metadata

Add portable, namespaced metadata without replacing standard tags:

- `ao3:work_id`
- `ao3:work_url`
- `ao3:category`
- `ao3:words`
- `ao3:kudos`
- `ao3:hits`
- `ao3:bookmarks`
- `ao3:comments`
- `ao3:chapters`
- `ao3:status`
- `ao3:local_words`
- `ao3:local_gfog`

Update the visible description statistics block to distinguish:

- `AO3 Words`
- `Local Words`
- `Gunning Fog`
- `Category`

Do not add category to standard Calibre tags or replace existing `dc:subject`
values unless separately approved. Existing fandom/tag metadata must remain
untouched.

### Calibre custom columns

The future sync must target only these custom columns:

- `#ao3_category` - text
- `#ao3_kudos` - int
- `#ao3_hits` - int
- `#ao3_bookmarks` - int
- `#ao3_comments` - int
- `#ao3_words` - int
- `#ao3_chapters` - text
- `#ao3_status` - text
- `#words` - int
- `#gfog` - float

`#pages`, standard metadata, tags, identifiers, comments, covers, series, and
formats are outside this write contract.

## File Plan

### `ao3_metadata.py`

- Add local metric fields to the shared metadata model without changing the
  meaning of AO3 `words`.
- Add category/local fields to the optional OPF metadata name list.
- Serialize and read all new namespaced values.
- Include category/local values in the visible description block for fresh
  downloads.
- Preserve missing values as absent metadata rather than empty fabricated
  values.
- Validate work ID/URL identity on read.
- Keep atomic ZIP replacement and EPUB validation behavior.

### `local_metrics.py`

- Keep the established pure-Python compatibility profile.
- Round every newly calculated Gfog result to two decimal places at the model
  boundary, not only at display time.
- Keep existing values untouched unless an explicit replacement flag is used.
- Preserve the all-spine/body/entity rules and sanitized malformed-EPUB errors.

### `download.py`

- Make the fresh-download path obtain AO3 metadata, calculate local metrics, and
  pass both into EPUB enrichment.
- Ensure failures do not replace the validated raw EPUB with a partial file.
- Do not print credentials or include them in logs.
- Add an explicit mode/configuration switch if raw un-enriched EPUB output must
  remain available.
- Do not make this path write `metadata.db` implicitly; Calibre writes remain a
  separate explicit stage.

### `calibre_sync.py`

- Decide whether the legacy sync remains unchanged or is replaced by a guarded
  future-download sync adapter.
- Preferred implementation: add a guarded adapter that reads enriched EPUB
  metadata and calls the same verified custom-column/write/readback helpers as
  `ao3_backfill.py`, rather than adding category/local fields to the unsafe
  legacy path.
- Preserve the existing seven-column legacy contract unless a migration is
  explicitly approved.
- Round/format Gfog as a numeric two-decimal value while preserving Calibre's
  numeric datatype.

### `ao3_backfill.py`

- Reuse the existing authenticated fetch/cache/scheduler implementation for
  future downloads rather than creating another AO3 client.
- Add a future-download handoff command or library function that accepts a
  validated EPUB plus its AO3 work ID and writes an append-only cache record
  containing category, AO3 counters/words, local words, and rounded Gfog.
- Reuse existing backup, closed-process, explicit approval, one-book, and
  dual-readback gates for Calibre synchronization.
- Require a fresh backup after any schema or Calibre write mutation.
- Keep cache provenance tied to the target library UUID.
- Add an explicit `enrich-epubs` command that:
  - requires a current verified `metadata.db` backup, a non-overwriting external
    EPUB backup directory, Calibre/Calibre-Web closed, and an explicit approval;
  - selects only cached `availability=ok` records with calculated local metrics;
  - preserves ambiguous/incomplete/unavailable EPUBs;
  - backs up each original EPUB before mutation and resumes safely if a backup
    already matches the original hash;
  - writes only namespaced portable AO3/local OPF metadata, never standard
    title/author/tags/identifiers/comments/series/cover fields;
  - validates the enriched EPUB and reads the written portable metadata back;
  - persists progress after each book and stops on the first unsafe failure.

### `ao3downloadernew/ao3downloader/parse_soup.py`

- Preserve the existing full-work `category` and `words` keys.
- Preserve optional engagement output behind the existing prompt.
- Normalize the CSV contract explicitly if CSV is consumed downstream:
  `category`/`categories` should map to the canonical category field, and
  comma-formatted numeric strings should be normalized before ingestion.
- Do not add a fake local Gfog value to listing metadata; it cannot be derived
  before an EPUB exists.

### `ao3downloadernew/ao3downloader/actions/getlinks.py`

- Preserve the existing metadata/no-metadata menu choice.
- Preserve the existing engagement opt-in and ensure category/AO3 words/counters
  remain available when metadata is requested.
- Add tests for the exact CSV columns and opt-out behavior if the CSV is made a
  formal input to the archiver handoff.

### `ao3downloadernew/ao3downloader/ao3.py`

- Only change this file if the existing CSV/full-work metadata path needs a
  normalized canonical record for the archiver handoff.
- Do not add Calibre writes or local metric calculation here.

### Tests

Add tests for:

- Rounded Gfog serialization and readback.
- Category and local metric OPF metadata in a newly enriched EPUB.
- AO3 words versus local Words separation.
- Missing category/local values remain absent and do not clear existing values.
- Raw EPUB remains valid after enrichment and is replaced atomically.
- Future-download metadata failure leaves the validated raw EPUB available.
- New-download CSV opt-in/opt-out for AO3 category, words, and engagement
  counters.
- CSV normalization from singular full-work `category` and plural listing
  `categories`.
- No local Gfog is fabricated in the listing-only path.
- Guarded one-book Calibre sync writes all approved fields and verifies through
  both Calibre interfaces.
- Existing standard metadata/tags/formats and unrelated custom columns remain
  unchanged.

## Operational Gates

### Fresh download

- Validate the downloaded EPUB before enrichment.
- Enrich only the fresh EPUB, never an existing library EPUB in the backfill
  path.
- Preserve a raw-download failure path if AO3 metadata retrieval stops.

### Calibre synchronization

- Confirm Calibre and Calibre-Web are closed.
- Verify custom-column schemas through `calibredb` and read-only SQLite.
- Require a non-empty current metadata.db backup with matching source hash/UUID.
- Require explicit write approval.
- Perform a one-book write and dual readback before bulk writes.
- Verify numeric `#ao3_kudos:>100`, numeric sorting, and Calibre-Web visibility.

### Network

- Require explicit `--approve-network` and `--use-env-credentials`.
- Keep token/login/work requests sequential.
- Use the persisted 30-second scheduler and exact AO3 `Retry-After` values.
- Allow one bounded reauthentication per work request.
- Stop on repeated Cloudflare, repeated rate limiting, failed authentication,
  malformed response, or a second unexpected-HTML failure.

## Acceptance Criteria

- A fresh downloaded EPUB contains AO3 category/counters/words and local
  Words/Gfog metadata without losing existing tags or standard metadata.
- A Calibre import/sync populates all approved custom columns with correct
  datatypes and rounded Gfog.
- Existing local Words/Gfog values are preserved by default.
- AO3 words are never confused with local Words.
- The downloader metadata menu can opt in/out of AO3 metadata and engagement
  fields without producing incompatible CSV schemas.
- No credentials appear in source, arguments, logs, cache, reports, exceptions,
  or EPUB metadata.
- Both repository test suites pass, including new functional and safety tests.

## Open Decision

Before implementation, confirm whether `ao3downloadernew` should remain only an
optional CSV/link harvester or whether this task should add a new automatic
download-to-Calibre integration there. The recommended scope is to make
`ao3Archiver` the canonical future-download/enrichment/sync owner and keep the
downloader repository limited to its existing harvesting/download role.
