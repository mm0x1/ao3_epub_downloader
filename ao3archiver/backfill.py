"""One-time backfill of AO3 statistics into an existing Calibre library.

Stages, each behind its own explicit gate: scan the library's EPUBs and map
them to AO3 works (read-only); fetch current AO3 statistics into an
append-only JSONL cache; optionally calculate missing local metrics; then
write the cached values into Calibre custom columns through Calibre's API,
with a verified metadata.db backup and read-back verification.
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import posixpath
import re
import shutil
import sqlite3
import time
from typing import cast
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

import requests

from ao3archiver.ao3_client import (
    _WorkLinkParser,
    account_lock,
    AO3Fetcher,
    AO3FetchRecord,
    Cooldown,
    CredentialsRejected,
    DEFAULT_DELAY_SECONDS,
    FAILURE_STREAK_COOLDOWN_THRESHOLD,
    login_authenticated_session,
    MINIMUM_DELAY_SECONDS,
    NetworkStopError,
    sign_in_patiently,
    SYSTEMIC_FETCH_ERRORS,
)
from ao3archiver.calibre_library import (
    ALL_COLUMNS,
    AO3_COLUMNS,
    BulkWriteError,
    create_backup,
    embed_calibre_metadata,
    EmbedResult,
    load_calibre_books,
    load_local_metric_values,
    LOCAL_METRIC_COLUMNS,
    read_all_custom_values,
    read_library_uuid,
    require_calibre_closed,
    run_calibre_bulk_write,
    running_calibre_processes,
    setup_custom_columns,
    verify_backup,
    verify_custom_columns,
    verify_library_values,
    verify_local_metric_columns,
    verify_written_values,
)
from ao3archiver.common import (
    ArchiverError,
    REPO_ROOT,
    sha256_file,
    STATE_DIR,
    utc_now,
    write_json_atomically,
)
from ao3archiver.credentials import load_run_credentials
from ao3archiver.metadata import (
    AO3Metadata,
    canonical_work_url,
    enrich_epub,
    read_ao3_metadata,
    read_calibre_user_metadata,
    validate_epub_file,
)
from ao3archiver.metrics import calculate_epub_metrics, LOCAL_METRICS_ALGORITHM
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

DEFAULT_BATCH_SIZE = 25


DEFAULT_CACHE_DIR = STATE_DIR


SCAN_REPORT_NAME = "scan.json"


CACHE_NAME = "ao3-cache.jsonl"




LOGGER = logging.getLogger("ao3.backfill")


PROGRESS_SUMMARY_EVERY = 25


REVALIDATION_PROGRESS_EVERY = 2000


LONG_WAIT_ANNOUNCE_SECONDS = 60.0


class BackfillError(ArchiverError):
    """Base error for a safety-gated backfill operation."""


@dataclass(frozen=True)
class EpubMapping:
    """One existing EPUB mapped to the primary AO3 work in its preface."""

    book_id: int
    epub_path: str
    work_id: str
    work_url: str
    preface_entry: str
    all_work_ids: tuple[str, ...] = ()
    ambiguous: bool = False
    malformed_preface: bool = False
    local_words: int | None = None
    local_gfog: float | None = None
    local_metrics_error: str | None = None
    local_metrics_calculated: bool = False


@dataclass(frozen=True)
class ScanIssue:
    book_id: int | None
    epub_path: str
    reason: str


@dataclass(frozen=True)
class ScanReport:
    library: str
    library_uuid: str
    generated_at: str
    book_count: int
    epub_count: int
    existing_ao3_identifier_count: int
    mappings: tuple[EpubMapping, ...]
    missing_work_ids: tuple[ScanIssue, ...]
    malformed_epubs: tuple[ScanIssue, ...]
    unmatched_books: tuple[ScanIssue, ...]
    local_metrics_algorithm: str = LOCAL_METRICS_ALGORITHM
    local_metrics_limit: int | None = None
    local_metrics_calculated: int = 0

    @property
    def ambiguous_mappings(self) -> tuple[EpubMapping, ...]:
        return tuple(mapping for mapping in self.mappings if mapping.ambiguous)

    @property
    def duplicate_work_ids(self) -> dict[str, tuple[EpubMapping, ...]]:
        grouped: dict[str, list[EpubMapping]] = {}
        for mapping in self.mappings:
            grouped.setdefault(mapping.work_id, []).append(mapping)
        return {
            work_id: tuple(mappings)
            for work_id, mappings in grouped.items()
            if len(mappings) > 1
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "library": self.library,
            "library_uuid": self.library_uuid,
            "generated_at": self.generated_at,
            "book_count": self.book_count,
            "epub_count": self.epub_count,
            "existing_ao3_identifier_count": self.existing_ao3_identifier_count,
            "local_metrics_algorithm": self.local_metrics_algorithm,
            "local_metrics_limit": self.local_metrics_limit,
            "local_metrics_calculated": self.local_metrics_calculated,
            "mappings": [
                {
                    **asdict(mapping),
                    "all_work_ids": list(mapping.all_work_ids),
                }
                for mapping in self.mappings
            ],
            "missing_work_ids": [asdict(issue) for issue in self.missing_work_ids],
            "malformed_epubs": [asdict(issue) for issue in self.malformed_epubs],
            "unmatched_books": [asdict(issue) for issue in self.unmatched_books],
        }

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "ScanReport":
        def issue_list(name: str) -> tuple[ScanIssue, ...]:
            raw = value.get(name, [])
            if not isinstance(raw, list):
                raise BackfillError(f"Scan report field {name} is not a list")
            issues: list[ScanIssue] = []
            for item in raw:
                if not isinstance(item, dict):
                    continue
                issues.append(
                    ScanIssue(
                        book_id=cast(int | None, item.get("book_id")),
                        epub_path=str(item.get("epub_path", "")),
                        reason=str(item.get("reason", "")),
                    )
                )
            return tuple(issues)

        raw_mappings = value.get("mappings", [])
        if not isinstance(raw_mappings, list):
            raise BackfillError("Scan report mappings field is not a list")

        def optional_nonnegative_int(item: dict[str, object], name: str) -> int | None:
            raw = item.get(name)
            if raw is None or raw == "":
                return None
            if isinstance(raw, bool):
                raise BackfillError(f"Scan report field {name} must not be boolean")
            if isinstance(raw, int) and raw >= 0:
                return raw
            if isinstance(raw, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", raw):
                return int(raw)
            raise BackfillError(f"Scan report field {name} is not a non-negative integer")

        def optional_finite_float(item: dict[str, object], name: str) -> float | None:
            raw = item.get(name)
            if raw is None or raw == "":
                return None
            if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
                raise BackfillError(f"Scan report field {name} is not numeric")
            try:
                parsed = float(raw)
            except ValueError as error:
                raise BackfillError(f"Scan report field {name} is not numeric") from error
            if not math.isfinite(parsed):
                raise BackfillError(f"Scan report field {name} is not finite")
            return parsed

        def strict_bool(item: dict[str, object], name: str, default: bool = False) -> bool:
            raw = item.get(name, default)
            if not isinstance(raw, bool):
                raise BackfillError(f"Scan report field {name} must be boolean")
            return raw

        mappings_list: list[EpubMapping] = []
        for item in raw_mappings:
            if not isinstance(item, dict):
                continue
            raw_work_ids = item.get("all_work_ids", [])
            work_ids = raw_work_ids if isinstance(raw_work_ids, list) else []
            book_id = item.get("book_id")
            if book_id is None:
                raise BackfillError("Scan report mapping has no book_id")
            mappings_list.append(
                EpubMapping(
                    book_id=int(cast(str | int, book_id)),
                    epub_path=str(item.get("epub_path", "")),
                    work_id=str(item.get("work_id", "")),
                    work_url=str(item.get("work_url", "")),
                    preface_entry=str(item.get("preface_entry", "")),
                    all_work_ids=tuple(str(work_id) for work_id in work_ids),
                    ambiguous=strict_bool(item, "ambiguous"),
                    malformed_preface=strict_bool(item, "malformed_preface"),
                    local_words=optional_nonnegative_int(item, "local_words"),
                    local_gfog=optional_finite_float(item, "local_gfog"),
                    local_metrics_error=(
                        str(item["local_metrics_error"])
                        if item.get("local_metrics_error") is not None
                        else None
                    ),
                    local_metrics_calculated=strict_bool(item, "local_metrics_calculated"),
                )
            )
        mappings = tuple(mappings_list)
        book_count = value.get("book_count")
        epub_count = value.get("epub_count")
        identifier_count = value.get("existing_ao3_identifier_count", 0)
        if book_count is None or epub_count is None:
            raise BackfillError("Scan report is missing inventory counts")
        def report_nonnegative_int(raw: object, name: str, default: int | None = None) -> int | None:
            if raw is None and default is not None:
                return default
            if isinstance(raw, bool):
                raise BackfillError(f"Scan report {name} is invalid")
            if isinstance(raw, int) and raw >= 0:
                return raw
            if isinstance(raw, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", raw):
                return int(raw)
            raise BackfillError(f"Scan report {name} is invalid")

        metrics_limit = report_nonnegative_int(value.get("local_metrics_limit"), "local_metrics_limit")
        metrics_calculated = report_nonnegative_int(
            value.get("local_metrics_calculated"),
            "local_metrics_calculated",
            default=0,
        )
        if metrics_calculated is None:
            raise BackfillError("Scan report local_metrics_calculated is missing")
        return cls(
            library=str(value["library"]),
            library_uuid=str(value.get("library_uuid", "")),
            generated_at=str(value["generated_at"]),
            book_count=int(cast(str | int, book_count)),
            epub_count=int(cast(str | int, epub_count)),
            existing_ao3_identifier_count=int(cast(str | int, identifier_count)),
            mappings=mappings,
            missing_work_ids=issue_list("missing_work_ids"),
            malformed_epubs=issue_list("malformed_epubs"),
            unmatched_books=issue_list("unmatched_books"),
            local_metrics_algorithm=str(
                value.get("local_metrics_algorithm", LOCAL_METRICS_ALGORITHM)
            ),
            local_metrics_limit=metrics_limit,
            local_metrics_calculated=metrics_calculated,
        )


@dataclass(frozen=True)
class CacheValidation:
    records: dict[str, AO3FetchRecord]
    work_ids: tuple[str, ...]
    missing: tuple[str, ...]
    unavailable: tuple[str, ...]
    incomplete: tuple[str, ...]
    complete: tuple[str, ...]
    mappings: dict[str, EpubMapping]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _entry_path(opf_path: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(opf_path), urllib.parse.unquote(href)))


def _work_ids_from_xhtml(raw: bytes) -> tuple[tuple[str, ...], bool]:
    text = raw.decode("utf-8", errors="replace")
    parser = _WorkLinkParser()
    parser.feed(text)
    parser.close()
    try:
        ET.fromstring(raw)
        xml_valid = True
    except ET.ParseError:
        xml_valid = False
    return tuple(parser.work_ids), xml_valid


def _xhtml_has_preface_marker(raw: bytes) -> bool:
    lowered = raw.decode("utf-8", errors="replace").casefold()
    return (
        'id="preface"' in lowered
        or "id='preface'" in lowered
        or " preface" in lowered
    )


def _epub_preface_work_ids(
    epub_path: Path,
) -> tuple[str | None, tuple[str, ...], str, bool, str | None]:
    """Return the first work link from the EPUB's identified preface document."""

    with zipfile.ZipFile(epub_path) as archive:
        container = ET.fromstring(archive.read("META-INF/container.xml"))
        rootfile_element = next(
            (
                element
                for element in container.iter()
                if _local_name(element.tag) == "rootfile" and element.attrib.get("full-path")
            ),
            None,
        )
        if rootfile_element is None:
            raise ValueError("EPUB container did not declare a rootfile")
        opf_path = urllib.parse.unquote(rootfile_element.attrib["full-path"])
        package = ET.fromstring(archive.read(opf_path))

        manifest: dict[str, tuple[str, str]] = {}
        for item in package.iter():
            if _local_name(item.tag) != "item" or item.attrib.get("media-type") != "application/xhtml+xml":
                continue
            item_id = item.attrib.get("id")
            href = item.attrib.get("href")
            if item_id and href:
                manifest[item_id] = (_entry_path(opf_path, href), href)

        spine = next((element for element in package.iter() if _local_name(element.tag) == "spine"), None)
        ordered: list[tuple[str, str]] = []
        if spine is not None:
            for itemref in spine:
                if _local_name(itemref.tag) != "itemref":
                    continue
                item = manifest.get(itemref.attrib.get("idref", ""))
                if item is not None:
                    ordered.append(item)
        if not ordered:
            ordered = list(manifest.values())

        # AO3 EPUBs use title_page/preface names when available. Older EPUBs
        # use split_000 or the first non-cover spine item instead. Never scan
        # later chapter documents: a related-work link there is not the EPUB's
        # primary work identifier.
        named_prefaces = [
            item
            for item in ordered
            if re.search(r"(?:preface|title[_-]?page)", item[1], re.IGNORECASE)
        ]
        non_cover = [
            item
            for item in ordered
            if Path(item[1]).stem.casefold() not in {"cover", "coverpage", "cover_page"}
        ]
        candidates: list[tuple[str, str]] = []
        if named_prefaces:
            candidates.append(named_prefaces[0])
            split_preface = next(
                (
                    item
                    for item in non_cover
                    if re.search(r"(?:^|[/_-])split_000\.", item[1], re.IGNORECASE)
                ),
                None,
            )
            if split_preface is not None and split_preface not in candidates:
                candidates.append(split_preface)
        elif non_cover:
            candidates.append(non_cover[0])
        if not candidates:
            return None, (), "", False, "no EPUB preface document was identified"

        malformed_preface = False
        archive_path = candidates[0][0]
        for archive_path, _ in candidates:
            try:
                raw = archive.read(archive_path)
                work_ids, xml_valid = _work_ids_from_xhtml(raw)
            except KeyError:
                continue
            malformed_preface = malformed_preface or not xml_valid
            if work_ids:
                is_named_preface = bool(named_prefaces and archive_path == named_prefaces[0][0])
                if not is_named_preface and not _xhtml_has_preface_marker(raw):
                    return None, (), archive_path, malformed_preface, "identified first-content EPUB document is not a preface"
                return work_ids[0], work_ids, archive_path, malformed_preface, None
        return None, (), archive_path, malformed_preface, "no AO3 work URL in EPUB preface"


