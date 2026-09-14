"""Read AO3 statistics and preserve them in EPUB metadata."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
import os
from pathlib import Path
import json
import re
import stat
import tempfile
from typing import Iterator
import zipfile
import xml.etree.ElementTree as ET


DC_NS = "http://purl.org/dc/elements/1.1/"
OPF_NS = "http://www.idpf.org/2007/opf"
CALIBRE_NS = "http://calibre.kovidgoyal.net/2009/metadata"
DCTERMS_NS = "http://purl.org/dc/terms/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"

STAT_FIELDS = (
    "published",
    "updated",
    "status",
    "words",
    "chapters",
    "comments",
    "kudos",
    "bookmarks",
    "hits",
)
WORK_ID_PATTERN = re.compile(r"/works/(\d+)(?:/|[?#]|$)")
AO3_STATS_BLOCK_PATTERN = re.compile(
    r"\s*<p><strong>AO3 statistics</strong>.*?</p>",
    re.DOTALL,
)
AO3_OPTIONAL_META_NAMES = (
    "ao3:published",
    "ao3:updated",
    "ao3:status",
    "ao3:words",
    "ao3:chapters",
    "ao3:comments",
    "ao3:kudos",
    "ao3:bookmarks",
    "ao3:hits",
    "ao3:category",
    "ao3:local_words",
    "ao3:local_gfog",
)


@dataclass(frozen=True)
class AO3Metadata:
    """Metadata that can be read from an AO3 work page or enriched EPUB."""

    work_id: str
    work_url: str
    title: str | None = None
    authors: tuple[str, ...] = ()
    category: str | None = None
    published: str | None = None
    updated: str | None = None
    status: str | None = None
    words: int | None = None
    chapters: str | None = None
    comments: int | None = None
    kudos: int | None = None
    bookmarks: int | None = None
    hits: int | None = None
    local_words: int | None = None
    local_gfog: float | None = None


class _AO3StatsParser(HTMLParser):
    """Extract the values in AO3's ``dd`` elements for work statistics."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stats: dict[str, str] = {}
        self.categories: list[str] = []
        self._depth = 0
        self._category_depth: int | None = None
        self._category_anchor_depth: int | None = None
        self._category_parts: list[str] = []
        self._active_stat: str | None = None
        self._active_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._depth += 1
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag.lower() == "dd" and "category" in classes and self._category_depth is None:
            self._category_depth = self._depth
        if (
            tag.lower() == "a"
            and self._category_depth is not None
            and self._category_anchor_depth is None
        ):
            self._category_anchor_depth = self._depth
            self._category_parts = []
        if tag.lower() != "dd":
            return

        stat = next((field for field in STAT_FIELDS if field in classes), None)
        if stat is not None:
            self._active_stat = stat
            self._active_parts = []

    def handle_data(self, data: str) -> None:
        if self._category_anchor_depth is not None and self._depth >= self._category_anchor_depth:
            self._category_parts.append(data)
        if self._active_stat is not None:
            self._active_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self._category_anchor_depth == self._depth:
            value = " ".join("".join(self._category_parts).split())
            if value:
                self.categories.append(value)
            self._category_anchor_depth = None
            self._category_parts = []
        if self._category_depth == self._depth:
            self._category_depth = None
        if tag.lower() == "dd" and self._active_stat is not None:
            value = " ".join("".join(self._active_parts).split())
            self.stats[self._active_stat] = value
            self._active_stat = None
            self._active_parts = []
        self._depth = max(0, self._depth - 1)


def extract_work_id(url: str) -> str:
    """Return the numeric AO3 work id from a work URL."""

    match = WORK_ID_PATTERN.search(url)
    if match is None:
        raise ValueError(f"Could not find an AO3 work id in URL: {url}")
    return match.group(1)


def canonical_work_url(url_or_id: str) -> str:
    """Return the stable public URL for an AO3 work."""

    work_id = url_or_id if url_or_id.isdigit() else extract_work_id(url_or_id)
    return f"https://archiveofourown.org/works/{work_id}"


def _parse_count(value: str | None) -> int | None:
    if value is None:
        return None

    digits = re.sub(r"[^0-9]", "", value)
    return int(digits) if digits else None


