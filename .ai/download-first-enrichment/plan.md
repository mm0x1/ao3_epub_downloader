# Download-First Enrichment Plan

Status: working state only. Do not commit. Supersedes
`.ai/ao3-calibre-backfill-auth-metrics/future-download-metadata-plan.md` for
everything concerning new downloads.

## The change

The Calibre backfill is a **one-time** operation against the existing 14,831-book
library. After it finishes, it is retired. All future books arrive through the
download flow:

1. Harvest work links with `../ao3downloadernew` (menu option `l`).
2. Run this repo's `download.py` over the resulting directory of link files. It
   downloads each work and bakes every piece of metadata into the EPUB.
3. Drag and drop the finished EPUBs into Calibre.

The decisive consequence: **step 3 must populate the Calibre custom columns by
itself.** There is no `calibredb` write, no post-import sync, and no second tool
run. The EPUB has to carry its own Calibre metadata.

## Why this works (verified, not assumed)

Calibre's importer reads custom-column values out of an EPUB's OPF from
`calibre:user_metadata:#<label>` meta tags. This was confirmed against the
installed Calibre rather than taken on faith:

- `calibre.ebooks.metadata.opf2.OPF.read_user_metadata` selects with the XPath
  `//*[name() = "meta" and starts-with(@name,"calibre:user_metadata:") and @content]`.
- An EPUB carrying those tags, added with `calibredb add` to a throwaway library,
  populated all ten columns with correct `int`, `float`, and `text` types.

Two constraints fell out of that verification, and both are now encoded in code
and tests:

- **The meta element must not carry a namespace prefix.** The XPath matches the
  *qualified* name, so `<opf:meta …>` is invisible to Calibre while `<meta …>`
  is read. Whether ElementTree emits a prefix depends on how the source OPF
  declared its namespaces, so `ao3_metadata._set_plain_meta_value` writes these
  tags unqualified. This was a real defect found during implementation: the
  first end-to-end attempt produced a file Calibre imported with every column
  empty.
- **Calibre never creates a missing column on import.** Values for columns that
  do not already exist are dropped silently. The target library must define them
  first.

The target library already defines all of them:

```
pages (1)  words (2)  gfog (3)  ao3_kudos (4)  ao3_hits (5)  ao3_bookmarks (6)
ao3_comments (7)  ao3_words (8)  ao3_chapters (9)  ao3_status (10)  ao3_category (11)
```

## What a downloaded EPUB now carries

`download.py` writes three layers into every EPUB it touches:

| Layer | Purpose | Read by |
| --- | --- | --- |
| Standard `dc:*` + AO3 identifier + description block | Title, author, summary, visible statistics | Calibre, any reader |
| `ao3:*` namespaced metas | Portable record that survives outside Calibre | This repo's `read_ao3_metadata` |
| `calibre:user_metadata:#*` metas | The ten custom columns | Calibre's importer |

The column contract is `calibre_sync.DOWNLOAD_CUSTOM_COLUMNS`:

`#ao3_kudos`, `#ao3_hits`, `#ao3_bookmarks`, `#ao3_comments`, `#ao3_words`,
`#ao3_chapters`, `#ao3_status`, `#ao3_category` (from the AO3 work page), plus
`#words` and `#gfog` (calculated locally from the downloaded EPUB).

Absent values are omitted rather than written as empty, so a work page missing a
status does not blank an existing cell.

## Done in this pass

- `run_log.py` — shared logging: per-run log file, flushed terminal output,
  progress with rate and wall-clock ETA, credential redaction, signal handling.
- `credentials.py` — one credential path (`.env` / environment, `personal.ini`
  fallback) shared by both entry points.
- `ao3_metadata.py` — `_calibre_user_metadata` plus the unprefixed-meta writer;
  `enrich_epub(..., calibre_columns=...)`.
