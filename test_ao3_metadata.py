import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
import os
import stat

from ao3_metadata import (
    AO3Metadata,
    _calibre_user_metadata,
    enrich_epub,
    enrich_epub_portable,
    has_ao3_metadata,
    metadata_from_csv_row,
    parse_ao3_metadata,
    read_ao3_metadata,
    validate_epub_file,
)


AO3_PAGE = """
<dl class="stats">
  <dt class="published">Published:</dt><dd class="published">2010-02-22</dd>
  <dt class="category">Categories:</dt><dd class="category tags"><a>F/F</a><a>M/M</a></dd>
  <dt class="words">Words:</dt><dd class="words">1,315</dd>
  <dt class="chapters">Chapters:</dt><dd class="chapters">1/1</dd>
  <dt class="kudos">Kudos:</dt><dd class="kudos">68</dd>
  <dt class="bookmarks">Bookmarks:</dt><dd class="bookmarks"><a>6</a></dd>
  <dt class="hits">Hits:</dt><dd class="hits">1,040</dd>
</dl>
"""


def create_epub(path):
    container = b'''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    package = b'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>Example Work</dc:title>
    <dc:creator>Example Author</dc:creator>
    <dc:description>Summary.</dc:description>
  </metadata>
</package>'''

    with zipfile.ZipFile(path, "w") as epub:
        epub.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        epub.writestr("META-INF/container.xml", container)
        epub.writestr("content.opf", package)


class AO3MetadataTest(unittest.TestCase):
    def test_parse_ao3_statistics(self):
        metadata = parse_ao3_metadata(AO3_PAGE, "https://archiveofourown.org/works/64805")

        self.assertEqual(metadata.work_id, "64805")
        self.assertEqual(metadata.kudos, 68)
        self.assertEqual(metadata.bookmarks, 6)
        self.assertEqual(metadata.hits, 1040)
        self.assertEqual(metadata.words, 1315)
        self.assertEqual(metadata.chapters, "1/1")
        self.assertEqual(metadata.category, "F/F, M/M")

    def test_parse_ao3_category_is_optional(self):
        metadata = parse_ao3_metadata(
            '<dl class="stats"><dd class="words">10</dd></dl>',
            "https://archiveofourown.org/works/64805",
        )

        self.assertIsNone(metadata.category)

    def test_metadata_from_ao3downloader_csv_row(self):
        metadata = metadata_from_csv_row(
            {
                "link": "https://archiveofourown.org/works/64805",
                "title": "Cursed",
                "author": "Medie",
                "words": "1,315",
                "chapters": "1/1",
                "comments": "0",
                "kudos": "68",
                "bookmarks": "6",
                "hits": "1,040",
                "complete": "True",
                "categories": "F/F, M/M",
            },
            "https://archiveofourown.org/works/64805",
        )

        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.kudos, 68)
        self.assertEqual(metadata.hits, 1040)
        self.assertEqual(metadata.status, "Complete")
        self.assertEqual(metadata.category, "F/F, M/M")

    def test_enrich_and_read_epub(self):
        metadata = AO3Metadata(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            published="2010-02-22",
            words=1315,
            chapters="1/1",
            kudos=68,
            bookmarks=6,
            hits=1040,
        )

        with tempfile.TemporaryDirectory() as directory:
            epub_path = f"{directory}/work.epub"
            create_epub(epub_path)
            enrich_epub(epub_path, metadata)

            self.assertTrue(has_ao3_metadata(epub_path))
            actual = read_ao3_metadata(epub_path)
            self.assertEqual(actual.work_url, metadata.work_url)
            self.assertEqual(actual.kudos, 68)
            self.assertEqual(actual.title, "Example Work")
            self.assertEqual(actual.authors, ("Example Author",))

            with zipfile.ZipFile(epub_path) as epub:
                package = epub.read("content.opf").decode("utf-8")
                self.assertIn("ao3:kudos", package)
                self.assertIn("AO3 statistics", package)
                self.assertEqual(epub.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)

    def test_portable_enrichment_preserves_standard_metadata(self):
        metadata = AO3Metadata(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            category="F/F, M/M",
            kudos=68,
            local_words=1234,
            local_gfog=8.456,
        )

        with tempfile.TemporaryDirectory() as directory:
            epub_path = f"{directory}/work.epub"
            create_epub(epub_path)
            enrich_epub_portable(epub_path, metadata)

            actual = read_ao3_metadata(epub_path)
            with zipfile.ZipFile(epub_path) as epub:
                package = epub.read("content.opf").decode("utf-8")

        self.assertEqual(actual.category, "F/F, M/M")
        self.assertEqual(actual.local_words, 1234)
        self.assertEqual(actual.local_gfog, 8.46)
        self.assertEqual(actual.title, "Example Work")
        self.assertEqual(actual.authors, ("Example Author",))
        self.assertIn("Summary.", package)
        self.assertNotIn("AO3 statistics", package)
        self.assertNotIn("scheme=\"ao3\"", package)

    def test_enrichment_replaces_existing_statistics_block(self):
        original = AO3Metadata(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            kudos=68,
        )
        refreshed = AO3Metadata(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            kudos=70,
        )

        with tempfile.TemporaryDirectory() as directory:
            epub_path = f"{directory}/work.epub"
            create_epub(epub_path)
            enrich_epub(epub_path, original)
            enrich_epub(epub_path, refreshed)

            with zipfile.ZipFile(epub_path) as epub:
                package = epub.read("content.opf").decode("utf-8")
                self.assertEqual(package.count("AO3 statistics"), 1)
            self.assertEqual(read_ao3_metadata(epub_path).kudos, 70)

    def test_refresh_removes_missing_counters_and_preserves_permissions(self):
        complete = AO3Metadata(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            kudos=68,
            hits=1040,
        )
        partial = AO3Metadata(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            kudos=70,
        )

        with tempfile.TemporaryDirectory() as directory:
            epub_path = f"{directory}/work.epub"
            create_epub(epub_path)
            os.chmod(epub_path, 0o644)
            enrich_epub(epub_path, complete)
            enrich_epub(epub_path, partial)

            validate_epub_file(epub_path)
            actual = read_ao3_metadata(epub_path)
            self.assertEqual(actual.kudos, 70)
            self.assertIsNone(actual.hits)
            self.assertEqual(stat.S_IMODE(os.stat(epub_path).st_mode), 0o644)


