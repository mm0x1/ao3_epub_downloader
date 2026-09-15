"""Everything that touches a Calibre library.

The custom-column contract, calibredb, the Calibre-closed check, metadata.db
backups, the bulk writer that runs under calibre-debug, and read-back
verification.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import cast
import urllib.parse

from ao3archiver.common import ArchiverError, sha256_file, utc_now, write_json_atomically

LOGGER = logging.getLogger("ao3.calibre")


# (label, display name, Calibre datatype, attribute on AO3Metadata/AO3FetchRecord)
AO3_COLUMNS = (
    ("ao3_kudos", "AO3 Kudos", "int", "kudos"),
    ("ao3_hits", "AO3 Hits", "int", "hits"),
    ("ao3_bookmarks", "AO3 Bookmarks", "int", "bookmarks"),
    ("ao3_comments", "AO3 Comments", "int", "comments"),
    ("ao3_words", "AO3 Words", "int", "words"),
    ("ao3_chapters", "AO3 Chapters", "text", "chapters"),
    ("ao3_status", "AO3 Status", "text", "status"),
    ("ao3_category", "AO3 Category", "text", "category"),
)


# Calculated from the EPUB itself. The backfill maps these onto EpubMapping
# attributes; downloads read the same attribute names from AO3Metadata.
LOCAL_METRIC_COLUMNS = (
    ("words", "Words", "int", "local_words"),
    ("gfog", "Gfog", "float", "local_gfog"),
)


# Everything a downloaded EPUB carries for Calibre to import.
ALL_COLUMNS = AO3_COLUMNS + LOCAL_METRIC_COLUMNS


class CalibreLibraryError(ArchiverError):
    """A Calibre library check or write that cannot proceed."""


class ColumnConfigurationError(CalibreLibraryError):
    """The Calibre custom columns do not match the required schema."""


class CalibreInUseError(CalibreLibraryError):
    """A Calibre process is using the library during a write operation."""


def sanitized_child_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.pop("AO3_USERNAME", None)
    environment.pop("AO3_PASSWORD", None)
    return environment


def run_calibredb(calibredb: str, library: Path, *arguments: str) -> str:
    command = [calibredb, "--library-path", str(library), *arguments]
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=sanitized_child_environment(),
    )
    if result.returncode != 0:
        message = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{' '.join(command)} failed: {message}")
    return result.stdout


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
        raise CalibreLibraryError("calibredb returned an unexpected book list")
    return [book for book in raw_books if isinstance(book, dict)]


def verify_custom_columns(calibredb: str, library: Path) -> dict[str, dict[str, object]]:
    """Verify the required schema through both calibredb and SQLite."""

    details_output = run_calibredb(calibredb, library, "custom_columns", "--details")
    details = parse_custom_column_details(details_output)
    sqlite_columns = read_custom_columns_sqlite(library)
    verified: dict[str, dict[str, object]] = {}
    for label, name, datatype, _ in AO3_COLUMNS:
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
        raise CalibreLibraryError(f"Could not read the Calibre library UUID: {error}") from error
    if row is None or not row[0]:
        raise CalibreLibraryError(f"Calibre metadata.db has no library UUID: {database}")
    return str(row[0])


CALIBRE_PROCESS_NAMES = frozenset({
    "calibre",
    "calibre-debug",
    "calibre-parallel",
    "calibre-server",
    "calibre-web",
    "calibreweb",
    "calibredb",
    "cps.py",
})


def _program_names(argv: Sequence[str]) -> list[str]:
    """Return the names of the program an argv actually runs.

    Only the executable counts, plus the script or module when the executable
    is a Python interpreter (Calibre-Web runs as ``python3 cps.py`` or
    ``python3 -m calibreweb``). Arguments such as ``--library "/home/drifter/
    Calibre Library"`` are data, and matching them made any process that merely
    mentioned the library look like Calibre.
    """

    if not argv:
        return []
    executable = Path(argv[0]).name.casefold()
    names = [executable]
    if executable.startswith("python"):
        rest = list(argv[1:])
        if "-m" in rest and rest.index("-m") + 1 < len(rest):
            names.append(rest[rest.index("-m") + 1].casefold())
        script = next((arg for arg in rest if not arg.startswith("-")), None)
        if script is not None:
            names.append(Path(script).name.casefold())
    return names


def _is_calibre_program(argv: Sequence[str]) -> bool:
    return any(
        name in CALIBRE_PROCESS_NAMES or "calibre-web" in name or "calibreweb" in name
        for name in _program_names(argv)
    )


def _proc_command_lines(proc_root: Path = Path("/proc")) -> list[tuple[int, list[str]]]:
    processes: list[tuple[int, list[str]]] = []
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
            continue
        argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0") if part]
        if argv:
            processes.append((int(entry.name), argv))
    return processes


def running_calibre_processes(
    command_lines: Callable[[], list[tuple[int, list[str]]]] = _proc_command_lines,
) -> tuple[str, ...]:
    own_pid = os.getpid()
    return tuple(
        f"{pid} {' '.join(argv)}"
        for pid, argv in command_lines()
        if pid != own_pid and _is_calibre_program(argv)
    )


def require_calibre_closed() -> None:
    processes = running_calibre_processes()
    if processes:
        raise CalibreInUseError(
            "Close Calibre and Calibre-Web before changing metadata.db:\n" + "\n".join(processes)
        )


def _backup_manifest_path(backup_path: Path) -> Path:
    return backup_path.with_name(f"{backup_path.name}.manifest.json")


def _validate_sqlite_backup(backup_path: Path, library_uuid: str) -> None:
    uri = f"file:{urllib.parse.quote(str(backup_path), safe='/')}?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise CalibreLibraryError(f"Backup failed SQLite integrity_check: {backup_path}")
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required_tables = {"books", "custom_columns", "library_id"}
            if not required_tables.issubset(tables):
                raise CalibreLibraryError(f"Backup is not a Calibre metadata database: {backup_path}")
            row = connection.execute("SELECT uuid FROM library_id LIMIT 1").fetchone()
    except sqlite3.Error as error:
        raise CalibreLibraryError(f"Backup is not a readable SQLite database: {backup_path}") from error
    if row is None or row[0] != library_uuid:
        raise CalibreLibraryError(f"Backup belongs to a different Calibre library: {backup_path}")


def verify_backup(backup_path: Path, library: Path) -> None:
    source = library / "metadata.db"
    if backup_path.resolve() == source.resolve():
        raise CalibreLibraryError("Backup path must not be metadata.db itself")
    if not backup_path.is_file() or backup_path.stat().st_size <= 0:
        raise CalibreLibraryError(f"Backup is missing or empty: {backup_path}")
    try:
        if os.path.samefile(source, backup_path):
            raise CalibreLibraryError("Backup must be an independent copy of metadata.db")
    except FileNotFoundError:
        pass
    manifest_path = _backup_manifest_path(backup_path)
    if not manifest_path.is_file():
        raise CalibreLibraryError(f"Backup provenance manifest is missing: {manifest_path}")
    try:
        with manifest_path.open(encoding="utf-8") as stream:
            manifest = json.load(stream)
    except (OSError, json.JSONDecodeError) as error:
        raise CalibreLibraryError(f"Backup provenance manifest is invalid: {manifest_path}") from error
    if not isinstance(manifest, dict):
        raise CalibreLibraryError(f"Backup provenance manifest is not an object: {manifest_path}")
    if manifest.get("source_path") != str(source.resolve()):
        raise CalibreLibraryError(f"Backup provenance points to a different library: {backup_path}")
    if not source.is_file() or source.stat().st_size <= 0:
        raise CalibreLibraryError(f"Current metadata.db is missing or empty: {source}")
    if manifest.get("source_size") != source.stat().st_size:
        raise CalibreLibraryError(f"Backup source size differs from the current library: {backup_path}")
    if manifest.get("source_sha256") != sha256_file(source):
        raise CalibreLibraryError(f"Backup source checksum differs from the current library: {backup_path}")
    if manifest.get("backup_size") != backup_path.stat().st_size:
        raise CalibreLibraryError(f"Backup size differs from its provenance manifest: {backup_path}")
    if manifest.get("backup_sha256") != sha256_file(backup_path):
        raise CalibreLibraryError(f"Backup checksum differs from its provenance manifest: {backup_path}")
    library_uuid = read_library_uuid(library)
    if manifest.get("library_uuid") != library_uuid:
        raise CalibreLibraryError(f"Backup library UUID differs from the current library: {backup_path}")
    _validate_sqlite_backup(backup_path, library_uuid)


def create_backup(library: Path, destination: Path) -> Path:
    require_calibre_closed()
    source = library / "metadata.db"
    if not source.is_file() or source.stat().st_size <= 0:
        raise CalibreLibraryError(f"metadata.db is missing or empty: {source}")
    if destination.resolve() == source.resolve():
        raise CalibreLibraryError("Backup destination must not be metadata.db itself")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or _backup_manifest_path(destination).exists():
        raise CalibreLibraryError(
            f"Backup destination already exists; choose a new destination explicitly: {destination}"
        )
    source_size = source.stat().st_size
    source_sha256 = sha256_file(source)
    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{time.time_ns()}.tmp")
    try:
        shutil.copy2(source, temporary)
        if temporary.stat().st_size <= 0:
            raise CalibreLibraryError(f"Backup copy is empty: {temporary}")
        library_uuid = read_library_uuid(library)
        _validate_sqlite_backup(temporary, library_uuid)
        if source.stat().st_size != source_size or sha256_file(source) != source_sha256:
            raise CalibreLibraryError("metadata.db changed while the backup was being created")
        try:
            os.link(temporary, destination)
        except FileExistsError as error:
            raise CalibreLibraryError(
                f"Backup destination appeared during creation; refusing to overwrite: {destination}"
            ) from error
    finally:
        temporary.unlink(missing_ok=True)
    write_json_atomically(
        _backup_manifest_path(destination),
        {
            "schema_version": 1,
            "source_path": str(source.resolve()),
            "source_size": source_size,
            "source_sha256": source_sha256,
            "backup_size": destination.stat().st_size,
            "backup_sha256": sha256_file(destination),
            "library_uuid": library_uuid,
            "created_at": utc_now(),
        },
    )
    verify_backup(destination, library)
    return destination


def setup_custom_columns(calibredb: str, library: Path, backup_path: Path) -> tuple[str, ...]:
    """Create missing columns only after the backup and process gates pass."""

    require_calibre_closed()
    verify_backup(backup_path, library)
    existing = read_custom_columns_sqlite(library)
    for label, name, datatype, _ in AO3_COLUMNS:
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
    for label, name, datatype, _ in AO3_COLUMNS:
        if label in existing:
            continue
        run_calibredb(calibredb, library, "add_custom_column", label, name, datatype)
        created.append(label)
    verify_custom_columns(calibredb, library)
    return tuple(created)


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
        raise CalibreLibraryError("calibredb returned an unexpected local metric value list")
    result: dict[int, dict[str, object]] = {}
    for book in books:
        if not isinstance(book, dict) or not str(book.get("id", "")).isdigit():
            continue
        book_id = int(cast(str | int, book["id"]))
        result[book_id] = {label: book.get(f"*{label}") for label, _, _, _ in LOCAL_METRIC_COLUMNS}
    return result


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
        raise CalibreLibraryError("calibredb returned an unexpected custom value list")
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
                raise CalibreLibraryError(
                    f"Post-write verification failed for book {book_id} column #{label}"
                )
    return {"books": len(expected), "fields": sum(len(fields) for fields in expected.values())}


# Kept in its own directory: calibre-debug puts the script's directory first on
# sys.path, and none of this package's module names may shadow Calibre's imports.
BULK_WRITE_SCRIPT = Path(__file__).with_name("calibre_scripts") / "bulk_write.py"


class BulkWriteError(CalibreLibraryError):
    """The Calibre API writer stopped; ``completed`` lists the columns it finished."""

    def __init__(self, message: str, completed: Sequence[str]) -> None:
        super().__init__(message)
        self.completed = tuple(completed)


EMBED_METADATA_SCRIPT = Path(__file__).with_name("calibre_scripts") / "embed_metadata.py"


def _run_calibre_script(
    calibre_debug: str,
    script: Path,
    payload: Mapping[str, object],
    on_event: Callable[[dict[str, object]], None],
) -> tuple[bool, str]:
    """Run a ``calibre_scripts`` helper under calibre-debug, passing on its JSON events.

    Returns whether it finished cleanly, and the tail of its stderr for errors.
    Raises OSError if calibre-debug cannot be started.
    """

    with tempfile.TemporaryDirectory(prefix="ao3-calibre-") as directory:
        payload_path = Path(directory) / "payload.json"
        payload_path.write_text(json.dumps(payload), encoding="utf-8")
        # stderr goes to a file, not a pipe: Calibre can print a warning per
        # book, and a full stderr pipe would block both processes forever.
        stderr_path = Path(directory) / "stderr.txt"
        with stderr_path.open("w", encoding="utf-8") as stderr_file:
            process = subprocess.Popen(
                [calibre_debug, str(script), "--", str(payload_path)],
                stdout=subprocess.PIPE,
                stderr=stderr_file,
                text=True,
                env=sanitized_child_environment(),
            )
            finished = False
            assert process.stdout is not None
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    # calibre-debug and its plugins may print their own chatter.
                    LOGGER.debug(f"calibre-debug: {line.rstrip()}")
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("event") == "done":
                    finished = True
                on_event(event)
            returncode = process.wait()
        stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    detail = " ".join(stderr.strip().splitlines()[-3:]) or f"exit status {returncode}"
    return finished and returncode == 0, detail


def run_calibre_bulk_write(
    calibre_debug: str,
    library: Path,
    plan: Mapping[str, Mapping[int, object]],
    datatypes: Mapping[str, str],
) -> tuple[str, ...]:
    """Write every planned column through Calibre's API and return the columns written."""

    completed: list[str] = []

    def on_event(event: dict[str, object]) -> None:
        if event.get("event") == "column":
            label = str(event.get("label"))
            completed.append(label)
            LOGGER.info(
                f"  #{label}: {event.get('requested')} values set, "
                f"{event.get('changed')} changed ({event.get('seconds')}s)"
            )

    payload = {
        "library": str(library),
        "columns": {
            label: {
                "datatype": datatypes[label],
                "values": {str(book_id): value for book_id, value in values.items()},
            }
            for label, values in plan.items()
        },
    }
    try:
        ok, detail = _run_calibre_script(calibre_debug, BULK_WRITE_SCRIPT, payload, on_event)
    except OSError as error:
        raise BulkWriteError(f"Could not start {calibre_debug}: {error}", completed) from error
    if not ok:
        raise BulkWriteError(
            f"Calibre API write stopped after {len(completed)} of {len(plan)} columns: {detail}",
            completed,
        )
    return tuple(completed)


