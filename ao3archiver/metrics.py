"""Calculate local EPUB words and Gunning Fog values without Calibre plugins.

The compatibility profile uses deterministic punctuation sentence boundaries
with decimal and common-abbreviation guards. It is intentionally identified as
a local profile rather than claiming to reproduce Calibre's bundled Punkt/ICU
implementation byte-for-byte.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
import math
from pathlib import Path
import posixpath
import re
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile

LOCAL_METRICS_ALGORITHM = "count-pages-compatible-pure-python-v1"
_WORD_PATTERN = re.compile(r"[^\W_]+(?:['\u2019\u2010\u2011\u2012\u2013\u2014-][^\W_]+)*", re.UNICODE)
_SENTENCE_ABBREVIATIONS = frozenset({"mr", "mrs", "ms", "dr", "st", "sr", "jr", "etc", "e.g"})
_BODY_PATTERN = re.compile(r"<body\b[^>]*>(.*?)</body\s*>", re.IGNORECASE | re.DOTALL)
_TAG_PATTERN = re.compile(r"<[^>]+>", re.DOTALL)

# These overrides and adjustment rules are the bundled Count Pages English
# fallback syllable rules. They are intentionally local so the backfill does
# not import or execute a Calibre plugin.
_SPECIAL_SYLLABLES = {
    "tottered": 2,
    "chummed": 1,
    "peeped": 1,
    "moustaches": 2,
    "shamefully": 3,
    "messieurs": 2,
    "satiated": 4,
    "sailmaker": 4,
    "sheered": 1,
    "disinterred": 3,
    "propitiatory": 6,
    "bepatched": 2,
    "particularized": 5,
    "caressed": 2,
    "trespassed": 2,
    "sepulchre": 3,
    "flapped": 1,
    "hemispheres": 3,
    "pencilled": 2,
    "motioned": 2,
    "poleman": 2,
    "slandered": 2,
    "sombre": 2,
    "etc": 4,
    "sidespring": 2,
    "mimes": 1,
    "effaces": 2,
    "mr": 2,
    "mrs": 2,
    "ms": 1,
    "dr": 2,
    "st": 1,
    "sr": 2,
    "jr": 2,
    "truckle": 2,
    "foamed": 1,
    "fringed": 2,
    "clattered": 2,
    "capered": 2,
    "mangroves": 2,
    "suavely": 2,
    "reclined": 2,
    "brutes": 1,
    "effaced": 2,
    "quivered": 2,
    "gaped": 1,
    "stammered": 2,
    "shivered": 2,
    "discoloured": 3,
    "gravesend": 2,
    "unstained": 2,
    "unexpressed": 3,
    "greyish": 2,
    "unostentatious": 5,
    "deafened": 2,
    "manoeuvred": 3,
    "sententiously": 4,
    "veriest": 3,
    "h'm": 1,
    "60": 2,
    "lb": 1,
}
_SYLLABLE_SUBTRACT = tuple(
    re.compile(pattern)
    for pattern in ("cial", "tia", "cius", "cious", "gui", "ion", "iou", "sia$", ".ely$")
)
_SYLLABLE_ADD = tuple(
    re.compile(pattern)
    for pattern in (
        "ia",
        "riet",
        "dien",
        "iu",
        "io",
        "ii",
        "[aeiouy]bl$",
        "mbl$",
        "[aeiou]{3}",
        "^mc",
        "ism$",
        "(.)(?!\\1)([aeiouy])\\2l$",
        "[^l]llien",
        "^coad.",
        "^coag.",
        "^coal.",
        "^coax.",
        "(.)(?!\\1)[gq]ua(.)(?!\\2)[aeiou]",
        "dnt$",
    )
)


@dataclass(frozen=True)
class LocalMetrics:
    words: int | None
    gfog: float | None
    error: str | None = None


class _BodyTextParser(HTMLParser):
    """Recover body text from valid or mildly malformed XHTML/HTML."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._body_depth = 0
        self.saw_body = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._body_depth > 0:
            self._body_depth += 1
        elif tag.casefold() == "body":
            self._body_depth = 1
            self.saw_body = True

    def handle_endtag(self, tag: str) -> None:
        if self._body_depth > 0:
            self._body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._body_depth > 0:
            self.parts.append(data)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _entry_path(opf_path: str, href: str) -> str:
    href_without_fragment, _ = urllib.parse.urldefrag(href)
    return posixpath.normpath(
        posixpath.join(posixpath.dirname(opf_path), urllib.parse.unquote(href_without_fragment))
    )