def _book_directory_id(path: Path) -> int | None:
    match = re.search(r"\((\d+)\)$", path.name)
    return int(match.group(1)) if match else None


def _identifier_has_ao3_work(value: object) -> bool:
    if isinstance(value, str):
        return bool(_WorkLinkParser.pattern.search(value))
    if isinstance(value, Mapping):
        return any(_identifier_has_ao3_work(item) for item in value.values())
    if isinstance(value, list):
        return any(_identifier_has_ao3_work(item) for item in value)
    return False


def scan_library(
    library: Path,
    calibredb: str = "calibredb",
    local_metrics_limit: int | None = DEFAULT_BATCH_SIZE,
) -> ScanReport:
    """Inventory EPUBs and map them to Calibre IDs without writing the library."""

    if not library.is_dir():
        raise BackfillError(f"Calibre library does not exist: {library}")
    if local_metrics_limit is not None and local_metrics_limit < 0:
        raise ValueError("local_metrics_limit must not be negative")
    books = load_calibre_books(calibredb, library)
    book_ids = {
        int(cast(str | int, book["id"]))
        for book in books
        if str(book.get("id", "")).isdigit()
    }
    existing_identifier_count = sum(
        1
        for book in books
        if _identifier_has_ao3_work(book.get("identifiers"))
    )

    epub_paths: list[tuple[int | None, Path]] = []
    for author_directory in sorted(library.iterdir()):
        if not author_directory.is_dir():
            continue
        for book_directory in sorted(author_directory.iterdir()):
            if not book_directory.is_dir():
                continue
            book_id = _book_directory_id(book_directory)
            for epub_path in sorted(book_directory.iterdir()):
                if epub_path.is_file() and epub_path.suffix.casefold() == ".epub":
                    epub_paths.append((book_id, epub_path))

    mappings: list[EpubMapping] = []
    missing: list[ScanIssue] = []
    malformed: list[ScanIssue] = []
    unmatched: list[ScanIssue] = []
    for book_id, epub_path in epub_paths:
        relative_path = str(epub_path.relative_to(library))
        if book_id is None or book_id not in book_ids:
            unmatched.append(ScanIssue(book_id, relative_path, "EPUB directory did not map to a Calibre book"))
            continue
        try:
            work_id, all_work_ids, preface_entry, malformed_preface, reason = _epub_preface_work_ids(epub_path)
        except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as error:
            malformed.append(ScanIssue(book_id, relative_path, f"{type(error).__name__}: {error}"))
            continue
        if malformed_preface:
            malformed.append(
                ScanIssue(
                    book_id,
                    relative_path,
                    "preface XHTML is not well-formed XML; HTML anchor recovery was used",
                )
            )
        if work_id is None:
            missing.append(ScanIssue(book_id, relative_path, reason or "no AO3 work URL in EPUB preface"))
            continue
        mappings.append(
            EpubMapping(
                book_id=book_id,
                epub_path=relative_path,
                work_id=work_id,
                work_url=canonical_work_url(work_id),
                preface_entry=preface_entry,
                all_work_ids=all_work_ids,
                ambiguous=len(all_work_ids) > 1,
                malformed_preface=malformed_preface,
            )
        )

    ambiguous_work_ids = {mapping.work_id for mapping in mappings if mapping.ambiguous}
    selected_metric_work_ids: set[str] = set()
    if local_metrics_limit is None:
        selected_metric_work_ids = {mapping.work_id for mapping in mappings}
    elif local_metrics_limit > 0:
        for mapping in mappings:
            if mapping.work_id in ambiguous_work_ids:
                continue
            selected_metric_work_ids.add(mapping.work_id)
            if len(selected_metric_work_ids) >= local_metrics_limit:
                break

    calculated_metric_work_ids: set[str] = set()
    for index, mapping in enumerate(mappings):
        if mapping.work_id not in selected_metric_work_ids:
            continue
        if local_metrics_limit is not None and mapping.work_id in calculated_metric_work_ids:
            continue
        local_metrics = calculate_epub_metrics(library / mapping.epub_path)
        mappings[index] = replace(
            mapping,
            local_words=local_metrics.words,
            local_gfog=local_metrics.gfog,
            local_metrics_error=local_metrics.error,
            local_metrics_calculated=True,
        )
        calculated_metric_work_ids.add(mapping.work_id)

    return ScanReport(
        library=str(library),
        library_uuid=read_library_uuid(library),
        generated_at=datetime.now(timezone.utc).isoformat(),
        book_count=len(books),
        epub_count=len(epub_paths),
        existing_ao3_identifier_count=existing_identifier_count,
        mappings=tuple(mappings),
        missing_work_ids=tuple(missing),
        malformed_epubs=tuple(malformed),
        unmatched_books=tuple(unmatched),
        local_metrics_limit=local_metrics_limit,
        local_metrics_calculated=len(calculated_metric_work_ids),
    )


def write_scan_report(report: ScanReport, report_path: Path) -> None:
    write_json_atomically(report_path, report.to_dict())