@dataclass(frozen=True)
class EmbedResult:
    """What calibre_scripts/embed_metadata.py reported."""

    processed: int
    failures: dict[int, str]


def embed_calibre_metadata(
    calibre_debug: str,
    library: Path,
    book_ids: Sequence[int],
    *,
    mode: str = "embed",
) -> EmbedResult:
    """Have Calibre write each book's metadata into its EPUB (no cover), or just record sizes.

    ``mode="embed"`` matches ``calibredb embed_metadata`` without the cover image;
    ``mode="refresh-sizes"`` records the EPUBs' current sizes in the library.
    """

    failures: dict[int, str] = {}
    processed = 0
    verb = "embedded" if mode == "embed" else "sized"

    def on_event(event: dict[str, object]) -> None:
        nonlocal processed
        kind = event.get("event")
        if kind == "book_error":
            failures[int(cast(int, event.get("book_id")))] = str(event.get("error"))
        elif kind == "progress":
            LOGGER.info(f"  {verb} {event.get('done')}/{event.get('total')} books")
        elif kind == "done":
            processed = int(cast(int, event.get("processed", 0)))

    payload = {"library": str(library), "book_ids": list(book_ids), "mode": mode}
    try:
        ok, detail = _run_calibre_script(calibre_debug, EMBED_METADATA_SCRIPT, payload, on_event)
    except OSError as error:
        raise CalibreLibraryError(f"Could not start {calibre_debug}: {error}") from error
    if not ok:
        raise CalibreLibraryError(f"Calibre stopped part-way ({mode}): {detail}")
    return EmbedResult(processed=processed, failures=failures)


