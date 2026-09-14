"""One credential path shared by the backfill and the downloader.

Values are read from the process environment, the repository-local ``.env``,
or a legacy ``personal.ini``. Nothing here formats a credential into a message:
callers only ever learn which source supplied the pair.
"""

from __future__ import annotations

import configparser
from collections.abc import Mapping
from dataclasses import dataclass
import os
from pathlib import Path

DEFAULT_DOTENV_PATH = Path(__file__).with_name(".env")
DEFAULT_INI_PATH = Path(__file__).with_name("personal.ini")
INI_SECTION = "archiveofourown.org"
CREDENTIAL_KEYS = ("AO3_USERNAME", "AO3_PASSWORD")


class CredentialError(RuntimeError):
    """Credentials are missing or the credential file is unreadable."""


@dataclass(frozen=True)
class AO3Credentials:
    """A resolved credential pair and the human-readable source it came from."""

    username: str
    password: str
    source: str

    def as_tuple(self) -> tuple[str, str]:
        return (self.username, self.password)


def load_dotenv_values(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise CredentialError("Could not read the AO3 .env file") from error

    values: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" not in stripped:
            raise CredentialError("The AO3 .env file is invalid")
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if key not in CREDENTIAL_KEYS:
            continue
        value = raw_value.strip()
        if len(value) >= 2 and value[0] in {"'", '"'}:
            if value[-1] != value[0]:
                raise CredentialError("The AO3 .env file is invalid")
            value = value[1:-1]
        values[key] = value
    return values


def load_ao3_credentials(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path | None = DEFAULT_DOTENV_PATH,
) -> AO3Credentials:
    """Load an explicitly selected credential pair without exposing either value."""

    dotenv_values = load_dotenv_values(dotenv_path) if dotenv_path is not None else {}
    values = dict(dotenv_values)
    process_values = os.environ if environ is None else environ
    from_environment: list[str] = []
    for key in CREDENTIAL_KEYS:
        if key in process_values:
            values[key] = process_values[key]
            from_environment.append(key)

    username = values.get("AO3_USERNAME")
    password = values.get("AO3_PASSWORD")
    if username is None or password is None or not username.strip() or not password:
        raise CredentialError(
            "AO3 environment credentials require non-empty AO3_USERNAME and AO3_PASSWORD"
        )

    if from_environment and dotenv_values:
        source = f"process environment ({', '.join(from_environment)}) over {dotenv_path}"
    elif from_environment:
        source = f"process environment ({', '.join(from_environment)})"
    else:
        source = str(dotenv_path)
    return AO3Credentials(username=username.strip(), password=password, source=source)


def load_ini_credentials(ini_path: Path = DEFAULT_INI_PATH) -> AO3Credentials | None:
    """Read the legacy ``personal.ini`` pair, or return None when it is unusable."""

    if not Path(ini_path).is_file():
        return None
    config = configparser.ConfigParser(interpolation=None)
    try:
        config.read(ini_path)
    except configparser.Error as error:
        raise CredentialError(f"Could not read {ini_path}") from error
    username = config.get(INI_SECTION, "username", fallback=None)
    password = config.get(INI_SECTION, "password", fallback=None)
    if not username or not password:
        return None
    return AO3Credentials(username=username.strip(), password=password, source=str(ini_path))


def resolve_credentials(
    *,
    dotenv_path: Path | None = DEFAULT_DOTENV_PATH,
    ini_path: Path | None = DEFAULT_INI_PATH,
    environ: Mapping[str, str] | None = None,
) -> AO3Credentials:
    """Prefer the environment/.env pair and fall back to the legacy ini file."""

    try:
        return load_ao3_credentials(environ, dotenv_path=dotenv_path)
    except CredentialError as error:
        fallback = load_ini_credentials(ini_path) if ini_path is not None else None
        if fallback is None:
            raise CredentialError(
                "No AO3 credentials found. Set AO3_USERNAME and AO3_PASSWORD in the "
                f"environment or {dotenv_path}, or fill in {ini_path}."
            ) from error
        return fallback
