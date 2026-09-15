"""Terminal and file logging for the long-running AO3 archive operations.

Both the Calibre backfill and the downloader run for days at a time. This
module gives them one shared contract: every line is flushed as it is written,
every run keeps its own log file, progress carries a rate and an ETA, and no
credential can reach either destination.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import math
from pathlib import Path
import signal
import sys
import time

from ao3archiver.common import STATE_DIR

DEFAULT_LOG_DIR = STATE_DIR / "logs"
REDACTED = "<redacted>"
ROOT_LOGGER_NAME = "ao3"
MINIMUM_SECRET_LENGTH = 3
MINIMUM_RATE_ELAPSED_SECONDS = 1.0
TERMINAL_TIME_FORMAT = "%H:%M:%S"
FILE_TIME_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


class RunInterrupted(RuntimeError):
    """A signal asked the run to stop at the next safe point."""


class CredentialFilter(logging.Filter):
    """Replace known secrets in every record before it reaches a handler.

    The backfill's rule is that credentials never appear in output. Enforcing
    that with a filter makes it structural instead of a convention every new
    log call has to remember.
    """

    def __init__(self) -> None:
        super().__init__()
        self._secrets: list[str] = []

    def add_secret(self, value: str | None) -> None:
        if not value or len(value) < MINIMUM_SECRET_LENGTH or value in self._secrets:
            return
        self._secrets.append(value)
        # Redact the longest match first so a username that is a substring of
        # the password cannot leave the remainder of the password behind.
        self._secrets.sort(key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        redacted = self.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class _TerminalFormatter(logging.Formatter):
    """Show a clock and keep the severity out of the way for normal progress."""

    def __init__(self) -> None:
        super().__init__(datefmt=TERMINAL_TIME_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        message = record.getMessage()
        if record.levelno <= logging.INFO:
            return f"{timestamp}  {message}"
        return f"{timestamp}  {record.levelname}  {message}"


_ACTIVE_FILTER: CredentialFilter | None = None


def register_secret(value: str | None) -> None:
    """Redact a value from every subsequent log record in this process.

    Credentials are loaded deep inside the fetch, far from the code that set
    logging up, so the registry is module level rather than passed around.
    """

    if _ACTIVE_FILTER is not None:
        _ACTIVE_FILTER.add_secret(value)


def redact_secrets(text: str) -> str:
    """Scrub registered credentials from text that is persisted rather than logged."""

    return text if _ACTIVE_FILTER is None else _ACTIVE_FILTER.redact(text)


@dataclass(frozen=True)
class RunLog:
    """One configured run: the logger, its log file, and its redaction filter."""

    logger: logging.Logger
    log_path: Path
    credentials: CredentialFilter

    def add_secret(self, value: str | None) -> None:
        self.credentials.add_secret(value)


def configure_logging(
    command: str,
    *,
    log_dir: Path | None = None,
    level: int = logging.INFO,
    stream: object | None = None,
    now_fn: Callable[[], datetime] = datetime.now,
    root_name: str = ROOT_LOGGER_NAME,
) -> RunLog:
    """Attach a flushing terminal handler and a per-run file handler.

    Handlers go on the shared ``ao3`` parent so module loggers such as
    ``ao3.backfill`` propagate into the same terminal and file; ``command``
    only names the log file.
    """

    directory = DEFAULT_LOG_DIR if log_dir is None else log_dir
    directory.mkdir(parents=True, exist_ok=True)
    started = now_fn().astimezone()
    log_path = directory / f"{command}-{started.strftime('%Y%m%dT%H%M%S')}.log"

    logger = logging.getLogger(root_name)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    global _ACTIVE_FILTER
    credentials = CredentialFilter()
    _ACTIVE_FILTER = credentials

    target = sys.stdout if stream is None else stream
    reconfigure = getattr(target, "reconfigure", None)
    if reconfigure is not None:
        try:
            # logging.StreamHandler flushes each record, but any residual
            # print() in the same process should stay interleaved correctly.
            reconfigure(line_buffering=True)
        except (ValueError, OSError):
            pass

    terminal = logging.StreamHandler(target)
    terminal.setFormatter(_TerminalFormatter())
    terminal.addFilter(credentials)
    logger.addHandler(terminal)

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", datefmt=FILE_TIME_FORMAT)
    )
    file_handler.addFilter(credentials)
    logger.addHandler(file_handler)

    return RunLog(logger=logger, log_path=log_path, credentials=credentials)


def format_duration(seconds: float | None) -> str:
    """Render an elapsed or remaining span compactly enough for a progress line."""

    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return "unknown"
    total = int(seconds)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d{hours:02d}h"
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def format_clock(moment: datetime | None, *, with_date: bool = True) -> str:
    if moment is None:
        return "unknown"
    return moment.strftime("%Y-%m-%d %H:%M" if with_date else "%m-%d %H:%M")


def log_banner(logger: logging.Logger, title: str, entries: Sequence[tuple[str, object]]) -> None:
    """Print an aligned key/value block so a run states its inputs up front."""

    logger.info("=" * 72)
    logger.info(title)
    width = max((len(label) for label, _ in entries), default=0)
    for label, value in entries:
        logger.info(f"  {label.ljust(width)}  {value}")
    logger.info("=" * 72)


class ProgressTracker:
    """Count completed items and report rate, elapsed time, and a wall-clock ETA."""

    def __init__(
        self,
        logger: logging.Logger,
        total: int,
        *,
        unit: str = "work",
        summary_every: int = 25,
        monotonic_fn: Callable[[], float] = time.monotonic,
        now_fn: Callable[[], datetime] = datetime.now,
    ) -> None:
        if total < 0:
            raise ValueError("total must not be negative")
        self.logger = logger
        self.total = total
        self.unit = unit
        self.summary_every = summary_every
        self.monotonic_fn = monotonic_fn
        self.now_fn = now_fn
        self.started_at = monotonic_fn()
        self.done = 0
        self.outcomes: Counter[str] = Counter()
        self._width = len(str(total)) if total else 1

    def add_to_total(self, count: int) -> None:
        """Grow the total when a run schedules extra attempts, such as a retry pass."""

        if count < 0:
            raise ValueError("count must not be negative")
        self.total += count

    @property
    def elapsed_seconds(self) -> float:
        return max(0.0, self.monotonic_fn() - self.started_at)

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)

    def rate_per_hour(self) -> float | None:
        elapsed = self.elapsed_seconds
        # Below a second the quotient is noise, not a rate; report nothing
        # rather than an implausible number.
        if self.done <= 0 or elapsed < MINIMUM_RATE_ELAPSED_SECONDS:
            return None
        return self.done / elapsed * 3600.0

    def eta(self) -> datetime | None:
        rate = self.rate_per_hour()
        if rate is None or rate <= 0 or not self.remaining:
            return None
        return self.now_fn().astimezone() + timedelta(hours=self.remaining / rate)

    def record(self, outcome: str, subject: str = "", detail: str = "") -> None:
        """Log one completed item and, periodically, a fuller summary."""

        self.done += 1
        self.outcomes[outcome] += 1
        self.logger.info(self.item_line(outcome, subject, detail))
        if (
            self.summary_every > 0
            and self.done % self.summary_every == 0
            and self.done < self.total
        ):
            self.log_summary()

    def item_line(self, outcome: str, subject: str = "", detail: str = "") -> str:
        percent = (self.done / self.total * 100.0) if self.total else 100.0
        rate = self.rate_per_hour()
        rate_text = f"{rate:.0f}/h" if rate is not None else "--/h"
        parts = [
            f"[{self.done:>{self._width}}/{self.total}]",
            f"{percent:5.1f}%",
        ]
        if subject:
            parts.append(subject)
        parts.append(outcome)
        if detail:
            parts.append(detail)
        parts.append(
            f"| {rate_text} | elapsed {format_duration(self.elapsed_seconds)} "
            f"| eta {format_clock(self.eta(), with_date=False)}"
        )
        return " ".join(parts)

    def outcome_summary(self) -> str:
        if not self.outcomes:
            return "none yet"
        return " · ".join(
            f"{outcome} {count}" for outcome, count in sorted(self.outcomes.items())
        )

    def log_summary(self, *, final: bool = False) -> None:
        heading = "final" if final else "progress"
        rate = self.rate_per_hour()
        rate_text = f"{rate:.1f} {self.unit}s/h" if rate is not None else "unknown rate"
        self.logger.info(f"--- {heading}: {self.done}/{self.total} {self.unit}s ---")
        self.logger.info(f"    {self.outcome_summary()}")
        line = f"    {rate_text} · elapsed {format_duration(self.elapsed_seconds)} · remaining {self.remaining}"
        if not final and self.remaining:
            line += f" · eta {format_clock(self.eta())}"
        self.logger.info(line)


class InterruptGuard:
    """Turn SIGINT/SIGTERM into a checked stop at the next safe point."""

    def __init__(
        self,
        logger: logging.Logger,
        *,
        unit: str = "item",
        sleep_fn: Callable[[float], None] = time.sleep,
        monotonic_fn: Callable[[], float] = time.monotonic,
        slice_seconds: float = 0.25,
    ) -> None:
        self.logger = logger
        self.unit = unit
        self._sleep_fn = sleep_fn
        self._monotonic_fn = monotonic_fn
        self._slice_seconds = slice_seconds
        self._requested = False

    @property
    def requested(self) -> bool:
        return self._requested

    def request(self) -> None:
        self._requested = True

    def check(self) -> None:
        if self._requested:
            raise RunInterrupted(f"stopped by signal after the current {self.unit}")

    def sleep(self, seconds: float) -> None:
        """Wait in slices so an interrupt is noticed without cutting the wait short.

        Raising out of the wait is safe because no request follows it, and the
        next run re-reads the persisted schedule and serves out the remainder.
        """

        self.check()
        if seconds <= 0:
            return
        deadline = self._monotonic_fn() + seconds
        while True:
            remaining = deadline - self._monotonic_fn()
            if remaining <= 0:
                break
            self._sleep_fn(min(self._slice_seconds, remaining))
            self.check()

    def _handle(self, signum: int, frame: object) -> None:
        if self._requested:
            # A second signal means the user wants out now.
            signal.signal(signum, signal.SIG_DFL)
            raise KeyboardInterrupt
        self._requested = True
        self.logger.warning(
            f"interrupt received; stopping after the current {self.unit} "
            "(press again to abort immediately)"
        )


@contextmanager
def interrupt_guard(
    logger: logging.Logger,
    *,
    unit: str = "item",
    sleep_fn: Callable[[float], None] = time.sleep,
) -> Iterator[InterruptGuard]:
    """Install signal handlers for the duration of a long run."""

    guard = InterruptGuard(logger, unit=unit, sleep_fn=sleep_fn)
    previous: dict[int, object] = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.signal(signum, guard._handle)
        except ValueError:
            # Not the main thread; the caller still gets a usable guard.
            pass
    try:
        yield guard
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)  # type: ignore[arg-type]
