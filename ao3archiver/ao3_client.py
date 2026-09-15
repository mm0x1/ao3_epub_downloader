"""Everything that talks to archiveofourown.org.

Login, the request policy (pacing, redirects, Cloudflare, rate limits, transient
retries), work-page classification, EPUB downloads, the one-run-per-account
lock, and the unattended work queue. Both the backfill and the downloader use
this module; neither talks to AO3 any other way.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import fcntl
import hashlib
import html as html_lib
from html.parser import HTMLParser
import logging
import math
import os
from pathlib import Path
import re
import time
from typing import cast
import urllib.parse

import requests

from ao3archiver.common import ArchiverError, STATE_DIR, utc_now
from ao3archiver.credentials import AO3Credentials
from ao3archiver.metadata import canonical_work_url, parse_ao3_metadata
from ao3archiver.run_log import format_clock, format_duration, ProgressTracker, RunInterrupted

LOGGER = logging.getLogger("ao3.client")


DEFAULT_DELAY_SECONDS = 30.0


# The default is deliberately cautious, but it is not an AO3-published limit.
# ao3downloadernew reserves its 30s for listing/search pages and uses no delay
# at all between individual work pages, which is what this tool fetches. The
# floor exists to stop a typo turning into a flood, not to enforce 30s.
MINIMUM_DELAY_SECONDS = 5.0


DEFAULT_TIMEOUT_SECONDS = 60.0


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


LOGIN_TRANSPORT_ATTEMPTS = 3


RETRY_AFTER_DATE_SKEW_SECONDS = 0.0


REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})


MAX_AO3_REDIRECTS = 5


# --keep-going: this many consecutive per-work failures points at AO3 or the
# session rather than the works, so the run pauses instead of burning through
# the queue while something global is wrong.
FAILURE_STREAK_COOLDOWN_THRESHOLD = 5


COOLDOWN_SCHEDULE_SECONDS = (300.0, 900.0, 1800.0, 3600.0)


AO3_HOSTS = frozenset({"archiveofourown.org", "www.archiveofourown.org"})


AO3_BASE_URL = "https://archiveofourown.org"


DOWNLOAD_HOSTS = AO3_HOSTS | frozenset({"download.archiveofourown.org"})


USER_AGENT = "ao3Archiver-download/1.0 (sequential work downloads)"


# The lock file name predates the downloader; keeping it means a backfill
# started under older code still excludes new runs.
ACCOUNT_LOCK_PATH = STATE_DIR / "ao3-cache.jsonl.fetch.lock"


class AO3Error(ArchiverError):
    """A request to AO3, or its response, that a run cannot use."""


class NetworkStopError(AO3Error):
    """AO3 returned a response for which the policy requires stopping."""


class UnexpectedHTML(NetworkStopError):
    """The response was not a recognizable AO3 work page."""


class RepeatedCloudflare(NetworkStopError):
    """Cloudflare responses repeated during a single fetch."""


class RepeatedRateLimit(NetworkStopError):
    """AO3 rate-limited repeated requests."""


class AuthenticationFailure(NetworkStopError):
    """AO3 indicated that authentication is required or failed."""


class LoginTransportError(AuthenticationFailure):
    """A login request never got an answer (timeout, dropped connection)."""


class CredentialsRejected(AuthenticationFailure):
    """AO3 refused the username/password itself; retrying cannot help.

    Kept distinct so an unattended run stops instead of repeatedly submitting
    credentials that are known to be wrong.
    """


class AnotherRunActive(AO3Error):
    """Another backfill fetch or download already holds the account lock."""


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


# Anchored to AO3's page-notice element: a fic's text could quote the sentence,
# but it cannot produce a <p class="notice"> in the page chrome.
UNREVEALED_WORK_NOTICE_PATTERN = re.compile(
    r"<p\s+class=[\"']notice[\"'][^>]*>\s*"
    r"this\s+work\s+is\s+part\s+of\s+an\s+ongoing\s+challenge\s+and\s+will\s+be\s+revealed\s+soon",
    re.IGNORECASE,
)


UNREVEALED_COLLECTION_PATTERN = re.compile(r'href=["\'](/collections/[^"\'?#]+)', re.IGNORECASE)


PAGE_TITLE_PATTERN = re.compile(r"<title>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _unrevealed_work_collection(html: str, work_id: str, response_url: str | None) -> str | None:
    """Return the collection path for an AO3 "Mystery Work" page, "" if unnamed, else None.

    A work in an unrevealed challenge collection is served at its own URL with
    HTTP 200, but AO3 withholds the preface, statistics, and download menu and
    shows only a page notice. AO3 uses two layouts: the work view (with a
    collection link) and, after the usual redirect to the first chapter, a
    chapter view without one. The chapter view carries enough structure to
    pass the work-page check, so this must run before that check.
    """

    if _work_id_from_url(response_url) != work_id:
        return None
    notice = UNREVEALED_WORK_NOTICE_PATTERN.search(html)
    if notice is None:
        return None
    # Search the original text so the collection slug keeps its case.
    notice_end = html.find("</p>", notice.end())
    notice_html = html[notice.end(): notice_end if notice_end != -1 else len(html)]
    match = UNREVEALED_COLLECTION_PATTERN.search(notice_html)
    return match.group(1) if match else ""


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
    collection = _unrevealed_work_collection(html, work_id, response_url)
    if collection is not None:
        # Cached as unavailable so writes and downloads skip it, and so a later
        # run with --retry-failed-once checks again once the collection reveals.
        where = f" in {collection}" if collection else ""
        return AO3FetchRecord(
            work_id=work_id,
            work_url=work_url,
            fetched_at=utc_now(),
            availability="unavailable",
            http_status=http_status,
            error=f"AO3 work is unrevealed (Mystery Work{where}); statistics are hidden",
        )
    if not _looks_like_ao3_work_page(html, work_id, response_url):
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
            fetched_at=utc_now(),
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
        fetched_at=utc_now(),
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


LOGGED_IN_BODY_PATTERN = re.compile(r"<body\b[^>]*\bclass=[\"'][^\"']*\blogged-in\b", re.IGNORECASE)


def _is_authentication_response(response: requests.Response) -> bool:
    if response.status_code in (401, 403):
        return True
    # A page AO3 rendered for a signed-in reader is not asking them to sign in.
    # This has to come before the phrase check: a work page includes the story
    # text, and work 25021798 quotes a paywall's "log in to continue", which
    # made a normal page look like a login demand on every attempt.
    if LOGGED_IN_BODY_PATTERN.search(response.text):
        return False
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
    max_transport_attempts: int = LOGIN_TRANSPORT_ATTEMPTS,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    sleep_fn: Callable[[float], None] = time.sleep,
    before_request: Callable[[], None] | None = None,
    request_started_callback: Callable[[float], None] | None = None,
    request_deferred_callback: Callable[[float, bool], None] | None = None,
) -> None:
    """Log in, starting over with a fresh token if a request gets no answer.

    AO3's login POST times out often enough to matter: each timeout used to
    cost an unattended run a five-minute cool-down. The retry repeats the whole
    login rather than resending the form, because a timed-out POST may have
    reached AO3, and a reused token risks AO3 showing the login form again,
    which reads as rejected credentials and stops the run.
    """

    for attempt in range(1, max_transport_attempts + 1):
        try:
            _login_once(
                session,
                username,
                password,
                source=source,
                timeout_seconds=timeout_seconds,
                max_transient_attempts=max_transient_attempts,
                delay_seconds=delay_seconds,
                sleep_fn=sleep_fn,
                before_request=before_request,
                request_started_callback=request_started_callback,
                request_deferred_callback=request_deferred_callback,
            )
            return
        except LoginTransportError as error:
            if attempt >= max_transport_attempts:
                raise
            wait = min(delay_seconds * (2.0 ** (attempt - 1)), TRANSIENT_MAX_BACKOFF_SECONDS)
            LOGGER.warning(
                f"{error}; starting the login again in {format_duration(wait)} "
                f"(attempt {attempt}/{max_transport_attempts})"
            )
            if request_deferred_callback is not None:
                request_deferred_callback(wait, False)
            sleep_fn(wait)


def _login_once(
    session: requests.Session,
    username: str,
    password: str,
    *,
    source: str | None,
    timeout_seconds: float,
    max_transient_attempts: int,
    delay_seconds: float,
    sleep_fn: Callable[[float], None],
    before_request: Callable[[], None] | None,
    request_started_callback: Callable[[float], None] | None,
    request_deferred_callback: Callable[[float, bool], None] | None,
) -> None:
    """One complete login attempt: fetch a token, submit it, confirm the session."""

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
        raise LoginTransportError(
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
        raise LoginTransportError(
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
            raise LoginTransportError(
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
    has_logged_in_body = bool(LOGGED_IN_BODY_PATTERN.search(login_response.text))
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
                    fetched_at=utc_now(),
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


@contextmanager
def account_lock(lock_path: Path = ACCOUNT_LOCK_PATH) -> Iterator[None]:
    """Hold the one-run-per-account lock for a whole run, or refuse to start.

    AO3 sees one account, so a backfill fetch and a download must never run at
    the same time, and neither may two of either.
    """

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            lock.seek(0)
            holder = lock.read().strip() or "an unknown process"
            raise AnotherRunActive(
                f"Another AO3 run already holds {lock_path} ({holder}): a backfill fetch or a "
                "download. Only one run can use the account at a time; wait for it to finish."
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


def ao3_run_active(lock_path: Path = ACCOUNT_LOCK_PATH) -> bool:
    """Report whether another run currently holds the account lock."""

    try:
        with account_lock(lock_path):
            return False
    except AnotherRunActive:
        return True


class RequestPacer:
    """In-memory counterpart of the backfill's persisted request scheduler.

    Every request start pushes the next allowed start ``delay_seconds`` out, and
    a server-requested wait (Retry-After) replaces that schedule exactly.
    """

    def __init__(
        self,
        delay_seconds: float,
        *,
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.delay_seconds = delay_seconds
        self.sleep_fn = sleep_fn
        self.monotonic_fn = monotonic_fn
        self._next_at: float | None = None

    def before_request(self) -> None:
        if self._next_at is None:
            return
        remaining = self._next_at - self.monotonic_fn()
        if remaining > 0:
            self.sleep_fn(remaining)

    def request_started(self, _wall_timestamp: float) -> None:
        self._next_at = self.monotonic_fn() + self.delay_seconds

    def defer(self, seconds: float, exact: bool = False) -> None:
        target = self.monotonic_fn() + seconds
        if exact or self._next_at is None:
            self._next_at = target
        else:
            self._next_at = max(self._next_at, target)


class _PageCapturingFetcher(AO3Fetcher):
    """``AO3Fetcher`` that also keeps the last work-page response it received.

    ``fetch`` returns immediately after the response it classified, so the kept
    response is always the page behind the returned record. The HTML carries
    the published date and the EPUB link, which the cached record does not.
    """

    last_response: requests.Response | None = None

    def _request_work_page(self, work_url: str) -> requests.Response:
        response = super()._request_work_page(work_url)
        self.last_response = response
        return response


@dataclass(frozen=True)
class FetchedWorkPage:
    record: AO3FetchRecord
    html: str | None


EPUB_LINK_PATTERN = r'href=["\']((?:https://[a-z.]*archiveofourown\.org)?/downloads/{work_id}/[^"\'#]*?\.epub(?:\?[^"\'#]*)?)["\']'


def find_epub_download_url(page_html: str, work_id: str) -> str:
    """Return the EPUB link from the work page's Download menu.

    Following the page's own link, as ao3downloadernew does, keeps the
    ``updated_at`` cache-buster AO3 adds so the latest version is fetched. The
    constructed fallback uses AO3's ``/downloads/<id>/<name>.epub`` route.
    """

    pattern = re.compile(EPUB_LINK_PATTERN.format(work_id=re.escape(work_id)), re.IGNORECASE)
    match = pattern.search(page_html)
    if match is not None:
        href = html_lib.unescape(match.group(1))
        url = href if href.startswith("https://") else AO3_BASE_URL + href
        if _is_ao3_url(url, DOWNLOAD_HOSTS):
            return url
    LOGGER.warning(f"work {work_id}: no EPUB link on the work page; using AO3's download route")
    return f"{AO3_BASE_URL}/downloads/{work_id}/{work_id}.epub"


class AO3DownloadClient:
    """One signed-in AO3 session that fetches work pages and EPUBs politely."""

    def __init__(
        self,
        credentials: AO3Credentials | None,
        *,
        delay_seconds: float,
        sleep_fn: Callable[[float], None] = time.sleep,
        session: requests.Session | None = None,
        monotonic_fn: Callable[[], float] = time.monotonic,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if delay_seconds < MINIMUM_DELAY_SECONDS:
            raise ValueError(f"delay_seconds must be at least {MINIMUM_DELAY_SECONDS:g}")
        self.credentials = credentials
        self.delay_seconds = delay_seconds
        self.sleep_fn = sleep_fn
        self.timeout_seconds = timeout_seconds
        self.session = session or requests.Session()
        self.pacer = RequestPacer(delay_seconds, sleep_fn=sleep_fn, monotonic_fn=monotonic_fn)
        self.fetcher = _PageCapturingFetcher(
            self.session,
            delay_seconds=delay_seconds,
            timeout_seconds=timeout_seconds,
            sleep_fn=sleep_fn,
            before_request=self.pacer.before_request,
            request_started_callback=self.pacer.request_started,
            request_deferred_callback=self.pacer.defer,
            reauthenticate=self.sign_in if credentials is not None else None,
            retry_unexpected_once=True,
        )
        # AO3Fetcher sets the backfill's User-Agent; downloads identify themselves.
        self.session.headers["User-Agent"] = USER_AGENT

    def close(self) -> None:
        self.session.close()

    def sign_in(self) -> None:
        if self.credentials is None:
            return
        login_authenticated_session(
            self.session,
            self.credentials.username,
            self.credentials.password,
            source=self.credentials.source,
            delay_seconds=self.delay_seconds,
            timeout_seconds=self.timeout_seconds,
            sleep_fn=self.sleep_fn,
            before_request=self.pacer.before_request,
            request_started_callback=self.pacer.request_started,
            request_deferred_callback=self.pacer.defer,
        )

    def recover_session(self, cooldown: Cooldown) -> None:
        """Start from a clean cookie jar and sign in, waiting out trouble."""

        if self.credentials is None:
            return
        self.session.cookies.clear()
        sign_in_patiently(self.sign_in, cooldown)

    def fetch_work_page(self, work_id: str) -> FetchedWorkPage:
        self.fetcher.last_response = None
        record = self.fetcher.fetch(work_id)
        response = self.fetcher.last_response
        page_html = response.text if response is not None and response.status_code == 200 else None
        return FetchedWorkPage(record=record, html=page_html)

    def _wait(self, seconds: float, *, exact: bool, reason: str) -> None:
        LOGGER.warning(
            f"{reason}; waiting {format_duration(seconds)}"
            + (" (server Retry-After)" if exact else " (backoff)")
        )
        self.pacer.defer(seconds, exact)
        self.sleep_fn(seconds)

    def _backoff(self, attempts: int) -> float:
        return min(self.delay_seconds * (2.0 ** (attempts - 1)), TRANSIENT_MAX_BACKOFF_SECONDS)

    def download_epub(self, url: str, work_id: str) -> bytes:
        """Download EPUB bytes under the same rules as a work-page fetch."""

        transient_attempts = 0
        cloudflare_count = 0
        rate_limit_count = 0
        reauthenticated = False
        subject = f"EPUB for work {work_id}"

        for _ in range(MAX_TRANSIENT_ATTEMPTS + MAX_RATE_LIMIT_ATTEMPTS + 4):
            try:
                response = _get_with_ao3_redirects(
                    self.session,
                    url,
                    timeout_seconds=self.timeout_seconds,
                    before_request=self.pacer.before_request,
                    request_started_callback=self.pacer.request_started,
                    request_deferred_callback=self.pacer.defer,
                    sleep_fn=self.sleep_fn,
                    allowed_hosts=DOWNLOAD_HOSTS,
                )
            except requests.RequestException as error:
                transient_attempts += 1
                if transient_attempts >= MAX_TRANSIENT_ATTEMPTS:
                    raise NetworkStopError(
                        f"repeated transient failures downloading the {subject} ({type(error).__name__})"
                    ) from error
                self._wait(
                    self._backoff(transient_attempts),
                    exact=False,
                    reason=f"{subject}: {type(error).__name__}",
                )
                continue

            status = response.status_code
            retry_after = _parse_retry_after(response.headers.get("Retry-After"))
            is_html = response.headers.get("Content-Type", "").casefold().startswith("text/html")

            if _is_cloudflare_challenge(response):
                cloudflare_count += 1
                if cloudflare_count >= 2:
                    if retry_after is not None:
                        self._wait(retry_after, exact=True, reason=f"{subject}: Cloudflare challenge")
                    raise RepeatedCloudflare(f"AO3 returned repeated Cloudflare challenges for the {subject}")
                self._wait(
                    retry_after if retry_after is not None else self.delay_seconds,
                    exact=retry_after is not None,
                    reason=f"{subject}: Cloudflare challenge",
                )
                continue

            # Only an HTML response can be a login page. EPUB bytes are story
            # text, and a story may well contain the words "please log in".
            if _is_login_redirect(response) or status in (401, 403) or (
                is_html and _is_authentication_response(response)
            ):
                if retry_after is not None:
                    self._wait(retry_after, exact=True, reason=f"{subject}: authentication required")
                if self.credentials is not None and not reauthenticated:
                    reauthenticated = True
                    LOGGER.warning(f"AO3 asked for authentication on the {subject}; signing in again once")
                    self.sign_in()
                    continue
                raise AuthenticationFailure(f"AO3 required authentication for the {subject}")

            if status == 429:
                rate_limit_count += 1
                wait = retry_after if retry_after is not None else self._backoff(rate_limit_count)
                if rate_limit_count >= MAX_RATE_LIMIT_ATTEMPTS:
                    if retry_after is not None:
                        self._wait(retry_after, exact=True, reason=f"{subject}: rate limited")
                    raise RepeatedRateLimit(
                        f"AO3 rate-limited the {subject} {rate_limit_count} times in a row; "
                        "consider raising --delay"
                    )
                self._wait(
                    wait,
                    exact=retry_after is not None,
                    reason=f"AO3 rate-limited the {subject} (attempt {rate_limit_count}/{MAX_RATE_LIMIT_ATTEMPTS})",
                )
                continue

            if status in TRANSIENT_STATUS_CODES:
                transient_attempts += 1
                origin_error = _cloudflare_origin_error(response, f"the {subject}")
                reason = origin_error or f"{subject}: transient HTTP {status}"
                if transient_attempts >= MAX_TRANSIENT_ATTEMPTS:
                    raise NetworkStopError(
                        f"AO3 returned {transient_attempts} consecutive transient HTTP {status} "
                        f"responses for the {subject}"
                    )
                self._wait(
                    retry_after if retry_after is not None else self._backoff(transient_attempts),
                    exact=retry_after is not None,
                    reason=reason,
                )
                continue

            if status != 200:
                raise NetworkStopError(f"AO3 returned HTTP {status} for the {subject}")
            if is_html:
                raise UnexpectedHTML(
                    f"AO3 returned an HTML page instead of the {subject} "
                    f"(page title: {_page_title(response.text) or '<none>'!r})"
                )
            return response.content

        raise NetworkStopError(f"could not download the {subject}")


@dataclass(frozen=True)
class WorkOutcome:
    """What happened to one work, as a progress label plus detail."""

    label: str
    detail: str = ""


class Cooldown:
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


def sign_in_patiently(sign_in: Callable[[], None], cooldown: Cooldown) -> None:
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


def run_work_queue(
    work_ids: Sequence[str],
    process: Callable[[str], WorkOutcome],
    *,
    progress: ProgressTracker,
    cooldown: Cooldown,
    recover_session: Callable[[], None],
    record_failure: Callable[[str, Exception], None],
) -> dict[str, str]:
    """Process works unattended and return those that still failed.

    A problem with one work is recorded and the queue moves on. A problem with
    AO3 or the session (Cloudflare, rate limiting, authentication) pauses the
    run, re-establishes the session, and retries the same work once before it
    is blamed on that work. Several failures in a row are treated the same way.
    Failed works get one more attempt after the main pass. Rejected credentials
    and interrupts always stop the run.
    """

    queue: deque[str] = deque(work_ids)
    failed: dict[str, str] = {}
    failure_streak = 0
    last_systemic_work_id: str | None = None
    retry_pass_started = False

    while queue or (failed and not retry_pass_started):
        if not queue:
            retry_pass_started = True
            LOGGER.info(f"main pass finished; retrying {len(failed)} failed works once more")
            progress.add_to_total(len(failed))
            queue.extend(failed)
            continue

        work_id = queue.popleft()
        try:
            outcome = process(work_id)
        except (RunInterrupted, CredentialsRejected):
            raise
        except Exception as error:
            if isinstance(error, SYSTEMIC_FETCH_ERRORS) and work_id != last_systemic_work_id:
                last_systemic_work_id = work_id
                queue.appendleft(work_id)
                cooldown.wait(f"work {work_id}: {error}; this affects every work")
                recover_session()
                continue
            message = f"{type(error).__name__}: {error}"
            if not isinstance(error, (NetworkStopError, requests.RequestException, OSError, ValueError)):
                LOGGER.error(f"work {work_id}: unexpected {message}", exc_info=True)
            record_failure(work_id, error)
            failed[work_id] = message
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

        failed.pop(work_id, None)
        failure_streak = 0
        last_systemic_work_id = None
        cooldown.reset()
        progress.record(outcome.label, f"work={work_id}", outcome.detail)

    return failed
