"""Copy AO3 statistics from enriched EPUBs into Calibre custom columns."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any
import xml.etree.ElementTree as ET
import zipfile

from ao3_metadata import AO3Metadata, extract_work_id, read_ao3_metadata


CUSTOM_COLUMNS = (
    ("ao3_kudos", "AO3 Kudos", "int", "kudos"),
    ("ao3_hits", "AO3 Hits", "int", "hits"),
    ("ao3_bookmarks", "AO3 Bookmarks", "int", "bookmarks"),
    ("ao3_comments", "AO3 Comments", "int", "comments"),
    ("ao3_words", "AO3 Words", "int", "words"),
    ("ao3_chapters", "AO3 Chapters", "text", "chapters"),
    ("ao3_status", "AO3 Status", "text", "status"),
)

# The existing EPUB sync intentionally keeps its seven-column contract. The
# backfill can add fields that come directly from a work page without changing
# the separate EPUB-writing path.
BACKFILL_CUSTOM_COLUMNS = CUSTOM_COLUMNS + (
    ("ao3_category", "AO3 Category", "text", "category"),
)

# What a freshly downloaded EPUB should carry so that importing it into Calibre
# populates every column the backfill writes, including the local metrics.
DOWNLOAD_CUSTOM_COLUMNS = BACKFILL_CUSTOM_COLUMNS + (
    ("words", "Words", "int", "local_words"),
    ("gfog", "Gfog", "float", "local_gfog"),
)


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


def _as_strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, dict):
        return tuple(
            str(item.get("name", item)) if isinstance(item, dict) else str(item)
            for item in value.values()
        )
    if isinstance(value, list):
        return tuple(
            str(item.get("name", item)) if isinstance(item, dict) else str(item)
            for item in value
        )
    if isinstance(value, tuple):
        return tuple(str(item) for item in value)
    return (str(value),)


def _normalise_text(value: str) -> str:
    return " ".join(value.casefold().split())


def _normalise_authors(value: Any) -> tuple[str, ...]:
    authors: list[str] = []
    for author in _as_strings(value):
        authors.extend(part for part in re.split(r"\s+&\s+", author) if part.strip())
    return tuple(_normalise_text(author) for author in authors)


def _book_work_id(book: dict[str, Any]) -> str | None:
    identifiers = book.get("identifiers")
    if not isinstance(identifiers, dict):
        return None

    for scheme, value in identifiers.items():
        values = _as_strings(value)
        if scheme.casefold() == "ao3":
            for identifier in values:
                try:
                    return extract_work_id(identifier)
                except ValueError:
                    continue
        for identifier in values:
            match = re.search(r"/works/(\d+)(?:/|[?#]|$)", identifier)
            if match:
                return match.group(1)
    return None


def _book_title_author_key(book: dict[str, Any]) -> tuple[str, tuple[str, ...]] | None:
    title = book.get("title")
    if not isinstance(title, str) or not title.strip():
        return None

    return _normalise_text(title), _normalise_authors(book.get("authors"))


def load_calibre_books(calibredb: str, library: Path) -> list[dict[str, Any]]:
    output = run_calibredb(
        calibredb,
        library,
        "list",
        "--for-machine",
        "--fields",
        "identifiers,title,authors",
    )
    books = json.loads(output)
    if not isinstance(books, list):
        raise RuntimeError("calibredb returned an unexpected book list")
    return [book for book in books if isinstance(book, dict)]


def load_enriched_epubs(directory: Path) -> list[AO3Metadata]:
    metadata: list[AO3Metadata] = []
    for epub_path in sorted(directory.glob("*.epub")):
        try:
            metadata.append(read_ao3_metadata(epub_path))
        except (KeyError, OSError, ValueError, zipfile.BadZipFile, ET.ParseError) as error:
            print(f"Skipping {epub_path}: {error}")
            continue
    return metadata


def _custom_column_block(details: str, label: str) -> str | None:
    for block in re.split(r"\n\s*\n", details):
        first_line = block.splitlines()[0].strip() if block.splitlines() else ""
        if first_line in {label, f"#{label}"}:
            return block
    return None


def ensure_custom_columns(calibredb: str, library: Path, create_missing: bool) -> None:
    details = run_calibredb(calibredb, library, "custom_columns", "--details")
    for label, name, datatype, _ in CUSTOM_COLUMNS:
        block = _custom_column_block(details, label)
        if block is not None:
            match = re.search(r"['\"]datatype['\"]\s*:\s*['\"]([^'\"]+)", block)
            if match is None or match.group(1) != datatype:
                raise RuntimeError(
                    f"Calibre column #{label} exists but is not a {datatype} column"
                )
            continue
        if not create_missing:
            raise RuntimeError(
                f"Calibre column #{label} is missing; rerun with --create-columns"
            )
        run_calibredb(calibredb, library, "add_custom_column", label, name, datatype)


def _unique_index(values: list[dict[str, Any]], key_function):
    index: dict[Any, dict[str, Any]] = {}
    duplicate_keys: set[Any] = set()
    for value in values:
        key = key_function(value)
        if key is None:
            continue
        if key in index:
            duplicate_keys.add(key)
        else:
            index[key] = value
    for key in duplicate_keys:
        index.pop(key, None)
    return index


def _metadata_value(metadata: AO3Metadata, attribute: str) -> str | None:
    value = getattr(metadata, attribute)
    if value is None:
        return None
    return str(value)


def sync_metadata(
    calibredb: str,
    library: Path,
    directory: Path,
    create_columns: bool = False,
    dry_run: bool = False,
) -> None:
    metadata = load_enriched_epubs(directory)
    if not metadata:
        print(f"No enriched EPUBs found in {directory}")
        return

    books = load_calibre_books(calibredb, library)
    by_work_id = _unique_index(books, _book_work_id)
    by_title_author = _unique_index(books, _book_title_author_key)

    if not dry_run:
        ensure_custom_columns(calibredb, library, create_columns)

    updated_books = 0
    unmatched = 0
    for item in metadata:
        book = by_work_id.get(item.work_id)
        if book is None and item.title:
            key = (
                _normalise_text(item.title),
                _normalise_authors(item.authors),
            )
            book = by_title_author.get(key)

        if book is None:
            unmatched += 1
            print(f"No Calibre book matched AO3 work {item.work_id} ({item.title or 'unknown title'})")
            continue

        book_id = book.get("id")
        if book_id is None:
            print(f"Calibre book for AO3 work {item.work_id} had no id")
            continue

        print(f"Updating Calibre book {book_id} from AO3 work {item.work_id}")
        for label, _, _, attribute in CUSTOM_COLUMNS:
            value = _metadata_value(item, attribute)
            if dry_run:
                print(f"  #{label} = {value or '<empty>'}")
            else:
                run_calibredb(
                    calibredb,
                    library,
                    "set_custom",
                    label,
                    str(book_id),
                    value or "",
                )

        updated_books += 1

    print(f"Updated {updated_books} Calibre books; {unmatched} EPUBs were not matched.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync AO3 statistics from downloaded EPUBs into Calibre custom columns."
    )
    parser.add_argument("--library", required=True, type=Path, help="path to the Calibre library")
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path("./downloaded"),
        help="directory containing enriched EPUBs (default: ./downloaded)",
    )
    parser.add_argument(
        "--calibredb",
        default="calibredb",
        help="calibredb executable (default: calibredb)",
    )
    parser.add_argument(
        "--create-columns",
        action="store_true",
        help="create the #ao3_* columns that do not exist yet",
    )
    parser.add_argument("--dry-run", action="store_true", help="show updates without changing Calibre")
    args = parser.parse_args()

    sync_metadata(
        args.calibredb,
        args.library,
        args.directory,
        create_columns=args.create_columns,
        dry_run=args.dry_run,
    )


if __name__ == "__main__":
    main()
