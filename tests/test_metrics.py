"""Tests for local EPUB metrics."""

from pathlib import Path
import tempfile
import unittest
import zipfile

from ao3archiver import metrics
from ao3archiver.metrics import (
    calculate_epub_metrics,
    calculate_text_metrics,
    extract_epub_text,
    LOCAL_METRICS_ALGORITHM,
)


def create_metrics_epub(path: Path, documents: dict[str, str]) -> None:
    container = b'''<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>'''
    manifest = "\n".join(
        f'<item id="item{index}" href="{name}" media-type="application/xhtml+xml"/>'
        for index, name in enumerate(documents, start=1)
    )
    spine = "\n".join(
        f'<itemref idref="item{index}"/>' for index in range(1, len(documents) + 1)
    )
    package = f'''<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Metrics</dc:title></metadata>
  <manifest>{manifest}</manifest>
  <spine>{spine}</spine>
</package>'''
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("OPS/content.opf", package)
        for name, content in documents.items():
            archive.writestr(f"OPS/{name}", content)


class LocalMetricsTest(unittest.TestCase):
    def test_formula_and_tokenizer_behavior_are_deterministic(self):
        result = calculate_text_metrics("cat dog. cat dog.")

        self.assertEqual(result.words, 4)
        self.assertEqual(result.gfog, 0.8)
        self.assertEqual(result.error, None)

        rounded = calculate_text_metrics("cat dog. cat. dog.")
        self.assertEqual(rounded.gfog, 0.53)

        tokenized = calculate_text_metrics("state-of-the-art 123 café. one—two")
        self.assertEqual(tokenized.words, 4)
        self.assertIsNotNone(tokenized.gfog)

    def test_sentence_profile_does_not_split_common_abbreviations_or_decimals(self):
        sentences = metrics._sentences("Dr. Smith used 3.14 words. Next sentence!")

        self.assertEqual(sentences, ("Dr. Smith used 3.14 words.", "Next sentence!"))

    def test_spine_body_text_includes_all_ao3_sections_and_entities(self):
        with tempfile.TemporaryDirectory() as directory:
            epub_path = Path(directory) / "work.epub"
            create_metrics_epub(
                epub_path,
                {
                    "title.xhtml": "<html><body><h1>Title &amp; Page.</h1></body></html>",
                    "preface.xhtml": "<html><body><p>Preface words.</p><script>script words</script>"
                    "<style>.story { color: red; }</style></body></html>",
                    "chapter.xhtml": "<html><body><p>Chapter heading. Story words.</p></body></html>",
                    "notes.xhtml": "<html><body><p>Author notes. End notes.</p></body></html>",
                },
            )

            text = extract_epub_text(epub_path)
            metrics = calculate_epub_metrics(epub_path)

        self.assertEqual(LOCAL_METRICS_ALGORITHM, "count-pages-compatible-pure-python-v1")
        self.assertIn("Title & Page.", text)
        self.assertIn("Preface words.", text)
        self.assertIn("script words", text)
        self.assertIn("story", text)
        self.assertIn("Author notes.", text)
        self.assertEqual(metrics.words, 17)
        self.assertIsNotNone(metrics.gfog)

    def test_empty_text_has_zero_words_and_unavailable_gfog(self):
        with tempfile.TemporaryDirectory() as directory:
            epub_path = Path(directory) / "empty.epub"
            create_metrics_epub(epub_path, {"empty.xhtml": "<html><body></body></html>"})

            result = calculate_epub_metrics(epub_path)

        self.assertEqual(result.words, 0)
        self.assertIsNone(result.gfog)
        self.assertEqual(result.error, "no words available for Gunning Fog")

    def test_malformed_html_is_recovered_and_malformed_epub_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            malformed_html = Path(directory) / "malformed-html.epub"
            create_metrics_epub(malformed_html, {"chapter.xhtml": "<html><body><p>broken words"})
            malformed_epub = Path(directory) / "malformed.epub"
            malformed_epub.write_bytes(b"not an epub")

            recovered = calculate_epub_metrics(malformed_html)
            unavailable = calculate_epub_metrics(malformed_epub)

        self.assertEqual(recovered.words, 2)
        self.assertIsNotNone(recovered.gfog)
        self.assertIsNone(unavailable.words)
        self.assertIsNone(unavailable.gfog)
        self.assertEqual(unavailable.error, "BadZipFile")


if __name__ == "__main__":
    unittest.main()