- `calibre_sync.py` — `DOWNLOAD_CUSTOM_COLUMNS`.
- `download.py` — rewritten around an explicit plan: dedupe, honest counts,
  `--limit`, `--dry-run`, `--retry-failed`, local metrics, full enrichment,
  resumable, interruptible.
- `ao3_backfill.py` — progress/ETA logging, split locks, per-cause login
  diagnostics, `check-auth`.
- Tests: 63 → 128.

## Remaining work

### Phase 1 — prove the flow on real data (no code changes expected)

1. `python3 ao3_backfill.py check-auth --approve-network` — confirm credentials
   in one round trip.
2. `python3 download.py --dry-run` — confirm the plan and counts.
3. `python3 download.py --limit 5` — download five works.
4. Drag those five into a **scratch Calibre library** that has the eleven
   columns, and confirm every column populates.
5. Only then run the full set.

Step 4 is the gate. Do not start a multi-day run before a real EPUB has made it
into a real library with its columns filled.

### Phase 2 — finish the backfill, then retire it

The backfill and the downloader both write to AO3 at 30 s and 15 s pacing
respectively. **Never run them at the same time**; AO3 sees one account. Finish
the backfill first (~123 h at the current delay), verify, then switch over.

### Phase 3 — `#pages`

`local_metrics.py` computes Words and Gfog but not Pages, so `#pages` stays
empty on import. Options, in order of preference:

1. Leave it, and let Calibre's Count Pages plugin fill it after import.
2. Add a pages estimate to `local_metrics.py` and an eleventh column entry.

Option 1 needs no code. Pick 2 only if the plugin turns out to be inconvenient
at this volume. Either way this is a decision to make deliberately, not a gap to
paper over: a fabricated page count is worse than an empty cell.

### Phase 4 — the `ao3downloadernew` seam

Keep that repo as a link harvester only. It writes `links/*.txt`; this repo
consumes the directory. That is the entire contract, and it needs no code on
either side.

Its optional metadata CSV is **not** wired in and should stay that way: the
download path reads the live work page anyway, so a CSV would be a second,
staler source of the same fields. Revisit only if the CSV ever carries something
the work page does not.

## Download flow hardening (done while the backfill ran)

`download.py` was rebuilt on the backfill's request layer, without changing how
the backfill behaves. Nothing was requested from AO3 while the fetch was running;
all verification was offline.

- **Page first, then EPUB.** The work page is fetched with `ao3_backfill.AO3Fetcher`
  (unmodified; a subclass in `download_client.py` keeps the final page HTML).
  The EPUB is taken from the page's own Download menu link — the approach
  `ao3downloadernew` uses — instead of a hardcoded `download.archiveofourown.org`
  URL. Deleted and Mystery Works are recorded before a download is spent.
- **Same rules for the EPUB request** (`AO3DownloadClient.download_epub`):
  Cloudflare challenge vs origin error, rate-limit budget with exact
  `Retry-After`, transient budget, one re-sign-in, and an HTML page in place of
  an EPUB treated as a failure. Login markers are only checked on HTML
  responses, since story text can contain "please log in".
- **Unattended by default** (`download_client.run_work_queue`): per-work failures
  are recorded and skipped, AO3-wide problems pause and re-sign-in, failures get
  a retry pass, rejected credentials stop the run.
- **Account lock.** `download.py` takes the backfill's run lock, so the two can
  never run against AO3 at once. Its dry run warns when the lock is held.
- **Failure log** is JSONL at `~/.local/share/ao3-calibre-backfill/download-failures.jsonl`.
  Only `permanent` entries (404/410/Mystery Work) are skipped by later runs;
  `download_errors.log` is no longer a skip list.
- **Completeness** now requires Calibre column values, so EPUBs enriched by the
  old downloader get re-enriched; a corrupt existing file is re-downloaded
  instead of enriched in place.
- `--delay` (default 10 s, floor 5 s) replaced the fixed 15 s + 200 s/50 pacing.
- Shared-code changes, both additive with unchanged defaults: an `allowed_hosts`
  parameter on the redirect follower, and `ao3_metadata.has_calibre_user_metadata`.