def parse_ao3_metadata(html: str, work_url: str) -> AO3Metadata:
    """Parse AO3's work statistics from an HTML response."""

    parser = _AO3StatsParser()
    parser.feed(html)
    parser.close()

    if not parser.stats:
        raise ValueError("AO3 work page did not contain a statistics block")

    values = parser.stats
    work_id = extract_work_id(work_url)
    return AO3Metadata(
        work_id=work_id,
        work_url=canonical_work_url(work_id),
        category=", ".join(parser.categories) or None,
        published=values.get("published"),
        updated=values.get("updated"),
        status=values.get("status"),
        words=_parse_count(values.get("words")),
        chapters=values.get("chapters"),
        comments=_parse_count(values.get("comments")),
        kudos=_parse_count(values.get("kudos")),
        bookmarks=_parse_count(values.get("bookmarks")),
        hits=_parse_count(values.get("hits")),
    )


def metadata_from_csv_row(row: Mapping[str, str | None], work_url: str) -> AO3Metadata | None:
    """Build metadata from an ao3downloader CSV row when counter fields exist."""

    counter_fields = ("comments", "kudos", "bookmarks", "hits")
    if not any(field in row for field in counter_fields):
        return None

    def value(name: str) -> str | None:
        raw = row.get(name)
        if raw is None:
            return None
        cleaned = raw.strip()
        return cleaned or None

    author = value("author")
    authors = (author,) if author else ()
    category = value("category") or value("categories")
    status = value("status")
    if status is None and value("complete") is not None:
        status = "Complete" if value("complete") == "True" else "Incomplete"

    work_id = extract_work_id(work_url)
    return AO3Metadata(
        work_id=work_id,
        work_url=canonical_work_url(work_id),
        title=value("title"),
        authors=authors,
        category=category,
        published=value("published"),
        updated=value("updated"),
        status=status,
        words=_parse_count(value("words")),
        chapters=value("chapters"),
        comments=_parse_count(value("comments")),
        kudos=_parse_count(value("kudos")),
        bookmarks=_parse_count(value("bookmarks")),
        hits=_parse_count(value("hits")),
    )


def _qname(namespace: str, name: str) -> str:
    return f"{{{namespace}}}{name}"


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find_rootfile(container: ET.Element) -> str:
    for element in container.iter():
        if _local_name(element.tag) == "rootfile":
            full_path = element.attrib.get("full-path")
            if full_path:
                return full_path
    raise ValueError("EPUB container did not declare a rootfile")


def _find_metadata_element(package: ET.Element) -> ET.Element:
    for element in package:
        if _local_name(element.tag) == "metadata":
            return element
    raise ValueError("EPUB package did not contain a metadata element")


def _metadata_children(metadata: ET.Element, name: str) -> Iterator[ET.Element]:
    for child in metadata:
        if _local_name(child.tag) == "meta" and child.attrib.get("name") == name:
            yield child


def _set_meta_value(metadata: ET.Element, name: str, value: str) -> None:
    matches = list(_metadata_children(metadata, name))
    existing = matches[0] if matches else None
    if existing is None:
        existing = ET.SubElement(metadata, _qname(OPF_NS, "meta"), {"name": name})
    for duplicate in matches[1:]:
        metadata.remove(duplicate)
    existing.set("content", value)


def _set_plain_meta_value(metadata: ET.Element, name: str, value: str) -> None:
    """Write a ``<meta>`` element with no namespace prefix.

    Calibre's user-metadata reader matches the *qualified* element name
    (``//*[name() = "meta" ...]``), so a prefixed ``<opf:meta>`` is silently
    ignored. Which prefix ElementTree emits depends on how the source OPF
    declared its namespaces, so these metas are written unqualified.
    """

    matches = list(_metadata_children(metadata, name))
    existing = matches[0] if matches else None
    if existing is None:
        existing = ET.SubElement(metadata, "meta", {"name": name})
    for duplicate in matches[1:]:
        metadata.remove(duplicate)
    existing.set("content", value)


def _remove_meta_value(metadata: ET.Element, name: str) -> None:
    for element in list(_metadata_children(metadata, name)):
        metadata.remove(element)


