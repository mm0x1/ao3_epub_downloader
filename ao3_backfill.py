"""Safely backfill AO3 engagement metadata into an existing Calibre library.

The scanner only reads EPUBs. The network stage only writes a JSONL cache, and
the Calibre stage only uses ``calibredb set_custom`` for the AO3 columns.
"""

from __future__ import annotations

import argparse
import ast
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import posixpath
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import cast
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

import fcntl
import requests

from credentials import (
    DEFAULT_DOTENV_PATH,
    AO3Credentials,
    CredentialError,
    load_ao3_credentials,
)
from run_log import (
    DEFAULT_LOG_DIR,
    ProgressTracker,
    RunInterrupted,
    configure_logging,
    format_clock,
    format_duration,
    interrupt_guard,
    log_banner,
    redact_secrets,
    register_secret,
)

from ao3_metadata import (
    AO3Metadata,
    canonical_work_url,
    enrich_epub_portable,
    parse_ao3_metadata,
    read_ao3_metadata,
    validate_epub_file,
)
from calibre_sync import BACKFILL_CUSTOM_COLUMNS, run_calibredb, sanitized_child_environment
from local_metrics import LOCAL_METRICS_ALGORITHM, calculate_epub_metrics


DEFAULT_DELAY_SECONDS = 30.0
# The default is deliberately cautious, but it is not an AO3-published limit.
# ao3downloadernew reserves its 30s for listing/search pages and uses no delay
# at all between individual work pages, which is what this tool fetches. The
# floor exists to stop a typo turning into a flood, not to enforce 30s.
MINIMUM_DELAY_SECONDS = 5.0
DEFAULT_TIMEOUT_SECONDS = 60.0
DEFAULT_BATCH_SIZE = 25
DEFAULT_CACHE_DIR = Path.home() / ".local" / "share" / "ao3-calibre-backfill"
SCAN_REPORT_NAME = "scan.json"
CACHE_NAME = "ao3-cache.jsonl"
BACKUP_NAME = "metadata.db.backup"
COUNTER_ATTRIBUTES = ("kudos", "hits", "bookmarks", "comments", "words")
# Cloudflare's 5xx range means Cloudflare could not reach or talk to AO3's
# origin. That is an AO3-side outage, not a bot challenge, and it is transient.
CLOUDFLARE_ORIGIN_STATUS_CODES = frozenset({520, 521, 522, 523, 524, 525, 526, 527, 530})
TRANSIENT_STATUS_CODES = frozenset({500, 502, 503, 504}) | CLOUDFLARE_ORIGIN_STATUS_CODES
# AO3 emits transient 5xx and Cloudflare origin errors routinely. These get
# their own retry budget so that --retry-failed-once, which is about unexpected
# HTML, does not also shrink tolerance for a passing AO3 blip to one attempt.
MAX_TRANSIENT_ATTEMPTS = 8
TRANSIENT_MAX_BACKOFF_SECONDS = 300.0
# AO3 answers a rate limit with an exact Retry-After, typically a five-minute
# pause. Waiting it out is the correct response, so this budget is generous and
# separate from max_attempts; a run should not die because AO3 asked it to slow
# down twice in 40 hours.
MAX_RATE_LIMIT_ATTEMPTS = 10
RETRY_AFTER_DATE_SKEW_SECONDS = 0.0
REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})
MAX_AO3_REDIRECTS = 5
LOCAL_METRIC_COLUMNS = (
    ("words", "Words", "int", "local_words"),
    ("gfog", "Gfog", "float", "local_gfog"),
)

# Keep the old public name available to existing callers/tests while the
# backfill explicitly uses the extended contract.
CUSTOM_COLUMNS = BACKFILL_CUSTOM_COLUMNS

LOGGER = logging.getLogger("ao3.backfill")
PROGRESS_SUMMARY_EVERY = 25
REVALIDATION_PROGRESS_EVERY = 2000
# --keep-going: this many consecutive per-work failures points at AO3 or the
# session rather than the works, so the run pauses instead of burning through
# the queue while something global is wrong.
FAILURE_STREAK_COOLDOWN_THRESHOLD = 5
COOLDOWN_SCHEDULE_SECONDS = (300.0, 900.0, 1800.0, 3600.0)
LONG_WAIT_ANNOUNCE_SECONDS = 60.0


class BackfillError(RuntimeError):
    """Base error for a safety-gated backfill operation."""


class ColumnConfigurationError(BackfillError):
    """The Calibre custom columns do not match the required schema."""


class CalibreInUseError(BackfillError):
    """A Calibre process is using the library during a write operation."""


class NetworkStopError(BackfillError):
    """AO3 returned a response for which the policy requires stopping."""


class UnexpectedHTML(NetworkStopError):
    """The response was not a recognizable AO3 work page."""


class RepeatedCloudflare(NetworkStopError):
    """Cloudflare responses repeated during a single fetch."""


class RepeatedRateLimit(NetworkStopError):
    """AO3 rate-limited repeated requests."""


class AuthenticationFailure(NetworkStopError):
    """AO3 indicated that authentication is required or failed."""


class CredentialsRejected(AuthenticationFailure):
    """AO3 refused the username/password itself; retrying cannot help.

    Kept distinct so an unattended run stops instead of repeatedly submitting
    credentials that are known to be wrong.
    """


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
class AO3FetchRecord:
    """A durable, sanitized result for one AO3 work-page request."""

    work_id: str
    work_url: str
    fetched_at: str
    availability: str
    title: str | None = None
    authors: tuple[str, ...] = ()
    category: str | None = None
    status: str | None = None
    chapters: str | None = None
    words: int | None = None
    comments: int | None = None
    kudos: int | None = None
    bookmarks: int | None = None
    hits: int | None = None
    http_status: int | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["authors"] = list(self.authors)
        return {"schema_version": 1, **result}

    @classmethod
    def from_dict(cls, value: dict[str, object]) -> "AO3FetchRecord":
        work_id = str(value.get("work_id", ""))
        if not re.fullmatch(r"[0-9]+", work_id):
            raise ValueError("cache field work_id must be numeric")
        work_url = str(value.get("work_url", ""))
        if work_url != canonical_work_url(work_id):
            raise ValueError("cache field work_url does not match work_id")
        availability = str(value.get("availability", ""))
        if availability not in {"ok", "incomplete", "unavailable"}:
            raise ValueError("cache field availability is invalid")
        fetched_at = str(value.get("fetched_at", ""))
        if not fetched_at:
            raise ValueError("cache field fetched_at is empty")
        authors = value.get("authors", [])
        if isinstance(authors, str):
            author_values = (authors,)
        elif isinstance(authors, list):
            author_values = tuple(str(author) for author in authors)
        else:
            author_values = ()

        def optional_int(name: str) -> int | None:
            raw = value.get(name)
            if raw is None or raw == "":
                return None
            if isinstance(raw, bool):
                raise ValueError(f"cache field {name} must not be boolean")
            if isinstance(raw, int):
                if raw < 0:
                    raise ValueError(f"cache field {name} must not be negative")
                return raw
            if isinstance(raw, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)", raw):
                return int(raw)
            raise ValueError(f"cache field {name} is not a non-negative integer")

        raw_http_status = value.get("http_status")
        http_status: int | None = None
        if raw_http_status is not None:
            if isinstance(raw_http_status, bool):
                raise ValueError("cache field http_status must not be boolean")
            if isinstance(raw_http_status, int):
                http_status = raw_http_status
            elif isinstance(raw_http_status, str) and re.fullmatch(r"[0-9]+", raw_http_status):
                http_status = int(raw_http_status)
            else:
                raise ValueError("cache field http_status is invalid")
            if not 100 <= http_status <= 599:
                raise ValueError("cache field http_status is outside the HTTP range")

        return cls(
            work_id=work_id,
            work_url=work_url,
            fetched_at=fetched_at,
            availability=availability,
            title=str(value["title"]) if value.get("title") is not None else None,
            authors=author_values,
            category=str(value["category"]) if value.get("category") is not None else None,
            status=str(value["status"]) if value.get("status") is not None else None,
            chapters=str(value["chapters"]) if value.get("chapters") is not None else None,
            words=optional_int("words"),
            comments=optional_int("comments"),
            kudos=optional_int("kudos"),
            bookmarks=optional_int("bookmarks"),
            hits=optional_int("hits"),
            http_status=http_status,
            error=str(value["error"]) if value.get("error") is not None else None,
        )

    def value_for(self, attribute: str) -> str:
        value = getattr(self, attribute)
        return "" if value is None else str(value)


@dataclass(frozen=True)
class CacheValidation:
    records: dict[str, AO3FetchRecord]
    work_ids: tuple[str, ...]
    missing: tuple[str, ...]
    unavailable: tuple[str, ...]
    incomplete: tuple[str, ...]
    complete: tuple[str, ...]
    mappings: dict[str, EpubMapping]


