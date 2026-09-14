# Implementation Plan

Task: AO3 Calibre backfill authentication, categories, local metrics, and safe
refresh/write reporting.

This is a working plan. It is not an authorization to make network requests,
write Calibre values, commit, or push.

## Goals

- Add explicit environment-variable authentication to the backfill utility with
  no secret leakage.
- Reauthenticate at most once for a work request after login redirect, 401, or
  403, while preserving earlier cache records on failure.
- Apply one persisted, sequential scheduler to token, login, work, and retry
  requests, including exact `Retry-After` cooldowns.
- Parse AO3 full-work categories into the shared AO3 model, JSONL cache, scan
  validation output, and a backfill-only Calibre text column.
- Calculate local EPUB words/Gunning Fog without rewriting EPUBs or invoking a
  Calibre plugin, with algorithm metadata in the scan report.
- Populate existing `#words`/`#gfog` only when blank under an explicit local
  metric write flag; preserve existing values and unavailable results.
- Add a read-only first-25 preview and an external cache snapshot operation so
  refresh mode is demonstrably bounded.

## File-Level Changes

### `ao3_metadata.py`

- Add `category: str | None` to `AO3Metadata`.
- Extend the current HTML parser with a dedicated `dd.category a` collector
  rather than flattening adjacent anchors through the numeric-stat parser.
- Preserve visible anchor order and join values with `", "`; return `None` for
  a missing category block.
- Accept `category`/`categories` in the existing CSV adapter when present,
  without changing the separate EPUB enrichment contract.
- Do not add category to EPUB metadata serialization or rewrite behavior.

### `calibre_sync.py`

- Preserve the existing seven-column `CUSTOM_COLUMNS` contract used by the
  legacy enriched-EPUB sync.
- Add a separate `BACKFILL_CUSTOM_COLUMNS` tuple that appends
  `("ao3_category", "AO3 Category", "text", "category")`.
- Do not make the legacy sync write the new category column.

### New `local_metrics.py`

- Add a small stdlib-only EPUB reader that follows the evidenced Count Pages
  inclusion boundary: manifest/spine order, every spine document body, HTML
  entity resolution, and no AO3 preface/title/author-note/chapter/end-note
  exclusion.
- Recover text with an HTML parser and report malformed/missing EPUB structure
  as an unavailable local metric rather than mutating the EPUB.
- Implement a named deterministic word-tokenizer compatibility profile based on
  the observed Count Pages behavior, including documented handling for Unicode
  text, punctuation, hyphenated words, numbers, and empty text. Do not claim
  bit-for-bit ICU equivalence.
- Implement the established Count Pages Gunning Fog formula and bundled English
  syllable rules needed for the calculation. Handle zero-word/zero-sentence
  input as unavailable Gfog rather than dividing by zero.
- Expose a `LOCAL_METRICS_ALGORITHM` identifier and a result type containing
  `words`, `gfog`, and a sanitized error/unavailable reason.

### `ao3_backfill.py`

- Remove the backfill CLI’s local-file credential path and add
  `load_environment_credentials()` plus `--use-env-credentials`. Require both
  `AO3_USERNAME` and `AO3_PASSWORD` as a pair; do not include either value in
  exceptions, logging, JSON, subprocess arguments, or `repr` output.
- Keep credential loading after cache provenance binding, pending-ID selection,
  and the no-pending early return.
- Update `login_authenticated_session()` to use the existing token-dispenser
  flow, send form fields through `data=`, and verify a session cookie or an
  unambiguous logged-in/logout response. Keep token/login requests sequential.
- Add login-redirect detection and an injected reauthentication callback to
  `AO3Fetcher`. On an authenticated response challenge, perform at most one
  reauthentication attempt for that work, then retry once through the same
  scheduler; a second challenge or failed login raises `AuthenticationFailure`.
- Check Cloudflare before generic 403 authentication classification so a
  Cloudflare challenge is not turned into a credential retry. Preserve existing
  repeated-Cloudflare, repeated-429, unexpected-HTML, wrong-work, and chapter
  redirect stop behavior.
- Extend cache context scheduling with a persisted next-eligible timestamp in
  addition to the most recent request timestamp. Route token, login, work, and
  retry requests through the shared scheduler; persist server cooldowns before
  sleeping and do not add the normal delay after an exact `Retry-After` wait.
- Extend `AO3FetchRecord` with optional `category`, defaulting old cache records
  to `None`. Populate it from `AO3Metadata` and keep it out of counter
  completeness checks.
- Extend `EpubMapping`/`ScanReport` with local words, Gfog, and a sanitized
  local-metric error, plus the algorithm identifier. Compute metrics during the
  no-network scan and revalidate them before writes.
- Add a first-refresh mapping selector/preview that deduplicates in scan order,
  excludes ambiguous IDs, and displays book/work/EPUB/cache/local-metric data
  without making requests.