def read_all_custom_values(calibredb: str, library: Path) -> dict[int, dict[str, object]]:
    """Every custom column's value for every book, as Calibre reports it."""

    labels = tuple(read_custom_columns_sqlite(library))
    return _read_custom_values_calibredb(calibredb, library, labels) if labels else {}


def verify_library_values(calibredb: str, library: Path) -> dict[str, object]:
    """Read custom values and exercise Calibre's numeric search/sort paths."""

    verify_custom_columns(calibredb, library)
    field_names = ",".join(f"*{label}" for label, _, _, _ in AO3_COLUMNS)
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
        raise CalibreLibraryError("calibredb returned an unexpected value list")
    populated: dict[str, int] = {}
    for label, _, _, _ in AO3_COLUMNS:
        populated[label] = sum(
            1
            for book in books
            if isinstance(book, dict) and book.get(f"*{label}") not in (None, "")
        )

    search_query = "#ao3_kudos:>100"
    try:
        # An explicit limit, so the count can never be silently capped.
        search_output = run_calibredb(
            calibredb, library, "search", "--limit", str(max(len(books), 1)), search_query
        )
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
        # calibredb search prints matching ids comma-separated on one line.
        "numeric_search_result_count": len(
            [book_id for book_id in re.split(r"[,\s]+", search_output) if book_id.strip()]
        ),
        "sorted_sample": sample,
    }