class _WorkLinkParser(HTMLParser):
    """Find work links in document order without requiring valid XHTML."""

    pattern = re.compile(
        r"(?:https?://)?(?:www\.)?archiveofourown\.org/works/(\d+)(?:[/?#]|$)"
        r"|/works/(\d+)(?:[/?#]|$)",
        re.IGNORECASE,
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.work_ids: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_anchor(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._handle_anchor(tag, attrs)

    def _handle_anchor(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() != "a":
            return
        href = dict(attrs).get("href") or ""
        match = self.pattern.search(href)
        if match:
            work_id = match.group(1) or match.group(2)
            if work_id not in self.work_ids:
                self.work_ids.append(work_id)


class _WorkPageTextParser(HTMLParser):
    """Extract title and author text from an AO3 work page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._title_depth: int | None = None
        self._title_parts: list[str] = []
        self._author_depth: int | None = None
        self._author_parts: list[str] = []
        self.authors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag.casefold() == "h2" and "title" in classes and self._title_depth is None:
            self._title_depth = self._depth
            self._title_parts = []
        if tag.casefold() == "a" and "author" in (attributes.get("rel") or "").split():
            self._author_depth = self._depth
            self._author_parts = []

    def handle_data(self, data: str) -> None:
        if self._title_depth is not None and self._depth >= self._title_depth:
            self._title_parts.append(data)
        if self._author_depth is not None and self._depth >= self._author_depth:
            self._author_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._author_depth == self._depth:
            author = " ".join("".join(self._author_parts).split())
            if author and author not in self.authors:
                self.authors.append(author)
            self._author_depth = None
            self._author_parts = []
        if self._title_depth == self._depth:
            self._title_depth = None
        self._depth = max(0, self._depth - 1)

    @property
    def title(self) -> str | None:
        value = " ".join("".join(self._title_parts).split())
        return value or None


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


def load_calibre_books(calibredb: str, library: Path) -> list[dict[str, object]]:
    output = run_calibredb(
        calibredb,
        library,
        "list",
        "--for-machine",
        "--fields",
        "id,title,authors,formats,identifiers",
    )
    raw_books = json.loads(output)
    if not isinstance(raw_books, list):
        raise BackfillError("calibredb returned an unexpected book list")
    return [book for book in raw_books if isinstance(book, dict)]


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


def _write_json_atomically(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_scan_report(report: ScanReport, report_path: Path) -> None:
    _write_json_atomically(report_path, report.to_dict())


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
            "failed_at": _utc_now(),
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

    @contextmanager
    def fetch_lock(self):
        """Hold a run-scoped lock so only one fetch process can ever be live.

        This is separate from ``operation_lock`` on purpose: a fetch runs for
        days, and read-only commands must still be able to inspect the cache
        while it runs.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.fetch_lock_path.open("a+", encoding="utf-8") as lock:
            try:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                lock.seek(0)
                holder = lock.read().strip() or "an unknown process"
                raise BackfillError(
                    f"Another fetch already holds {self.fetch_lock_path} ({holder}). "
                    "Never run two fetch processes against one cache."
                ) from error
            lock.seek(0)
            lock.truncate()
            lock.write(f"pid {os.getpid()}\n")
            lock.flush()
            try:
                yield
            finally:
                lock.seek(0)
                lock.truncate()
                lock.flush()
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

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
                _write_json_atomically(self.context_path, actual)
            return
        if self.path.exists() and self.path.stat().st_size > 0:
            raise BackfillError(
                f"Non-empty cache has no provenance context; refusing to adopt it: {self.path}"
            )
        _write_json_atomically(
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
        _write_json_atomically(self.context_path, context)

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
        _write_json_atomically(self.context_path, context)

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
        _write_json_atomically(self.context_path, context)

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
            if _sha256_file(destination) != _sha256_file(self.path):
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


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_page_title_and_authors(html: str) -> tuple[str | None, tuple[str, ...]]:
    parser = _WorkPageTextParser()
    parser.feed(html)
    parser.close()
    return parser.title, tuple(parser.authors)


def _html_contains_work_id(html: str, work_id: str) -> bool:
    return bool(
        re.search(
            rf"(?:https?://)?(?:www\.)?archiveofourown\.org/works/{re.escape(work_id)}(?:[/?#\"'<\s]|$)",
            html,
            re.IGNORECASE,
        )
    )


def _work_id_from_url(value: str | None) -> str | None:
    if not value:
        return None
    match = _WorkLinkParser.pattern.search(value)
    if match is None:
        return None
    return match.group(1) or match.group(2)


def _is_work_response_url(value: str, work_id: str) -> bool:
    if not _is_ao3_url(value):
        return False
    path = urllib.parse.urlparse(value).path.rstrip("/")
    return bool(re.fullmatch(rf"/works/{re.escape(work_id)}(?:/.*)?", path))


def _looks_like_ao3_work_page(
    html: str,
    work_id: str,
    response_url: str | None = None,
) -> bool:
    lowered = html.casefold()
    response_work_id = _work_id_from_url(response_url)
    requested_link = _html_contains_work_id(html, work_id)
    requested_response = response_work_id == work_id
    has_preface = 'id="preface"' in lowered or "id='preface'" in lowered
    has_stats = "<dl class=\"stats" in lowered or "<dl class='stats" in lowered
    has_title = "class=\"title heading" in lowered or "class='title heading" in lowered
    has_chapter_id = bool(re.search(r"id=['\"][^'\"]*chapter[-_]", lowered))
    has_chapter_page = (
        "chapters-show" in lowered
        and "works-show" in lowered
        and "userstuff" in lowered
    )
    structure = (
        has_preface
        or has_stats
        or ("archive of our own" in lowered and has_title)
        or (requested_response and (has_chapter_id or has_chapter_page))
    )
    return (requested_link or requested_response) and structure


UNREVEALED_WORK_NOTICE_PATTERN = re.compile(
    r"this\s+work\s+is\s+part\s+of\s+an\s+ongoing\s+challenge\s+and\s+will\s+be\s+revealed\s+soon",
    re.IGNORECASE,
)
UNREVEALED_COLLECTION_PATTERN = re.compile(r'href=["\'](/collections/[^"\'?#]+)', re.IGNORECASE)
PAGE_TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _unrevealed_work_collection(html: str, work_id: str, response_url: str | None) -> str | None:
    """Return the collection path if this is an AO3 "Mystery Work" page, else None.

    A work in an unrevealed challenge collection is served at its own URL with
    HTTP 200, but AO3 withholds the preface, statistics, and chapters and shows
    only a notice. That is a legitimate state of the work, not unexpected HTML.
    Only called after the normal structure check fails, so a real work page can
    never be classified this way.
    """

    if _work_id_from_url(response_url) != work_id:
        return None
    if "works-show" not in html.casefold():
        return None
    notice = UNREVEALED_WORK_NOTICE_PATTERN.search(html)
    if notice is None:
        return None
    # Search the original text so the collection slug keeps its case.
    match = UNREVEALED_COLLECTION_PATTERN.search(html, notice.end())
    return match.group(1) if match else "<unknown collection>"


def _page_title(html: str) -> str | None:
    match = PAGE_TITLE_PATTERN.search(html)
    return " ".join(match.group(1).split()) if match else None


def _metadata_from_page(
    html: str,
    work_url: str,
    http_status: int,
    response_url: str | None = None,
) -> AO3FetchRecord:
    work_id = work_url.rsplit("/", 1)[-1]
    response_work_id = _work_id_from_url(response_url)
    if response_url is not None and not _is_work_response_url(response_url, work_id):
        raise UnexpectedHTML("AO3 response URL was not the requested work route")
    if response_work_id is not None and response_work_id != work_id:
        raise UnexpectedHTML("AO3 response URL did not match the requested work")
    if not _looks_like_ao3_work_page(html, work_id, response_url):
        collection = _unrevealed_work_collection(html, work_id, response_url)
        if collection is not None:
            # Cached as unavailable so writes skip it, and so a later run with
            # --retry-failed-once picks it up again once the collection reveals.
            return AO3FetchRecord(
                work_id=work_id,
                work_url=work_url,
                fetched_at=_utc_now(),
                availability="unavailable",
                http_status=http_status,
                error=f"AO3 work is unrevealed (Mystery Work in {collection}); statistics are hidden",
            )
        raise UnexpectedHTML(
            f"AO3 response did not contain the requested work page "
            f"(page title: {_page_title(html) or '<none>'!r})"
        )
    title, authors = _parse_page_title_and_authors(html)
    try:
        metadata = parse_ao3_metadata(html, work_url)
    except ValueError as error:
        return AO3FetchRecord(
            work_id=work_id,
            work_url=work_url,
            fetched_at=_utc_now(),
            availability="incomplete",
            title=title,
            authors=authors,
            http_status=http_status,
            error="AO3 page did not expose a statistics block",
        )

    if metadata.work_id != work_id:
        raise UnexpectedHTML("AO3 response work ID did not match the requested work")
    availability = "ok"
    if any(getattr(metadata, field) is None for field in COUNTER_ATTRIBUTES):
        availability = "incomplete"
    return AO3FetchRecord(
        work_id=metadata.work_id,
        work_url=metadata.work_url,
        fetched_at=_utc_now(),
        availability=availability,
        title=title or metadata.title,
        authors=authors or metadata.authors,
        category=metadata.category,
        status=metadata.status,
        chapters=metadata.chapters,
        words=metadata.words,
        comments=metadata.comments,
        kudos=metadata.kudos,
        bookmarks=metadata.bookmarks,
        hits=metadata.hits,
        http_status=http_status,
        error=None if availability == "ok" else "one or more counters were unavailable",
    )


def _is_cloudflare_challenge(response: requests.Response) -> bool:
    """Detect a Cloudflare bot challenge or block, which must stop the run.

    ``id="cf-wrapper"`` is deliberately not a marker: it wraps every Cloudflare
    error page, including the 5xx origin errors that mean AO3 itself is down.
    Treating those as a challenge reports an AO3 outage as a bot block and
    turns a transient failure into a fatal one.
    """

    content_type = response.headers.get("Content-Type", "").casefold()
    if not content_type.startswith("text/html"):
        return False
    if response.status_code in CLOUDFLARE_ORIGIN_STATUS_CODES:
        return False
    lowered = response.text.casefold()
    markers = (
        "<title>just a moment...</title>",
        "<title>attention required!</title>",
        "<title>access denied</title>",
        "cf-browser-verification",
        'id="challenge-error-text"',
        "_cf_chl_opt",
    )
    return any(marker in lowered for marker in markers)


def _cloudflare_origin_error(response: requests.Response, step: str) -> str | None:
    """Describe a Cloudflare-to-origin failure, or None if this is not one."""

    if response.status_code not in CLOUDFLARE_ORIGIN_STATUS_CODES:
        return None
    reasons = {
        520: "the origin returned an unknown error",
        521: "the origin refused the connection",
        522: "the connection to the origin timed out",
        523: "the origin is unreachable",
        524: "the origin took too long to respond",
        525: "the TLS handshake with the origin failed",
        526: "the origin's certificate is invalid",
        527: "the connection to the origin was interrupted",
        530: "the origin returned an error",
    }
    reason = reasons.get(response.status_code, "the origin could not be reached")
    return (
        f"Cloudflare could not reach AO3 at {step}: HTTP "
        f"{response.status_code} because {reason}. This is a server-side failure "
        "between Cloudflare and AO3, not a credential or rate-limit problem, "
        "and it is usually transient"
    )


def _is_authentication_response(response: requests.Response) -> bool:
    if response.status_code in (401, 403):
        return True
    lowered = response.text.casefold()
    return any(
        marker in lowered
        for marker in (
            "please log in",
            "invalid username or password",
            "must be logged in",
            "you must log in",
            "log in to continue",
            "please sign in",
            "authentication required",
        )
    )


AO3_HOSTS = frozenset({"archiveofourown.org", "www.archiveofourown.org"})


def _is_ao3_url(value: object, hosts: frozenset[str] = AO3_HOSTS) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = urllib.parse.urlparse(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and port in {None, 443}
        and not parsed.username
        and not parsed.password
        and hostname in hosts
    )


def _is_login_url(value: object) -> bool:
    if not _is_ao3_url(value):
        return False
    parsed = urllib.parse.urlparse(cast(str, value))
    return parsed.path.rstrip("/").casefold() == "/users/login"


def _is_login_redirect(response: requests.Response) -> bool:
    if _is_login_url(getattr(response, "url", None)):
        return True
    history = getattr(response, "history", ())
    return any(_is_login_url(getattr(item, "url", None)) for item in history)


def _is_final_login_response(response: requests.Response) -> bool:
    return _is_login_url(getattr(response, "url", None))


def _auth_cookie_fingerprints(session: requests.Session) -> frozenset[str]:
    fingerprints: set[str] = set()
    for cookie in session.cookies:
        if cookie.name not in {"user_session", "user_session_secure"}:
            continue
        value = "\x00".join(
            str(getattr(cookie, field, ""))
            for field in ("domain", "path", "name", "value")
        )
        fingerprints.add(hashlib.sha256(value.encode("utf-8")).hexdigest())
    return frozenset(fingerprints)


def _redirect_target(
    response: requests.Response,
    current_url: str,
    hosts: frozenset[str] = AO3_HOSTS,
) -> str | None:
    if response.status_code not in REDIRECT_STATUS_CODES:
        return None
    location = response.headers.get("Location")
    if not location:
        raise NetworkStopError("AO3 response contained a redirect without a destination")
    target = urllib.parse.urljoin(current_url, location)
    if not _is_ao3_url(target, hosts):
        raise NetworkStopError("AO3 response redirected away from archiveofourown.org")
    return target


WORK_ROUTE_PATTERN = re.compile(r"/works/(\d+)/?")
FIRST_CHAPTER_ROUTE_PATTERN = re.compile(r"/works/(\d+)/chapters/\d+/?")


def _is_same_work_chapter_redirect(current_url: str, target: str) -> bool:
    """True for AO3's canonical ``/works/<id>`` -> ``/works/<id>/chapters/<n>`` hop.

    AO3 redirects almost every work to its first chapter. The redirect response
    costs the server a fraction of a second and a browser follows it at once,
    so pacing it as a separate request only doubled the run time.
    """

    if not _is_ao3_url(target):
        return False
    source = WORK_ROUTE_PATTERN.fullmatch(urllib.parse.urlparse(current_url).path)
    destination = FIRST_CHAPTER_ROUTE_PATTERN.fullmatch(urllib.parse.urlparse(target).path)
    return bool(source and destination and source.group(1) == destination.group(1))


def _get_with_ao3_redirects(
    session: requests.Session,
    url: str,
    *,
    timeout_seconds: float,
    before_request: Callable[[], None],
    request_started_callback: Callable[[float], None] | None,
    request_deferred_callback: Callable[[float, bool], None] | None = None,
    sleep_fn: Callable[[float], None] = time.sleep,
    wall_time_fn: Callable[[], float] = time.time,
    allowed_hosts: frozenset[str] = AO3_HOSTS,
) -> requests.Response:
    current_url = url
    paced = True
    for redirect_count in range(MAX_AO3_REDIRECTS + 1):
        if paced:
            before_request()
        if request_started_callback is not None:
            request_started_callback(wall_time_fn())
        response = session.get(
            current_url,
            timeout=timeout_seconds,
            allow_redirects=False,
        )
        target = _redirect_target(response, current_url, allowed_hosts)
        if target is None:
            return response
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        if retry_after is not None:
            if request_deferred_callback is not None:
                request_deferred_callback(retry_after, True)
            sleep_fn(retry_after)
        if redirect_count >= MAX_AO3_REDIRECTS:
            raise NetworkStopError("AO3 returned too many redirects")
        # Only the same-work chapter hop skips the wait; any other redirect is
        # paced like a fresh request. A Retry-After above has already been served.
        paced = not _is_same_work_chapter_redirect(current_url, target)
        current_url = target
    raise NetworkStopError("AO3 redirect handling failed")


DOTENV_PATH = DEFAULT_DOTENV_PATH


def load_ao3_credential_pair(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path | None = DOTENV_PATH,
) -> AO3Credentials:
    """Load credentials and keep the source name for failure diagnostics."""

    try:
        pair = load_ao3_credentials(environ, dotenv_path=dotenv_path)
    except CredentialError as error:
        raise AuthenticationFailure(str(error)) from error
    # Registering here means every later log record is scrubbed, whatever
    # code path happens to format a message.
    register_secret(pair.username)
    register_secret(pair.password)
    return pair


def load_environment_credentials(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path | None = DOTENV_PATH,
) -> tuple[str, str]:
    """Load an explicitly selected credential pair without exposing either value."""

    return load_ao3_credential_pair(environ, dotenv_path=dotenv_path).as_tuple()


def _retry_while_transient(
    perform: Callable[[], requests.Response],
    *,
    step: str,
    delay_seconds: float,
    sleep_fn: Callable[[float], None],
    request_deferred_callback: Callable[[float, bool], None] | None,
    max_transient_attempts: int,
) -> requests.Response:
    """Repeat a login request while AO3 returns a transient or origin failure.

    A Cloudflare *challenge* is returned untouched for the caller to reject; a
    challenge is a decision by Cloudflare, not a blip, and retrying it is what
    gets an address blocked.
    """

    for attempt in range(1, max_transient_attempts + 1):
        response = perform()
        if response.status_code not in TRANSIENT_STATUS_CODES:
            return response
        if _is_cloudflare_challenge(response):
            return response
        detail = _cloudflare_origin_error(response, step) or (
            f"AO3 returned a transient HTTP {response.status_code} at {step}"
        )
        if attempt >= max_transient_attempts:
            suffix = f" after {attempt} attempts" if attempt > 1 else ""
            raise AuthenticationFailure(f"{detail}{suffix}")
        retry_after = _parse_retry_after(response.headers.get("Retry-After"))
        wait = (
            retry_after
            if retry_after is not None
            else min(delay_seconds * (2.0 ** (attempt - 1)), TRANSIENT_MAX_BACKOFF_SECONDS)
        )
        LOGGER.warning(
            f"{detail}; retrying in {format_duration(wait)} "
            f"(attempt {attempt}/{max_transient_attempts})"
        )
        if request_deferred_callback is not None:
            request_deferred_callback(wait, retry_after is not None)
        sleep_fn(wait)
    raise AuthenticationFailure(f"AO3 stayed unavailable at {step}")


def login_authenticated_session(
    session: requests.Session,
    username: str,
    password: str,
    *,
    source: str | None = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    max_transient_attempts: int = MAX_TRANSIENT_ATTEMPTS,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    before_request: Callable[[], None] | None = None,
    request_started_callback: Callable[[float], None] | None = None,
    request_deferred_callback: Callable[[float, bool], None] | None = None,
) -> None:
    """Log in without exposing credentials or silently retrying failures."""

    session.headers.update({
        "User-Agent": "ao3-calibre-backfill/1.0 (sequential metadata refresh)",
        "Accept": "text/html,application/xhtml+xml",
    })
    schedule_request = before_request or (lambda: None)
    described_source = source or "the supplied credentials"
    LOGGER.info(f"authenticating with credentials from {described_source}")
    LOGGER.info("login step 1/2: requesting the AO3 token dispenser")
    try:
        token_response = _retry_while_transient(
            lambda: _get_with_ao3_redirects(
                session,
                "https://archiveofourown.org/token_dispenser.json",
                timeout_seconds=timeout_seconds,
                before_request=schedule_request,
                request_started_callback=request_started_callback,
                request_deferred_callback=request_deferred_callback,
                sleep_fn=sleep_fn,
            ),
            step="the token dispenser",
            delay_seconds=delay_seconds,
            sleep_fn=sleep_fn,
            request_deferred_callback=request_deferred_callback,
            max_transient_attempts=max_transient_attempts,
        )
    except requests.RequestException as error:
        raise AuthenticationFailure(
            f"AO3 token dispenser request failed to connect: {type(error).__name__}"
        ) from error
    if token_response.status_code != 200 or _is_cloudflare_challenge(token_response):
        retry_after = _parse_retry_after(token_response.headers.get("Retry-After"))
        if retry_after is not None and request_deferred_callback is not None:
            request_deferred_callback(retry_after, True)
        if _is_cloudflare_challenge(token_response):
            raise AuthenticationFailure(
                f"Cloudflare challenged the AO3 token dispenser (HTTP {token_response.status_code})"
            )
        raise AuthenticationFailure(
            f"AO3 token dispenser returned HTTP {token_response.status_code}"
            + (f"; Retry-After {retry_after:g}s" if retry_after is not None else "")
        )
    try:
        token_value = token_response.json().get("token")
    except (ValueError, AttributeError):
        token_value = None
    if not isinstance(token_value, str) or not token_value:
        raise AuthenticationFailure(
            "AO3 token dispenser returned HTTP 200 without a usable token value"
        )
    LOGGER.info("login step 1/2: token received")

    if before_request is not None:
        before_request()
    else:
        sleep_fn(delay_seconds)
    if request_started_callback is not None:
        request_started_callback(time.time())
    payload = {
        "user[login]": username,
        "user[password]": password,
        "user[remember_me]": "1",
        "commit": "Log in",
        "utf8": "\u2713",
        "authenticity_token": token_value,
    }
    cookies_before_login = _auth_cookie_fingerprints(session)
    LOGGER.info("login step 2/2: submitting the login form")

    def submit_login() -> requests.Response:
        return session.post(
            "https://archiveofourown.org/users/login",
            data=payload,
            timeout=timeout_seconds,
            allow_redirects=False,
        )

    try:
        login_response = _retry_while_transient(
            submit_login,
            step="the login form",
            delay_seconds=delay_seconds,
            sleep_fn=sleep_fn,
            request_deferred_callback=request_deferred_callback,
            max_transient_attempts=max_transient_attempts,
        )
    except requests.RequestException as error:
        raise AuthenticationFailure(
            f"AO3 login POST failed to connect: {type(error).__name__}"
        ) from error
    if login_response.status_code in (307, 308):
        raise AuthenticationFailure("AO3 login request used an unsafe redirect")
    login_redirect = _redirect_target(
        login_response,
        "https://archiveofourown.org/users/login",
    )
    if login_redirect is not None:
        redirect_retry_after = _parse_retry_after(login_response.headers.get("Retry-After"))
        if redirect_retry_after is not None:
            if request_deferred_callback is not None:
                request_deferred_callback(redirect_retry_after, True)
            sleep_fn(redirect_retry_after)
        try:
            login_response = _get_with_ao3_redirects(
                session,
                login_redirect,
                timeout_seconds=timeout_seconds,
                before_request=before_request or (lambda: sleep_fn(delay_seconds)),
                request_started_callback=request_started_callback,
                request_deferred_callback=request_deferred_callback,
                sleep_fn=sleep_fn,
            )
        except requests.RequestException as error:
            raise AuthenticationFailure(
                f"AO3 login redirect request failed to connect: {type(error).__name__}"
            ) from error
    retry_after = _parse_retry_after(login_response.headers.get("Retry-After"))
    if retry_after is not None and request_deferred_callback is not None and (
        login_response.status_code != 200
        or _is_cloudflare_challenge(login_response)
        or _is_authentication_response(login_response)
    ):
        request_deferred_callback(retry_after, True)
    status = login_response.status_code
    destination = getattr(login_response, "url", None) or "<no final URL>"
    LOGGER.info(f"login step 2/2: HTTP {status} at {destination}")
    if _is_cloudflare_challenge(login_response):
        raise AuthenticationFailure(f"Cloudflare challenged the AO3 login (HTTP {status})")
    if status in (401, 403):
        raise AuthenticationFailure(
            f"AO3 refused the login with HTTP {status}; this is usually a rejected "
            f"credential or a challenge page for {described_source}"
        )
    if _is_authentication_response(login_response):
        raise CredentialsRejected(f"AO3 rejected the credentials from {described_source}")
    if _is_final_login_response(login_response):
        raise CredentialsRejected(
            f"AO3 returned the login form again (HTTP {status}); the credentials "
            f"from {described_source} were not accepted"
        )
    if status != 200:
        raise AuthenticationFailure(f"AO3 login returned an unexpected HTTP {status}")
    has_session_cookie = bool(_auth_cookie_fingerprints(session) - cookies_before_login)
    has_logout_link = bool(
        re.search(r"href=[\"'][^\"']*/users/logout(?:[\"'?]|$)", login_response.text, re.IGNORECASE)
    )
    has_logged_in_body = bool(
        re.search(r"<body\b[^>]*\bclass=[\"'][^\"']*\blogged-in\b", login_response.text, re.IGNORECASE)
    )
    if not has_session_cookie and not has_logout_link and not has_logged_in_body:
        raise AuthenticationFailure(
            f"AO3 returned HTTP {status} at {destination} with no session cookie, "
            "no logout link, and no logged-in body class"
        )
    evidence = ", ".join(
        label
        for label, present in (
            ("session cookie", has_session_cookie),
            ("logout link", has_logout_link),
            ("logged-in body class", has_logged_in_body),
        )
        if present
    )
    LOGGER.info(f"authenticated session established ({evidence})")


def _parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    stripped = value.strip()
    if stripped.isdigit():
        return float(stripped)
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0.0, retry_at.timestamp() - time.time() + RETRY_AFTER_DATE_SKEW_SECONDS)


class AO3Fetcher:
    """Fetch AO3 work pages sequentially under the explicit request policy."""

    def __init__(
        self,
        session: requests.Session | None = None,
        *,
        delay_seconds: float = DEFAULT_DELAY_SECONDS,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = 4,
        max_transient_attempts: int = MAX_TRANSIENT_ATTEMPTS,
        max_rate_limit_attempts: int = MAX_RATE_LIMIT_ATTEMPTS,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        wall_time_fn: Callable[[], float] = time.time,
        before_request: Callable[[], None] | None = None,
        request_started_callback: Callable[[float], None] | None = None,
        request_deferred_callback: Callable[[float, bool], None] | None = None,
        reauthenticate: Callable[[], None] | None = None,
        retry_unexpected_once: bool = False,
    ) -> None:
        if not math.isfinite(delay_seconds) or delay_seconds < MINIMUM_DELAY_SECONDS:
            raise ValueError(f"delay_seconds must be at least {MINIMUM_DELAY_SECONDS:g}")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if max_transient_attempts < 1:
            raise ValueError("max_transient_attempts must be positive")
        if max_rate_limit_attempts < 1:
            raise ValueError("max_rate_limit_attempts must be positive")
        self.session = session or requests.Session()
        self.delay_seconds = delay_seconds
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.max_transient_attempts = max_transient_attempts
        self.max_rate_limit_attempts = max_rate_limit_attempts
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn
        self.wall_time_fn = wall_time_fn
        self.before_request = before_request
        self.request_started_callback = request_started_callback
        self.request_deferred_callback = request_deferred_callback
        self.reauthenticate = reauthenticate
        self.retry_unexpected_once = retry_unexpected_once
        self._last_request_at: float | None = None
        self.last_request_wall_time: float | None = None
        self.session.headers.update({
            "User-Agent": "ao3-calibre-backfill/1.0 (sequential metadata refresh)",
            "Accept": "text/html,application/xhtml+xml",
        })

    def _before_request(self) -> None:
        if self.before_request is not None:
            self.before_request()
            return
        now = self.monotonic_fn()
        if self._last_request_at is not None:
            remaining = self.delay_seconds - (now - self._last_request_at)
            if remaining > 0:
                self.sleep_fn(remaining)
        self._last_request_at = self.monotonic_fn()

    def _transient_backoff(self, attempts: int) -> float:
        return min(
            self.delay_seconds * (2.0 ** (attempts - 1)),
            TRANSIENT_MAX_BACKOFF_SECONDS,
        )

    def _wait_for_retry(self, seconds: float, *, exact: bool = False) -> None:
        LOGGER.warning(
            f"waiting {format_duration(seconds)} before retrying"
            + (" (server Retry-After)" if exact else " (backoff)")
        )
        if self.request_deferred_callback is not None:
            self.request_deferred_callback(seconds, exact)
        self.sleep_fn(seconds)
        # Retry-After is the server's exact requested wait. Do not add the
        # normal inter-work delay after that wait.
        self._last_request_at = None

    def _request_started(self, timestamp: float) -> None:
        self.last_request_wall_time = timestamp
        if self.request_started_callback is not None:
            self.request_started_callback(timestamp)

    def _redirect_deferred(self, seconds: float, exact: bool) -> None:
        if self.request_deferred_callback is not None:
            self.request_deferred_callback(seconds, exact)
        self._last_request_at = None

    def _request_work_page(self, work_url: str) -> requests.Response:
        return _get_with_ao3_redirects(
            self.session,
            work_url,
            timeout_seconds=self.timeout_seconds,
            before_request=self._before_request,
            request_started_callback=self._request_started,
            request_deferred_callback=self._redirect_deferred,
            sleep_fn=self.sleep_fn,
            wall_time_fn=self.wall_time_fn,
        )

    def fetch(self, work_id: str) -> AO3FetchRecord:
        work_url = canonical_work_url(work_id)
        transient_attempts = 0
        cloudflare_count = 0
        rate_limit_count = 0
        reauthentication_attempted = False
        unexpected_html_attempts = 0

        for attempt in range(
            self.max_attempts + self.max_transient_attempts + self.max_rate_limit_attempts
        ):
            try:
                response = self._request_work_page(work_url)
            except requests.RequestException as error:
                transient_attempts += 1
                if transient_attempts >= self.max_transient_attempts:
                    raise NetworkStopError(
                        f"repeated transient AO3 request failures ({type(error).__name__})"
                    ) from error
                self._wait_for_retry(self._transient_backoff(transient_attempts))
                continue

            if _is_cloudflare_challenge(response):
                cloudflare_count += 1
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if cloudflare_count >= 2:
                    if retry_after is not None:
                        self._wait_for_retry(retry_after, exact=True)
                    raise RepeatedCloudflare(f"AO3 returned repeated Cloudflare responses for work {work_id}")
                self._wait_for_retry(
                    retry_after
                    if retry_after is not None
                    else self.delay_seconds * (2.0 ** (cloudflare_count - 1)),
                    exact=retry_after is not None,
                )
                continue

            if _is_login_redirect(response) or _is_authentication_response(response):
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if retry_after is not None:
                    self._wait_for_retry(retry_after, exact=True)
                if self.reauthenticate is not None and not reauthentication_attempted:
                    reauthentication_attempted = True
                    LOGGER.warning(
                        f"AO3 asked for authentication on work {work_id}; reauthenticating once"
                    )
                    self.reauthenticate()
                    continue
                raise AuthenticationFailure(f"AO3 authentication failure for work {work_id}")

            if response.status_code == 429:
                rate_limit_count += 1
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                wait = (
                    retry_after
                    if retry_after is not None
                    else self._transient_backoff(rate_limit_count)
                )
                LOGGER.warning(
                    f"AO3 rate-limited work {work_id}; waiting {format_duration(wait)} "
                    f"({'exact Retry-After' if retry_after is not None else 'backoff'}, "
                    f"attempt {rate_limit_count}/{self.max_rate_limit_attempts})"
                )
                if rate_limit_count >= self.max_rate_limit_attempts:
                    if retry_after is not None:
                        self._wait_for_retry(retry_after, exact=True)
                    raise RepeatedRateLimit(
                        f"AO3 rate-limited work {work_id} {rate_limit_count} times in a row; "
                        "consider raising --delay"
                    )
                self._wait_for_retry(wait, exact=retry_after is not None)
                continue

            if response.status_code in (404, 410):
                response_url = getattr(response, "url", None)
                if not isinstance(response_url, str) or not _is_work_response_url(response_url, work_id):
                    raise UnexpectedHTML("AO3 unavailable response URL was not the requested work route")
                return AO3FetchRecord(
                    work_id=work_id,
                    work_url=work_url,
                    fetched_at=_utc_now(),
                    availability="unavailable",
                    http_status=response.status_code,
                    error=f"AO3 returned HTTP {response.status_code}",
                )

            if response.status_code in TRANSIENT_STATUS_CODES:
                transient_attempts += 1
                origin_error = _cloudflare_origin_error(response, f"work {work_id}")
                if origin_error is not None:
                    LOGGER.warning(origin_error)
                retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                if transient_attempts >= self.max_transient_attempts:
                    if retry_after is not None:
                        self._wait_for_retry(retry_after, exact=True)
                    raise NetworkStopError(
                        f"AO3 returned {transient_attempts} consecutive transient "
                        f"HTTP {response.status_code} responses for work {work_id}"
                    )
                self._wait_for_retry(
                    retry_after
                    if retry_after is not None
                    else self._transient_backoff(transient_attempts),
                    exact=retry_after is not None,
                )
                continue

            if response.status_code != 200:
                raise NetworkStopError(f"AO3 returned unexpected HTTP {response.status_code}")

            content_type = response.headers.get("Content-Type", "").casefold()
            if content_type and not content_type.startswith("text/html"):
                raise UnexpectedHTML("AO3 returned a non-HTML response for a work page")
            response_url = getattr(response, "url", None)
            if not isinstance(response_url, str) or not _is_work_response_url(response_url, work_id):
                raise UnexpectedHTML("AO3 response URL was not the requested work route")
            try:
                return _metadata_from_page(response.text, work_url, response.status_code, response_url)
            except UnexpectedHTML as error:
                if self.retry_unexpected_once and unexpected_html_attempts == 0:
                    unexpected_html_attempts += 1
                    LOGGER.warning(f"work {work_id}: {error}; retrying once")
                    self._wait_for_retry(self.delay_seconds)
                    continue
                destination = response_url or "<no final URL>"
                raise UnexpectedHTML(
                    f"work {work_id}: HTTP {response.status_code} response at {destination} "
                    f"was not a recognized AO3 work page: {error}"
                ) from error

        raise NetworkStopError(f"Could not fetch AO3 work {work_id}")


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


def verify_custom_columns(calibredb: str, library: Path) -> dict[str, dict[str, object]]:
    """Verify the required schema through both calibredb and SQLite."""

    details_output = run_calibredb(calibredb, library, "custom_columns", "--details")
    details = parse_custom_column_details(details_output)
    sqlite_columns = read_custom_columns_sqlite(library)
    verified: dict[str, dict[str, object]] = {}
    for label, name, datatype, _ in CUSTOM_COLUMNS:
        detail = details.get(label)
        if detail is None:
            raise ColumnConfigurationError(f"Calibre column #{label} is missing from calibredb output")
        actual_type = str(detail.get("datatype", ""))
        if actual_type != datatype:
            raise ColumnConfigurationError(
                f"Calibre column #{label} has datatype {actual_type!r}; expected {datatype!r}"
            )
        if str(detail.get("name", "")) != name:
            raise ColumnConfigurationError(
                f"Calibre column #{label} has display name {detail.get('name')!r}; expected {name!r}"
            )
        sqlite_column = sqlite_columns.get(label)
        if sqlite_column is None:
            raise ColumnConfigurationError(f"Calibre column #{label} is missing from metadata.db")
        if sqlite_column["datatype"] != datatype:
            raise ColumnConfigurationError(
                f"metadata.db column #{label} has datatype {sqlite_column['datatype']!r}; expected {datatype!r}"
            )
        verified[label] = {**detail, "sqlite_id": sqlite_column["id"]}
    return verified


def verify_local_metric_columns(calibredb: str, library: Path) -> dict[str, dict[str, object]]:
    """Verify the existing local Words/Gfog columns through both interfaces."""

    details = parse_custom_column_details(
        run_calibredb(calibredb, library, "custom_columns", "--details")
    )
    sqlite_columns = read_custom_columns_sqlite(library)
    verified: dict[str, dict[str, object]] = {}
    for label, name, datatype, _ in LOCAL_METRIC_COLUMNS:
        detail = details.get(label)
        if detail is None:
            raise ColumnConfigurationError(f"Calibre column #{label} is missing from calibredb output")
        if str(detail.get("datatype", "")) != datatype:
            raise ColumnConfigurationError(
                f"Calibre column #{label} has datatype {detail.get('datatype')!r}; expected {datatype!r}"
            )
        if str(detail.get("name", "")) != name:
            raise ColumnConfigurationError(
                f"Calibre column #{label} has display name {detail.get('name')!r}; expected {name!r}"
            )
        sqlite_column = sqlite_columns.get(label)
        if sqlite_column is None or sqlite_column["datatype"] != datatype:
            raise ColumnConfigurationError(
                f"metadata.db column #{label} is missing or has the wrong datatype"
            )
        verified[label] = {**detail, "sqlite_id": sqlite_column["id"]}
    return verified


def parse_custom_column_details(output: str) -> dict[str, dict[str, object]]:
    """Parse Calibre 9.x's human-readable ``--details`` output."""

    lines = output.splitlines()
    result: dict[str, dict[str, object]] = {}
    for index, line in enumerate(lines):
        label = line.strip()
        if not label or label.startswith("{"):
            continue
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines) or not lines[next_index].lstrip().startswith("{"):
            continue
        value: object | None = None
        for end_index in range(next_index + 1, len(lines) + 1):
            candidate = "\n".join(lines[next_index:end_index])
            try:
                value = ast.literal_eval(candidate)
            except (SyntaxError, ValueError):
                continue
            break
        if not isinstance(value, dict):
            continue
        if isinstance(value.get("label"), str):
            parsed_label = str(value["label"]).removeprefix("#")
            result[parsed_label] = {str(key): item for key, item in value.items()}
    return result


def read_custom_columns_sqlite(library: Path) -> dict[str, dict[str, object]]:
    database = library / "metadata.db"
    if not database.is_file():
        raise ColumnConfigurationError(f"Calibre metadata.db does not exist: {database}")
    uri = f"file:{urllib.parse.quote(str(database), safe='/')}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        rows = connection.execute(
            "SELECT id, label, name, datatype FROM custom_columns ORDER BY id"
        ).fetchall()
    return {
        str(row[1]): {"id": row[0], "label": row[1], "name": row[2], "datatype": row[3]}
        for row in rows
    }


def read_library_uuid(library: Path) -> str:
    database = library / "metadata.db"
    uri = f"file:{urllib.parse.quote(str(database), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            row = connection.execute("SELECT uuid FROM library_id LIMIT 1").fetchone()
    except sqlite3.Error as error:
        raise BackfillError(f"Could not read the Calibre library UUID: {error}") from error
    if row is None or not row[0]:
        raise BackfillError(f"Calibre metadata.db has no library UUID: {database}")
    return str(row[0])


def verify_report_library(report: ScanReport, library: Path) -> None:
    if Path(report.library).resolve() != library.resolve():
        raise BackfillError(
            f"Scan report library {report.library!r} does not match write target {str(library)!r}"
        )
    current_uuid = read_library_uuid(library)
    if not report.library_uuid or report.library_uuid != current_uuid:
        raise BackfillError("Scan report library UUID does not match the current metadata.db")


def running_calibre_processes() -> tuple[str, ...]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,comm=,args="],
        capture_output=True,
        text=True,
        check=True,
        env=sanitized_child_environment(),
    )
    matches: list[str] = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 2)
        if len(fields) < 3:
            continue
        pid, comm, command = fields
        if pid == str(os.getpid()):
            continue
        process_name = comm.casefold()
        process_names = {
            "calibre",
            "calibre-parallel",
            "calibre-server",
            "calibre-web",
            "calibreweb",
            "calibredb",
        }
        if process_name in process_names:
            matches.append(line.strip())
            continue
        try:
            command_tokens = shlex.split(command)
        except ValueError:
            command_tokens = command.split()
        executable_tokens = command_tokens
        if any(
            Path(token).name.casefold() in process_names
            or "calibre-web" in token.casefold()
            or "calibreweb" in token.casefold()
            for token in executable_tokens
        ):
            matches.append(line.strip())
    return tuple(matches)