def _get_meta_value(metadata: ET.Element, name: str) -> str | None:
    element = next(_metadata_children(metadata, name), None)
    if element is None:
        return None
    return element.attrib.get("content")


def _find_dc_element(metadata: ET.Element, name: str) -> ET.Element | None:
    qualified_name = _qname(DC_NS, name)
    return next((child for child in metadata if child.tag == qualified_name), None)


def _set_ao3_identifier(metadata: ET.Element, work_url: str) -> None:
    identifier = None
    for child in metadata:
        if child.tag != _qname(DC_NS, "identifier"):
            continue
        if child.attrib.get("id") == "ao3" or child.attrib.get(_qname(OPF_NS, "scheme")) == "ao3":
            identifier = child
            break

    if identifier is None:
        identifier = ET.Element(_qname(DC_NS, "identifier"))
        metadata.append(identifier)

    identifier.set("id", "ao3")
    identifier.set(_qname(OPF_NS, "scheme"), "ao3")
    identifier.text = work_url


def _format_statistics(metadata: AO3Metadata) -> str:
    values: list[tuple[str, str | int | float | None]] = [
        ("Published", metadata.published),
        ("Updated", metadata.updated),
        ("Status", metadata.status),
        ("Words", metadata.words),
        ("Chapters", metadata.chapters),
        ("Comments", metadata.comments),
        ("Kudos", metadata.kudos),
        ("Bookmarks", metadata.bookmarks),
        ("Hits", metadata.hits),
        ("Category", metadata.category),
        ("Local Words", metadata.local_words),
        ("Gunning Fog", metadata.local_gfog),
    ]
    lines = [
        f"{label}: {escape(str(value))}"
        for label, value in values
        if value is not None
    ]
    return "<p><strong>AO3 statistics</strong><br/>" + "<br/>".join(lines) + "</p>"


def _update_description(metadata_element: ET.Element, metadata: AO3Metadata) -> None:
    description = _find_dc_element(metadata_element, "description")
    if description is None:
        description = ET.Element(_qname(DC_NS, "description"))
        metadata_element.append(description)

    current = description.text or ""
    statistics = _format_statistics(metadata)
    if AO3_STATS_BLOCK_PATTERN.search(current):
        description.text = AO3_STATS_BLOCK_PATTERN.sub(statistics, current, count=1)
    else:
        separator = "\n\n" if current.strip() else ""
        description.text = f"{current}{separator}{statistics}"


def _metadata_values(metadata: AO3Metadata) -> dict[str, str]:
    values = {
        "ao3:metadata_version": "1",
        "ao3:work_id": metadata.work_id,
        "ao3:work_url": metadata.work_url,
    }
    optional_values: dict[str, str | int | float | None] = {
        "ao3:published": metadata.published,
        "ao3:updated": metadata.updated,
        "ao3:status": metadata.status,
        "ao3:words": metadata.words,
        "ao3:chapters": metadata.chapters,
        "ao3:comments": metadata.comments,
        "ao3:kudos": metadata.kudos,
        "ao3:bookmarks": metadata.bookmarks,
        "ao3:hits": metadata.hits,
        "ao3:category": metadata.category,
        "ao3:local_words": metadata.local_words,
        "ao3:local_gfog": metadata.local_gfog,
    }
    values.update(
        {
            name: str(value)
            for name, value in optional_values.items()
            if value is not None
        }
    )
    return values


def _calibre_user_metadata(
    metadata: AO3Metadata,
    columns: Sequence[tuple[str, str, str, str]],
) -> dict[str, str]:
    """Render Calibre custom-column values as OPF ``calibre:user_metadata`` metas.

    Calibre's importer reads these on add and populates the matching custom
    columns, so a downloaded EPUB carries its own Calibre metadata. Columns
    that do not already exist in the target library are ignored by Calibre.
    """

    values: dict[str, str] = {}
    for index, (label, display_name, datatype, attribute) in enumerate(columns, start=1):
        value = getattr(metadata, attribute, None)
        if value is None:
            continue
        payload = {
            "table": f"custom_column_{index}",
            "column": "value",
            "datatype": datatype,
            "is_multiple": None,
            "kind": "field",
            "name": display_name,
            "search_terms": [f"#{label}"],
            "label": label,
            "colnum": index,
            "display": {},
            "is_custom": True,
            "is_category": False,
            "link_column": "value",
            "category_sort": "value",
            "is_csp": False,
            "is_editable": True,
            "#value#": value,
            "#extra#": None,
        }
        values[f"calibre:user_metadata:#{label}"] = json.dumps(payload, sort_keys=True)
    return values