- Add an exclusive, non-overwriting cache snapshot operation for the JSONL cache
  before refresh mode.
- Extend cache validation output with title, category, AO3 counters/words, local
  words, and Gfog.
- Use the backfill-only column tuple for setup and verification. Verify local
  `words`/`gfog` columns only when local metric writes are explicitly requested.
- Change writes so `None`, incomplete, and unavailable values are skipped and
  existing cells are preserved; numeric zero remains writable as zero.
- Add explicit local metric write options: default fill blanks only, with a
  separate replacement flag guarded by the normal write approval. Keep category
  writes in the AO3 custom-column stage and never call `set_metadata`.
- Strengthen backup verification to reject a backup whose manifest source hash
  or source size no longer matches the current `metadata.db`.
- Preserve numeric `#ao3_kudos:>100` and sorting verification and include the
  new category/local population details in read-only output.

### `test_ao3_metadata.py`

- Test `Gen`, multiple categories including `F/F` and `M/M`, and missing
  category metadata.
- Test model/CSV compatibility without requiring EPUB mutation.

### `test_local_metrics.py` (new)

- Test spine order and inclusion of preface/title/chapter headings/author notes/
  end notes.
- Test entities, scripts/styles handling, punctuation, hyphenated words,
  numbers, Unicode/non-English text, empty text, malformed HTML, malformed EPUB,
  zero-word Gfog, and the established Gunning Fog formula/syllable behavior.

### `test_ao3_backfill.py`

- Add environment loading, missing/partial variable rejection, and no-secret
  leakage tests.
- Add token success, token failure, login rejection, cookie/logged-in response,
  and correct POST-body tests.
- Add login redirect/401/403 refresh tests, one-reauth bound tests, failed
  reauth stop/preserve tests, and Cloudflare ordering tests.
- Add shared scheduler tests covering token/login/work request ordering,
  persisted timestamps, exact integer/date `Retry-After`, and no extra delay.
- Add category cache round-trip/validation tests, already-cached refresh tests,
  provenance rejection, snapshot preservation, primary preface/chapter/wrong-
  work behavior, and unexpected/auth HTML stops.
- Add scan/report and dry-run output assertions for local/AO3 metrics.
- Add custom-column verification for `ao3_category`, blank-vs-zero write tests,
  skip/preserve tests for unavailable values, fill-blank local metric tests,
  explicit replacement tests, and no-standard-metadata/EPUB mutation tests.

### `README.md`

- Document `--use-env-credentials` without showing or requesting values.
- Document category setup/verification, cache refresh preview/snapshot flow,
  local metric algorithm identity, blank-only default writes, and the existing
  explicit network/Calibre approval gates.
- Remove or revise the backfill-only local credential-file instructions without
  touching the ignored credential file.

### `.ai/ao3-calibre-backfill-auth-metrics/*`

- Keep progress, research, decisions, and this plan as uncommitted working
  artifacts only.

## Operational Sequence After Implementation

1. Run archiver tests and the downloader tests in its supported environment.
2. Reinspect both worktrees and confirm only intended files changed.
3. Run a fresh no-network inventory scan with a bounded `--metrics-limit` for
   the reviewed batch; report missing URLs, malformed EPUBs, ambiguous mappings,
   duplicate IDs, metric scope, and current UUID. Use a larger explicit limit
   only when the additional sequential EPUB-read time is acceptable.
4. Confirm Calibre/Calibre-Web are closed and verify the live library plus a new
   non-empty provenance-checked backup. Do not overwrite existing backups.
5. Set up/verify `ao3_category` through `calibredb` and read-only SQLite under
   explicit column approval. Create and verify a new backup after setup because
   the schema mutation invalidates the pre-setup backup freshness hash. No
   network request occurs until this gate passes.
6. Validate cache provenance and create a non-overwriting external snapshot.
7. Show the first 25 non-ambiguous scan-order mappings using the preview command.
8. Stop for explicit network approval. If approved and the two environment
   variables are available, run exactly `--refresh --limit 25` with one
   sequential request at a time; otherwise do not request.
9. Validate refreshed cache samples, including category, AO3 counters/words,
   local words, and Gfog. Stop for explicit Calibre-write approval.
10. If approved, perform the required one-book custom-column write test, then
    only the approved bulk scope; read back through `calibredb` and read-only
    SQLite, verify numeric sort/search, and check Calibre-Web visibility. Do not
    claim completion if the live visibility check cannot be performed.

## Proposed Review Split

This work spans authentication/cache scheduling, parser/category support, local
metrics, and Calibre writes and is likely above the single-change review
threshold. Proposed sequential review units are:

1. Authenticated cache refresh, category parsing/model, scheduler, preview, and
   cache-focused tests.
2. Local EPUB metrics/reporting and metric-focused tests.
3. Backfill custom-column setup/write safety, documentation, and end-to-end
   verification tests.

The split is a human decision gate. No implementation starts until the plan and
this split decision are approved.