def require_calibre_closed() -> None:
    processes = running_calibre_processes()
    if processes:
        raise CalibreInUseError(
            "Close Calibre and Calibre-Web before changing metadata.db:\n" + "\n".join(processes)
        )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _backup_manifest_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.manifest.json")


def _validate_sqlite_backup(backup_path: Path, library_uuid: str) -> None:
    uri = f"file:{urllib.parse.quote(str(backup_path), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise BackfillError(f"Backup failed SQLite integrity_check: {backup_path}")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required_tables = {"books", "custom_columns", "library_id"}
            if not required_tables.issubset(tables):
                raise BackfillError(f"Backup is not a Calibre metadata database: {backup_path}")
            row = connection.execute("SELECT uuid FROM library_id LIMIT 1").fetchone()
    except sqlite3.Error as error:
        raise BackfillError(f"Backup is not a readable SQLite database: {backup_path}") from error
    if row is None or row[0] != library_uuid:
        raise BackfillError(f"Backup belongs to a different Calibre library: {backup_path}")


def verify_backup(backup_path: Path, library: Path) -> None:
    source = library / "metadata.db"
    if backup_path.resolve() == source.resolve():
        raise BackfillError("Backup path must not be metadata.db itself")
    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise BackfillError(f"Backup is missing or empty: {backup_path}")
    try:
        if os.path.samefile(source, backup_path):
            raise BackfillError("Backup must be an independent copy of metadata.db")
    except FileNotFoundError:
        pass
    manifest_path = _backup_manifest_path(backup_path)
    if not manifest_path.is_file():
        raise BackfillError(f"Backup provenance manifest is missing: {manifest_path}")
    try:
        with manifest_path.open(encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise BackfillError(f"Backup provenance manifest is invalid: {manifest_path}") from error
    if not isinstance(manifest, dict):
        raise BackfillError(f"Backup provenance manifest is not an object: {manifest_path}")
    if manifest.get("source_path") != str(source.resolve()):
        raise BackfillError(f"Backup provenance points to a different library: {backup_path}")
    if not source.is_file() or source.stat().st_size <= 0:
        raise BackfillError(f"Current metadata.db is missing or empty: {source}")
    if manifest.get("source_size") != source.stat().st_size:
        raise BackfillError(f"Backup source size differs from the current library: {backup_path}")
    if manifest.get("source_sha256") != _sha256_file(source):
        raise BackfillError(f"Backup source checksum differs from the current library: {backup_path}")
    if manifest.get("backup_size") != backup_path.stat().st_size:
        raise BackfillError(f"Backup size differs from its provenance manifest: {backup_path}")
    if manifest.get("backup_sha256") != _sha256_file(backup_path):
        raise BackfillError(f"Backup checksum differs from its provenance manifest: {backup_path}")
    library_uuid = read_library_uuid(library)
    if manifest.get("library_uuid") != library_uuid:
        raise BackfillError(f"Backup library UUID differs from the current library: {backup_path}")
    _validate_sqlite_backup(backup_path, library_uuid)


def create_backup(library: Path, destination: Path) -> Path:
    require_calibre_closed()
    source = library / "metadata.db"
    if not source.is_file() or source.stat().st_size <= 0:
        raise BackfillError(f"metadata.db is missing or empty: {source}")
    if destination.resolve() == source.resolve():
        raise BackfillError("Backup destination must not be metadata.db itself")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or _backup_manifest_path(destination).exists():
        raise BackfillError(
            f"Backup destination already exists; choose a new destination explicitly: {destination}"
        )
    source_size = source.stat().st_size
    source_sha256 = _sha256_file(source)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size <= 0:
            raise BackfillError(f"Backup copy is empty: {temporary}")
        library_uuid = read_library_uuid(library)
        _validate_sqlite_backup(temporary, library_uuid)
        if source.stat().st_size != source_size or _sha256_file(source) != source_sha256:
            raise BackfillError("metadata.db changed while the backup was being created")
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise BackfillError(
                f"Backup destination appeared during creation; refusing to overwrite: {destination}"
            ) from error
    finally:
        temporary.unlink(missing_ok=True)
    _write_json_atomically(
        _backup_manifest_path(destination),
        {
            "schema_version": 1,
            "source_path": str(source.resolve()),
            "source_size": source_size,
            "source_sha256": source_sha256,
            "backup_size": destination.stat().st_size,
            "backup_sha256": _sha256_file(destination),
            "library_uuid": library_uuid,
            "created_at": _utc_now(),
        },
    )
    verify_backup(destination, library)
    return destination


def setup_custom_columns(calibredb: str, library: Path, backup_path: Path) -> tuple[str, ...]:
    """Create missing columns only after the backup and process gates pass."""

    require_calibre_closed()
    verify_backup(backup_path, library)
    existing = read_custom_columns_sqlite(library)
    for label, name, datatype, _ in CUSTOM_COLUMNS:
        current = existing.get(label)
        if current is None:
            continue
        if current["datatype"] != datatype:
            raise ColumnConfigurationError(
                f"Calibre column #{label} exists as {current['datatype']!r}, expected {datatype!r}"
            )
        if current["name"] != name:
            raise ColumnConfigurationError(
                f"Calibre column #{label} exists with display name {current['name']!r}, expected {name!r}"
            )

    created: list[str] = []
    for label, name, datatype, _ in CUSTOM_COLUMNS:
        if label in existing:
            continue
        run_calibredb(calibredb, library, "add_custom_column", label, name, datatype)
        created.append(label)
    verify_custom_columns(calibredb, library)
    return tuple(created)


class _Cooldown:
    """Escalating pauses for problems that affect every work, not one."""

    def __init__(
        self,
        sleep_fn: Callable[[float], None],
        schedule: Sequence[float] = COOLDOWN_SCHEDULE_SECONDS,
    ) -> None:
        self.sleep_fn = sleep_fn
        self.schedule = tuple(schedule)
        self.level = 0
        self.count = 0
        self.total_seconds = 0.0

    def reset(self) -> None:
        self.level = 0

    def wait(self, reason: str) -> None:
        seconds = self.schedule[min(self.level, len(self.schedule) - 1)]
        self.level += 1
        self.count += 1
        self.total_seconds += seconds
        resume_at = datetime.now().astimezone() + timedelta(seconds=seconds)
        LOGGER.warning(
            f"{reason}; cooling down {format_duration(seconds)} "
            f"(until {format_clock(resume_at)}), then retrying"
        )
        self.sleep_fn(seconds)


# Errors that describe AO3 or the session as a whole. Each gets a cool-down and
# one more attempt at the same work before it is treated as that work's fault.
SYSTEMIC_FETCH_ERRORS = (RepeatedCloudflare, RepeatedRateLimit, AuthenticationFailure)


def _sign_in_patiently(sign_in: Callable[[], None], cooldown: _Cooldown) -> None:
    """Keep trying to sign in through transient trouble, but never past bad credentials."""

    while True:
        try:
            sign_in()
        except CredentialsRejected:
            raise
        except (NetworkStopError, requests.RequestException) as error:
            cooldown.wait(f"sign-in failed: {error}")
            continue
        # Deliberately no cooldown.reset(): a working login page does not prove
        # work pages are back. Only a cached work resets the escalation, so a
        # lasting outage settles into long pauses instead of draining the queue.
        return


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
        cooldown = _Cooldown(sleep_fn)
        failed_this_run: dict[str, str] = {}
        try:
            credentials = load_ao3_credential_pair() if use_env_credentials else None
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
                    _sign_in_patiently(sign_in, cooldown)
                else:
                    sign_in()

            def recover_session() -> None:
                if sign_in is None:
                    return
                # Start from a clean jar so a half-expired session cannot linger.
                authenticated_session.cookies.clear()
                _sign_in_patiently(sign_in, cooldown)

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
    cooldown: _Cooldown,
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


def load_local_metric_values(calibredb: str, library: Path) -> dict[int, dict[str, object]]:
    output = run_calibredb(
        calibredb,
        library,
        "list",
        "--for-machine",
        "--fields",
        "id,*words,*gfog",
    )
    books = json.loads(output)
    if not isinstance(books, list):
        raise BackfillError("calibredb returned an unexpected local metric value list")
    result: dict[int, dict[str, object]] = {}
    for book in books:
        if not isinstance(book, dict) or not str(book.get("id", "")).isdigit():
            continue
        book_id = int(cast(str | int, book["id"]))
        result[book_id] = {label: book.get(f"*{label}") for label, _, _, _ in LOCAL_METRIC_COLUMNS}
    return result


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
        if manifest.get("backup_sha256") != _sha256_file(backup_path):
            raise BackfillError(f"EPUB backup checksum differs from its manifest: {backup_path}")
        return

    backup_path.parent.mkdir(parents=True, exist_ok=True)
    source_size = epub_path.stat().st_size
    source_sha256 = _sha256_file(epub_path)
    shutil.copy2(epub_path, backup_path)
    if epub_path.stat().st_size != source_size or _sha256_file(epub_path) != source_sha256:
        raise BackfillError(f"EPUB changed while its backup was being created: {epub_path}")
    if backup_path.stat().st_size != source_size or _sha256_file(backup_path) != source_sha256:
        raise BackfillError(f"EPUB backup does not match its source: {backup_path}")
    _write_json_atomically(
        manifest_path,
        {
            "schema_version": 1,
            "source_path": str(epub_path.resolve()),
            "source_size": source_size,
            "source_sha256": source_sha256,
            "backup_size": backup_path.stat().st_size,
            "backup_sha256": _sha256_file(backup_path),
            "created_at": _utc_now(),
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
        return round(float(cast(str | int | float, value)), 2)
    except (TypeError, ValueError) as error:
        raise BackfillError("Calibre local Gfog value is invalid") from error


def _portable_metadata_for_record(
    mapping: EpubMapping,
    record: AO3FetchRecord,
    current_local_values: dict[str, object],
) -> AO3Metadata:
    local_words = (
        mapping.local_words
        if mapping.local_metrics_calculated
        else _optional_local_int(current_local_values.get("words"))
    )
    local_gfog = (
        mapping.local_gfog
        if mapping.local_metrics_calculated
        else _optional_local_float(current_local_values.get("gfog"))
    )
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
) -> dict[str, object]:
    """Bake namespaced portable metadata into approved existing EPUBs."""

    if limit is not None and limit < 1:
        raise ValueError("limit must be positive when specified")
    require_calibre_closed()
    verify_backup(backup_path, library)
    verify_report_library(report, library)
    verify_custom_columns(calibredb, library)
    validation = validate_cache(report, cache, include_ambiguous, require_binding=True)
    if validation.missing and not allow_partial:
        raise BackfillError(
            f"{len(validation.missing)} works are not cached; use --allow-partial for a reviewed batch"
        )
    current_local_values = load_local_metric_values(calibredb, library)
    candidates, skipped_ambiguous = _mappings_for_write(
        report,
        validation.records,
        include_ambiguous,
    )
    candidates = [
        (mapping, record)
        for mapping, record in candidates
        if record.availability == "ok"
    ]
    if limit is not None:
        candidates = candidates[:limit]
    if candidates:
        verify_report_inputs(
            report,
            library,
            calibredb,
            tuple(mapping for mapping, _ in candidates),
        )

    enriched: list[int] = []
    skipped_unavailable = [
        mapping.book_id
        for mapping in report.mappings
        if mapping.work_id in validation.records
        and validation.records[mapping.work_id].availability != "ok"
    ]
    failed: list[dict[str, object]] = []
    for mapping, record in candidates:
        epub_path = library / mapping.epub_path
        backup_epub_path = epub_backup_dir / mapping.epub_path
        try:
            _backup_epub_once(epub_path, backup_epub_path)
            metadata = _portable_metadata_for_record(
                mapping,
                record,
                current_local_values.get(mapping.book_id, {}),
            )
            enrich_epub_portable(epub_path, metadata)
            validate_epub_file(epub_path)
            stored = read_ao3_metadata(epub_path)
            if stored.work_id != metadata.work_id or stored.work_url != metadata.work_url:
                raise BackfillError(f"Portable EPUB metadata identity mismatch: {epub_path}")
            if metadata.category is not None and stored.category != metadata.category:
                raise BackfillError(f"Portable EPUB category verification failed: {epub_path}")
            if metadata.local_words is not None and stored.local_words != metadata.local_words:
                raise BackfillError(f"Portable EPUB local Words verification failed: {epub_path}")
            if metadata.local_gfog is not None and stored.local_gfog != metadata.local_gfog:
                raise BackfillError(f"Portable EPUB Gfog verification failed: {epub_path}")
            enriched.append(mapping.book_id)
        except (BackfillError, OSError, ValueError, ET.ParseError, zipfile.BadZipFile) as error:
            failed.append(
                {
                    "book_id": mapping.book_id,
                    "work_id": mapping.work_id,
                    "epub_path": mapping.epub_path,
                    "error": str(error),
                }
            )
            break

    return {
        "enriched": enriched,
        "skipped_ambiguous": [mapping.book_id for mapping in skipped_ambiguous],
        "skipped_unavailable": skipped_unavailable,
        "not_cached": [mapping.book_id for mapping in report.mappings if mapping.work_id not in validation.records],
        "failed": failed,
    }


def _read_custom_values_calibredb(
    calibredb: str,
    library: Path,
    labels: Sequence[str],
) -> dict[int, dict[str, object]]:
    output = run_calibredb(
        calibredb,
        library,
        "list",
        "--for-machine",
        "--fields",
        ",".join(("id", *(f"*{label}" for label in labels))),
    )
    books = json.loads(output)
    if not isinstance(books, list):
        raise BackfillError("calibredb returned an unexpected custom value list")
    result: dict[int, dict[str, object]] = {}
    for book in books:
        if not isinstance(book, dict) or not str(book.get("id", "")).isdigit():
            continue
        book_id = int(cast(str | int, book["id"]))
        result[book_id] = {label: book.get(f"*{label}") for label in labels}
    return result


def _read_custom_values_sqlite(
    library: Path,
    labels: Sequence[str],
) -> dict[int, dict[str, object]]:
    columns = read_custom_columns_sqlite(library)
    database = library / "metadata.db"
    uri = f"file:{urllib.parse.quote(str(database), safe='/')}?mode=ro"
    result: dict[int, dict[str, object]] = {}
    with sqlite3.connect(uri, uri=True) as connection:
        for label in labels:
            column = columns.get(label)
            if column is None:
                raise ColumnConfigurationError(f"Calibre column #{label} is missing from metadata.db")
            column_id = int(cast(int, column["id"]))
            table = f"custom_column_{column_id}"
            if column["datatype"] == "text":
                rows = connection.execute(
                    f"SELECT link.book, values_table.value "
                    f"FROM books_custom_column_{column_id}_link AS link "
                    f"JOIN {table} AS values_table ON values_table.id = link.value"
                ).fetchall()
            else:
                rows = connection.execute(f"SELECT book, value FROM {table}").fetchall()
            for book_id, value in rows:
                result.setdefault(int(book_id), {})[label] = value
    return result


def verify_written_values(
    calibredb: str,
    library: Path,
    expected: dict[str, dict[str, str]],
) -> dict[str, int]:
    """Read written values back through calibredb and read-only SQLite."""

    if not expected:
        return {"books": 0, "fields": 0}
    require_calibre_closed()
    labels = tuple(sorted({label for fields in expected.values() for label in fields}))
    calibredb_values = _read_custom_values_calibredb(calibredb, library, labels)
    sqlite_values = _read_custom_values_sqlite(library, labels)
    for book_id_text, fields in expected.items():
        book_id = int(book_id_text)
        for label, value in fields.items():
            actual_calibre = calibredb_values.get(book_id, {}).get(label)
            actual_sqlite = sqlite_values.get(book_id, {}).get(label)
            if str(actual_calibre) != value or str(actual_sqlite) != value:
                raise BackfillError(
                    f"Post-write verification failed for book {book_id} column #{label}"
                )
    return {"books": len(expected), "fields": sum(len(fields) for fields in expected.values())}


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
) -> dict[str, object]:
    """Write approved AO3/category values and optionally blank local metrics."""

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
    written_values: dict[str, dict[str, str]] = {}
    seen_book_ids: set[int] = set()
    for mapping, record in candidates:
        if mapping.book_id in seen_book_ids:
            continue
        seen_book_ids.add(mapping.book_id)
        if record.availability != "ok":
            skipped_unavailable.append(mapping.book_id)
            continue
        fields_to_write: list[tuple[str, str]] = []
        for label, _, _, attribute in BACKFILL_CUSTOM_COLUMNS:
            value = getattr(record, attribute)
            if value is not None and value != "":
                fields_to_write.append((label, str(value)))
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
                fields_to_write.append((label, str(value)))
        if not fields_to_write:
            skipped_without_values.append(mapping.book_id)
            continue
        written_fields: list[str] = []
        try:
            require_calibre_closed()
            for label, value in fields_to_write:
                run_calibredb(
                    calibredb,
                    library,
                    "set_custom",
                    label,
                    str(mapping.book_id),
                    value,
                )
                written_fields.append(label)
        except CalibreInUseError:
            raise
        except (BackfillError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            written_values[str(mapping.book_id)] = {
                label: value for label, value in fields_to_write if label in written_fields
            }
            failed.append(
                {
                    "book_id": mapping.book_id,
                    "work_id": mapping.work_id,
                    "error": str(error),
                    "partially_written_fields": written_fields,
                }
            )
            break
        written_values[str(mapping.book_id)] = dict(fields_to_write)
        updated.append(mapping.book_id)

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


def verify_library_values(calibredb: str, library: Path) -> dict[str, object]:
    """Read custom values and exercise Calibre's numeric search/sort paths."""

    verify_custom_columns(calibredb, library)
    field_names = ",".join(f"*{label}" for label, _, _, _ in CUSTOM_COLUMNS)
    output = run_calibredb(
        calibredb,
        library,
        "list",
        "--for-machine",
        "--fields",
        f"id,title,{field_names}",
    )
    books = json.loads(output)
    if not isinstance(books, list):
        raise BackfillError("calibredb returned an unexpected value list")
    populated: dict[str, int] = {}
    for label, _, _, _ in CUSTOM_COLUMNS:
        populated[label] = sum(
            1
            for book in books
            if isinstance(book, dict) and book.get(f"*{label}") not in (None, "")
        )

    search_query = "#ao3_kudos:>100"
    try:
        search_output = run_calibredb(calibredb, library, "search", search_query)
    except RuntimeError as error:
        if "No books matching the search expression" not in str(error):
            raise
        search_output = ""
    sorted_output = run_calibredb(
        calibredb,
        library,
        "list",
        "--for-machine",
        "--search",
        search_query,
        "--sort-by",
        "*ao3_kudos",
        "--fields",
        "id,title,*ao3_kudos",
    )
    sorted_books = json.loads(sorted_output)
    sample = []
    if isinstance(sorted_books, list):
        for book in sorted_books[:5]:
            if isinstance(book, dict):
                sample.append(book)
    return {
        "book_count": len(books),
        "populated": populated,
        "numeric_search": search_query,
        "numeric_search_result_count": len([line for line in search_output.splitlines() if line.strip()]),
        "sorted_sample": sample,
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
    protected = (library, Path(__file__).resolve().parent, Path("/home/drifter/repos/ao3downloadernew"))
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
    backup.add_argument("--destination", type=Path, default=None)
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
        help="bake portable AO3/local metadata into approved existing EPUBs",
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


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    run = configure_logging(args.command, log_dir=args.log_dir)
    logger = run.logger
    logger.info(f"ao3_backfill {args.command} \u00b7 log file: {run.log_path}")

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
        credentials = load_ao3_credential_pair()
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
        destination = args.destination or _cache_paths(args.cache_dir)[0].with_name(BACKUP_NAME)
        _ensure_cache_location(destination.parent, args.library)
        backup_path = create_backup(args.library, destination)
        logger.info(f"Backup: {backup_path} bytes={backup_path.stat().st_size}")
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
        )
        _log_block(logger, json.dumps(result, indent=2, sort_keys=True))
        if result["failed"]:
            raise BackfillError("One or more EPUB enrichments failed; no further records were attempted")
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
        )
        if result["failed"]:
            try:
                result["partial_post_write_verification"] = verify_written_values(
                    args.calibredb,
                    args.library,
                    cast(dict[str, dict[str, str]], result["written_values"]),
                )
            except (BackfillError, RuntimeError, OSError, sqlite3.Error, ValueError) as error:
                result["partial_post_write_verification_error"] = str(error)
            _log_block(logger, json.dumps(result, indent=2, sort_keys=True))
            raise BackfillError("One or more Calibre records failed; no further records were attempted")
        try:
            result["post_write_verification"] = verify_written_values(
                args.calibredb,
                args.library,
                cast(dict[str, dict[str, str]], result["written_values"]),
            )
        except (BackfillError, RuntimeError, OSError, sqlite3.Error, ValueError) as error:
            result["post_write_verification_error"] = str(error)
            _log_block(logger, json.dumps(result, indent=2, sort_keys=True))
            raise BackfillError("Post-write Calibre verification failed") from error
        _log_block(logger, json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "verify-library":
        _log_block(
            logger,
            json.dumps(verify_library_values(args.calibredb, args.library), indent=2, sort_keys=True),
        )
        return 0

    raise BackfillError(f"Unknown command: {args.command}")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RunInterrupted as error:
        LOGGER.warning(f"{error}; rerun the same command to resume from the cache")
        raise SystemExit(130) from error
    except BackfillError as error:
        LOGGER.error(str(error))
        raise SystemExit(2) from error
