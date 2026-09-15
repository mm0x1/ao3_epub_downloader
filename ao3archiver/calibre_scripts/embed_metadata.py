"""Write each book's Calibre metadata into its EPUB file, without the cover.

``backfill.py enrich-epubs`` runs this under ``calibre-debug``, which provides
Calibre's bundled Python; it cannot be imported by the rest of the project.

It does what ``calibredb embed_metadata`` does, using the same Calibre calls,
except that it never embeds the cover image: that would add about 2.4 GB of
covers to a 14,000-book library and can change a book's spine.

Usage: calibre-debug ao3archiver/calibre_scripts/embed_metadata.py -- <payload.json>

The payload is ``{"library": path, "book_ids": [...], "mode": "embed" | "refresh-sizes"}``.
``refresh-sizes`` only records each EPUB's current size in the library, for
files changed by something other than Calibre. Progress is printed as one JSON
object per line.
"""

import json
import sys

PROGRESS_EVERY = 500


def _emit(**event):
    print(json.dumps(event, sort_keys=True), flush=True)


def main(payload_path):
    from calibre.ebooks.metadata.meta import set_metadata
    from calibre.library import db

    with open(payload_path, encoding="utf-8") as stream:
        payload = json.load(stream)
    mode = payload["mode"]
    if mode not in ("embed", "refresh-sizes"):
        raise SystemExit(f"unknown mode {mode!r}")
    book_ids = [int(book_id) for book_id in payload["book_ids"]]

    cache = db(payload["library"]).new_api
    processed = 0
    try:
        for index, book_id in enumerate(book_ids, start=1):
            errors = []
            try:
                path = cache.format_abspath(book_id, "EPUB")
                if not path:
                    raise ValueError("the book has no EPUB format")
                if mode == "embed":
                    metadata = cache.get_metadata(book_id, get_cover=False)
                    with open(path, "r+b") as stream:
                        # Calibre swallows writer failures unless a reporter is given.
                        set_metadata(stream, metadata, stream_type="epub",
                                     report_error=lambda *details: errors.append(str(details[-1])))
                cache.format_metadata(book_id, "EPUB", allow_cache=False, update_db=True)
            except Exception as error:
                errors.append(f"{type(error).__name__}: {error}")
            if errors:
                _emit(event="book_error", book_id=book_id, error=errors[-1].strip().splitlines()[-1][:500])
            else:
                processed += 1
            if index % PROGRESS_EVERY == 0 or index == len(book_ids):
                _emit(event="progress", done=index, total=len(book_ids))
    finally:
        cache.close()
    _emit(event="done", processed=processed)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: calibre-debug embed_metadata.py -- <payload.json>")
    main(sys.argv[1])
