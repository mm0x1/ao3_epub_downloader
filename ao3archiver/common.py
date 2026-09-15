"""Paths, the base error, and small helpers shared across the package."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
# Logs, caches, backups, and failure records live outside the repository.
STATE_DIR = Path.home() / ".local" / "share" / "ao3-calibre-backfill"


class ArchiverError(RuntimeError):
    """Base class for failures this project reports on purpose."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json_atomically(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, sort_keys=True)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