def load_scan_report(report_path: Path) -> ScanReport:
    with report_path.open(encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise BackfillError(f"Scan report is not a JSON object: {report_path}")
    return ScanReport.from_dict(value)


class CacheStore:
    """Append-only cache with one durable JSON record per fetched work."""

    def __init__(self, path: Path) -> None:
        self.path = path
        # The backfill is strictly sequential, so one depth counter per store
        # is enough to make nested acquisitions safe. flock() would otherwise
        # deadlock against this same process on a second descriptor.
        self._lock_depth = 0

    @property
    def lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.lock")

    @property
    def fetch_lock_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.fetch.lock")

    @property
    def context_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.context.json")

    @property
    def failures_path(self) -> Path:
        return self.path.with_name(f"{self.path.name}.failures.jsonl")

    def has_context(self) -> bool:
        return self.context_path.is_file()

    def record_failure(self, work_id: str, error_type: str, message: str) -> None:
        """Append a failed fetch to a log kept apart from the cache.

        The cache resolves the latest record per work, so writing a failure
        there could hide an earlier good observation. A failed work simply stays
        uncached, and the next run picks it up again.
        """

        entry = {
            "work_id": work_id,
            "work_url": canonical_work_url(work_id),
            "failed_at": utc_now(),
            "error_type": error_type,
            "error": redact_secrets(message),
        }
        with self.operation_lock():
            with self.failures_path.open("a", encoding="utf-8") as stream:
                json.dump(entry, stream, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())

    def failed_work_ids(self) -> dict[str, str]:
        """Return the latest recorded error for each work that ever failed."""

        if not self.failures_path.is_file():
            return {}
        failures: dict[str, str] = {}
        with self.operation_lock():
            lines = self.failures_path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # A torn final line from a killed process is not worth stopping for.
                continue
            if isinstance(entry, dict) and isinstance(entry.get("work_id"), str):
                failures[entry["work_id"]] = f"{entry.get('error_type')}: {entry.get('error')}"
        return failures

    @contextmanager
    def operation_lock(self):
        """Hold the short cache lock; safe to nest within one process."""

        if self._lock_depth > 0:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            self._lock_depth = 1
            try:
                yield
            finally:
                self._lock_depth = 0
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def fetch_lock(self) -> AbstractContextManager[None]:
        """Hold the account-wide run lock beside this cache; see ``account_lock``.

        This is separate from ``operation_lock`` on purpose: a fetch runs for
        days, and read-only commands must still be able to inspect the cache
        while it runs.
        """

        return account_lock(self.fetch_lock_path)

    def records(self) -> dict[str, AO3FetchRecord]:
        with self.operation_lock():
            return self._records_unlocked()

    def _records_unlocked(self) -> dict[str, AO3FetchRecord]:
        if not self.path.exists():
            return {}
        result: dict[str, AO3FetchRecord] = {}
        raw_cache = self.path.read_bytes()
        lines = raw_cache.splitlines(keepends=True)
        offset = 0
        for line_number, raw_line in enumerate(lines, start=1):
            if not raw_line.strip():
                offset += len(raw_line)
                continue
            line = raw_line.decode("utf-8")
            try:
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("record is not an object")
                record = AO3FetchRecord.from_dict(value)
            except (UnicodeDecodeError, TypeError, ValueError, KeyError, json.JSONDecodeError) as error:
                if line_number == len(lines) and not raw_line.endswith(b"\n"):
                    # A process can be killed after fsyncing part of a JSON
                    # object. Discard only that incomplete final record; any
                    # complete preceding records remain durable and resumable.
                    with self.path.open("r+b") as stream:
                        stream.truncate(offset)
                    break
                raise BackfillError(f"Invalid cache record at {self.path}:{line_number}: {error}") from error
            result[record.work_id] = record
            offset += len(raw_line)
        return result

    def append(self, record: AO3FetchRecord) -> None:
        with self.operation_lock():
            self._append_unlocked(record)

    def _append_unlocked(self, record: AO3FetchRecord) -> None:
        with self.path.open("a", encoding="utf-8") as stream:
            if self.path.stat().st_size > 0:
                with self.path.open("rb") as existing:
                    existing.seek(-1, os.SEEK_END)
                    if existing.read(1) != b"\n":
                        stream.write("\n")
            json.dump(record.to_dict(), stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

    def bind(self, report: ScanReport) -> None:
        with self.operation_lock():
            self._bind_unlocked(report)

    def _bind_unlocked(self, report: ScanReport) -> None:
        expected = {
            "schema_version": 1,
            "library": str(Path(report.library).resolve()),
            "library_uuid": report.library_uuid,
        }
        if self.context_path.exists():
            with self.context_path.open(encoding="utf-8") as stream:
                actual = json.load(stream)
            if not isinstance(actual, dict) or any(actual.get(key) != value for key, value in expected.items()):
                raise BackfillError(
                    f"Cache context does not match scan report: {self.context_path}"
                )
            needs_context_update = (
                "last_request_at" not in actual or "next_request_at" not in actual
            )
            if "last_request_at" not in actual:
                actual["last_request_at"] = None
            if "next_request_at" not in actual:
                actual["next_request_at"] = None
            if needs_context_update:
                write_json_atomically(self.context_path, actual)
            return
        if self.path.exists() and self.path.stat().st_size > 0:
            raise BackfillError(
                f"Non-empty cache has no provenance context; refusing to adopt it: {self.path}"
            )
        write_json_atomically(
            self.context_path,
            {**expected, "last_request_at": None, "next_request_at": None},
        )

    def _context_unlocked(self) -> dict[str, object]:
        if not self.context_path.exists():
            raise BackfillError(f"Cache context is missing: {self.context_path}")
        with self.context_path.open(encoding="utf-8") as stream:
            value = json.load(stream)
        if not isinstance(value, dict):
            raise BackfillError(f"Cache context is not an object: {self.context_path}")
        return value

    def last_request_at_unlocked(self) -> float | None:
        value = self._context_unlocked().get("last_request_at")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BackfillError(f"Cache context has an invalid last request timestamp: {self.context_path}")
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise BackfillError(f"Cache context has an invalid last request timestamp: {self.context_path}")
        return timestamp

    def set_last_request_at_unlocked(self, timestamp: float) -> None:
        context = self._context_unlocked()
        context["last_request_at"] = timestamp
        write_json_atomically(self.context_path, context)

    def next_request_at_unlocked(self) -> float | None:
        value = self._context_unlocked().get("next_request_at")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise BackfillError(f"Cache context has an invalid next request timestamp: {self.context_path}")
        timestamp = float(value)
        if not math.isfinite(timestamp):
            raise BackfillError(f"Cache context has an invalid next request timestamp: {self.context_path}")
        return timestamp

    def set_request_started_unlocked(self, timestamp: float, delay_seconds: float) -> None:
        if not math.isfinite(timestamp) or not math.isfinite(delay_seconds) or delay_seconds < 0:
            raise ValueError("request timestamps and delays must be finite and non-negative")
        context = self._context_unlocked()
        context["last_request_at"] = timestamp
        context["next_request_at"] = timestamp + delay_seconds
        write_json_atomically(self.context_path, context)

    def defer_requests_unlocked(
        self,
        seconds: float,
        wall_time_fn: Callable[[], float] = time.time,
        *,
        exact: bool = False,
    ) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            raise ValueError("request deferral must be finite and non-negative")
        context = self._context_unlocked()
        current = context.get("next_request_at")
        current_timestamp = float(current) if isinstance(current, (int, float)) else None
        requested = wall_time_fn() + seconds
        context["next_request_at"] = requested if exact else max(current_timestamp or requested, requested)
        write_json_atomically(self.context_path, context)

    def request_schedule(self) -> tuple[float | None, float | None]:
        """Read the persisted (last, next) request timestamps under the lock."""

        with self.operation_lock():
            return (self.last_request_at_unlocked(), self.next_request_at_unlocked())

    def set_request_started(self, timestamp: float, delay_seconds: float) -> None:
        with self.operation_lock():
            self.set_request_started_unlocked(timestamp, delay_seconds)

    def defer_requests(
        self,
        seconds: float,
        wall_time_fn: Callable[[], float] = time.time,
        *,
        exact: bool = False,
    ) -> None:
        with self.operation_lock():
            self.defer_requests_unlocked(seconds, wall_time_fn, exact=exact)

    def snapshot(self, destination: Path) -> Path:
        """Copy the current JSONL cache without overwriting a destination."""

        with self.operation_lock():
            if not self.path.is_file() or self.path.stat().st_size <= 0:
                raise BackfillError(f"Cannot snapshot a missing or empty cache: {self.path}")
            if destination.resolve() == self.path.resolve():
                raise BackfillError("Cache snapshot destination must differ from the cache")
            destination.parent.mkdir(parents=True, exist_ok=True)
            try:
                with self.path.open("rb") as source, destination.open("xb") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
            except FileExistsError as error:
                raise BackfillError(f"Cache snapshot destination already exists: {destination}") from error
            if destination.stat().st_size != self.path.stat().st_size:
                raise BackfillError(f"Cache snapshot size differs from source: {destination}")
            if sha256_file(destination) != sha256_file(self.path):
                raise BackfillError(f"Cache snapshot checksum differs from source: {destination}")
            return destination

    def verify_binding(self, report: ScanReport) -> None:
        with self.operation_lock():
            actual = self._context_unlocked()
            expected = {
                "schema_version": 1,
                "library": str(Path(report.library).resolve()),
                "library_uuid": report.library_uuid,
            }
            if any(actual.get(key) != value for key, value in expected.items()):
                raise BackfillError(
                    f"Cache context does not match scan report: {self.context_path}"
                )


class CacheRequestScheduler:
    """Coordinate all AO3 request starts through one persisted cache context."""

    def __init__(
        self,
        cache: CacheStore,
        delay_seconds: float,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        wall_time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.cache = cache
        self.delay_seconds = delay_seconds
        self.sleep_fn = sleep_fn
        self.wall_time_fn = wall_time_fn

    def before_request(self) -> None:
        now = self.wall_time_fn()
        last_request_at, next_request_at = self.cache.request_schedule()
        eligible_at = next_request_at
        if eligible_at is None and last_request_at is not None:
            minimum = last_request_at + self.delay_seconds
            eligible_at = minimum
        if eligible_at is not None:
            remaining = eligible_at - now
            if remaining > 0:
                if remaining > LONG_WAIT_ANNOUNCE_SECONDS:
                    # An unexplained silent terminal is the thing to avoid.
                    LOGGER.info(
                        f"waiting {format_duration(remaining)} before the next request "
                        "(server-requested or persisted pacing)"
                    )
                self.sleep_fn(remaining)

    def request_started(self, timestamp: float) -> None:
        self.cache.set_request_started(timestamp, self.delay_seconds)

    def defer_requests(self, seconds: float, exact: bool = False) -> None:
        self.cache.defer_requests(seconds, self.wall_time_fn, exact=exact)


def _unique_work_ids(report: ScanReport, include_ambiguous: bool) -> list[str]:
    ambiguous_ids = {mapping.work_id for mapping in report.ambiguous_mappings}
    result: list[str] = []
    for mapping in report.mappings:
        if not include_ambiguous and mapping.work_id in ambiguous_ids:
            continue
        if mapping.work_id not in result:
            result.append(mapping.work_id)
    return result


def first_refresh_mappings(
    report: ScanReport,
    limit: int = DEFAULT_BATCH_SIZE,
    include_ambiguous: bool = False,
) -> tuple[EpubMapping, ...]:
    """Select unique scan-order mappings for an explicit refresh preview."""

    if limit < 1:
        raise ValueError("limit must be positive")
    ambiguous_ids = {mapping.work_id for mapping in report.ambiguous_mappings}
    seen: set[str] = set()
    selected: list[EpubMapping] = []
    for mapping in report.mappings:
        if not include_ambiguous and mapping.work_id in ambiguous_ids:
            continue
        if mapping.work_id in seen:
            continue
        seen.add(mapping.work_id)
        selected.append(mapping)
        if len(selected) >= limit:
            break
    return tuple(selected)


def render_refresh_preview(
    report: ScanReport,
    records: dict[str, AO3FetchRecord],
    limit: int = DEFAULT_BATCH_SIZE,
    include_ambiguous: bool = False,
) -> str:
    lines = [
        f"refresh preview: {limit} scan-order unique work IDs",
        "book_id work_id cached local_words gfog epub",
    ]
    for mapping in first_refresh_mappings(report, limit, include_ambiguous):
        record = records.get(mapping.work_id)
        lines.append(
            f"{mapping.book_id} {mapping.work_id} "
            f"{('yes' if record is not None else 'no')} "
            f"{display_value(mapping.local_words)} {display_value(mapping.local_gfog)} "
            f"{mapping.epub_path}"
        )
    return "\n".join(lines)


def verify_report_library(report: ScanReport, library: Path) -> None:
    if Path(report.library).resolve() != library.resolve():
        raise BackfillError(
            f"Scan report library {report.library!r} does not match write target {str(library)!r}"
        )
    current_uuid = read_library_uuid(library)
    if not report.library_uuid or report.library_uuid != current_uuid:
        raise BackfillError("Scan report library UUID does not match the current metadata.db")


def fetch_pending(
    report: ScanReport,
    cache: CacheStore,
    *,
    calibredb: str,
    library: Path,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    limit: int = DEFAULT_BATCH_SIZE,
    continue_after_review: bool = False,
    refresh: bool = False,
    include_ambiguous: bool = False,
    use_env_credentials: bool = False,
    retry_failed_once: bool = False,
    fetcher: AO3Fetcher | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    keep_going: bool = False,
) -> tuple[AO3FetchRecord, ...]:
    """Fetch at most one explicit sequential batch and persist each result."""

    if limit < 1:
        raise ValueError("limit must be positive")
    if not math.isfinite(delay_seconds) or delay_seconds < MINIMUM_DELAY_SECONDS:
        raise ValueError(f"delay_seconds must be at least {MINIMUM_DELAY_SECONDS:g}")
    if limit > DEFAULT_BATCH_SIZE and not continue_after_review:
        raise BackfillError(
            f"The initial network batch is limited to {DEFAULT_BATCH_SIZE}; "
            "rerun with --continue-after-review for a larger batch"
        )
    verify_report_library(report, library)
    # The column gate is intentionally before the first request.
    verify_custom_columns(calibredb, library)
    if (use_env_credentials or retry_failed_once) and fetcher is not None:
        raise ValueError("runtime retry/auth options cannot be combined with a supplied fetcher")
    fetched: list[AO3FetchRecord] = []
    # The fetch lock is held for the whole run so two fetches can never race,
    # while the cache lock is taken only for each short read or append. A fetch
    # spans days; read-only commands must stay usable in another terminal.
    with cache.fetch_lock():
        with cache.operation_lock():
            cache._bind_unlocked(report)
            records = cache._records_unlocked()
            all_work_ids = _unique_work_ids(report, include_ambiguous)
            pending = [
                work_id
                for work_id in all_work_ids
                if refresh or work_id not in records
                or (
                    retry_failed_once
                    and work_id in records
                    and records[work_id].availability in {"incomplete", "unavailable"}
                )
            ][:limit]
            cached_count = len(records)
        if not pending:
            LOGGER.info("nothing to fetch: every selected work is already cached")
            return ()

        pending_ids = set(pending)
        pending_mappings_by_work_id: dict[str, EpubMapping] = {}
        for mapping in report.mappings:
            if mapping.work_id in pending_ids:
                pending_mappings_by_work_id.setdefault(mapping.work_id, mapping)

        estimated_seconds = len(pending) * delay_seconds
        log_banner(
            LOGGER,
            "AO3 fetch",
            [
                ("library", library),
                ("cache", cache.path),
                ("works in scan", len(all_work_ids)),
                ("already cached", cached_count),
                ("this batch", len(pending)),
                ("delay", f"{delay_seconds:g}s between requests"),
                (
                    "on failure",
                    "keep going: skip the work, cool down on AO3-wide problems"
                    if keep_going
                    else "stop the run",
                ),
                (
                    "estimated finish",
                    f"{format_duration(estimated_seconds)} "
                    f"(about {format_clock(datetime.now().astimezone() + timedelta(seconds=estimated_seconds))})",
                ),
            ],
        )

        verify_report_inputs(
            report,
            library,
            calibredb,
            tuple(pending_mappings_by_work_id.values()),
        )

        scheduler = CacheRequestScheduler(cache, delay_seconds, sleep_fn=sleep_fn)
        progress = ProgressTracker(
            LOGGER,
            len(pending),
            unit="work",
            summary_every=PROGRESS_SUMMARY_EVERY,
        )
        authenticated_session: requests.Session | None = None
        cooldown = Cooldown(sleep_fn)
        failed_this_run: dict[str, str] = {}
        try:
            credentials = load_run_credentials() if use_env_credentials else None
            sign_in: Callable[[], None] | None = None
            if credentials is not None:
                session = requests.Session()
                authenticated_session = session

                def sign_in() -> None:
                    login_authenticated_session(
                        session,
                        credentials.username,
                        credentials.password,
                        source=credentials.source,
                        delay_seconds=delay_seconds,
                        sleep_fn=sleep_fn,
                        before_request=scheduler.before_request,
                        request_started_callback=scheduler.request_started,
                        request_deferred_callback=scheduler.defer_requests,
                    )

                if keep_going:
                    sign_in_patiently(sign_in, cooldown)
                else:
                    sign_in()

            def recover_session() -> None:
                if sign_in is None:
                    return
                # Start from a clean jar so a half-expired session cannot linger.
                authenticated_session.cookies.clear()
                sign_in_patiently(sign_in, cooldown)

            if fetcher is not None:
                active_fetcher = fetcher
            else:
                active_fetcher = AO3Fetcher(
                    session=authenticated_session,
                    delay_seconds=delay_seconds,
                    sleep_fn=sleep_fn,
                    before_request=scheduler.before_request,
                    request_started_callback=scheduler.request_started,
                    request_deferred_callback=scheduler.defer_requests,
                    max_attempts=2 if retry_failed_once else 4,
                    retry_unexpected_once=retry_failed_once,
                    reauthenticate=sign_in,
                )

            queue: deque[str] = deque(pending)
            failure_streak = 0
            last_systemic_work_id: str | None = None
            retry_pass_started = False
            while queue or (keep_going and failed_this_run and not retry_pass_started):
                if not queue:
                    retry_pass_started = True
                    LOGGER.info(
                        f"main pass finished; retrying {len(failed_this_run)} failed works once more"
                    )
                    progress.add_to_total(len(failed_this_run))
                    queue.extend(failed_this_run)
                    continue

                work_id = queue.popleft()
                try:
                    record = active_fetcher.fetch(work_id)
                except (RunInterrupted, CredentialsRejected):
                    raise
                except Exception as error:
                    if not keep_going:
                        raise
                    if isinstance(error, SYSTEMIC_FETCH_ERRORS) and work_id != last_systemic_work_id:
                        # Not this work's fault yet: pause, re-establish the
                        # session, and give the same work one more attempt.
                        last_systemic_work_id = work_id
                        queue.appendleft(work_id)
                        cooldown.wait(f"work {work_id}: {error}; this affects every work")
                        recover_session()
                        continue

                    message = f"{type(error).__name__}: {error}"
                    if not isinstance(error, (NetworkStopError, requests.RequestException)):
                        # Unexpected in code terms: keep the traceback in the log file.
                        LOGGER.error(f"work {work_id}: unexpected {message}", exc_info=True)
                    cache.record_failure(work_id, type(error).__name__, str(error))
                    failed_this_run[work_id] = message
                    progress.record("failed", f"work={work_id}", message)
                    failure_streak += 1
                    if failure_streak >= FAILURE_STREAK_COOLDOWN_THRESHOLD:
                        cooldown.wait(
                            f"{failure_streak} works failed in a row, which points at AO3 "
                            "or the session rather than the works"
                        )
                        recover_session()
                        failure_streak = 0
                    continue

                cache.append(record)
                fetched.append(record)
                failed_this_run.pop(work_id, None)
                failure_streak = 0
                last_systemic_work_id = None
                cooldown.reset()
                progress.record(
                    record.availability,
                    f"work={record.work_id}",
                    f"kudos={display_value(record.kudos)} hits={display_value(record.hits)}"
                    if record.availability == "ok"
                    else record.error or "no detail recorded",
                )
        except RunInterrupted:
            progress.log_summary(final=True)
            LOGGER.warning(
                f"stopped after {len(fetched)} cached works; rerun the same command to resume"
            )
            raise
        finally:
            if authenticated_session is not None:
                authenticated_session.close()
        progress.log_summary(final=True)
        if keep_going:
            _log_keep_going_summary(cache, cooldown, failed_this_run)
    return tuple(fetched)


def _log_keep_going_summary(
    cache: CacheStore,
    cooldown: Cooldown,
    failed_this_run: Mapping[str, str],
) -> None:
    if cooldown.count:
        LOGGER.warning(
            f"cooled down {cooldown.count} time{'s' if cooldown.count != 1 else ''} "
            f"for {format_duration(cooldown.total_seconds)} in total"
        )
    if not failed_this_run:
        LOGGER.info("every attempted work was cached")
        return
    LOGGER.warning(
        f"{len(failed_this_run)} works still failed after a second attempt; they stay "
        f"uncached, so the next run retries them. Details: {cache.failures_path}"
    )
    for work_id, message in list(failed_this_run.items())[:10]:
        LOGGER.warning(f"  work={work_id} {message}")
    if len(failed_this_run) > 10:
        LOGGER.warning(f"  ... and {len(failed_this_run) - 10} more in the failure log")


def validate_cache(
    report: ScanReport,
    cache: CacheStore,
    include_ambiguous: bool = False,
    require_binding: bool = True,
) -> CacheValidation:
    if require_binding:
        cache.verify_binding(report)
    records = cache.records()
    work_ids = _unique_work_ids(report, include_ambiguous)
    missing = tuple(work_id for work_id in work_ids if work_id not in records)
    unavailable = tuple(
        work_id for work_id in work_ids if work_id in records and records[work_id].availability == "unavailable"
    )
    incomplete = tuple(
        work_id for work_id in work_ids if work_id in records and records[work_id].availability == "incomplete"
    )
    complete = tuple(
        work_id for work_id in work_ids if work_id in records and records[work_id].availability == "ok"
    )
    mappings: dict[str, EpubMapping] = {}
    for mapping in report.mappings:
        if mapping.work_id in work_ids:
            mappings.setdefault(mapping.work_id, mapping)
    return CacheValidation(
        records=records,
        work_ids=tuple(work_ids),
        missing=missing,
        unavailable=unavailable,
        incomplete=incomplete,
        complete=complete,
        mappings=mappings,
    )


def display_value(value: object) -> str:
    return "<unavailable>" if value is None else str(value)


def print_cache_validation(
    validation: CacheValidation,
    sample_size: int = 10,
    emit: Callable[[str], None] = print,
) -> None:
    emit(f"works in scan: {len(validation.work_ids)}")
    emit(f"complete records: {len(validation.complete)}")
    emit(f"incomplete records: {len(validation.incomplete)}")
    emit(f"AO3-unavailable records: {len(validation.unavailable)}")
    emit(f"not yet cached: {len(validation.missing)}")
    emit("sample cache records:")
    for work_id in validation.work_ids[:sample_size]:
        record = validation.records.get(work_id)
        if record is None:
            emit(f"  work={work_id} <not cached>")
            continue
        mapping = validation.mappings.get(work_id)
        local_words = (
            display_value(mapping.local_words)
            if mapping is not None and mapping.local_metrics_calculated
            else "<not calculated>"
        )
        local_gfog = (
            display_value(mapping.local_gfog)
            if mapping is not None and mapping.local_metrics_calculated
            else "<not calculated>"
        )
        emit(
            f"  work={record.work_id} title={record.title or '<unavailable>'!r} "
            f"category={record.category or '<unavailable>'!r} "
            f"kudos={display_value(record.kudos)} hits={display_value(record.hits)} "
            f"bookmarks={display_value(record.bookmarks)} comments={display_value(record.comments)} "
            f"ao3_words={display_value(record.words)} "
            f"local_words={local_words} gfog={local_gfog}"
        )


def _mappings_for_write(
    report: ScanReport,
    records: dict[str, AO3FetchRecord],
    include_ambiguous: bool,
) -> tuple[list[tuple[EpubMapping, AO3FetchRecord]], list[EpubMapping]]:
    ambiguous_ids = {mapping.work_id for mapping in report.ambiguous_mappings}
    selected: list[tuple[EpubMapping, AO3FetchRecord]] = []
    skipped: list[EpubMapping] = []
    for mapping in report.mappings:
        if not include_ambiguous and mapping.work_id in ambiguous_ids:
            skipped.append(mapping)
            continue
        record = records.get(mapping.work_id)
        if record is not None:
            selected.append((mapping, record))
    return selected, skipped


def verify_report_inputs(
    report: ScanReport,
    library: Path,
    calibredb: str,
    mappings: Sequence[EpubMapping] | None = None,
) -> None:
    """Ensure the scan still describes this library immediately before writes."""

    verify_report_library(report, library)
    selected_mappings = tuple(report.mappings if mappings is None else mappings)
    LOGGER.info(
        f"revalidating {len(selected_mappings)} scanned EPUBs against the live library"
    )
    current_books = load_calibre_books(calibredb, library)
    current_ids = {
        int(cast(str | int, book["id"]))
        for book in current_books
        if str(book.get("id", "")).isdigit()
    }
    for index, mapping in enumerate(selected_mappings, start=1):
        if index % REVALIDATION_PROGRESS_EVERY == 0:
            LOGGER.info(f"  revalidated {index}/{len(selected_mappings)} EPUBs")
        if mapping.book_id not in current_ids:
            raise BackfillError(f"Calibre book {mapping.book_id} is no longer present")
        epub_path = library / mapping.epub_path
        if not epub_path.is_file():
            raise BackfillError(f"Scanned EPUB is no longer present: {epub_path}")
        if _book_directory_id(epub_path.parent) != mapping.book_id:
            raise BackfillError(f"Scanned EPUB moved away from Calibre book {mapping.book_id}")
        try:
            work_id, all_work_ids, preface_entry, malformed_preface, _ = _epub_preface_work_ids(epub_path)
        except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as error:
            raise BackfillError(f"Could not revalidate scanned EPUB {epub_path}: {error}") from error
        if (
            work_id != mapping.work_id
            or all_work_ids != mapping.all_work_ids
            or preface_entry != mapping.preface_entry
            or malformed_preface != mapping.malformed_preface
            or (len(all_work_ids) > 1) != mapping.ambiguous
        ):
            raise BackfillError(
                f"Scanned EPUB {epub_path} no longer matches the saved preface mapping"
            )
        if mapping.local_metrics_calculated:
            local_metrics = calculate_epub_metrics(epub_path)
            if (
                local_metrics.words != mapping.local_words
                or local_metrics.gfog != mapping.local_gfog
                or local_metrics.error != mapping.local_metrics_error
            ):
                raise BackfillError(
                    f"Scanned EPUB {epub_path} no longer matches the saved local metrics"
                )
    LOGGER.info(f"revalidated {len(selected_mappings)} EPUBs; the scan still matches the library")


def calculate_missing_local_metrics(
    report: ScanReport,
    library: Path,
    calibredb: str,
    limit: int | None = None,
) -> tuple[ScanReport, int]:
    """Calculate local metrics only for mapped books with blank local columns."""

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive when specified")
    require_calibre_closed()
    verify_report_library(report, library)
    verify_local_metric_columns(calibredb, library)
    existing_values = load_local_metric_values(calibredb, library)
    mappings = list(report.mappings)
    candidates = tuple(
        mapping
        for mapping in mappings
        if not mapping.local_metrics_calculated
        and any(
            existing_values.get(mapping.book_id, {}).get(label) in (None, "")
            for label, _, _, _ in LOCAL_METRIC_COLUMNS
        )
    )
    verify_report_inputs(report, library, calibredb, candidates)
    calculated_work_ids: set[str] = set()
    calculated_mappings = 0
    for index, mapping in enumerate(mappings):
        if mapping.local_metrics_calculated:
            continue
        current = existing_values.get(mapping.book_id, {})
        if all(current.get(label) not in (None, "") for label, _, _, _ in LOCAL_METRIC_COLUMNS):
            continue
        if limit is not None and calculated_mappings >= limit:
            break
        metrics = calculate_epub_metrics(library / mapping.epub_path)
        mappings[index] = replace(
            mapping,
            local_words=metrics.words,
            local_gfog=metrics.gfog,
            local_metrics_error=metrics.error,
            local_metrics_calculated=True,
        )
        calculated_work_ids.add(mapping.work_id)
        calculated_mappings += 1
    return (
        replace(
            report,
            mappings=tuple(mappings),
            local_metrics_calculated=report.local_metrics_calculated + len(calculated_work_ids),
        ),
        calculated_mappings,
    )


def _epub_backup_manifest_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.manifest.json")


def _backup_epub_once(epub_path: Path, backup_path: Path) -> None:
    """Create or verify one non-overwriting original EPUB backup."""

    manifest_path = _epub_backup_manifest_path(backup_path)
    if backup_path.exists() or manifest_path.exists():
        if not backup_path.is_file() or not manifest_path.is_file():
            raise BackfillError(f"EPUB backup is incomplete: {backup_path}")
        with manifest_path.open(encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict):
            raise BackfillError(f"EPUB backup manifest is invalid: {manifest_path}")
        if manifest.get("backup_size") != backup_path.stat().st_size:
            raise BackfillError(f"EPUB backup size differs from its manifest: {backup_path}")
        if manifest.get("backup_sha256") != sha256_file(backup_path):
            raise BackfillError(f"EPUB backup checksum differs from its manifest: {backup_path}")
        return

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source_size = epub_path.stat().st_size
    source_sha256 = sha256_file(epub_path)
    shutil.copy2(epub_path, backup_path)
    if epub_path.stat().st_size != source_size or sha256_file(epub_path) != source_sha256:
        raise BackfillError(f"EPUB changed while its backup was being created: {epub_path}")
    if backup_path.stat().st_size != source_size or sha256_file(backup_path) != source_sha256:
        raise BackfillError(f"EPUB backup does not match its source: {backup_path}")
    write_json_atomically(
        manifest_path,
        {
            "schema_version": 1,
            "source_path": str(epub_path.resolve()),
            "source_size": source_size,
            "source_sha256": source_sha256,
            "backup_size": backup_path.stat().st_size,
            "backup_sha256": sha256_file(backup_path),
            "created_at": utc_now(),
        },
    )


def _optional_local_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BackfillError("Calibre local Words value is boolean")
    try:
        return int(cast(str | int | float, value))
    except (TypeError, ValueError) as error:
        raise BackfillError("Calibre local Words value is invalid") from error


def _optional_local_float(value: object) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise BackfillError("Calibre local Gfog value is boolean")
    try:
        # Exactly as Calibre stores it: a baked EPUB must match the library.
        return float(cast(str | int | float, value))
    except (TypeError, ValueError) as error:
        raise BackfillError("Calibre local Gfog value is invalid") from error


def _portable_metadata_for_record(
    mapping: EpubMapping,
    record: AO3FetchRecord,
    current_local_values: dict[str, object],
) -> AO3Metadata:
    """Metadata to bake into a library EPUB, matching what Calibre holds.

    Calibre's existing Words/Gfog win, exactly as stored; the scan's own
    calculation fills in only where Calibre's cell is blank. That is the same
    rule the Calibre write follows, so the file and the library never disagree.
    """

    calibre_words = _optional_local_int(current_local_values.get("words"))
    calibre_gfog = _optional_local_float(current_local_values.get("gfog"))
    calculated = mapping.local_metrics_calculated
    local_words = calibre_words if calibre_words is not None else (mapping.local_words if calculated else None)
    local_gfog = calibre_gfog if calibre_gfog is not None else (mapping.local_gfog if calculated else None)
    return AO3Metadata(
        work_id=record.work_id,
        work_url=record.work_url,
        title=record.title,
        authors=record.authors,
        category=record.category,
        status=record.status,
        words=record.words,
        chapters=record.chapters,
        comments=record.comments,
        kudos=record.kudos,
        bookmarks=record.bookmarks,
        hits=record.hits,
        local_words=local_words,
        local_gfog=local_gfog,
    )


EPUB_PASS_PROGRESS_EVERY = 1000
AO3_COLUMN_LABELS = tuple(label for label, _, _, _ in AO3_COLUMNS)


def _stale_ao3_columns(
    candidates: Sequence[tuple[EpubMapping, AO3FetchRecord]],
    calibre_values: Mapping[int, Mapping[str, object]],
) -> list[tuple[int, str, object, object]]:
    """Cached AO3 values that Calibre does not hold yet (the write has not caught up)."""

    stale = []
    for mapping, record in candidates:
        current = calibre_values.get(mapping.book_id, {})
        for label, _, _, attribute in AO3_COLUMNS:
            cached = getattr(record, attribute)
            if cached is not None and cached != "" and current.get(label) != cached:
                stale.append((mapping.book_id, label, cached, current.get(label)))
    return stale


def _verify_baked_epub(
    epub_path: Path,
    metadata: AO3Metadata,
    expected_columns: Mapping[str, object],
) -> None:
    """Prove the file now carries what the library holds, as Calibre would read it."""

    stored = read_ao3_metadata(epub_path)
    if stored.work_id != metadata.work_id or stored.work_url != metadata.work_url:
        raise BackfillError(f"Baked EPUB has the wrong AO3 work: {epub_path}")
    file_columns = read_calibre_user_metadata(epub_path)
    for label, value in expected_columns.items():
        if value is None or value == "":
            continue
        if file_columns.get(label) != value:
            raise BackfillError(
                f"Baked EPUB column #{label} is {file_columns.get(label)!r}, library has {value!r}: {epub_path}"
            )


def enrich_existing_epubs(
    report: ScanReport,
    cache: CacheStore,
    *,
    calibredb: str,
    library: Path,
    backup_path: Path,
    epub_backup_dir: Path,
    limit: int | None = 1,
    allow_partial: bool = False,
    include_ambiguous: bool = False,
    embed_calibre: bool = True,
    calibre_debug: str = "calibre-debug",
    embedder: Callable[..., EmbedResult] = embed_calibre_metadata,
) -> dict[str, object]:
    """Bake each library book's full metadata into its EPUB, so the file stands alone.

    Four passes, in this order because each depends on the one before:

    1. back up every original EPUB (a rerun never overwrites a backup);
    2. have Calibre write its own metadata for the book into the file, without
       the cover: title, authors, tags, series, description, every custom column;
    3. add the AO3 identifier, statistics block, ``ao3:*`` fields, and column
       values on top, then check the file's columns match the library exactly;
    4. have Calibre record the files' new sizes.

    Every pass is safe to repeat, so a stopped run resumes by running it again.
    """

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive when specified")
    require_calibre_closed()
    verify_backup(backup_path, library)
    verify_report_library(report, library)
    verify_custom_columns(calibredb, library)
    verify_local_metric_columns(calibredb, library)
    validation = validate_cache(report, cache, include_ambiguous, require_binding=True)
    if validation.missing and not allow_partial:
        raise BackfillError(
            f"{len(validation.missing)} works are not cached; use --allow-partial for a reviewed batch"
        )
    selected, skipped_ambiguous = _mappings_for_write(report, validation.records, include_ambiguous)
    candidates: list[tuple[EpubMapping, AO3FetchRecord]] = []
    seen_book_ids: set[int] = set()
    for mapping, record in selected:
        if mapping.book_id not in seen_book_ids:
            seen_book_ids.add(mapping.book_id)
            candidates.append((mapping, record))
    if limit is not None:
        candidates = candidates[:limit]
    result: dict[str, object] = {
        "enriched": [],
        "failed": [],
        "skipped_ambiguous": [mapping.book_id for mapping in skipped_ambiguous],
        "not_cached": [mapping.book_id for mapping in report.mappings if mapping.work_id not in validation.records],
        "epub_backup_dir": str(epub_backup_dir),
    }
    if not candidates:
        LOGGER.info("no books to bake")
        return result

    verify_report_inputs(report, library, calibredb, tuple(mapping for mapping, _ in candidates))
    calibre_values = read_all_custom_values(calibredb, library)
    stale = _stale_ao3_columns(candidates, calibre_values)
    if stale:
        book_id, label, cached, current = stale[0]
        raise BackfillError(
            f"{len(stale)} cached AO3 values are not in Calibre yet (first: book {book_id} "
            f"#{label} cached {cached!r}, Calibre has {current!r}). Run `backfill.py write` "
            "first so the EPUBs and the library agree. Nothing has been changed."
        )

    total = len(candidates)
    LOGGER.info(f"pass 1/4: backing up {total} original EPUBs to {epub_backup_dir}")
    for index, (mapping, _) in enumerate(candidates, start=1):
        _backup_epub_once(library / mapping.epub_path, epub_backup_dir / mapping.epub_path)
        if index % EPUB_PASS_PROGRESS_EVERY == 0 or index == total:
            LOGGER.info(f"  backed up {index}/{total}")

    book_ids = [mapping.book_id for mapping, _ in candidates]
    if embed_calibre:
        LOGGER.info(f"pass 2/4: Calibre writes its metadata into {total} EPUBs (covers left out)")
        embedded = embedder(calibre_debug, library, book_ids, mode="embed")
        if embedded.failures:
            book_id, error = next(iter(embedded.failures.items()))
            raise BackfillError(
                f"Calibre could not embed metadata for {len(embedded.failures)} books (first: book "
                f"{book_id}: {error}). The originals are backed up in {epub_backup_dir}; the AO3 pass "
                "did not run."
            )
    else:
        LOGGER.info("pass 2/4: skipped (--no-calibre-metadata)")

    LOGGER.info("pass 3/4: adding AO3 metadata and checking each file against the library")
    enriched = cast(list[int], result["enriched"])
    failed = cast(list[dict[str, object]], result["failed"])
    for mapping, record in candidates:
        epub_path = library / mapping.epub_path
        current = calibre_values.get(mapping.book_id, {})
        try:
            metadata = _portable_metadata_for_record(mapping, record, current)
            enrich_epub(epub_path, metadata, calibre_columns=ALL_COLUMNS)
            validate_epub_file(epub_path)
            expected = dict(current) if embed_calibre else {}
            for label, _, _, attribute in ALL_COLUMNS:
                if getattr(metadata, attribute) is not None:
                    expected[label] = getattr(metadata, attribute)
            _verify_baked_epub(epub_path, metadata, expected)
        except (BackfillError, OSError, ValueError, ET.ParseError, zipfile.BadZipFile) as error:
            failed.append({
                "book_id": mapping.book_id,
                "work_id": mapping.work_id,
                "epub_path": mapping.epub_path,
                "error": str(error),
            })
            LOGGER.error(f"book {mapping.book_id}: {error}; stopping (original backed up in {epub_backup_dir})")
            break
        enriched.append(mapping.book_id)
        if len(enriched) % EPUB_PASS_PROGRESS_EVERY == 0 or len(enriched) == total:
            LOGGER.info(f"  baked {len(enriched)}/{total}")

    if enriched:
        LOGGER.info(f"pass 4/4: recording the new sizes of {len(enriched)} EPUBs in Calibre")
        sized = embedder(calibre_debug, library, enriched, mode="refresh-sizes")
        if sized.failures:
            failed.append({"error": f"Calibre could not record new sizes for {len(sized.failures)} books",
                           "book_ids": sorted(sized.failures)})
    return result


def write_calibre_values(
    report: ScanReport,
    cache: CacheStore,
    *,
    calibredb: str,
    library: Path,
    backup_path: Path,
    limit: int | None = 1,
    allow_partial: bool = False,
    include_ambiguous: bool = False,
    write_local_metrics: bool = False,
    replace_local_metrics: bool = False,
    calibre_debug: str = "calibre-debug",
    bulk_writer: Callable[
        [str, Path, Mapping[str, Mapping[int, object]], Mapping[str, str]], tuple[str, ...]
    ] = run_calibre_bulk_write,
) -> dict[str, object]:
    """Write approved AO3/category values and optionally blank local metrics.

    Selection is unchanged from the per-field ``calibredb`` writer: only ``ok``
    records, never an empty value, and local metrics only into blank cells
    unless replacement is requested. The values are then written column by
    column through Calibre's API in a single ``calibre-debug`` process.
    """

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive when specified")
    if replace_local_metrics and not write_local_metrics:
        raise ValueError("replace_local_metrics requires write_local_metrics")
    require_calibre_closed()
    verify_backup(backup_path, library)
    verify_custom_columns(calibredb, library)
    if write_local_metrics:
        verify_local_metric_columns(calibredb, library)
    validation = validate_cache(report, cache, include_ambiguous, require_binding=True)
    if validation.missing and not allow_partial:
        raise BackfillError(
            f"{len(validation.missing)} works are not cached; use --allow-partial for a reviewed batch"
        )
    records = validation.records
    candidates, skipped_ambiguous = _mappings_for_write(report, records, include_ambiguous)
    if limit is not None:
        candidates = candidates[:limit]
    if candidates:
        verify_report_inputs(
            report,
            library,
            calibredb,
            tuple(mapping for mapping, _ in candidates),
        )
    current_local_values = load_local_metric_values(calibredb, library) if write_local_metrics else {}

    updated: list[int] = []
    failed: list[dict[str, object]] = []
    skipped_unavailable: list[int] = []
    skipped_without_values: list[int] = []
    planned_values: dict[str, dict[str, str]] = {}
    plan: dict[str, dict[int, object]] = {}
    datatypes = {
        label: datatype
        for label, _, datatype, _ in (*AO3_COLUMNS, *LOCAL_METRIC_COLUMNS)
    }
    seen_book_ids: set[int] = set()
    for mapping, record in candidates:
        if mapping.book_id in seen_book_ids:
            continue
        seen_book_ids.add(mapping.book_id)
        if record.availability != "ok":
            skipped_unavailable.append(mapping.book_id)
            continue
        fields_to_write: list[tuple[str, object]] = []
        for label, _, _, attribute in AO3_COLUMNS:
            value = getattr(record, attribute)
            if value is not None and value != "":
                fields_to_write.append((label, value))
        if write_local_metrics:
            existing = current_local_values.get(mapping.book_id, {})
            for label, _, _, attribute in LOCAL_METRIC_COLUMNS:
                if not mapping.local_metrics_calculated:
                    continue
                value = getattr(mapping, attribute)
                if value is None:
                    continue
                if not replace_local_metrics and existing.get(label) not in (None, ""):
                    continue
                fields_to_write.append((label, value))
        if not fields_to_write:
            skipped_without_values.append(mapping.book_id)
            continue
        planned_values[str(mapping.book_id)] = {label: str(value) for label, value in fields_to_write}
        for label, value in fields_to_write:
            plan.setdefault(label, {})[mapping.book_id] = value
        updated.append(mapping.book_id)

    written_values = planned_values
    if plan:
        LOGGER.info(
            f"writing {sum(len(values) for values in plan.values())} values for "
            f"{len(updated)} books across {len(plan)} columns through Calibre's API"
        )
        require_calibre_closed()
        try:
            bulk_writer(calibre_debug, library, plan, datatypes)
        except BulkWriteError as error:
            # Each column is written in one call, so a failure leaves the
            # columns finished before it fully written and the rest untouched.
            completed = set(error.completed)
            written_values = {
                book_id: {label: value for label, value in fields.items() if label in completed}
                for book_id, fields in planned_values.items()
            }
            written_values = {book_id: fields for book_id, fields in written_values.items() if fields}
            updated = [int(book_id) for book_id in written_values]
            failed.append(
                {
                    "error": str(error),
                    "completed_columns": sorted(completed),
                    "incomplete_columns": sorted(set(plan) - completed),
                }
            )

    return {
        "updated": updated,
        "skipped_ambiguous": [mapping.book_id for mapping in skipped_ambiguous],
        "skipped_unavailable": skipped_unavailable,
        "skipped_without_values": skipped_without_values,
        "written_values": written_values,
        "not_cached": [
            mapping.book_id
            for mapping in report.mappings
            if mapping.work_id not in records
        ],
        "failed": failed,
    }


def render_scan_summary(report: ScanReport, sample_size: int = 10) -> str:
    lines = [
        f"Calibre books: {report.book_count}",
        f"EPUB files: {report.epub_count}",
        f"Existing AO3 identifiers: {report.existing_ao3_identifier_count}",
        f"Primary AO3 mappings: {len(report.mappings)}",
        f"Missing primary AO3 URLs: {len(report.missing_work_ids)}",
        f"Malformed EPUB/XHTML records: {len(report.malformed_epubs)}",
        f"Ambiguous preface mappings: {len(report.ambiguous_mappings)}",
        f"Duplicate work IDs: {len(report.duplicate_work_ids)}",
        f"Local metrics algorithm: {report.local_metrics_algorithm}",
        f"Local metrics calculated: {report.local_metrics_calculated}"
        + (
            f" (initial limit {report.local_metrics_limit})"
            if report.local_metrics_limit is not None
            else " (all mapped EPUBs)"
        ),
        "Sample mappings:",
    ]
    for mapping in report.mappings[:sample_size]:
        local_words = (
            display_value(mapping.local_words)
            if mapping.local_metrics_calculated
            else "<not calculated>"
        )
        local_gfog = (
            display_value(mapping.local_gfog)
            if mapping.local_metrics_calculated
            else "<not calculated>"
        )
        lines.append(
            f"  book={mapping.book_id} work={mapping.work_id} "
            f"local_words={local_words} gfog={local_gfog} epub={mapping.epub_path}"
        )
    return "\n".join(lines)


def _path_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _ensure_cache_location(cache_dir: Path, library: Path) -> None:
    protected = (library, REPO_ROOT, Path("/home/drifter/repos/ao3downloadernew"))
    if any(_path_inside(cache_dir, parent) for parent in protected):
        raise BackfillError("Cache and reports must be outside the Calibre library and Git repositories")


def _cache_paths(cache_dir: Path) -> tuple[Path, Path]:
    return cache_dir / SCAN_REPORT_NAME, cache_dir / CACHE_NAME


def _add_common_paths(parser: argparse.ArgumentParser, *, require_library: bool = True) -> None:
    parser.add_argument("--library", type=Path, required=require_library, help="Calibre library path")
    parser.add_argument("--calibredb", default="calibredb", help="calibredb executable")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    scan = subparsers.add_parser("scan", help="read EPUBs and write a JSON scan report")
    _add_common_paths(scan)
    scan.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    scan.add_argument("--report", type=Path, default=None)
    scan.add_argument("--sample", type=int, default=10)
    metric_scope = scan.add_mutually_exclusive_group()
    metric_scope.add_argument(
        "--metrics-limit",
        type=int,
        default=DEFAULT_BATCH_SIZE,
        help="calculate local metrics for this many non-ambiguous work IDs (0 disables; default: 25)",
    )
    metric_scope.add_argument(
        "--metrics-all",
        action="store_true",
        help="calculate local metrics for every mapped EPUB; this can take a long time",
    )

    processes = subparsers.add_parser("processes", help="show active Calibre processes")

    backup = subparsers.add_parser("backup", help="create and verify a metadata.db backup")
    _add_common_paths(backup)
    backup.add_argument(
        "--destination",
        type=Path,
        default=None,
        help="where to write the backup (default: a new timestamped file in the cache dir)",
    )
    backup.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)

    columns = subparsers.add_parser("setup-columns", help="create and verify AO3 backfill custom columns")
    _add_common_paths(columns)
    columns.add_argument("--backup", type=Path, required=True)
    columns.add_argument("--approve-columns", action="store_true")

    verify_columns = subparsers.add_parser("verify-columns", help="verify AO3 custom columns read-only")
    _add_common_paths(verify_columns)

    missing_metrics = subparsers.add_parser(
        "calculate-missing-metrics",
        help="calculate local metrics only for mapped books with blank Words/Gfog values",
    )
    _add_common_paths(missing_metrics)
    missing_metrics.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    missing_metrics.add_argument("--report", type=Path, default=None)
    missing_metrics.add_argument("--limit", type=int, default=None)

    enrich = subparsers.add_parser(
        "enrich-epubs",
        help="bake each library book's full metadata (Calibre's and AO3's) into its EPUB file",
    )
    _add_common_paths(enrich)
    enrich.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    enrich.add_argument("--report", type=Path, default=None)
    enrich.add_argument("--backup", type=Path, required=True)
    enrich.add_argument("--epub-backup-dir", type=Path, required=True)
    enrich.add_argument("--limit", type=int, default=1)
    enrich.add_argument("--allow-partial", action="store_true")
    enrich.add_argument("--include-ambiguous", action="store_true")
    enrich.add_argument("--approve-epub-write", action="store_true")
    enrich.add_argument(
        "--no-calibre-metadata",
        action="store_true",
        help="add only the AO3 metadata; do not write Calibre's own title, tags, series, etc. into the files",
    )
    enrich.add_argument("--calibre-debug", default="calibre-debug", help="calibre-debug executable")

    fetch = subparsers.add_parser("fetch", help="fetch current AO3 metadata into the JSONL cache")
    _add_common_paths(fetch)
    fetch.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    fetch.add_argument("--report", type=Path, default=None)
    fetch.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"seconds between work requests (default: {DEFAULT_DELAY_SECONDS:g}, "
             f"minimum: {MINIMUM_DELAY_SECONDS:g})",
    )
    fetch.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE)
    fetch.add_argument("--continue-after-review", action="store_true")
    fetch.add_argument("--refresh", action="store_true")
    fetch.add_argument("--include-ambiguous", action="store_true")
    fetch.add_argument(
        "--retry-failed-once",
        action="store_true",
        help="retry incomplete/unavailable records and unexpected HTML once, then stop on a second failure",
    )
    fetch.add_argument(
        "--use-env-credentials",
        action="store_true",
        help="authenticate with AO3_USERNAME and AO3_PASSWORD",
    )
    fetch.add_argument("--approve-network", action="store_true")
    fetch.add_argument(
        "--keep-going",
        action="store_true",
        help="for unattended runs: record a failed work and move on, and cool down "
             "instead of exiting when AO3 or the session is the problem",
    )

    validate = subparsers.add_parser("validate-cache", help="validate cached AO3 records read-only")
    validate.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    validate.add_argument("--report", type=Path, default=None)
    validate.add_argument("--sample", type=int, default=10)
    validate.add_argument("--include-ambiguous", action="store_true")

    preview = subparsers.add_parser(
        "preview-refresh",
        help="show the bounded scan-order refresh mappings without making requests",
    )
    preview.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    preview.add_argument("--report", type=Path, default=None)
    preview.add_argument("--limit", type=int, default=DEFAULT_BATCH_SIZE)
    preview.add_argument("--include-ambiguous", action="store_true")

    snapshot = subparsers.add_parser(
        "snapshot-cache",
        help="copy the current JSONL cache to a new external path without overwriting",
    )
    snapshot.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    snapshot.add_argument("--destination", type=Path, required=True)

    write = subparsers.add_parser("write", help="write cached values to AO3 custom columns")
    _add_common_paths(write)
    write.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    write.add_argument("--report", type=Path, default=None)
    write.add_argument("--backup", type=Path, required=True)
    write.add_argument("--limit", type=int, default=1)
    write.add_argument("--allow-partial", action="store_true")
    write.add_argument("--include-ambiguous", action="store_true")
    write.add_argument("--write-local-metrics", action="store_true")
    write.add_argument("--replace-local-metrics", action="store_true")
    write.add_argument("--approve-write", action="store_true")
    write.add_argument("--calibre-debug", default="calibre-debug", help="calibre-debug executable")

    library = subparsers.add_parser("verify-library", help="verify populated values and numeric Calibre queries")
    _add_common_paths(library)

    check_auth = subparsers.add_parser(
        "check-auth",
        help="log in to AO3 and stop, to prove credentials before a multi-day fetch",
    )
    check_auth.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE_DIR)
    check_auth.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS)
    check_auth.add_argument("--approve-network", action="store_true")

    for subparser in subparsers.choices.values():
        subparser.add_argument(
            "--log-dir",
            type=Path,
            default=None,
            help=f"directory for this run's log file (default: {DEFAULT_LOG_DIR})",
        )

    return parser


