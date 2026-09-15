"""Write Calibre custom-column values in bulk through Calibre's own API.

``backfill.py write`` runs this under ``calibre-debug``, which provides
Calibre's bundled Python; it cannot be imported by the rest of the project.

One ``cache.set_field`` call per column replaces one ``calibredb set_custom``
subprocess per field per book: on this library that is well under a second
instead of roughly 36 hours. The effect on the library is the same as
``calibredb``: values land in metadata.db and the books are queued in
``metadata_dirtied`` for Calibre to refresh their metadata.opf backups.

Usage: calibre-debug ao3archiver/calibre_scripts/bulk_write.py -- <payload.json>

The payload is ``{"library": path, "columns": {label: {"datatype": t,
"values": {book_id: value}}}}``. Progress is printed as one JSON object per
line so the caller can report it and knows exactly which columns completed.
"""

import json
import sys
import time


def _convert(datatype, value):
    if datatype == "int":
        return int(value)
    if datatype == "float":
        return float(value)
    return str(value)


def _emit(**event):
    print(json.dumps(event, sort_keys=True), flush=True)


def main(payload_path):
    from calibre.library import db

    with open(payload_path, encoding="utf-8") as stream:
        payload = json.load(stream)

    cache = db(payload["library"]).new_api
    try:
        field_metadata = cache.field_metadata
        for label, column in payload["columns"].items():
            key = "#" + label
            expected = column["datatype"]
            actual = field_metadata[key]["datatype"] if key in field_metadata else None
            if actual != expected:
                raise SystemExit(f"column {key} has datatype {actual!r}, expected {expected!r}")
            values = {int(book_id): _convert(expected, value) for book_id, value in column["values"].items()}
            started = time.monotonic()
            changed = cache.set_field(key, values)
            _emit(
                event="column",
                label=label,
                requested=len(values),
                changed=len(changed),
                seconds=round(time.monotonic() - started, 3),
            )
    finally:
        cache.close()
    _emit(event="done")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: calibre-debug bulk_write.py -- <payload.json>")
    main(sys.argv[1])
