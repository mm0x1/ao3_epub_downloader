# Research Findings

This document records read-only research for the AO3 Calibre backfill task. It
contains no credentials or credential-derived values.

## Repository State

- `/home/drifter/repos/ao3Archiver` is on `main` with pre-existing modified,
  deleted, and untracked files. The untracked backfill implementation is in
  `ao3_backfill.py`, with adjacent model and sync code in `ao3_metadata.py` and
  `calibre_sync.py`.
- `/home/drifter/repos/ao3downloadernew` is also dirty, with pre-existing edits
  in its AO3 parser, actions, and tests. Those changes must remain untouched
  unless an approved plan specifically requires an integration change.
- No `.ai` state directory existed before this task.

## Existing Backfill Architecture

### Authentication

- The backfill currently reads only an explicitly supplied local credential file
  through `load_local_credentials()` and does not log its values
  (`ao3_backfill.py:914-928`).
- `login_authenticated_session()` requests
  `token_dispenser.json`, submits the login form, and checks for a session cookie
  or logout link (`ao3_backfill.py:931-1000`). It currently accepts a local
  credential path, not environment variables.
- `fetch_pending()` verifies the report/library, verifies all AO3 custom
  columns, binds the cache, calculates pending IDs, then opens one sequential
  session (`ao3_backfill.py:1437-1516`). It returns before creating a session if
  there are no pending IDs (`ao3_backfill.py:1471-1478`).
- A persisted cache context stores `last_request_at`; cache operations use an
  exclusive lock and append records with `fsync()`
  (`ao3_backfill.py:636-760`). The current scheduler waits against that shared
  timestamp before login/work requests (`ao3_backfill.py:1480-1507`).
- A work response classified as authentication-related currently raises
  immediately (`ao3_backfill.py:896-911`, `ao3_backfill.py:1087-1089`). There is
  no bounded reauthentication path for an expired session.
- `Retry-After` parsing supports integer seconds and HTTP dates
  (`ao3_backfill.py:1003-1015`), but the persisted timestamp is the request
  start time and does not persist a longer server cooldown if a process stops
  during that wait (`ao3_backfill.py:1060-1065`, `1480-1486`).

### AO3 page validation and existing failure gates

- Same-work chapter redirects are accepted when the final URL contains the
  requested work ID; wrong-work redirects are rejected
  (`ao3_backfill.py:806-840`).
- The fetcher stops on unexpected non-HTML, malformed/unrecognized HTML,
  repeated Cloudflare, repeated rate limits, and unexpected status responses
  (`ao3_backfill.py:1066-1146`).
- Cache records are appended only after a parsed result is returned, preserving
  earlier records when a later request raises (`ao3_backfill.py:1504-1515`).
- Current CLI network use requires `--approve-network`, and the existing
  authentication option is `--use-local-credentials`; there is no environment
  credential option (`ao3_backfill.py:1822-1837`, `1907-1929`).

## Authentication Patterns in the Downloader

- `ao3downloadernew/ao3downloader/parse_soup.py:64-81` extracts the login
  authenticity token from `form#new_user`.
- `ao3downloadernew/ao3downloader/parse_soup.py:383-384` identifies a logged-in
  page with `body.logged-in`.
- `ao3downloadernew/ao3downloader/parse_soup.py:257-280` parses full work-page
  metadata, including category text and counters.
- The downloader’s current repository login/request implementation is a
  separate client from the backfill and should not be changed implicitly. Its
  existing request plumbing and interactive credential behavior require a
  separate scope decision if environment authentication is intended there.

## Category Metadata

- The authoritative full work-page selector in the downloader is
  `dd.category a`; it reads visible anchor text and joins the values in AO3
  order with `", "` (`ao3downloadernew/ao3downloader/parse_soup.py:257-280`).
- Existing refreshed fixtures contain `Gen` and multiple categories including
  `F/M`, `M/M`, `Other`, and `F/F`
  (`ao3downloadernew/test/fixtures/unlockedWork.html:242-251`,
  `ao3downloadernew/test/fixtures/explicitWorkLoggedIn.html:250-259`).
