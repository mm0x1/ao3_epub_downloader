# ao3Archiver

Bulk-download works from Archive of Our Own as EPUBs that arrive in Calibre
with their AO3 statistics already filled in: kudos, hits, bookmarks, comments,
word count, chapters, status, and category, plus a local word count and Gunning
Fog readability score calculated from the file itself.

It started as a substitute for Calibre's FanFicFare plugin, which cannot get
past AO3's Cloudflare protection.

- [How it works](#how-it-works)
- [Setup](#setup)
- [1. Harvest links](#1-harvest-links)
- [2. Download](#2-download)
- [3. Import into Calibre](#3-import-into-calibre)
- [What goes into each EPUB](#what-goes-into-each-epub)
- [Backfilling an existing Calibre library](#backfilling-an-existing-calibre-library)
- [Files outside the repository](#files-outside-the-repository)
- [Project layout](#project-layout)
- [Development](#development)

## How it works

1. **Harvest links** with [ao3downloader](https://github.com/nianeyna/ao3downloader):
   it saves every work link from AO3 search results as `.txt` files.
2. **Download** with `download.py`. For each link it reads the AO3 work page,
   downloads the EPUB, and bakes all of the work's metadata into the file.
3. **Drag the EPUBs into Calibre.** The custom columns fill in on import, with
   no sync step.

For a library that already holds thousands of AO3 EPUBs, `backfill.py` fills
the same columns in place, once.

## Setup

**Python 3** (developed and tested on 3.14) with `requests`:

```sh
git clone <this repository> && cd ao3Archiver
python3 -m venv venv && source venv/bin/activate   # optional
pip install -r requirements.txt
```

**AO3 credentials** go in a `.env` file in the repository root. It is ignored by
Git, and neither tool ever prints or stores the values:

```sh
AO3_USERNAME=your-username
AO3_PASSWORD=your-password
```

`AO3_USERNAME` and `AO3_PASSWORD` in the environment override the file.
`download.py` also falls back to a `personal.ini` copied from
`personal.ini.example`; the backfill does not.

**Calibre custom columns** must exist before you import anything. Calibre
silently drops values for columns a library does not have. Create them once,
either in *Preferences → Add your own columns* or with
`backfill.py setup-columns` (see [Backfilling](#backfilling-an-existing-calibre-library)):

| Lookup name | Heading | Type |
| --- | --- | --- |
| `ao3_kudos` | AO3 Kudos | Integers |
| `ao3_hits` | AO3 Hits | Integers |
| `ao3_bookmarks` | AO3 Bookmarks | Integers |
| `ao3_comments` | AO3 Comments | Integers |
| `ao3_words` | AO3 Words | Integers |
| `ao3_chapters` | AO3 Chapters | Text |
| `ao3_status` | AO3 Status | Text |
| `ao3_category` | AO3 Category | Text |
| `words` | Words | Integers |
| `gfog` | Gfog | Floating point numbers |

## 1. Harvest links

1. Set up [ao3downloader](https://github.com/nianeyna/ao3downloader) following its
   README, including the Python version it asks for.
2. Run `python ao3downloader.py` and choose
   `l: get all work links from an ao3 listing (saves links only)`.
3. Paste the URL of an AO3 search results page. Logging in is optional.
4. Repeat for as many searches as you like.

Copy the resulting `.txt` files from `ao3downloader/downloads` into a `links`
folder in this repository. Duplicate links across files are fine.

## 2. Download

Preview the plan first, then run it:

```sh
python3 download.py --dry-run
python3 download.py
```

The plan shows how many links there are, how many are duplicates, how many are
already done, and an estimated finish time. Every run after that resumes
automatically: finished EPUBs are skipped, and half-finished ones are completed.

For each work, the script:

1. reads the AO3 work page;
2. skips the work if AO3 reports it deleted, or hidden in an unrevealed
   challenge collection ("Mystery Work"), and records that;
3. downloads the EPUB from the page's own Download link;
4. calculates local Words and Gunning Fog from the file;
5. writes everything into the EPUB and reads it back to confirm Calibre will see it.

| Option | Effect |
| --- | --- |
| `--dry-run` | Show the plan and estimated finish time; make no requests. |
| `--limit N` | Process at most N works. Useful for a first run. |
| `--delay SECONDS` | Seconds between AO3 requests (default 10, minimum 5). A new download costs two paced requests. |
| `--links DIR` / `--output DIR` | Read links from, and save EPUBs to, somewhere other than `links/` and `downloaded/`. |
| `--metadata-only` | Enrich EPUBs already on disk; download nothing. |
| `--refresh-metadata` | Re-read AO3 statistics even for EPUBs that are already complete. |
| `--retry-unavailable` | Include works previously found deleted or unrevealed. |
| `--failure-log PATH` | Where failed and unavailable works are recorded. |
| `--log-dir DIR` | Where this run's log file goes. |

### Leaving it running

A download of tens of thousands of works takes days, so the script is built to
run unattended:

- **A work that fails** is recorded and skipped. Failed works get one more try at
  the end of the run, and the next run tries them again.
- **A problem that affects every work** — a Cloudflare challenge, repeated rate
  limiting, a dropped login, or five failures in a row — pauses the run for 5,
  15, 30, then 60 minutes, signs in again, and carries on. AO3's `Retry-After`
  is always honoured, and a passing AO3 error (HTTP 5xx, including Cloudflare's
  525) is simply retried.
- **Rejected credentials stop the run**, since retrying them cannot succeed.
- **Only one AO3 run can use the account at a time.** `download.py` refuses to
  start while a backfill fetch or another download is running.

Every line of output is also written, as it happens, to a timestamped log file,
so a long run can be started in the background and followed:

```sh
nohup python3 download.py &
tail -f "$(ls -t ~/.local/share/ao3-calibre-backfill/logs/download-*.log | head -1)"
```

Press Ctrl-C (or `kill` the process) to stop at the next safe point; the run
prints a summary and exits. Run the same command again to resume.

## 3. Import into Calibre

Drag the EPUBs from `downloaded/` into Calibre, or add them with
`calibredb add`. The ten custom columns fill in on import, and each book also
gets its AO3 work URL as an `ao3` identifier.

Sort by **AO3 Kudos**, or search numerically: `#ao3_kudos:>1000`,
`#words:<20000`, `#gfog:<8`. `#ao3_chapters:"~[?]"` finds works whose final
chapter count is still unknown (`3/?`), which covers most works in progress; a
work with a declared total, such as `3/10`, does not match. (A plain `"?"` matches
every book.)

**Calibre-Web** reads the same columns from Calibre's database. Restart it or
rescan after importing, and check that its setting for ignored custom columns
does not match `ao3_`.

## What goes into each EPUB

AO3's EPUB export does not include the work's statistics, so they are read from
the work page and written into the file in three forms:

- **Calibre column values** (`calibre:user_metadata`), which fill the custom
  columns on import;
- **portable `ao3:*` metadata** and an `ao3` identifier, which travel with the
  file outside Calibre;
- **a visible "AO3 statistics" block** in the description, shown in Calibre's
  comments.

| Column | From | Notes |
| --- | --- | --- |
| `#ao3_kudos`, `#ao3_hits`, `#ao3_bookmarks`, `#ao3_comments` | AO3 work page | AO3 omits a Kudos, Comments, or Bookmarks row when the count is zero, so a missing row is recorded as 0. |
| `#ao3_words` | AO3 work page | AO3's own count. |
| `#ao3_chapters` | AO3 work page | For example `12/12`, or `3/?` while in progress. |
| `#ao3_status` | AO3 work page | The date AO3 shows as *Completed* or *Updated*. One-chapter works have none. |
| `#ao3_category` | AO3 work page | For example `F/F` or `Gen, M/M`. |
| `#words` | the EPUB | Every spine body, so it includes AO3's preface and notes. |
| `#gfog` | the EPUB | Gunning Fog index, two decimals (`count-pages-compatible-pure-python-v1`). |

Nothing is written for a value AO3 does not show, so a missing statistic never
blanks a column.

## Backfilling an existing Calibre library

`backfill.py` fills the same columns for EPUBs already in a Calibre library. It
is a one-time job. It never rewrites EPUBs, `metadata.opf` files, tags,
identifiers, or other standard metadata. Every stage that talks to AO3 or
changes the library needs an explicit `--approve-…` flag.

Close Calibre and Calibre-Web first. The commands that change the library check
this and refuse to run otherwise.

**1. Scan the library** (read-only). This maps every EPUB to its AO3 work from
the preface, and saves a report:

```sh
python3 backfill.py scan --library "/path/to/Calibre Library"
```

Local metrics are calculated for the first 25 works by default. Use
`--metrics-limit N`, or `--metrics-all` for every EPUB, if you want more.
`calculate-missing-metrics` later fills just the blanks, which is usually all
that is needed.

**2. Create the columns**, after a verified backup of `metadata.db`:

```sh
python3 backfill.py backup --library "/path/to/Calibre Library"
python3 backfill.py setup-columns --library "/path/to/Calibre Library" \
  --backup "<path printed by backup>" --approve-columns
```

**3. Check the login** with one round trip before starting a long fetch:

```sh
python3 backfill.py check-auth --approve-network
```

**4. Fetch AO3 statistics** into an append-only cache. At 10 seconds per work,
about 15,000 works take roughly two days:

```sh
nohup python3 backfill.py fetch --library "/path/to/Calibre Library" \
  --use-env-credentials --approve-network \
  --continue-after-review --keep-going --delay 10 --limit 15000 &
```

- Without `--continue-after-review` a batch is capped at 25 works, so you can
  review a small run first. `--delay` defaults to 30 seconds, with a minimum of 5.
- `--keep-going` makes the run unattended, with the same failure handling as
  [downloads](#leaving-it-running). Without it, the first failure stops the run.
- `--retry-failed-once` also re-requests works that came back incomplete or
  unavailable, and retries an unrecognised page once.
- `--refresh` re-fetches works that are already cached, and
  `--include-ambiguous` includes EPUBs whose preface links more than one work.
- Every result is written to disk as it arrives. To resume after a stop, run
  the same command again.

Check progress from another terminal at any time:

```sh
python3 backfill.py validate-cache
```

**5. Write to Calibre.** Fill in any missing local metrics, take a backup
immediately before writing, and write one book first. Each `backup` creates a
new timestamped file and prints the exact `--backup "…"` to pass next; backups
are never overwritten.

```sh
python3 backfill.py calculate-missing-metrics --library "/path/to/Calibre Library"
python3 backfill.py backup --library "/path/to/Calibre Library"
python3 backfill.py write --library "/path/to/Calibre Library" \
  --backup "<path printed by backup>" \
  --write-local-metrics --approve-write --limit 1
```

Check that book in Calibre. Then take a fresh backup, since any write
invalidates the previous one, and write the rest:

```sh
python3 backfill.py backup --library "/path/to/Calibre Library"
python3 backfill.py write --library "/path/to/Calibre Library" \
  --backup "<path printed by backup>" \
  --write-local-metrics --approve-write --limit 20000
python3 backfill.py verify-library --library "/path/to/Calibre Library"
```

- The write uses Calibre's own API in a single `calibre-debug` process, one call
  per column: about 110,000 values in seconds. Every value is then read back
  through both `calibredb` and SQLite.
- Only works that came back complete are written, and empty values are never
  written.
- `--write-local-metrics` fills `#words`/`#gfog` only where they are blank. Add
  `--replace-local-metrics` to overwrite existing values. Add `--allow-partial`
  if some works are not cached.
- The write refuses to start unless the backup matches the current
  `metadata.db` exactly. Calibre empties its trash once a day whenever a library
  is opened, which counts as a change. If that happens between the backup and
  the write, take a new backup.
- Calibre refreshes each book's `metadata.opf` the next time it opens the
  library, just as it does after any `calibredb` edit.

**Optional: portable EPUB metadata.** `enrich-epubs` writes the `ao3:*` metadata
into the library's EPUB files themselves, backing up each original first:

```sh
python3 backfill.py enrich-epubs --library "/path/to/Calibre Library" \
  --backup "<path printed by backup>" \
  --epub-backup-dir ~/.local/share/ao3-calibre-backfill/epub-originals \
  --approve-epub-write --limit 1
```

Run `python3 backfill.py <command> --help` for every option.

## Files outside the repository

Logs, caches, and backups live in `~/.local/share/ao3-calibre-backfill/`, so
nothing personal ends up in the repository:

| Path | Contents |
| --- | --- |
| `logs/` | One timestamped log per run of either tool. |
| `download-failures.jsonl` | Works that failed or are unavailable. Permanent entries (deleted, unrevealed) are skipped by later runs. |
| `scan.json` | The backfill's map of library EPUBs to AO3 works. |
| `ao3-cache.jsonl` | Fetched AO3 statistics. Append-only; the latest record per work wins. |
| `ao3-cache.jsonl.failures.jsonl` | Works the backfill could not fetch. |
| `ao3-cache.jsonl.fetch.lock` | Held by whichever AO3 run is active. |
| `metadata.db.<timestamp>.backup` | Verified `metadata.db` backups, each with a checksum manifest. Never overwritten. |
| `write-result-*.json` | Per-book results of each Calibre write. |

## Project layout

```text
download.py                 entry point: download works
backfill.py                 entry point: one-time library backfill
ao3archiver/
  ao3_client.py             everything that talks to AO3: login, request policy,
                            page classification, EPUB downloads, run lock, work queue
  download.py               the download workflow
  backfill.py               the backfill workflow
  calibre_library.py        columns, calibredb, backups, the bulk writer, verification
  calibre_scripts/
    bulk_write.py           runs under calibre-debug; kept apart from the package
  metadata.py               AO3 page statistics and EPUB metadata
  metrics.py                local Words and Gunning Fog
  credentials.py            .env / environment / personal.ini
  run_log.py                logging, progress and ETA, interrupts
  common.py                 shared paths, base error, small helpers
tests/                      one test module per package module, fakes in support.py
```

## Development

```sh
python3 -m pip install pytest
python3 -m pytest
```

The suite runs offline against a fake AO3. Two integration tests also exercise
the real `calibre-debug` and `calibredb`, and are skipped when Calibre is not
installed.