**Verified offline end to end:** the real `run_downloads` → real client → real
fetcher → real enrichment, fed your real AO3-generated `64805.epub` as the
download, imported with `calibredb add` into a scratch library: all ten columns
filled, and Calibre also mapped the `ao3` identifier to the work URL (this
answers open question 2 below).

**Decided: `#ao3_status` stays a bare date.** The parser keeps AO3's
`Completed:`/`Updated:` date but not the label, and oneshots have no status row
(38% of works). Deriving "Completed"/"In progress" from `#ao3_chapters` was
offered and declined; completeness remains searchable through `#ao3_chapters`.

**Still owed:**
1. A live `--limit 5` run plus a drag-and-drop into a scratch library, **after
   the backfill finishes**. The EPUB-link fallback route and whether AO3
   redirects downloads to `download.archiveofourown.org` are unverified live.
2. Once the backfill is retired, fold `fetch_pending`'s keep-going loop onto
   `run_work_queue`, and move the AO3 client pieces out of `ao3_backfill.py`
   into one module. Both were deferred so a backfill restart could not pick up
   a refactor mid-run.

## Operating the two long runs

Both tools now write their own timestamped log file under
`~/.local/share/ao3-calibre-backfill/logs/` and flush every line, so `python -u`
and `tee` are no longer needed.

```bash
# Backfill (one-time, ~40 h at 10 s once chapter redirects are unpaced):
nohup python3 ao3_backfill.py fetch \
  --library "/home/drifter/Calibre Library" \
  --use-env-credentials --approve-network --retry-failed-once \
  --continue-after-review --delay 10 --limit 14800 --keep-going &

# Downloads (ongoing, ~169 h for the current 32,525 links):
nohup python3 download.py &

# Watch either one:
tail -f ~/.local/share/ao3-calibre-backfill/logs/<command>-<timestamp>.log
```

Ctrl-C (or `kill`) stops at the next safe point, prints a summary, and exits
130. Both are resumable: rerun the same command. The backfill resumes from its
append-only cache; the downloader re-plans from what is already on disk.

While a fetch runs, `validate-cache` and `preview-refresh` work from another
terminal — the run-scoped lock and the short cache lock are now separate.

## Cloudflare and transient failures

AO3 emits passing 5xx errors routinely, including Cloudflare's origin range
(520-530). A one-off `HTTP 525` at login does **not** mean AO3 is down and has
nothing to do with credentials — `../ao3downloadernew/ao3downloader/repo.py`
treats exactly this set as retryable and, with its default `max_retries = 0`,
retries indefinitely with a 30 s-capped backoff.

This repo now follows that lead, with one deliberate difference:

- **Origin/transient 5xx** — retried with capped exponential backoff, up to
  `MAX_TRANSIENT_ATTEMPTS` (8), at both the login and per-work level. The budget
  is separate from `max_attempts` so `--retry-failed-once` cannot shrink outage
  tolerance to a single retry.
- **Rate limits (429)** — AO3's exact `Retry-After` is honoured (typically a
  five-minute pause), with its own budget of `MAX_RATE_LIMIT_ATTEMPTS` (10).
  Waiting out backpressure is the correct response, so this deliberately
  relaxes the older "stop on repeated rate limiting" rule.
- **Cloudflare challenges** — never retried. `Just a moment...`,
  `cf-browser-verification`, `_cf_chl_opt` and friends mean Cloudflare made a
  decision, and retrying into it is what gets an address blocked. These still
  stop the run, per the existing safety policy.
- `id="cf-wrapper"` is deliberately **not** a challenge marker: it wraps every
  Cloudflare error page including the 5xx ones. Treating it as a challenge
  reported a transient blip as a bot block and made it fatal.

### Findings from the first live run (stopped at 405/14,777)

