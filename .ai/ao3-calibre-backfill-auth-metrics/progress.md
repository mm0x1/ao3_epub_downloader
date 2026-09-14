# AO3 Calibre Backfill: Authentication and Local Metrics

Task slug: `ao3-calibre-backfill-auth-metrics`

Planning artifacts are working state only and must not be committed.

## Phases

- [completed] 0. Set up state and inspect worktrees, Calibre, and existing artifacts
- [completed] 1. Research authentication, category parsing, local metrics, and Calibre write patterns
- [completed] 2. Grill unresolved decisions with the user
- [completed] 3. Write and obtain approval for the implementation plan
- [completed] 4. Decide whether the work must be split into sequential PR-sized changes
- [completed] 5. Implement only after the plan and gates are approved
- [in progress] 6. Reinspect, test, review, and perform only explicitly approved refresh/write actions

## Safety constraints

- Preserve all pre-existing worktree changes in both repositories.
- Never print, persist, or modify credentials.
- Keep network requests and Calibre writes behind explicit gates.
- Do not commit, push, reset, clean, checkout, remove, or overwrite unrelated changes.

## Live validation state

- Fresh no-network scan completed with complete inventory/mapping diagnostics and
  `--metrics-limit 25`; all 25 first non-ambiguous work IDs have local metrics.
- External cache snapshot created before refresh:
  `ao3-cache.before-refresh-20260911T203829Z.jsonl`.
- `ao3_category` was created and verified as an empty `text` custom column
  through `calibredb` and read-only SQLite after explicit approval.
- Fresh post-category backup is valid at
  `metadata.db.post-category-20260911T203829Z.backup`.
- The repository-local `.env` is now present, ignored, and mode `0600`; the
  explicit refresh loaded it successfully without exposing values.
- The approved refresh made six successful sequential requests, then stopped on
  unexpected HTML for work `59117515`; the remaining previewed IDs were not
  requested.
- The failed work's prior incomplete cache record is unchanged, and the
  pre-refresh snapshot still preserves the original cache contents.
- Calibre AO3 custom-column population is one book after the approved test
  write; no bulk or local-metric overwrite has been performed.
- One-book live write completed for Calibre book 27876 / work 65995504. Eight
  AO3/category fields were written and dual readback verified; local Words/Gfog
  values were preserved because those existing cells were already populated.
- The user requested stopping rather than retrying the full-library metric pass;
  the bounded scan approach is now the default operational path.

## Initial observations

- Archiver has pre-existing modified and untracked files, including `ao3_backfill.py`,
  `ao3_metadata.py`, `calibre_sync.py`, and their tests.
- Downloader has pre-existing edits across its parser, AO3, actions, and tests.
- Existing downloader category extraction is in `ao3downloader/parse_soup.py`.
- Existing archiver `AO3Metadata` has no category field yet.
- No `.ai` state directory existed before this task.