def _serialize_package(package: ET.Element) -> bytes:
    ET.register_namespace("", OPF_NS)
    ET.register_namespace("dc", DC_NS)
    ET.register_namespace("opf", OPF_NS)
    ET.register_namespace("calibre", CALIBRE_NS)
    ET.register_namespace("dcterms", DCTERMS_NS)
    ET.register_namespace("xsi", XSI_NS)
    return ET.tostring(package, encoding="utf-8", xml_declaration=True)


def validate_epub_file(epub_path: str | Path) -> None:
    """Raise if a file is not a minimally valid EPUB container."""

    path = Path(epub_path)
    with zipfile.ZipFile(path, "r") as source:
        entries = source.infolist()
        if not entries or entries[0].filename != "mimetype":
            raise ValueError("EPUB mimetype entry must be first")
        if entries[0].compress_type != zipfile.ZIP_STORED:
            raise ValueError("EPUB mimetype entry must be uncompressed")
        if source.read("mimetype") != b"application/epub+zip":
            raise ValueError("EPUB has an invalid mimetype")

        container = ET.fromstring(source.read("META-INF/container.xml"))
        opf_path = _find_rootfile(container)
        package = ET.fromstring(source.read(opf_path))
        _find_metadata_element(package)


def _rewrite_epub(epub_path: Path, replacements: dict[str, bytes]) -> None:
    original_mode = stat.S_IMODE(epub_path.stat().st_mode)
    temporary_path: str | None = None
    try:
        with zipfile.ZipFile(epub_path, "r") as source:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{epub_path.name}.",
                suffix=".tmp",
                dir=epub_path.parent,
                delete=False,
            ) as temporary:
                temporary_path = temporary.name

            with zipfile.ZipFile(temporary_path, "w") as target:
                for info in source.infolist():
                    data = replacements.get(info.filename, source.read(info.filename))
                    if info.filename == "mimetype":
                        target.writestr(info, data, compress_type=zipfile.ZIP_STORED)
                    else:
                        target.writestr(info, data)

        os.chmod(temporary_path, original_mode)
        os.replace(temporary_path, epub_path)
        temporary_path = None
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def enrich_epub(
    epub_path: str | Path,
    metadata: AO3Metadata,
    *,
    calibre_columns: Sequence[tuple[str, str, str, str]] | None = None,
) -> None:
    """Add AO3 identifiers, statistics, and a visible comments block to an EPUB.

    When ``calibre_columns`` is given, Calibre custom-column values are baked in
    as well so importing the file populates those columns directly.
    """

    path = Path(epub_path)
    with zipfile.ZipFile(path, "r") as source:
        container = ET.fromstring(source.read("META-INF/container.xml"))
        opf_path = _find_rootfile(container)
        package = ET.fromstring(source.read(opf_path))

    metadata_element = _find_metadata_element(package)
    _set_ao3_identifier(metadata_element, metadata.work_url)
    _update_description(metadata_element, metadata)
    metadata_values = _metadata_values(metadata)
    for name, value in metadata_values.items():
        _set_meta_value(metadata_element, name, value)
    for name in AO3_OPTIONAL_META_NAMES:
        if name not in metadata_values:
            _remove_meta_value(metadata_element, name)
    if calibre_columns:
        for name, value in _calibre_user_metadata(metadata, calibre_columns).items():
            _set_plain_meta_value(metadata_element, name, value)

    _rewrite_epub(path, {opf_path: _serialize_package(package)})


def enrich_epub_portable(epub_path: str | Path, metadata: AO3Metadata) -> None:
    """Add namespaced AO3/local metadata without changing standard EPUB fields."""

    path = Path(epub_path)
    with zipfile.ZipFile(path, "r") as source:
        container = ET.fromstring(source.read("META-INF/container.xml"))
        opf_path = _find_rootfile(container)
        package = ET.fromstring(source.read(opf_path))

    metadata_element = _find_metadata_element(package)
    for name, value in _metadata_values(metadata).items():
        _set_meta_value(metadata_element, name, value)
    _rewrite_epub(path, {opf_path: _serialize_package(package)})