def _report_and_cache(args: argparse.Namespace) -> tuple[Path, Path]:
    cache_dir = args.cache_dir
    _ensure_cache_location(cache_dir, args.library if hasattr(args, "library") else Path("/tmp"))
    default_report, cache_path = _cache_paths(cache_dir)
    return args.report or default_report, cache_path


def _log_block(logger: logging.Logger, text: str) -> None:
    for line in text.splitlines():
        logger.info(line)


def _new_backup_path(directory: Path, now: datetime | None = None) -> Path:
    """Name a backup that does not exist yet; backups are never overwritten."""

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    candidate = directory / f"metadata.db.{stamp}.backup"
    suffix = 2
    while candidate.exists() or candidate.with_name(f"{candidate.name}.manifest.json").exists():
        candidate = directory / f"metadata.db.{stamp}-{suffix}.backup"
        suffix += 1
    return candidate


def _write_result_report(result: Mapping[str, object], directory: Path) -> Path:
    """Keep the full per-book write result on disk; the terminal gets a summary."""

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"write-result-{stamp}.json"
    write_json_atomically(path, result)
    return path


def _log_write_summary(logger: logging.Logger, result: Mapping[str, object], result_path: Path) -> None:
    def count(key: str) -> int:
        value = result.get(key)
        return len(value) if isinstance(value, (list, dict)) else 0

    written = cast(dict[str, dict[str, str]], result.get("written_values", {}))
    logger.info(f"books updated: {count('updated')} ({sum(len(fields) for fields in written.values())} values)")
    logger.info(f"skipped, not ok on AO3: {count('skipped_unavailable')}")
    logger.info(f"skipped, nothing to write: {count('skipped_without_values')}")
    logger.info(f"skipped, ambiguous: {count('skipped_ambiguous')}")
    logger.info(f"not cached: {count('not_cached')}")
    verification = result.get("post_write_verification")
    if isinstance(verification, dict):
        logger.info(
            f"verified {verification.get('fields')} values on {verification.get('books')} books "
            "through calibredb and SQLite"
        )
    if "post_write_verification_error" in result:
        logger.error(f"verification failed: {result['post_write_verification_error']}")
    for failure in cast(list[dict[str, object]], result.get("failed", [])):
        logger.error(f"write failure: {failure.get('error')}")
        logger.error(f"  completed columns: {failure.get('completed_columns')}")
        logger.error(f"  not written: {failure.get('incomplete_columns')}")
    logger.info(f"full result: {result_path}")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run = configure_logging(args.command, log_dir=args.log_dir)
    logger = run.logger
    logger.info(f"backfill {args.command} \u00b7 log file: {run.log_path}")

    if args.command == "processes":
        processes = running_calibre_processes()
        if not processes:
            logger.info("no Calibre processes are using the library")
        for process in processes:
            logger.info(process)
        return 0

    if args.command == "check-auth":
        if not args.approve_network:
            raise BackfillError("check-auth makes AO3 requests and needs --approve-network")
        if not math.isfinite(args.delay) or args.delay < MINIMUM_DELAY_SECONDS:
            raise BackfillError(f"--delay must be at least {MINIMUM_DELAY_SECONDS:g} seconds")
        cache = CacheStore(_cache_paths(args.cache_dir)[1])
        credentials = load_run_credentials()
        log_banner(
            logger,
            "AO3 authentication check",
            [
                ("credential source", credentials.source),
                ("request pacing", cache.context_path if cache.has_context() else f"{args.delay:g}s (no cache context)"),
            ],
        )
        session = requests.Session()
        try:
            scheduler = CacheRequestScheduler(cache, args.delay) if cache.has_context() else None
            login_authenticated_session(
                session,
                credentials.username,
                credentials.password,
                source=credentials.source,
                delay_seconds=args.delay,
                before_request=None if scheduler is None else scheduler.before_request,
                request_started_callback=None if scheduler is None else scheduler.request_started,
                request_deferred_callback=None if scheduler is None else scheduler.defer_requests,
            )
        finally:
            session.close()
        logger.info("credentials are valid; no work pages were requested")
        return 0

    if args.command == "scan":
        if not args.metrics_all and args.metrics_limit < 0:
            raise BackfillError("--metrics-limit must not be negative")
        report = scan_library(
            args.library,
            args.calibredb,
            None if args.metrics_all else args.metrics_limit,
        )
        report_path = args.report or _cache_paths(args.cache_dir)[0]
        _ensure_cache_location(report_path.parent, args.library)
        write_scan_report(report, report_path)
        _log_block(logger, render_scan_summary(report, args.sample))
        logger.info(f"Scan report: {report_path}")
        if report.missing_work_ids:
            logger.info("EPUBs with no identifiable primary AO3 URL:")
            for issue in report.missing_work_ids:
                logger.info(f"  book={issue.book_id} epub={issue.epub_path} reason={issue.reason}")
        return 0

    if args.command == "backup":
        destination = args.destination or _new_backup_path(args.cache_dir)
        _ensure_cache_location(destination.parent, args.library)
        backup_path = create_backup(args.library, destination)
        logger.info(f"Backup: {backup_path} bytes={backup_path.stat().st_size}")
        logger.info(f'For the next write or setup-columns, pass: --backup "{backup_path}"')
        return 0

    if args.command == "setup-columns":
        if not args.approve_columns:
            raise BackfillError("Column creation requires --approve-columns after explicit review")
        created = setup_custom_columns(args.calibredb, args.library, args.backup)
        logger.info(f"Created columns: {', '.join(created) if created else '<none; all already existed>'}")
        logger.info("Verified calibredb custom_columns --details and read-only metadata.db schema.")
        return 0

    if args.command == "verify-columns":
        verified = verify_custom_columns(args.calibredb, args.library)
        for label, details in verified.items():
            logger.info(f"#{label}: datatype={details['datatype']} name={details['name']}")
        return 0

    if args.command == "calculate-missing-metrics":
        report_path, _ = _report_and_cache(args)
        report, calculated = calculate_missing_local_metrics(
            load_scan_report(report_path),
            args.library,
            args.calibredb,
            args.limit,
        )
        write_scan_report(report, report_path)
        logger.info(f"Calculated local metrics for {calculated} mapped EPUBs with blank local values.")
        logger.info(f"Scan report: {report_path}")
        return 0

    if args.command == "enrich-epubs":
        if not args.approve_epub_write:
            raise BackfillError("EPUB enrichment requires --approve-epub-write after explicit review")
        report_path, cache_path = _report_and_cache(args)
        _ensure_cache_location(args.epub_backup_dir, args.library)
        result = enrich_existing_epubs(
            load_scan_report(report_path),
            CacheStore(cache_path),
            calibredb=args.calibredb,
            library=args.library,
            backup_path=args.backup,
            epub_backup_dir=args.epub_backup_dir,
            limit=args.limit,
            allow_partial=args.allow_partial,
            include_ambiguous=args.include_ambiguous,
            embed_calibre=not args.no_calibre_metadata,
            calibre_debug=args.calibre_debug,
        )
        result_path = _write_result_report(result, cache_path.parent)
        logger.info(f"books baked: {len(cast(list[int], result['enriched']))}")
        logger.info(f"skipped, ambiguous: {len(cast(list[int], result['skipped_ambiguous']))}")
        logger.info(f"not cached: {len(cast(list[int], result['not_cached']))}")
        logger.info(f"original EPUBs backed up in: {result['epub_backup_dir']}")
        logger.info(f"full result: {result_path}")
        if result["failed"]:
            for failure in cast(list[dict[str, object]], result["failed"]):
                logger.error(f"failure: {failure}")
            raise BackfillError("EPUB baking stopped part-way; rerun the same command after fixing the cause")
        return 0

    if args.command == "fetch":
        if not args.approve_network:
            raise BackfillError("Network requests require --approve-network after explicit review approval")
        if not math.isfinite(args.delay) or args.delay < MINIMUM_DELAY_SECONDS:
            raise BackfillError(f"--delay must be at least {MINIMUM_DELAY_SECONDS:g} seconds")
        report_path, cache_path = _report_and_cache(args)
        report = load_scan_report(report_path)
        with interrupt_guard(logger, unit="work") as guard:
            fetched = fetch_pending(
                report,
                CacheStore(cache_path),
                calibredb=args.calibredb,
                library=args.library,
                delay_seconds=args.delay,
                limit=args.limit,
                continue_after_review=args.continue_after_review,
                refresh=args.refresh,
                include_ambiguous=args.include_ambiguous,
                use_env_credentials=args.use_env_credentials,
                retry_failed_once=args.retry_failed_once,
                sleep_fn=guard.sleep,
                keep_going=args.keep_going,
            )
        logger.info(f"Fetched and cached {len(fetched)} works sequentially.")
        if len(fetched) == DEFAULT_BATCH_SIZE and not args.continue_after_review:
            logger.info("Initial batch complete; paused for review before any larger batch.")
        return 0

    if args.command == "validate-cache":
        report_path, cache_path = _report_and_cache(args)
        validation = validate_cache(
            load_scan_report(report_path),
            CacheStore(cache_path),
            args.include_ambiguous,
            require_binding=True,
        )
        print_cache_validation(validation, args.sample, emit=logger.info)
        cache = CacheStore(cache_path)
        unresolved = {
            work_id: message
            for work_id, message in cache.failed_work_ids().items()
            if work_id not in validation.records
        }
        logger.info(f"recorded fetch failures not yet cached: {len(unresolved)}")
        if unresolved:
            logger.info(f"  details: {cache.failures_path}")
        return 0

    if args.command == "preview-refresh":
        report_path, cache_path = _report_and_cache(args)
        report = load_scan_report(report_path)
        cache = CacheStore(cache_path)
        cache.verify_binding(report)
        _log_block(
            logger,
            render_refresh_preview(
                report,
                cache.records(),
                args.limit,
                args.include_ambiguous,
            ),
        )
        return 0

    if args.command == "snapshot-cache":
        _ensure_cache_location(args.destination.parent, Path("/tmp"))
        snapshot = CacheStore(_cache_paths(args.cache_dir)[1]).snapshot(args.destination)
        logger.info(f"Cache snapshot: {snapshot} bytes={snapshot.stat().st_size}")
        return 0

    if args.command == "write":
        if not args.approve_write:
            raise BackfillError("Calibre writes require --approve-write after cache validation")
        report_path, cache_path = _report_and_cache(args)
        result = write_calibre_values(
            load_scan_report(report_path),
            CacheStore(cache_path),
            calibredb=args.calibredb,
            library=args.library,
            backup_path=args.backup,
            limit=args.limit,
            allow_partial=args.allow_partial,
            include_ambiguous=args.include_ambiguous,
            write_local_metrics=args.write_local_metrics,
            replace_local_metrics=args.replace_local_metrics,
            calibre_debug=args.calibre_debug,
        )
        written = cast(dict[str, dict[str, str]], result["written_values"])
        logger.info("verifying written values through calibredb and read-only SQLite")
        try:
            result["post_write_verification"] = verify_written_values(args.calibredb, args.library, written)
        except (BackfillError, RuntimeError, OSError, sqlite3.Error, ValueError) as error:
            result["post_write_verification_error"] = str(error)
        result_path = _write_result_report(result, cache_path.parent)
        _log_write_summary(logger, result, result_path)
        if result["failed"]:
            raise BackfillError("The Calibre write stopped part-way; see the summary above")
        if "post_write_verification_error" in result:
            raise BackfillError("Post-write Calibre verification failed")
        return 0

    if args.command == "verify-library":
        _log_block(
            logger,
            json.dumps(verify_library_values(args.calibredb, args.library), indent=2, sort_keys=True),
        )
        return 0

    raise BackfillError(f"Unknown command: {args.command}")


def run() -> None:
    """Command-line entry point used by ``backfill.py``."""

    try:
        raise SystemExit(main())
    except RunInterrupted as error:
        LOGGER.warning(f"{error}; rerun the same command to resume from the cache")
        raise SystemExit(130) from error
    except ArchiverError as error:
        LOGGER.error(str(error))
        raise SystemExit(2) from error