- The downloader’s listing-page `span.category`/`categories` representation is
  a different contract (`parse_soup.py:310-352`) and must not replace the
  full-work selector.
- The archiver’s `AO3Metadata` model has no category field yet
  (`ao3_metadata.py:54-70`), and its generic statistics parser is not suitable
  for adjacent category anchors without a dedicated category collector
  (`ao3_metadata.py:73-155`).
- The safest compatible representation is an optional scalar string
  `category: str | None`, preserving visible AO3 order and comma-space
  formatting. Missing category metadata remains `None`, not a fabricated
  `No category` value.
- The requested Calibre target is safe and compatible: `ao3_category`, display
  name `AO3 Category`, datatype `text`. It should remain separate from standard
  Calibre tags and from EPUB mutation.

## Local Words, Pages, and Gfog Evidence

### Live Calibre configuration

  identifies these custom columns:

  | label | display name | datatype |
  |---|---|---|
  | `pages` | `Pages` | `int` |
  | `words` | `Words` | `int` |
  | `gfog` | `Gfog` | `float` |

- The same mapping and display formats are recorded in
  `/home/drifter/Calibre Library/metadata_db_prefs_backup.json:138-210`.
- The live Calibre preferences contain Count Pages and FanFicFare settings
  (only key names and value lengths were inspected, never credential values):
  `namespaced:CountPagesPlugin:settings` and
  `namespaced:FanFicFarePlugin:settings`.
- The live library currently has 14,831 books. Existing local custom values are
  populated for most books, while the AO3 columns are empty. The process check
  observed no active Calibre/Calibre-Web process during read-only inspection.

### Count Pages implementation

The local Count Pages plugin is installed at
`/home/drifter/.config/calibre/plugins/Count Pages.zip`. Its source establishes
the existing meanings without requiring a new formula:

- `statistics.py:get_word_count()` opens the EPUB with Calibre’s `EbookIterator`
  and calls `_get_epub_standard_word_count()`.
- `_read_epub_contents()` iterates `iterator.spine` and reads every spine
  document (`statistics.py` in the plugin archive). With `strip_html=True`,
  `_get_body_text()` parses the body, resolves entities through
  `xml_to_unicode(... resolve_entities=True)`, and concatenates body strings.
  There is no source-level exclusion for AO3 prefaces, title pages, author
  notes, chapter headings, or end notes; all spine body text is included.
- The installed plugin configuration has `useIcuWordcount: true`
  (`/home/drifter/.config/calibre/plugins/Count Pages.json:33-37`). The plugin
  therefore prefers Calibre ICU `count_words(book_text, lang)` and falls back
  to Calibre’s older word-count object when ICU is unavailable
  (`statistics.py:_get_epub_standard_word_count`).
- The configured page algorithm is Count Pages `PageCount`, with algorithm 0
  named `Paragraphs (APNX accurate)` (`config.py:57-60`, `141-160`). Pages are
  outside the requested local metric write target and must not be recalculated
  or overwritten by this task.
- Gfog is explicitly Count Pages `GunningFog`, with the formula:

  `0.4 * (averageWordsPerSentence + 100 * complexwordCount / wordCount)`

  (`statistics.py:get_gunning_fog_index`). The inputs are produced by
  `nltk_lite/textanalyzer.py:24-48`.
- The plugin tokenizes with `(?u)\W+|\$[\d\.]+|\S+`, removes only the special
  tokens `. , ! ?`, and otherwise retains punctuation-bearing tokens after
  removing those four punctuation characters (`nltk_lite/textanalyzer.py:16-20`,
  `24-48`).
- Sentences come from the bundled English tokenizer. Syllables use the bundled
  English fallback counter, with special-word overrides, silent-final-`e`, vowel
  groups, and plugin-specific add/subtract regex rules
  (`nltk_lite/syllables_en.py`). Complex words are words with at least three
  counted syllables, except capitalized words unless they begin a sentence
  (`nltk_lite/textanalyzer.py:86-122`).
