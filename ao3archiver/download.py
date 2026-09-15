"""Download AO3 works and bake their metadata into each EPUB for Calibre.

Given a directory of link files (as produced by ``ao3downloadernew``), this
fetches each work page, downloads the EPUB it links to, calculates the local
Words/Gfog metrics from the file, and writes everything into the EPUB: an
``ao3`` identifier, a visible statistics block, portable ``ao3:*`` metadata,
and Calibre ``calibre:user_metadata`` values. Dragging the files into Calibre
then fills the custom columns with no separate sync step.

Runs are unattended by default: a failing work is recorded and skipped, an
AO3-wide problem pauses the run instead of ending it, and rerunning the same
command resumes from what is already on disk.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Literal
import xml.etree.ElementTree as ET
import zipfile

from ao3archiver.ao3_client import (
    account_lock,
    ACCOUNT_LOCK_PATH,
    ao3_run_active,
    AO3DownloadClient,
    Cooldown,
    FetchedWorkPage,
    find_epub_download_url,
    MINIMUM_DELAY_SECONDS,
    run_work_queue,
    WorkOutcome,
)
from ao3archiver.calibre_library import ALL_COLUMNS
from ao3archiver.common import ArchiverError, REPO_ROOT, STATE_DIR
from ao3archiver.credentials import AO3Credentials, load_run_credentials
from ao3archiver.metadata import (
    AO3Metadata,
    canonical_work_url,
    enrich_epub,
    has_ao3_metadata,
    has_calibre_user_metadata,
    parse_ao3_metadata,
    read_ao3_metadata,
    validate_epub_file,
)
from ao3archiver.metrics import calculate_epub_metrics
from ao3archiver.run_log import (
    configure_logging,
    DEFAULT_LOG_DIR,
    format_clock,
    format_duration,
    interrupt_guard,
    log_banner,
    ProgressTracker,
    redact_secrets,
    RunInterrupted,
)

DEFAULT_LINKS_DIR = REPO_ROOT / "links"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "downloaded"
DEFAULT_DELAY_SECONDS = 10.0
DEFAULT_FAILURE_LOG = STATE_DIR / "download-failures.jsonl"
PROGRESS_SUMMARY_EVERY = 25
DRY_RUN_PREVIEW = 20
WORK_ID_PATTERN = re.compile(r"/works/(\d+)")

LOGGER = logging.getLogger("ao3.download")

EPUB_ERRORS = (ValueError, OSError, zipfile.BadZipFile, ET.ParseError)
Action = Literal["download", "enrich"]
# Paced requests per work: the work page (its chapter redirect is immediate)
# plus, for a new download, the EPUB itself.
REQUESTS_PER_ACTION: dict[Action, int] = {"download": 2, "enrich": 1}


# --- links ---------------------------------------------------------------------


@dataclass(frozen=True)
class WorkTarget:
    """One AO3 work to process, and the link file it came from."""

    work_id: str
    url: str
    source: str


@dataclass(frozen=True)
class LinkInventory:
    """What a links directory contains after deduplication."""

    targets: tuple[WorkTarget, ...]
    files: int
    lines: int
    duplicates: int
    invalid: tuple[str, ...]


def load_link_inventory(directory: Path) -> LinkInventory:
    """Read every ``*.txt`` link file once, keeping the first link to each work."""

    seen: dict[str, WorkTarget] = {}
    invalid: list[str] = []
    lines = 0
    files = sorted(Path(directory).glob("*.txt"))
    for file_path in files:
        with file_path.open("r", encoding="utf-8", errors="replace") as stream:
            for line in stream:
                url = line.strip()
                if not url:
                    continue
                lines += 1
                match = WORK_ID_PATTERN.search(url)
                if match is None:
                    invalid.append(url)
                    continue
                seen.setdefault(
                    match.group(1),
                    WorkTarget(work_id=match.group(1), url=url, source=file_path.name),
                )
    return LinkInventory(
        targets=tuple(seen.values()),
        files=len(files),
        lines=lines,
        duplicates=lines - len(seen) - len(invalid),
        invalid=tuple(invalid),
    )


# --- failure log ---------------------------------------------------------------


@dataclass(frozen=True)
class FailureEntry:
    work_id: str
    stage: str
    error_type: str
    error: str
    permanent: bool
    failed_at: str


class FailureLog:
    """Append-only JSONL record of works that failed or are unavailable on AO3.

    ``permanent`` marks works AO3 reports as gone or hidden (HTTP 404/410, an
    unrevealed Mystery Work). Those are skipped by later runs unless asked for.
    Everything else is retried automatically, because under the hardened request
    policy a failure is far more likely to be a passing problem than a lost work.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def record(
        self,
        work_id: str,
        *,
        stage: str,
        error_type: str,
        message: str,
        permanent: bool,
    ) -> None:
        entry = {
            "work_id": work_id,
            "work_url": canonical_work_url(work_id),
            "stage": stage,
            "error_type": error_type,
            "error": redact_secrets(message),
            "permanent": permanent,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            json.dump(entry, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def latest(self) -> dict[str, FailureEntry]:
        if not self.path.is_file():
            return {}
        entries: dict[str, FailureEntry] = {}
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                entry = FailureEntry(
                    work_id=str(value["work_id"]),
                    stage=str(value.get("stage", "")),
                    error_type=str(value.get("error_type", "")),
                    error=str(value.get("error", "")),
                    permanent=bool(value.get("permanent", False)),
                    failed_at=str(value.get("failed_at", "")),
                )
            except (json.JSONDecodeError, KeyError, TypeError):
                # A torn final line from a killed process is not worth stopping for.
                continue
            entries[entry.work_id] = entry
        return entries

    def permanently_unavailable(self) -> frozenset[str]:
        return frozenset(work_id for work_id, entry in self.latest().items() if entry.permanent)


# --- planning ------------------------------------------------------------------


@dataclass(frozen=True)
class PlannedWork:
    target: WorkTarget
    epub_path: Path
    action: Action


@dataclass(frozen=True)
class PlanCounts:
    complete: int = 0
    unavailable: int = 0
    missing_file: int = 0


def _is_valid_epub(epub_path: Path) -> bool:
    if not epub_path.is_file():
        return False
    try:
        validate_epub_file(epub_path)
    except EPUB_ERRORS:
        return False
    return True


def is_complete(epub_path: Path) -> bool:
    """A file is done only if it is a valid EPUB that Calibre can read columns from.

    EPUBs enriched by the earlier downloader have ``ao3:*`` metadata but no
    Calibre column values, so they are re-enriched rather than trusted.
    """

    return (
        _is_valid_epub(epub_path)
        and has_ao3_metadata(epub_path)
        and has_calibre_user_metadata(epub_path)
    )


def plan_work(
    inventory: LinkInventory,
    output_dir: Path,
    *,
    skip_work_ids: frozenset[str] = frozenset(),
    refresh: bool = False,
    metadata_only: bool = False,
    limit: int | None = None,
) -> tuple[tuple[PlannedWork, ...], PlanCounts]:
    """Decide, before any request, exactly which works this run touches."""

    planned: list[PlannedWork] = []
    complete = unavailable = missing_file = 0
    for target in inventory.targets:
        if target.work_id in skip_work_ids:
            unavailable += 1
            continue
        epub_path = output_dir / f"{target.work_id}.epub"
        if not _is_valid_epub(epub_path):
            # Missing, truncated, or corrupt: enriching it in place cannot work.
            if metadata_only:
                missing_file += 1
                continue
            planned.append(PlannedWork(target, epub_path, "download"))
            continue
        if not refresh and is_complete(epub_path):
            complete += 1
            continue
        planned.append(PlannedWork(target, epub_path, "enrich"))
    if limit is not None:
        planned = planned[:limit]
    return tuple(planned), PlanCounts(complete, unavailable, missing_file)


def estimate_seconds(planned: Sequence[PlannedWork], delay_seconds: float) -> float:
    return sum(REQUESTS_PER_ACTION[item.action] for item in planned) * delay_seconds


# --- EPUB handling -------------------------------------------------------------


def save_epub_atomically(content: bytes, destination: Path) -> None:
    """Write EPUB bytes to ``destination`` only if they form a valid EPUB."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}.",
            suffix=".tmp",
            dir=destination.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
        validate_epub_file(temporary_path)
        os.replace(temporary_path, destination)
        temporary_path = None
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def metadata_from_page(page: FetchedWorkPage, work_id: str) -> AO3Metadata:
    """Build full metadata from the work page, falling back to the fetch record."""

    work_url = canonical_work_url(work_id)
    if page.html is not None:
        try:
            parsed = parse_ao3_metadata(page.html, work_url)
        except ValueError:
            parsed = None
        if parsed is not None:
            return replace(
                parsed,
                title=page.record.title or parsed.title,
                authors=page.record.authors or parsed.authors,
            )
    return AO3Metadata(
        work_id=work_id,
        work_url=work_url,
        title=page.record.title,
        authors=page.record.authors,
    )


def with_local_metrics(metadata: AO3Metadata, epub_path: Path) -> AO3Metadata:
    metrics = calculate_epub_metrics(epub_path)
    if metrics.error:
        LOGGER.warning(f"work {metadata.work_id}: local metrics unavailable: {metrics.error}")
    return replace(metadata, local_words=metrics.words, local_gfog=metrics.gfog)


def enrich_for_calibre(epub_path: Path, metadata: AO3Metadata) -> None:
    """Bake all metadata into the EPUB and prove it reads back the way Calibre will."""

    enrich_epub(epub_path, metadata, calibre_columns=ALL_COLUMNS)
    validate_epub_file(epub_path)
    stored = read_ao3_metadata(epub_path)
    if stored.work_id != metadata.work_id:
        raise ValueError(f"{epub_path.name}: stored AO3 work ID {stored.work_id} != {metadata.work_id}")
    expects_columns = any(
        getattr(metadata, attribute) is not None for _, _, _, attribute in ALL_COLUMNS
    )
    if expects_columns and not has_calibre_user_metadata(epub_path):
        raise ValueError(f"{epub_path.name}: Calibre would not read the column values just written")


def describe(metadata: AO3Metadata) -> str:
    def show(value: object) -> str:
        return "n/a" if value is None else str(value)

    return (
        f"kudos={show(metadata.kudos)} hits={show(metadata.hits)} "
        f"words={show(metadata.local_words)} gfog={show(metadata.local_gfog)}"
    )


class WorkProcessor:
    """Carries one planned work from its AO3 page to an enriched EPUB on disk."""

    def __init__(
        self,
        client: AO3DownloadClient,
        failure_log: FailureLog,
        planned: Sequence[PlannedWork],
    ) -> None:
        self.client = client
        self.failure_log = failure_log
        self.planned = {item.target.work_id: item for item in planned}
        self.stage = "page"

    def __call__(self, work_id: str) -> WorkOutcome:
        item = self.planned[work_id]
        self.stage = "page"
        page = self.client.fetch_work_page(work_id)
        if page.record.availability == "unavailable":
            reason = page.record.error or "unavailable on AO3"
            self.failure_log.record(
                work_id, stage="page", error_type="Unavailable", message=reason, permanent=True
            )
            return WorkOutcome("unavailable", reason)

        metadata = metadata_from_page(page, work_id)
        if item.action == "download":
            self.stage = "download"
            url = find_epub_download_url(page.html or "", work_id)
            save_epub_atomically(self.client.download_epub(url, work_id), item.epub_path)

        self.stage = "enrich"
        metadata = with_local_metrics(metadata, item.epub_path)
        enrich_for_calibre(item.epub_path, metadata)

        label = "downloaded" if item.action == "download" else "enriched"
        if page.record.availability == "incomplete":
            return WorkOutcome(label, f"no AO3 statistics on the page; {describe(metadata)}")
        return WorkOutcome(label, describe(metadata))

    def record_failure(self, work_id: str, error: Exception) -> None:
        self.failure_log.record(
            work_id,
            stage=self.stage,
            error_type=type(error).__name__,
            message=str(error),
            permanent=False,
        )


# --- run -----------------------------------------------------------------------


ClientFactory = Callable[[AO3Credentials, float, Callable[[float], None]], AO3DownloadClient]


def _default_client(
    credentials: AO3Credentials,
    delay_seconds: float,
    sleep_fn: Callable[[float], None],
) -> AO3DownloadClient:
    return AO3DownloadClient(credentials, delay_seconds=delay_seconds, sleep_fn=sleep_fn)


@dataclass(frozen=True)
class RunSummary:
    planned: int
    outcomes: dict[str, int]
    still_failed: dict[str, str]


def run_downloads(
    links_dir: Path,
    output_dir: Path,
    *,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    refresh: bool = False,
    metadata_only: bool = False,
    retry_unavailable: bool = False,
    limit: int | None = None,
    dry_run: bool = False,
    sleep_fn: Callable[[float], None] = time.sleep,
    failure_log_path: Path = DEFAULT_FAILURE_LOG,
    lock_path: Path = ACCOUNT_LOCK_PATH,
    credentials_loader: Callable[[], AO3Credentials] = lambda: load_run_credentials(allow_ini_fallback=True),
    client_factory: ClientFactory = _default_client,
) -> RunSummary:
    """Plan the run, then download and enrich every planned work unattended."""

    LOGGER.info(f"reading link files from {links_dir}")
    inventory = load_link_inventory(links_dir)
    if not inventory.targets:
        LOGGER.warning(f"no AO3 work links found in {links_dir}")
        return RunSummary(planned=0, outcomes={}, still_failed={})

    failure_log = FailureLog(failure_log_path)
    unavailable = frozenset() if retry_unavailable else failure_log.permanently_unavailable()
    LOGGER.info(f"checking existing files in {output_dir}")
    planned, counts = plan_work(
        inventory,
        output_dir,
        skip_work_ids=unavailable,
        refresh=refresh,
        metadata_only=metadata_only,
        limit=limit,
    )

    actions = Counter(item.action for item in planned)
    seconds = estimate_seconds(planned, delay_seconds)
    entries: list[tuple[str, object]] = [
        ("link files", inventory.files),
        ("link lines", inventory.lines),
        ("unique works", len(inventory.targets)),
        ("duplicate lines", inventory.duplicates),
        ("unparseable lines", len(inventory.invalid)),
        ("already complete", counts.complete),
        (
            "unavailable on AO3",
            f"{counts.unavailable} (skipped; --retry-unavailable to include)" if counts.unavailable else "0",
        ),
    ]
    if metadata_only:
        entries.append(("not downloaded yet", counts.missing_file))
    entries += [
        ("to download", actions["download"]),
        ("to enrich only", actions["enrich"]),
        ("output folder", output_dir),
        ("delay", f"{delay_seconds:g}s between requests"),
        (
            "estimated finish",
            f"{format_duration(seconds)} "
            f"(about {format_clock(datetime.now().astimezone() + timedelta(seconds=seconds))})",
        ),
        ("failure log", failure_log_path),
    ]
    log_banner(LOGGER, "AO3 download" + (" (dry run)" if dry_run else ""), entries)
    for url in inventory.invalid[:5]:
        LOGGER.warning(f"ignoring unparseable link: {url}")

    if not planned:
        LOGGER.info("nothing to do: every link is already downloaded and enriched")
        return RunSummary(planned=0, outcomes={}, still_failed={})

    if dry_run:
        if ao3_run_active(lock_path):
            LOGGER.warning("another AO3 run (the backfill or a download) is active; a real run would refuse to start")
        for item in planned[:DRY_RUN_PREVIEW]:
            LOGGER.info(
                f"would {item.action}: work={item.target.work_id} "
                f"(from {item.target.source}) -> {item.epub_path}"
            )
        if len(planned) > DRY_RUN_PREVIEW:
            LOGGER.info(f"... and {len(planned) - DRY_RUN_PREVIEW} more")
        return RunSummary(planned=len(planned), outcomes={}, still_failed={})

    with account_lock(lock_path):
        credentials = credentials_loader()
        LOGGER.info(f"using credentials from {credentials.source}")
        client = client_factory(credentials, delay_seconds, sleep_fn)
        cooldown = Cooldown(sleep_fn)
        progress = ProgressTracker(
            LOGGER, len(planned), unit="work", summary_every=PROGRESS_SUMMARY_EVERY
        )
        processor = WorkProcessor(client, failure_log, planned)
        try:
            client.recover_session(cooldown)
            still_failed = run_work_queue(
                [item.target.work_id for item in planned],
                processor,
                progress=progress,
                cooldown=cooldown,
                recover_session=lambda: client.recover_session(cooldown),
                record_failure=processor.record_failure,
            )
        except RunInterrupted:
            progress.log_summary(final=True)
            LOGGER.warning("stopped; rerun the same command to resume from what is on disk")
            raise
        finally:
            client.close()

    progress.log_summary(final=True)
    if cooldown.count:
        LOGGER.warning(
            f"cooled down {cooldown.count} time{'s' if cooldown.count != 1 else ''} "
            f"for {format_duration(cooldown.total_seconds)} in total"
        )
    if still_failed:
        LOGGER.warning(
            f"{len(still_failed)} works still failed after a second attempt; the next run "
            f"retries them. Details: {failure_log_path}"
        )
        for work_id, message in list(still_failed.items())[:10]:
            LOGGER.warning(f"  work={work_id} {message}")
    return RunSummary(
        planned=len(planned),
        outcomes=dict(progress.outcomes),
        still_failed=still_failed,
    )


# --- CLI -----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download AO3 works and bake AO3 and local metadata into each EPUB.",
    )
    parser.add_argument("--links", type=Path, default=DEFAULT_LINKS_DIR, help="directory of link .txt files")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_DIR, help="directory for the EPUBs")
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"seconds between AO3 requests (default: {DEFAULT_DELAY_SECONDS:g}, "
             f"minimum: {MINIMUM_DELAY_SECONDS:g})",
    )
    parser.add_argument("--limit", type=int, default=None, help="process at most this many works")
    parser.add_argument("--dry-run", action="store_true", help="show the plan without making requests")
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="only enrich EPUBs already on disk; download nothing",
    )
    parser.add_argument(
        "--refresh-metadata",
        action="store_true",
        help="re-fetch AO3 statistics for EPUBs that are already complete",
    )
    parser.add_argument(
        "--retry-unavailable",
        action="store_true",
        help="include works previously found deleted or unrevealed on AO3",
    )
    parser.add_argument(
        "--failure-log",
        type=Path,
        default=DEFAULT_FAILURE_LOG,
        help=f"JSONL record of failed and unavailable works (default: {DEFAULT_FAILURE_LOG})",
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help=f"directory for this run's log file (default: {DEFAULT_LOG_DIR})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error("--limit must be positive")
    if not math.isfinite(args.delay) or args.delay < MINIMUM_DELAY_SECONDS:
        parser.error(f"--delay must be at least {MINIMUM_DELAY_SECONDS:g} seconds")

    run = configure_logging("download", log_dir=args.log_dir)
    run.logger.info(f"ao3 download · log file: {run.log_path}")
    with interrupt_guard(run.logger, unit="work") as guard:
        run_downloads(
            args.links,
            args.output,
            delay_seconds=args.delay,
            refresh=args.refresh_metadata,
            metadata_only=args.metadata_only,
            retry_unavailable=args.retry_unavailable,
            limit=args.limit,
            dry_run=args.dry_run,
            sleep_fn=guard.sleep,
            failure_log_path=args.failure_log,
        )
    return 0



def run() -> None:
    """Command-line entry point used by ``download.py``."""

    try:
        raise SystemExit(main())
    except RunInterrupted as error:
        LOGGER.warning(f"{error}; rerun the same command to resume")
        raise SystemExit(130) from error
    except ArchiverError as error:
        LOGGER.error(str(error))
        raise SystemExit(2) from error
