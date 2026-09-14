# Decisions

These decisions were provided by the user during the grill phase. No network
requests or Calibre writes were authorized by these answers.

## Scope

- Change the archiver backfill utility only.
- Do not extend the normal `download.py` flow or the separate
  `ao3downloadernew` client in this task.
- Preserve all existing changes in both repositories.

## Authentication

- Use environment credentials only for the backfill authentication path.
- Add the explicit `--use-env-credentials` opt-in.
- Do not silently fall back to `personal.ini`.
- The existing ignored credential file remains local and untouched; the
  backfill CLI should not use the old local-file option after this change.
- The later first-25 refresh may use environment credentials only after the
  mapping review and a separate explicit network approval. No credentials are
  requested in chat.

## Categories

- Extend the shared `AO3Metadata` parser/model and the AO3 cache-facing model.
- Use the full work-page `dd.category a` selector pattern and preserve ordered
  visible category text such as `F/F, M/M`.
- Add the category to the backfill custom-column contract as
  `ao3_category` / `AO3 Category` / `text`.
- Do not change standard Calibre tags or the separate enriched-EPUB sync
  contract.

## Local Metrics

- Try to identify an already-installed Python package that can provide the
  required Unicode word-count behavior without relying on a Calibre plugin.
  The current archiver and downloader virtual environments have no external
  `icu`/`PyICU`/`regex`/`nltk` word-count package available. Calibre’s embedded
  runtime has ICU, but the implementation must not depend on Calibre or its
  Count Pages plugin.
- If no suitable package is available, implement a deterministic pure-Python
  compatibility path based on the evidenced Count Pages behavior, document its
  tokenizer identity and limitations, and test it. Do not claim exact ICU
  equivalence.
- Use the evidenced EPUB spine/body/entity inclusion rules and the established
  Gunning Fog formula/syllable rules rather than inventing a new metric.
- Populate local `#words` and `#gfog` only when those cells are blank by
  default. Any replacement behavior requires its own explicit flag and later
  approval.
- Preserve existing values when a local metric is unavailable or malformed.

## Write Safety

- Unavailable/incomplete AO3 values are skipped and existing Calibre values are
  preserved. They must never become numeric zero.
- Category and local metrics use the same read-only schema, backup, closed
  process, explicit approval, one-book test, and post-write verification gates
  as the existing AO3 columns.
- The implementation must not modify standard Calibre metadata, EPUB files, or
  unrelated custom columns.

## Still-Gated Actions

- The implementation plan requires explicit approval before coding.
- Network access requires a later explicit approval after the first 25
  non-ambiguous mappings are shown.
- Calibre writes require a separate explicit approval after refreshed cache
  validation.
- No commit or push is authorized.

## Unattended Refresh Policy

- The long-running fetch may explicitly use `--retry-failed-once`.
- Incomplete/unavailable records and unexpected HTML receive one bounded retry;
  a second failure, repeated Cloudflare/rate-limit response, failed auth, or
  malformed response stops the process rather than continuing blindly.
- `--metrics-all` is required before a bulk local Words/Gfog write; the default
  bounded scan is intentionally only the first 25 non-ambiguous work IDs.