- The plugin’s configured readability columns collide in this library: the
  database preferences map Flesch Reading, Flesch Grade, and Gunning Fog to
  `#gfog`. FanFicFare also maps AO3 `numWords` to `#words`. Therefore existing
  values have no per-row provenance, but the formula and configured plugin
  semantics for newly calculated values are established.

### Implication for a no-plugin local implementation

- New calculation code must explicitly implement the established Count Pages
  text selection and Gunning Fog rules rather than invoking Calibre or a plugin
  during backfill.
- The exact installed ICU word-count implementation is not part of this
  repository and is not safe to silently replace with an invented tokenizer.
  The plan must choose either a documented local-compatible fallback or a
  constrained implementation whose differences are tested and surfaced in the
  report. It must not claim bit-for-bit ICU equivalence without evidence.
- Existing `#words` and `#gfog` values must default to preservation. Any local
  metric write mode should be explicit and should distinguish blank values from
  unavailable/malformed calculations.

## Calibre Safety Design and Gaps

- The backfill verifies the seven existing AO3 custom columns through both
  `calibredb custom_columns --details` and read-only SQLite
  (`ao3_backfill.py:1160-1188`).
- Column setup requires Calibre closed and a verified, non-empty backup before
  calling `add_custom_column` (`ao3_backfill.py:1408-1434`).
- Writes require Calibre closed, a verified backup, column verification, report
  revalidation, cache binding, cache validation, and `--approve-write`
  (`ao3_backfill.py:1625-1650`, `1942-1959`). The write path uses only
  `calibredb set_custom` and does not touch standard metadata or EPUBs
  (`ao3_backfill.py:1657-1673`).
- The new category column should follow the same setup/verify/write gates. The
  category contract must be included in schema verification before any network
  request, and category writes must remain custom-column-only.
- Current write behavior can write blank values for incomplete/unavailable
  records when `--allow-partial` is used (`ao3_backfill.py:291-293`,
  `1644-1648`). The approved plan must decide whether to reject such records or
  skip individual unavailable fields so a write cannot erase existing values by
  accident.
- Backup provenance currently verifies the backup’s own manifest, checksum, and
  UUID (`ao3_backfill.py:1344-1369`), but the verifier does not compare the
  manifest’s recorded source hash with the current live database. A fresh
  write-safety backup and an explicit freshness check are required before
  category/local-metric writes.

## Verification Baseline

- The prior reported archiver suite passed with 28 tests.
- The downloader’s supported virtual environment has a separate passing test
  baseline reported by research; the system interpreter may not have all its
  dependencies. This must be rerun without modifying the downloader worktree.
- No network request was made during research.

## Open Decisions for the Grill

1. Is the implementation scope the archiver backfill utility only, or should
   environment authentication also be added to the normal archiver downloader
   and/or the separate `ao3downloadernew` client? Extending either client would
   be a separate integration concern.
2. Should environment authentication be the only new authenticated path while
   retaining the existing explicitly selected local-file path, or should the
   local-file option be removed? The requirement forbids silent fallback, but
   does not require removing the explicit local option.
3. Should the category column be added to the shared `calibre_sync.py` contract,
   or only to the backfill contract to avoid changing the separate EPUB-sync
   behavior? The safer minimal choice is backfill-specific unless the existing
   EPUB path is explicitly included.
4. For local `#words` and `#gfog`, should the default write mode populate only
   blank values, with a separate explicit replacement flag, or should all local
   values be replaced after review? Existing values are populated and their
   provenance is not recoverable.
5. Is it acceptable to implement local word counting using a documented,
   tested standard-library/tokenization fallback while matching Count Pages’s
   spine/body/entity inclusion and Gunning Fog formula, with the report marking
   the tokenizer? Exact ICU equivalence cannot be claimed from repository
   evidence alone.
6. Should a cache record with an unavailable/incomplete AO3 result be eligible
   for a custom-column write at all, or must the write skip that record and
   preserve any existing value? The safe default is skip/preserve.
7. For the first-25 refresh, should the session use environment credentials if
   both variables are present and the explicit flag is approved, otherwise run
   unauthenticated and stop on the restricted work? Network requests must wait
   for a separate explicit approval after the mappings are shown.
