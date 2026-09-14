A python script to help download works from AO3 in bulk. It takes in a folder full of .txt files and will download every work in those .txt files. It was created as a substitute for Calibre's FanFicFare plugin, which does not work when AO3 is protected with Cloudflare.

### Prerequisites
- Python installed on your PC
- https://github.com/nianeyna/ao3downloader. See the repo instructions for how to get it running. Pay attention to the Python version that you need to run ao3downloader in their README (Python 3.11.4). Once you have ao3downloader running, continue with the steps below.

1. 

### Step 1: Grabbing Links
Use ao3downloader to grab all work urls from ao3 search result pages.

1. clone https://github.com/nianeyna/ao3downloader
2. `cd ao3downloader`
3. 
```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
4. `python ao3downloader.py`
5. enter option `l: get all work links from an ao3 listing (saves links only)` in the menu
6. when it asks for an ao3 link, enter the link to your ao3 search results page. Ex. "https://archiveofourown.org/works?commit=Sort+and+Filter&work_search%5Bsort_column%5D=hits&include_work_search%5Brating_ids%5D%5B%5D=10&include_work_search%5Bcategory_ids%5D%5B%5D=23&work_search%5Bother_tag_names%5D=Alternate+Universe&work_search%5Bexcluded_tag_names%5D=&work_search%5Bcrossover%5D=&work_search%5Bcomplete%5D=T&work_search%5Bwords_from%5D=10000&work_search%5Bwords_to%5D=&work_search%5Bdate_from%5D=&work_search%5Bdate_to%5D=&work_search%5Bquery%5D=&work_search%5Blanguage_id%5D=en&tag_id=%EB%B0%A9%ED%83%84%EC%86%8C%EB%85%84%EB%8B%A8+%7C+Bangtan+Boys+%7C+BTS"
7. if it asks you to login, login if you have an account. You dont need to though.
8. Keep doing this for various search results

At the end of this process, you should have a bunch of .txt files in ao3downloader/downloads that are filled with work links.
### Step 2: Downloading Links
Use the script in this repo to download works in bulk. It requires python. It will take in a folder full of `.txt` files and download them all.

1. clone this repository and cd into it
2. 
```
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```
3. create a folder named `links` in the same directory as this repository
4. place all of the txt files from step 1 into the links folder
5. supply your AO3 credentials in either place. Both files are ignored by Git:
   - a `.env` file containing `AO3_USERNAME=` and `AO3_PASSWORD=` (preferred), or
   - a local `personal.ini` copied from `personal.ini.example` (fallback).
6. `python download.py --dry-run` to see the plan, then `python download.py`

For each work, the script fetches the AO3 work page, downloads the EPUB that the
page's Download menu links to, calculates local Words and Gunning Fog from the
file, and writes all of it into the EPUB (see below). Works that are deleted or
hidden in an unrevealed collection are recorded and skipped without spending a
download on them.

#### Options

| Flag | Effect |
| --- | --- |
| `--dry-run` | Show the plan and the estimated finish time without making any request. |
| `--limit N` | Process at most N works. Useful for a first run. |
| `--delay SECONDS` | Seconds between AO3 requests (default 10, minimum 5). A new download costs two paced requests. |
| `--links DIR` / `--output DIR` | Override `./links` and `./downloaded`. |
| `--metadata-only` | Enrich EPUBs already on disk; download nothing. |
| `--refresh-metadata` | Re-fetch AO3 statistics even for EPUBs that are already complete. |
| `--retry-unavailable` | Include works previously found deleted or unrevealed on AO3. |
| `--failure-log PATH` | Where failed and unavailable works are recorded. |
| `--log-dir DIR` | Where to write this run's log file. |

#### Unattended runs

The script is built to be left running:

- A work that fails is recorded in
  `~/.local/share/ao3-calibre-backfill/download-failures.jsonl` and the run moves
  on. Failed works get one more attempt at the end of the run, and any that still
  fail are retried automatically by the next run.
- A problem that affects every work (a Cloudflare challenge, repeated rate
  limiting, a dropped login, several failures in a row) pauses the run — 5, 15,
  30, then 60 minutes — signs in again, and carries on. AO3's `Retry-After` is
  always honoured.
- Rejected credentials stop the run, since retrying them cannot succeed.
- Only one AO3 run can use the account at a time. `download.py` refuses to start
  while an `ao3_backfill.py fetch` (or another download) is running.

The old `download_errors.log` is no longer used as a skip list; the plan reports
how many entries it still holds.

#### Progress and logs

Every run prints a plan up front (link count, duplicates, how many are already
done, and an estimated finish time), then one line per work with a running rate
and ETA, and a summary every 25 works. The same output is written to a
timestamped file under `~/.local/share/ao3-calibre-backfill/logs/`, flushed as
it goes, so a long run can be started with `nohup` and followed with `tail -f`.

Press Ctrl-C to stop at the next safe point; the run prints a summary and exits
130. Rerunning the same command resumes from what is already on disk.

### Metadata for Calibre and Calibre-Web

The EPUB download endpoint does not put AO3 counters such as kudos, hits,
bookmarks, or comments in the EPUB package metadata. The counters are visible
on the AO3 work page, so this project reads that page for each work and adds the
values to the EPUB.

The values are written in three places:

- The EPUB description contains a visible `AO3 statistics` block.
- The EPUB contains `ao3:*` metadata and an `ao3` work identifier, a portable
  record that does not depend on Calibre.
- The EPUB contains `calibre:user_metadata` entries, so **dragging the file into
  Calibre populates the custom columns directly** with no separate sync step.

The columns filled on import are `#ao3_kudos`, `#ao3_hits`, `#ao3_bookmarks`,
`#ao3_comments`, `#ao3_words`, `#ao3_chapters`, `#ao3_status`, `#ao3_category`,
plus `#words` and `#gfog` calculated from the downloaded file. Calibre only
fills columns that **already exist** in the target library, and silently ignores
the rest, so create them first (see the backfill section's `setup-columns`, or
add them in Calibre's Preferences).

New downloads are enriched automatically. To enrich EPUBs that were downloaded
before this feature was added, run:

```text
python download.py --metadata-only
```

This skips EPUBs that are already complete, meaning they carry both the `ao3:*`
metadata and the Calibre column values; files enriched by the earlier version of
this script lack the latter and are re-enriched. Use
`python download.py --metadata-only --refresh-metadata` when you intentionally
want to refresh counters that may have changed on AO3.

#### Numeric sorting and filtering

Kudos cannot be represented by an EPUB standard field, and putting `Kudos: 68`
in Tags would sort it as text rather than as a number. Create Calibre custom
columns and sync the enriched files instead:

```text
python calibre_sync.py --library "/path/to/your/calibre/library" --create-columns
```

The command creates and fills these columns:

- `#ao3_kudos`
- `#ao3_hits`
- `#ao3_bookmarks`
- `#ao3_comments`
- `#ao3_words`
- `#ao3_chapters`
- `#ao3_status`

Use `--dry-run` first if you want to inspect the matches without changing the
Calibre database:

```text
python calibre_sync.py --library "/path/to/your/calibre/library" --dry-run
```

Close Calibre and Calibre-Web before running the sync so neither application is
writing `metadata.db` at the same time. Restart or rescan Calibre-Web after the
sync. Its UI configuration can hide custom columns; make sure the regular
expression for ignored columns does not match `ao3_` if you want to display
them.

In Calibre, sort the library by `AO3 Kudos` descending or search with a numeric
query such as `#ao3_kudos:>100`. Calibre-Web reads the same custom columns from
the Calibre database, so the values are available there as well.

### Existing library backfill

`ao3_backfill.py` handles an existing Calibre library without rewriting EPUBs,
`metadata.opf` files, identifiers, or other standard metadata. Its scan report
and append-only AO3 cache default to
`~/.local/share/ao3-calibre-backfill`, outside this repository.

Run the read-only scan first. It inventories every EPUB and mapping, and by
default records local EPUB word counts and the documented pure-Python Gunning
Fog compatibility metric for the first 25 non-ambiguous work IDs without
rewriting EPUBs:

```text
python ao3_backfill.py scan --library "/path/to/your/calibre/library"
```

Use `--metrics-limit 0` for an inventory-only scan, or pass a larger explicit
limit when you are prepared for the additional sequential EPUB-read time. Use
`--metrics-all` when local `#words`/`#gfog` values are required for every mapped
EPUB before a bulk Calibre write; this can take substantially longer than the
inventory scan.

For the safe default write policy, calculate only the missing local values
instead of recomputing the entire library:

```text
python ao3_backfill.py calculate-missing-metrics \
  --library "/path/to/your/calibre/library" \
  --cache-dir "$HOME/.local/share/ao3-calibre-backfill"
```

This reads EPUBs and Calibre values but does not write Calibre. Existing local
Words/Gfog values remain untouched.

Before creating columns, close Calibre and Calibre-Web and make a verified
backup. The setup command refuses to create a column with the wrong datatype:

```text
python ao3_backfill.py backup --library "/path/to/your/calibre/library"
python ao3_backfill.py setup-columns \
  --library "/path/to/your/calibre/library" \
  --backup "$HOME/.local/share/ao3-calibre-backfill/metadata.db.backup" \
  --approve-columns
```

Column creation changes `metadata.db`, so create another non-overwriting,
provenance-checked backup after setup and use that newer backup for each later
write batch. The freshness gate intentionally rejects a backup whose recorded
source hash no longer matches the live database.

```text
python ao3_backfill.py backup \
  --library "/path/to/your/calibre/library" \
  --destination "$HOME/.local/share/ao3-calibre-backfill/metadata.db.post-category.backup"
```

The backfill custom-column setup also creates and verifies `#ao3_category`
(`AO3 Category`, text) through `calibredb` and read-only SQLite. It never
changes standard Calibre tags.

The fetch command requires an explicit approval flag, defaults to one
sequential request every 30 seconds, and limits the first batch to 25 works.
It checks the custom-column gate before making its first request. An AO3
`Retry-After` response overrides the normal delay with the server-provided
wait, and the cooldown is persisted in the cache context. Repeated rate
limits, Cloudflare responses, authentication failures, or unexpected HTML stop
the process without discarding earlier cache entries:

```text
python ao3_backfill.py fetch --library "/path/to/your/calibre/library" \
  --approve-network --limit 25
python ao3_backfill.py validate-cache
```

Review the exact first-25 refresh mappings without making requests, then take
an external snapshot of the append-only cache before refresh mode:

```text
python ao3_backfill.py snapshot-cache \
  --cache-dir "$HOME/.local/share/ao3-calibre-backfill" \
  --destination "$HOME/.local/share/ao3-calibre-backfill/ao3-cache.before-refresh.jsonl"
python ao3_backfill.py preview-refresh \
  --cache-dir "$HOME/.local/share/ao3-calibre-backfill" \
  --limit 25
```

Run the long fetch in the foreground so request progress, retries, and stop
conditions remain visible. `tee` keeps a copy of the same output in the
external cache directory:

```text
set -o pipefail
python /home/drifter/repos/ao3Archiver/ao3_backfill.py fetch \
  --library "/home/drifter/Calibre Library" \
  --cache-dir "$HOME/.local/share/ao3-calibre-backfill" \
  --approve-network --use-env-credentials --retry-failed-once \
  --continue-after-review --delay 30 --limit 14800 \
  2>&1 | tee "$HOME/.local/share/ao3-calibre-backfill/fetch-full.log"
fetch_status=${PIPESTATUS[0]}
printf 'fetch exit status: %s\n' "$fetch_status"
```

Do not start a second fetch process while this one is running. The cache lock
prevents concurrent cache access, but one foreground process is the intended
operational model. Already cached work IDs are not requested again unless
`--refresh` is explicitly supplied.

`--retry-failed-once` retries incomplete/unavailable cache records and an
unexpected work-page response once. A second failure, repeated Cloudflare,
repeated rate limiting, failed authentication, or malformed HTML stops the
batch rather than continuing blindly. The cache is durable, so after reviewing
the stop reason you can resume with the same command and the scheduler will
honor the persisted request timestamp/cooldown.

Add `--include-ambiguous` to both fetch commands only after reviewing the 11
multi-work prefaces in the scan report.

For an explicitly approved authenticated refresh, put `AO3_USERNAME` and
`AO3_PASSWORD` in the ignored repository-local `.env` file or in the process
environment, then opt in with `--use-env-credentials`. Process environment
values take precedence. The backfill does not fall back to `personal.ini`:

```text
python ao3_backfill.py fetch --library "/path/to/your/calibre/library" \
  --approve-network --use-env-credentials --refresh --limit 25
```

Do not place the values in command history, logs, reports, cache records, or
chat. A successful login is verified by an authenticated cookie or an
unambiguous logged-in response. If a work request is redirected to login or
returns HTTP 401/403, the backfill performs at most one reauthentication
attempt for that work. Failed reauthentication stops the batch while
preserving earlier cache records.

After reviewing the cache, use the explicit write approval. The default write
is one book so its values can be checked in Calibre before a larger batch:

```text
python ao3_backfill.py write --library "/path/to/your/calibre/library" \
  --backup "$HOME/.local/share/ao3-calibre-backfill/metadata.db.post-category.backup" \
  --approve-write --allow-partial --limit 1
```

Zero counters are stored as numeric zero; unavailable counters are skipped and
existing custom-column values are preserved. EPUBs whose preface contains
multiple work IDs are reported as ambiguous and skipped unless
`--include-ambiguous` is supplied for both fetch and write review.

To make the metadata portable inside existing EPUBs, use the separate explicit
EPUB-enrichment stage. It writes only namespaced `ao3:*` metadata and creates a
non-overwriting external backup for each original EPUB; it does not replace
title, author, tags, identifiers, comments, series, covers, or formats:

```text
python ao3_backfill.py enrich-epubs \
  --library "/path/to/your/calibre/library" \
  --cache-dir "$HOME/.local/share/ao3-calibre-backfill" \
  --backup "$HOME/.local/share/ao3-calibre-backfill/metadata.db.current.backup" \
  --epub-backup-dir "$HOME/.local/share/ao3-calibre-backfill/epub-originals" \
  --approve-epub-write --allow-partial --limit 1
```

Run the one-book enrichment first, inspect the EPUB and its portable metadata,
then run the approved bulk enrichment before the Calibre custom-column write.

Local `#words` and `#gfog` values are separate from AO3 `#ao3_words`. They are
calculated from all EPUB spine body text, including AO3 prefaces, title pages,
chapter headings, author notes, and end notes. The default local write mode
fills only blank `#words`/`#gfog` cells and requires an explicit flag in
addition to `--approve-write`:

```text
python ao3_backfill.py write --library "/path/to/your/calibre/library" \
  --backup "$HOME/.local/share/ao3-calibre-backfill/metadata.db.post-category.backup" \
  --approve-write --allow-partial --write-local-metrics --limit 1
```

The report identifies the local algorithm as
`count-pages-compatible-pure-python-v1`. It follows the established Count
Pages Gunning Fog formula and English syllable rules, with a deterministic
punctuation sentence profile that guards common abbreviations and decimals. It
does not claim bit-for-bit equivalence to Calibre's bundled sentence tokenizer
or ICU word counting. Use
`--replace-local-metrics` only after explicitly reviewing existing-value
provenance.
