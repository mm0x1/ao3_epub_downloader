"""AO3 request layer for ``download.py``, built on the backfill's proven policy.

Work pages go through ``ao3_backfill.AO3Fetcher`` unchanged, so downloads get
the same Cloudflare, rate-limit, transient-retry, chapter-redirect, and Mystery
Work handling that the backfill exercised against thousands of live works. The
EPUB request needs its own loop because its success response is binary, but it
applies the same rules with the same helpers.

The account-level pieces (pacing, sign-in, cool-downs, run lock) live here too,
so the backfill and the downloader can never run against AO3 at the same time.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import html as html_lib
import logging
from pathlib import Path
import re
import time

import requests

from ao3_backfill import (
    AO3_HOSTS,
    CACHE_NAME,
    DEFAULT_CACHE_DIR,
    DEFAULT_TIMEOUT_SECONDS,
    FAILURE_STREAK_COOLDOWN_THRESHOLD,
    MAX_RATE_LIMIT_ATTEMPTS,
    MAX_TRANSIENT_ATTEMPTS,
    MINIMUM_DELAY_SECONDS,
    SYSTEMIC_FETCH_ERRORS,
    TRANSIENT_MAX_BACKOFF_SECONDS,
    TRANSIENT_STATUS_CODES,
    AO3Fetcher,
    AO3FetchRecord,
    AuthenticationFailure,
    BackfillError,
    CacheStore,
    CredentialsRejected,
    NetworkStopError,
    RepeatedCloudflare,
    RepeatedRateLimit,
    UnexpectedHTML,
    _cloudflare_origin_error,
    _Cooldown,
    _get_with_ao3_redirects,
    _is_ao3_url,
    _is_authentication_response,
    _is_cloudflare_challenge,
    _is_login_redirect,
    _page_title,
    _parse_retry_after,
    _sign_in_patiently,
    login_authenticated_session,
)
from credentials import AO3Credentials
from run_log import ProgressTracker, RunInterrupted, format_duration

LOGGER = logging.getLogger("ao3.download")

AO3_BASE_URL = "https://archiveofourown.org"
DOWNLOAD_HOSTS = AO3_HOSTS | frozenset({"download.archiveofourown.org"})
USER_AGENT = "ao3Archiver-download/1.0 (sequential work downloads)"
# The backfill's run lock doubles as an account lock: AO3 sees one account, so a
# download must not run while a backfill fetch (or another download) is active.
ACCOUNT_LOCK_CACHE_PATH = DEFAULT_CACHE_DIR / CACHE_NAME


class AnotherRunActive(RuntimeError):
    """Another backfill fetch or download already holds the account lock."""


@contextmanager
def exclusive_ao3_run(cache_path: Path = ACCOUNT_LOCK_CACHE_PATH) -> Iterator[None]:
    """Hold the account lock for the whole run, or refuse to start."""

    stack = ExitStack()
    try:
        stack.enter_context(CacheStore(cache_path).fetch_lock())
    except BackfillError as error:
        raise AnotherRunActive(
            f"Another AO3 run is active: the backfill fetch or another download holds "
            f"{CacheStore(cache_path).fetch_lock_path}. Only one run can use the account "
            "at a time; wait for it to finish."
        ) from error
    with stack:
        yield


def ao3_run_active(cache_path: Path = ACCOUNT_LOCK_CACHE_PATH) -> bool:
    """Report whether another run currently holds the account lock."""

    try:
        with exclusive_ao3_run(cache_path):
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

    def recover_session(self, cooldown: _Cooldown) -> None:
        """Start from a clean cookie jar and sign in, waiting out trouble."""

        if self.credentials is None:
            return
        self.session.cookies.clear()
        _sign_in_patiently(self.sign_in, cooldown)

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


def run_work_queue(
    work_ids: Sequence[str],
    process: Callable[[str], WorkOutcome],
    *,
    progress: ProgressTracker,
    cooldown: _Cooldown,
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