def _spine_paths(archive: zipfile.ZipFile) -> tuple[str, ...]:
    container = ET.fromstring(archive.read("META-INF/container.xml"))
    rootfile = next(
        (
            element
            for element in container.iter()
            if _local_name(element.tag) == "rootfile" and element.attrib.get("full-path")
        ),
        None,
    )
    if rootfile is None:
        raise ValueError("EPUB container did not declare a rootfile")
    opf_path = urllib.parse.unquote(rootfile.attrib["full-path"])
    package = ET.fromstring(archive.read(opf_path))

    manifest: dict[str, str] = {}
    for element in package.iter():
        if _local_name(element.tag) != "item":
            continue
        item_id = element.attrib.get("id")
        href = element.attrib.get("href")
        if item_id and href:
            manifest[item_id] = _entry_path(opf_path, href)

    spine = next((element for element in package.iter() if _local_name(element.tag) == "spine"), None)
    if spine is None:
        raise ValueError("EPUB package did not declare a spine")
    paths = tuple(
        manifest[itemref.attrib["idref"]]
        for itemref in spine
        if _local_name(itemref.tag) == "itemref" and itemref.attrib.get("idref") in manifest
    )
    if not paths:
        raise ValueError("EPUB spine did not contain readable documents")
    return paths


def extract_epub_text(epub_path: str | Path) -> str:
    """Read all spine body text in document order without rewriting the EPUB."""

    parts: list[str] = []
    with zipfile.ZipFile(epub_path) as archive:
        for path in _spine_paths(archive):
            raw = archive.read(path).decode("utf-8", errors="replace")
            fast_body = _BODY_PATTERN.search(raw)
            if fast_body is not None:
                body_text = _TAG_PATTERN.sub(" ", fast_body.group(1))
                parts.append(" ".join(unescape(body_text).split()))
                continue
            parser = _BodyTextParser()
            parser.feed(raw)
            parser.close()
            if not parser.saw_body:
                raise ValueError("EPUB spine document did not contain a body")
            parts.append(" ".join(" ".join(parser.parts).split()))
    return " ".join(part for part in parts if part)


def _words(text: str) -> tuple[str, ...]:
    return tuple(_WORD_PATTERN.findall(text))


def _sentences(text: str) -> tuple[str, ...]:
    sentences: list[str] = []
    start = 0
    index = 0
    while index < len(text):
        character = text[index]
        if character not in ".!?":
            index += 1
            continue
        if character == ".":
            previous = text[index - 1] if index > 0 else ""
            following = text[index + 1] if index + 1 < len(text) else ""
            if previous.isdigit() and following.isdigit():
                index += 1
                continue
            prefix = text[start : index + 1].rstrip()
            word_match = re.search(r"([\w.]+)[.!?]*$", prefix, re.UNICODE)
            if (
                word_match
                and word_match.group(1).rstrip(".").casefold() in _SENTENCE_ABBREVIATIONS
            ):
                index += 1
                continue
        end = index + 1
        while end < len(text) and text[end] in ".!?":
            end += 1
        sentence = text[start:end].strip()
        if sentence:
            sentences.append(sentence)
        start = end
        index = end
    remainder = text[start:].strip()
    if remainder:
        sentences.append(remainder)
    return tuple(sentences)


def _syllables(word: str) -> int:
    normalized = word.strip().lower()
    if not normalized:
        return 0
    special = _SPECIAL_SYLLABLES.get(normalized)
    if special is not None:
        return special
    if normalized[-1] == "e":
        normalized = normalized[:-1]
    count = 0
    previous_vowel = False
    for character in normalized:
        is_vowel = character in "aeiouy"
        if is_vowel and not previous_vowel:
            count += 1
        previous_vowel = is_vowel
    for pattern in _SYLLABLE_ADD:
        if pattern.search(normalized):
            count += 1
    for pattern in _SYLLABLE_SUBTRACT:
        if pattern.search(normalized):
            count -= 1
    return max(0, count)


def _complex_word_count(words: tuple[str, ...], sentences: tuple[str, ...]) -> int:
    complex_words = 0
    for word in words:
        if _syllables(word) < 3:
            continue
        if not word[0].isupper() or any(sentence.startswith(word) for sentence in sentences):
            complex_words += 1
    return complex_words


def calculate_text_metrics(text: str) -> LocalMetrics:
    """Calculate the documented local compatibility metrics for plain text."""

    words = _words(text)
    if not words:
        return LocalMetrics(words=0, gfog=None, error="no words available for Gunning Fog")
    sentences = _sentences(text)
    if not sentences:
        return LocalMetrics(words=len(words), gfog=None, error="no sentences available for Gunning Fog")
    complex_words = _complex_word_count(words, sentences)
    average_words_per_sentence = len(words) / len(sentences)
    gfog = 0.4 * (average_words_per_sentence + 100 * (complex_words / len(words)))
    if not math.isfinite(gfog):
        return LocalMetrics(words=len(words), gfog=None, error="Gunning Fog result was not finite")
    return LocalMetrics(words=len(words), gfog=round(gfog, 2))


def calculate_epub_metrics(epub_path: str | Path) -> LocalMetrics:
    """Calculate local metrics, returning a sanitized unavailable result on failure."""

    try:
        text = extract_epub_text(epub_path)
        return calculate_text_metrics(text)
    except (OSError, KeyError, ValueError, ET.ParseError, zipfile.BadZipFile) as error:
        return LocalMetrics(words=None, gfog=None, error=type(error).__name__)