def _read_dc_text(metadata: ET.Element, name: str) -> str | None:
    element = _find_dc_element(metadata, name)
    if element is None or element.text is None:
        return None
    value = " ".join(element.text.split())
    return value or None


def _read_dc_authors(metadata: ET.Element) -> tuple[str, ...]:
    return tuple(
        " ".join(child.text.split())
        for child in metadata
        if child.tag == _qname(DC_NS, "creator") and child.text and child.text.strip()
    )


def _read_int_meta(metadata: ET.Element, name: str) -> int | None:
    value = _get_meta_value(metadata, name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as error:
        raise ValueError(f"Invalid integer in EPUB metadata {name}: {value}") from error


def _read_float_meta(metadata: ET.Element, name: str) -> float | None:
    value = _get_meta_value(metadata, name)
    if value is None:
        return None
    try:
        return round(float(value), 2)
    except ValueError as error:
        raise ValueError(f"Invalid float in EPUB metadata {name}: {value}") from error


def read_ao3_metadata(epub_path: str | Path) -> AO3Metadata:
    """Read metadata previously written by :func:`enrich_epub`."""

    path = Path(epub_path)
    with zipfile.ZipFile(path, "r") as source:
        container = ET.fromstring(source.read("META-INF/container.xml"))
        opf_path = _find_rootfile(container)
        package = ET.fromstring(source.read(opf_path))

    metadata_element = _find_metadata_element(package)
    work_id = _get_meta_value(metadata_element, "ao3:work_id")
    work_url = _get_meta_value(metadata_element, "ao3:work_url")
    if work_id is None or work_url is None:
        raise ValueError(f"EPUB does not contain AO3 metadata: {path}")
    if work_url != canonical_work_url(work_id):
        raise ValueError(f"EPUB AO3 metadata URL does not match its work id: {path}")

    return AO3Metadata(
        work_id=work_id,
        work_url=work_url,
        title=_read_dc_text(metadata_element, "title"),
        authors=_read_dc_authors(metadata_element),
        category=_get_meta_value(metadata_element, "ao3:category"),
        published=_get_meta_value(metadata_element, "ao3:published"),
        updated=_get_meta_value(metadata_element, "ao3:updated"),
        status=_get_meta_value(metadata_element, "ao3:status"),
        words=_read_int_meta(metadata_element, "ao3:words"),
        chapters=_get_meta_value(metadata_element, "ao3:chapters"),
        comments=_read_int_meta(metadata_element, "ao3:comments"),
        kudos=_read_int_meta(metadata_element, "ao3:kudos"),
        bookmarks=_read_int_meta(metadata_element, "ao3:bookmarks"),
        hits=_read_int_meta(metadata_element, "ao3:hits"),
        local_words=_read_int_meta(metadata_element, "ao3:local_words"),
        local_gfog=_read_float_meta(metadata_element, "ao3:local_gfog"),
    )


def has_calibre_user_metadata(epub_path: str | Path) -> bool:
    """Return whether an EPUB carries Calibre custom-column values Calibre can read.

    Mirrors Calibre's own reader, which only matches an unprefixed ``<meta>``.
    """

    try:
        with zipfile.ZipFile(epub_path, "r") as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            package = ET.fromstring(archive.read(_find_rootfile(container)))
        metadata = _find_metadata_element(package)
    except (KeyError, ValueError, OSError, zipfile.BadZipFile, ET.ParseError):
        return False
    return any(
        child.tag == "meta"
        and child.attrib.get("name", "").startswith("calibre:user_metadata:#")
        and child.attrib.get("content")
        for child in metadata
    )


def has_ao3_metadata(epub_path: str | Path) -> bool:
    """Return whether an EPUB contains metadata written by this project."""

    try:
        read_ao3_metadata(epub_path)
    except (KeyError, ValueError, OSError, zipfile.BadZipFile, ET.ParseError):
        return False
    return True