class CalibreUserMetadataTest(unittest.TestCase):
    """Guard the exact contract Calibre's importer requires."""

    COLUMNS = (
        ("ao3_kudos", "AO3 Kudos", "int", "kudos"),
        ("ao3_category", "AO3 Category", "text", "category"),
        ("gfog", "Gfog", "float", "local_gfog"),
        ("ao3_status", "AO3 Status", "text", "status"),
    )

    def metadata(self):
        return AO3Metadata(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            kudos=68,
            category="F/F, M/M",
            local_gfog=8.45,
        )

    def test_only_populated_columns_are_written(self):
        values = _calibre_user_metadata(self.metadata(), self.COLUMNS)

        self.assertEqual(
            sorted(values),
            [
                "calibre:user_metadata:#ao3_category",
                "calibre:user_metadata:#ao3_kudos",
                "calibre:user_metadata:#gfog",
            ],
        )

    def test_each_payload_carries_the_label_datatype_and_typed_value(self):
        values = _calibre_user_metadata(self.metadata(), self.COLUMNS)
        payload = json.loads(values["calibre:user_metadata:#ao3_kudos"])

        self.assertEqual(payload["label"], "ao3_kudos")
        self.assertEqual(payload["datatype"], "int")
        self.assertEqual(payload["name"], "AO3 Kudos")
        self.assertEqual(payload["search_terms"], ["#ao3_kudos"])
        self.assertTrue(payload["is_custom"])
        self.assertEqual(payload["#value#"], 68)

    def test_float_and_text_values_keep_their_json_types(self):
        values = _calibre_user_metadata(self.metadata(), self.COLUMNS)

        self.assertEqual(json.loads(values["calibre:user_metadata:#gfog"])["#value#"], 8.45)
        self.assertEqual(
            json.loads(values["calibre:user_metadata:#ao3_category"])["#value#"], "F/F, M/M"
        )

    def test_calibre_metas_are_written_without_a_namespace_prefix(self):
        """Calibre matches ``name() = "meta"``, so ``<opf:meta>`` is invisible to it."""

        with tempfile.TemporaryDirectory() as directory:
            epub_path = f"{directory}/work.epub"
            create_epub(epub_path)
            enrich_epub(epub_path, self.metadata(), calibre_columns=self.COLUMNS)

            with zipfile.ZipFile(epub_path) as archive:
                package = ET.fromstring(archive.read("content.opf"))

        metadata_element = next(
            child for child in package if child.tag.rsplit("}", 1)[-1] == "metadata"
        )
        calibre_metas = [
            child
            for child in metadata_element
            if child.attrib.get("name", "").startswith("calibre:user_metadata:")
        ]
        self.assertEqual(len(calibre_metas), 3)
        for element in calibre_metas:
            self.assertEqual(element.tag, "meta", "Calibre ignores a namespaced meta element")

    def test_enrichment_without_calibre_columns_writes_none_of_them(self):
        with tempfile.TemporaryDirectory() as directory:
            epub_path = f"{directory}/work.epub"
            create_epub(epub_path)
            enrich_epub(epub_path, self.metadata())

            with zipfile.ZipFile(epub_path) as archive:
                content = archive.read("content.opf").decode("utf-8")

        self.assertNotIn("calibre:user_metadata", content)
        self.assertIn("ao3:kudos", content)

    def test_re_enrichment_replaces_rather_than_duplicates_a_column(self):
        with tempfile.TemporaryDirectory() as directory:
            epub_path = f"{directory}/work.epub"
            create_epub(epub_path)
            enrich_epub(epub_path, self.metadata(), calibre_columns=self.COLUMNS)
            updated = AO3Metadata(**{**self.metadata().__dict__, "kudos": 99})
            enrich_epub(epub_path, updated, calibre_columns=self.COLUMNS)

            with zipfile.ZipFile(epub_path) as archive:
                content = archive.read("content.opf").decode("utf-8")
                package = ET.fromstring(archive.read("content.opf"))

        self.assertEqual(content.count('name="calibre:user_metadata:#ao3_kudos"'), 1)
        metadata_element = next(
            child for child in package if child.tag.rsplit("}", 1)[-1] == "metadata"
        )
        kudos = next(
            child
            for child in metadata_element
            if child.attrib.get("name") == "calibre:user_metadata:#ao3_kudos"
        )
        self.assertEqual(json.loads(kudos.attrib["content"])["#value#"], 99)


if __name__ == "__main__":
    unittest.main()