- **Mystery Works.** Work `62373328` returned HTTP 200 at its own URL with only
  `<p class="notice">This work is part of an ongoing challenge and will be
  revealed soon!</p>` — no preface, stats, or chapters. The recogniser treated
  that as unexpected HTML and, with `--retry-failed-once`, stopped the run. It
  is now cached as `unavailable` with the collection path in `error`, so writes
  skip it and a later `--retry-failed-once` run re-checks it. Unknown pages
  still stop the run, but the message now includes the page `<title>`.
- **Redirect pacing doubled the run.** Nearly every `/works/<id>` 302s to
  `/works/<id>/chapters/<n>`, and the scheduler waited the full delay before
  that 0.1 s hop. A live trace measured 20.1 s per work at `--delay 10`. That
  one hop is now followed immediately (any other redirect is still paced, and a
  `Retry-After` is still served); the same trace measured 9.2 s.
- **No rate limiting** at 10 s across 405 works: zero 429s in the log.

### Unattended runs: `--keep-going`

Requested so the backfill can run unattended and do as much as it can. Without
the flag the old stop-on-failure behaviour is unchanged.

- **One work fails** (unrecognised page, unexpected status, exhausted transient
  retries, even an unexpected code error): the failure is appended to
  `ao3-cache.jsonl.failures.jsonl` and the run moves on. Failures never go into
  the cache itself, where the latest record per work wins and a failure could
  hide a good observation. The work stays uncached, so the next run retries it.
- **Failed works get one more attempt** after the main pass.
- **AO3-wide problems** (Cloudflare challenge, exhausted rate-limit budget,
  authentication failure): cool down, sign in again with a clean cookie jar,
  and retry the same work. If that same work fails the same way straight after
  recovering, it is blamed on the work instead, so one odd work cannot hold the
  run in cool-downs forever.
- **Five failures in a row** are treated as AO3-wide too.
- Cool-downs escalate 5 m → 15 m → 30 m → 60 m and stay at 60 m. **Only a cached
  work resets them**; a successful sign-in does not, so a lasting work-page
  outage settles into hourly probes instead of draining the queue into failures.
- **Rejected credentials still stop the run** (`CredentialsRejected`): retrying
  known-bad credentials cannot succeed.
- `validate-cache` reports failures that are not yet cached.

If throttling does show up, the downloader's README points at the IP rather than
the client: the practical remedy is a different IP (and note that most VPN exit
addresses are already on Cloudflare's list, so turning a VPN *off* helps more
often than turning one on).

## Risks

| Risk | Mitigation |
| --- | --- |
| A column is renamed or deleted in Calibre | Import silently drops that value. Re-check `calibredb custom_columns` before a bulk import. |
| Both tools run at once | AO3 rate-limits or blocks the account. Run one at a time; the backfill's fetch lock only guards against a second *fetch*. |
| A work is deleted or locked on AO3 | Recorded in `download_errors.log` and skipped next run; `--retry-failed` re-includes it. |
| Calibre changes its user_metadata XPath | The regression test asserts the unprefixed-meta contract and would fail loudly. |
| Drag-and-drop creates duplicates | Calibre matches on title/author, not the AO3 identifier. Import into a scratch library first if unsure. |

## Open questions

1. `#pages` — plugin after import, or computed locally? (Phase 3)
2. ~~Does Calibre map the EPUB's `ao3` scheme identifier on import?~~ **Yes** —
   verified with `calibredb add`: `identifiers: {"ao3": "https://archiveofourown.org/works/<id>"}`.
3. The 15 s / 200 s-per-50 download pacing in `download.py` is inherited, not
   derived from any stated AO3 limit. `ao3downloadernew`'s `settings.ini` uses
   `ExtraWaitTime=0` between work hits and reserves its 30 s
   (`LinkPageWaitTime`) for listing pages only, which suggests the download
   pacing could come down substantially too. The backfill's `--delay` floor is
   now 5 s with a 30 s default; `download.py` has no equivalent knob yet.
