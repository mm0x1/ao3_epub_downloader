import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from typing import cast
from unittest.mock import patch
import zipfile

import requests

import ao3_backfill
from run_log import RunInterrupted, configure_logging, register_secret


AO3_PAGE = """
<html><title>Archive of Our Own</title>
<h2 class="title heading">Example Work</h2>
<h3 class="byline heading"><a rel="author">Example Author</a></h3>
<dl class="stats">
  <dd class="category tags"><a>F/F</a><a>M/M</a></dd>
  <dd class="words">1,315</dd>
  <dd class="chapters">1/1</dd>
  <dd class="comments">0</dd>
  <dd class="kudos">68</dd>
  <dd class="bookmarks"><a>6</a></dd>
  <dd class="hits">1,040</dd>
</dl></html>
"""


def create_epub(path: Path, preface: str, chapter: str = "<html><body>chapter</body></html>") -> None:
    container = b"""<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>"""
    package = b"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Example</dc:title></metadata>
  <manifest>
    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>
    <item id="preface" href="preface.xhtml" media-type="application/xhtml+xml"/>
    <item id="chapter" href="chapter.xhtml" media-type="application/xhtml+xml"/>
  </manifest>
  <spine><itemref idref="cover"/><itemref idref="preface"/><itemref idref="chapter"/></spine>
</package>"""
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("content.opf", package)
        archive.writestr("cover.xhtml", "<html><body>cover</body></html>")
        archive.writestr("preface.xhtml", preface)
        archive.writestr("chapter.xhtml", chapter)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        text: str,
        headers: dict[str, str] | None = None,
        url: str | None = None,
        json_value: object | None = None,
        history: list["FakeResponse"] | None = None,
    ) -> None:
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}
        self.url = url
        self.json_value = json_value
        self.history = history or []

    def json(self) -> object:
        if isinstance(self.json_value, Exception):
            raise self.json_value
        return self.json_value


class FakeCookie:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeSession:
    def __init__(
        self,
        responses: list[FakeResponse],
        post_responses: list[FakeResponse] | None = None,
    ) -> None:
        self.headers: dict[str, str] = {}
        self.responses = responses
        self.post_responses = post_responses or []
        self.urls: list[str] = []
        self.post_urls: list[str] = []
        self.post_data: list[dict[str, str]] = []
        self.cookies: list[FakeCookie] = []

    def get(
        self,
        url: str,
        timeout: float,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        self.urls.append(url)
        return self.responses.pop(0)

    def post(
        self,
        url: str,
        data: dict[str, str],
        timeout: float,
        allow_redirects: bool = True,
    ) -> FakeResponse:
        self.post_urls.append(url)
        self.post_data.append(data)
        response = self.post_responses.pop(0)
        if response.status_code == 200 and "logout" in response.text.casefold():
            self.cookies.append(FakeCookie("user_session"))
        return response


def sample_report() -> ao3_backfill.ScanReport:
    return ao3_backfill.ScanReport(
        library="/library",
        library_uuid="library-uuid",
        generated_at="2026-01-01T00:00:00+00:00",
        book_count=1,
        epub_count=1,
        existing_ao3_identifier_count=0,
        mappings=(
            ao3_backfill.EpubMapping(
                book_id=7,
                epub_path="Author/Example (7)/Example.epub",
                work_id="64805",
                work_url="https://archiveofourown.org/works/64805",
                preface_entry="preface.xhtml",
            ),
        ),
        missing_work_ids=(),
        malformed_epubs=(),
        unmatched_books=(),
    )


class AO3BackfillTest(unittest.TestCase):
    def test_primary_preface_link_wins_over_related_work_link(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "work.epub"
            create_epub(
                path,
                '<html><body><a href="https://archiveofourown.org/works/123">primary</a>'
                '<a href="https://archiveofourown.org/works/999">related</a></body></html>',
            )

            work_id, work_ids, entry, malformed, reason = ao3_backfill._epub_preface_work_ids(path)

        self.assertEqual(work_id, "123")
        self.assertEqual(work_ids, ("123", "999"))
        self.assertEqual(entry, "preface.xhtml")
        self.assertFalse(malformed)
        self.assertIsNone(reason)

    def test_cache_resume_skips_work_ids_already_cached(self):
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.append(record)
            loaded = cache.records()

        pending = [
            work_id
            for work_id in ao3_backfill._unique_work_ids(sample_report(), include_ambiguous=True)
            if work_id not in loaded
        ]
        self.assertEqual(pending, [])
        self.assertEqual(loaded["64805"].kudos, 0)

    def test_missing_counter_is_not_coerced_to_zero(self):
        html = """
        <html><title>Archive of Our Own</title>
        <h2 class="title heading">Partial</h2>
        <a href="https://archiveofourown.org/works/64805">Partial</a>
        <dd class="kudos">0</dd><dd class="hits">12</dd></html>
        """

        record = ao3_backfill._metadata_from_page(
            html,
            "https://archiveofourown.org/works/64805",
            200,
        )

        self.assertEqual(record.kudos, 0)
        self.assertEqual(record.hits, 12)
        self.assertIsNone(record.comments)
        self.assertEqual(record.availability, "incomplete")
        self.assertEqual(record.value_for("comments"), "")

    def test_preface_structure_allows_a_legitimate_incomplete_work_page(self):
        record = ao3_backfill._metadata_from_page(
            '<html><div id="preface"><a href="https://archiveofourown.org/works/64805">'
            'work</a></div></html>',
            "https://archiveofourown.org/works/64805",
            200,
            "https://archiveofourown.org/works/64805",
        )

        self.assertEqual(record.availability, "incomplete")
        self.assertIsNone(record.kudos)

    def test_same_work_chapter_redirect_can_supply_work_stats(self):
        record = ao3_backfill._metadata_from_page(
            '<html><div id="chapter-150732991"><a href="/works/64805">work</a>'
            '<dl class="stats"><dd class="kudos">0</dd><dd class="hits">12</dd>'
            '<dd class="words">100</dd><dd class="comments">1</dd>'
            '<dd class="bookmarks">2</dd></dl></div></html>',
            "https://archiveofourown.org/works/64805",
            200,
            "https://archiveofourown.org/works/64805/chapters/150732991",
        )

        self.assertEqual(record.kudos, 0)
        self.assertEqual(record.hits, 12)
        self.assertEqual(record.availability, "ok")

    def test_same_work_chapter_template_without_stats_is_cached_incomplete(self):
        record = ao3_backfill._metadata_from_page(
            '<html><div class="works-show"><div class="chapters-show region">'
            '<div class="userstuff">chapter text</div></div></div></html>',
            "https://archiveofourown.org/works/64805",
            200,
            "https://archiveofourown.org/works/64805/chapters/150732991",
        )

        self.assertEqual(record.availability, "incomplete")
        self.assertEqual(record.error, "AO3 page did not expose a statistics block")

    def test_unstructured_chapter_html_is_not_cached_as_incomplete(self):
        with self.assertRaises(ao3_backfill.UnexpectedHTML):
            ao3_backfill._metadata_from_page(
                '<html><div class="chapter"><a href="/works/64805">work</a></div></html>',
                "https://archiveofourown.org/works/64805",
                200,
                "https://archiveofourown.org/works/64805/chapters/150732991",
            )

    def test_retry_after_is_used_for_rate_limit_retry(self):
        session = FakeSession([
            FakeResponse(429, "", {"Retry-After": "7"}),
            FakeResponse(
                200,
                AO3_PAGE + '<a href="https://archiveofourown.org/works/64805">work</a>',
                {"Content-Type": "text/html; charset=utf-8"},
                "https://archiveofourown.org/works/64805",
            ),
        ])
        sleeps: list[float] = []
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=sleeps.append,
            monotonic_fn=lambda: 0,
        )

        record = fetcher.fetch("64805")

        self.assertEqual(sleeps, [7.0])
        self.assertEqual(len(session.urls), 2)
        self.assertEqual(record.kudos, 68)
        self.assertEqual(record.comments, 0)
        self.assertEqual(record.category, "F/F, M/M")

    def test_retry_after_http_date_is_parsed_exactly(self):
        with patch.object(ao3_backfill.time, "time", return_value=100.0):
            self.assertEqual(
                ao3_backfill._parse_retry_after("Thu, 01 Jan 1970 00:01:47 GMT"),
                7.0,
            )

    def test_unexpected_html_stops_without_caching_a_partial_record(self):
        session = FakeSession([
            FakeResponse(
                200,
                '<html><title>Archive of Our Own</title><h1>Temporary error</h1></html>',
                {"Content-Type": "text/html"},
                "https://archiveofourown.org/works/64805",
            ),
        ])
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        with self.assertRaises(ao3_backfill.UnexpectedHTML):
            fetcher.fetch("64805")

    def test_unexpected_html_can_be_retried_once_when_explicitly_enabled(self):
        bad = FakeResponse(
            200,
            '<html><a href="https://archiveofourown.org/works/64805">work</a></html>',
            {"Content-Type": "text/html"},
            "https://archiveofourown.org/works/64805",
        )
        good = FakeResponse(
            200,
            AO3_PAGE + '<a href="https://archiveofourown.org/works/64805">work</a>',
            {"Content-Type": "text/html"},
            "https://archiveofourown.org/works/64805",
        )
        session = FakeSession([bad, good])
        sleeps: list[float] = []
        fetcher = ao3_backfill.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=sleeps.append,
            retry_unexpected_once=True,
        )

        record = fetcher.fetch("64805")

        self.assertEqual(record.availability, "ok")
        self.assertEqual(sleeps, [30.0])

    def test_authentication_page_stops_even_with_a_requested_work_link(self):
        session = FakeSession([
            FakeResponse(
                200,
                '<html><title>Archive of Our Own</title><h2 class="title heading">'
                'Example</h2><a href="https://archiveofourown.org/works/64805">work</a>'
                '<p>You must be logged in to access this page.</p></html>',
                {"Content-Type": "text/html"},
                "https://archiveofourown.org/works/64805",
            ),
        ])
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        with self.assertRaises(ao3_backfill.AuthenticationFailure):
            fetcher.fetch("64805")

    def test_repeated_cloudflare_responses_stop_the_batch(self):
        challenge = '<html><title>Just a moment...</title><div id="cf-wrapper"></div></html>'
        session = FakeSession([
            FakeResponse(503, challenge, {"Content-Type": "text/html"}),
            FakeResponse(503, challenge, {"Content-Type": "text/html"}),
        ])
        sleeps: list[float] = []
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=sleeps.append,
            monotonic_fn=lambda: 0,
        )

        with self.assertRaises(ao3_backfill.RepeatedCloudflare):
            fetcher.fetch("64805")

        self.assertEqual(sleeps, [30.0])

    def test_a_cloudflare_origin_outage_is_retried_as_transient(self):
        """A cf-wrapper 5xx page is an AO3 outage; retrying it is the whole point."""

        outage = '<html><div id="cf-wrapper">Error 522</div></html>'
        record_page = FakeResponse(
            200,
            AO3_PAGE,
            {"Content-Type": "text/html"},
            url="https://archiveofourown.org/works/64805",
        )
        session = FakeSession([
            FakeResponse(522, outage, {"Content-Type": "text/html"}),
            record_page,
        ])
        sleeps: list[float] = []
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=sleeps.append,
            monotonic_fn=lambda: 0,
        )

        record = fetcher.fetch("64805")

        self.assertEqual(record.availability, "ok")
        self.assertEqual(record.kudos, 68)

    def test_the_transient_budget_is_independent_of_max_attempts(self):
        """--retry-failed-once lowers max_attempts; it must not gut outage tolerance."""

        outage = '<html><div id="cf-wrapper">Error 525</div></html>'
        record_page = FakeResponse(
            200,
            AO3_PAGE,
            {"Content-Type": "text/html"},
            url="https://archiveofourown.org/works/64805",
        )
        session = FakeSession(
            [FakeResponse(525, outage, {"Content-Type": "text/html"})] * 4 + [record_page]
        )
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
            monotonic_fn=lambda: 0,
            max_attempts=2,
        )

        record = fetcher.fetch("64805")

        self.assertEqual(record.availability, "ok")
        self.assertEqual(len(session.urls), 5)

    def test_a_rate_limit_is_waited_out_and_the_work_still_succeeds(self):
        """AO3's 429 carries an exact Retry-After; honouring it is the fix."""

        record_page = FakeResponse(
            200,
            AO3_PAGE,
            {"Content-Type": "text/html"},
            url="https://archiveofourown.org/works/64805",
        )
        session = FakeSession([
            FakeResponse(429, "slow down", {"Retry-After": "300"}),
            FakeResponse(429, "slow down", {"Retry-After": "300"}),
            record_page,
        ])
        sleeps: list[float] = []
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=10,
            sleep_fn=sleeps.append,
            monotonic_fn=lambda: 0,
            max_attempts=2,
        )

        record = fetcher.fetch("64805")

        self.assertEqual(record.availability, "ok")
        self.assertEqual(sleeps, [300.0, 300.0])

    def test_the_rate_limit_budget_is_independent_of_max_attempts(self):
        """--retry-failed-once must not reduce rate-limit tolerance to one retry."""

        session = FakeSession([FakeResponse(429, "slow down", {"Retry-After": "1"})] * 3)
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=10,
            sleep_fn=lambda _: None,
            monotonic_fn=lambda: 0,
            max_attempts=2,
            max_rate_limit_attempts=3,
        )

        with self.assertRaises(ao3_backfill.RepeatedRateLimit) as raised:
            fetcher.fetch("64805")

        self.assertEqual(len(session.urls), 3)
        self.assertIn("--delay", str(raised.exception))

    def test_transient_backoff_is_capped(self):
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, FakeSession([])),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(fetcher._transient_backoff(1), 30.0)
        self.assertEqual(fetcher._transient_backoff(2), 60.0)
        self.assertEqual(
            fetcher._transient_backoff(20), ao3_backfill.TRANSIENT_MAX_BACKOFF_SECONDS
        )

    def test_an_origin_outage_does_not_count_toward_the_cloudflare_stop(self):
        outage = '<html><div id="cf-wrapper">Error 525</div></html>'
        session = FakeSession([
            FakeResponse(525, outage, {"Content-Type": "text/html"}),
            FakeResponse(525, outage, {"Content-Type": "text/html"}),
        ])
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
            monotonic_fn=lambda: 0,
            max_attempts=2,
            max_transient_attempts=2,
        )

        # It still stops, but as a transient failure rather than a bot block.
        with self.assertRaises(ao3_backfill.NetworkStopError) as raised:
            fetcher.fetch("64805")
        self.assertNotIsInstance(raised.exception, ao3_backfill.RepeatedCloudflare)

    # Trimmed from a live AO3 response for an unrevealed challenge work.
    MYSTERY_WORK_PAGE = """<html><head><title>Mystery Work | Archive of Our Own</title></head>
<body class="logged-in"><div id="inner" class="wrapper">
<div id="main" class="works-show region" role="main">
  <div class="flash"></div>
  <p class="notice">
  This work is part of an ongoing challenge and will be revealed soon!
      You can find details here:
      <a href="/collections/Fic_Prison">Pending fic deletion because eh</a>
</p>
<!-- BEGIN revealed -->
<!-- END revealed -->
</div></div></body></html>"""

    def test_an_unrevealed_mystery_work_is_cached_as_unavailable(self):
        url = "https://archiveofourown.org/works/62373328"

        record = ao3_backfill._metadata_from_page(self.MYSTERY_WORK_PAGE, url, 200, url)

        self.assertEqual(record.availability, "unavailable")
        self.assertEqual(record.http_status, 200)
        self.assertIn("unrevealed", record.error)
        # The slug keeps its case; it is read from the original markup.
        self.assertIn("/collections/Fic_Prison", record.error)

    def test_a_mystery_work_does_not_stop_the_fetch(self):
        session = FakeSession([
            FakeResponse(
                200,
                self.MYSTERY_WORK_PAGE,
                {"Content-Type": "text/html"},
                url="https://archiveofourown.org/works/62373328",
            )
        ])
        fetcher = ao3_backfill.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=10,
            sleep_fn=lambda _: None,
            monotonic_fn=lambda: 0,
            retry_unexpected_once=True,
        )

        record = fetcher.fetch("62373328")

        self.assertEqual(record.availability, "unavailable")
        self.assertEqual(len(session.urls), 1, "a recognised state needs no retry")

    def test_the_mystery_notice_for_a_different_work_is_not_trusted(self):
        with self.assertRaises(ao3_backfill.UnexpectedHTML):
            ao3_backfill._metadata_from_page(
                self.MYSTERY_WORK_PAGE,
                "https://archiveofourown.org/works/62373328",
                200,
                "https://archiveofourown.org/works/11111111",
            )

    def test_an_unknown_page_reports_its_title_in_the_stop_message(self):
        """The last stop said only 'not a recognized AO3 work page'."""

        url = "https://archiveofourown.org/works/64805"
        page = "<html><head><title>Something New | Archive of Our Own</title></head><body></body></html>"

        with self.assertRaises(ao3_backfill.UnexpectedHTML) as raised:
            ao3_backfill._metadata_from_page(page, url, 200, url)

        self.assertIn("Something New | Archive of Our Own", str(raised.exception))

    def test_wrong_work_response_is_rejected(self):
        with self.assertRaises(ao3_backfill.UnexpectedHTML):
            ao3_backfill._metadata_from_page(
                '<html><title>Archive of Our Own</title><dl class="stats">'
                '<dd class="kudos">4</dd></dl>'
                '<a href="https://archiveofourown.org/works/999">wrong</a></html>',
                "https://archiveofourown.org/works/64805",
                200,
                "https://archiveofourown.org/works/999",
            )

    def test_chapter_link_is_not_used_as_the_preface_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chapter-only.epub"
            create_epub(
                path,
                "<html><body>preface without an AO3 link</body></html>",
                '<html><body><a href="https://archiveofourown.org/works/999">chapter</a></body></html>',
            )

            work_id, _, entry, _, reason = ao3_backfill._epub_preface_work_ids(path)

        self.assertIsNone(work_id)
        self.assertEqual(entry, "preface.xhtml")
        self.assertEqual(reason, "no AO3 work URL in EPUB preface")

    def test_non_finite_delay_is_rejected(self):
        with self.assertRaises(ValueError):
            ao3_backfill.AO3Fetcher(delay_seconds=float("nan"))

    def test_multiline_calibre_details_are_parsed_and_datatype_is_verified(self):
        details = "\n\n".join(
            f"{label}\n\n{{'datatype': '{datatype}', 'name': '{name}', 'label': '{label}'}}"
            for label, name, datatype, _ in ao3_backfill.CUSTOM_COLUMNS
        )
        parsed = ao3_backfill.parse_custom_column_details(details)

        self.assertEqual(parsed["ao3_kudos"]["datatype"], "int")
        self.assertEqual(parsed["ao3_status"]["name"], "AO3 Status")

        sqlite_columns = {
            label: {"id": index, "label": label, "name": name, "datatype": datatype}
            for index, (label, name, datatype, _) in enumerate(ao3_backfill.CUSTOM_COLUMNS, start=4)
        }
        with patch.object(ao3_backfill, "run_calibredb", return_value=details), patch.object(
            ao3_backfill, "read_custom_columns_sqlite", return_value=sqlite_columns
        ):
            verified = ao3_backfill.verify_custom_columns("calibredb", Path("/library"))

        self.assertEqual(verified["ao3_kudos"]["sqlite_id"], 4)
        self.assertEqual(verified["ao3_category"]["datatype"], "text")

    def test_dry_run_summary_reports_counts_and_sample_mapping(self):
        summary = ao3_backfill.render_scan_summary(sample_report())

        self.assertIn("Calibre books: 1", summary)
        self.assertIn("Primary AO3 mappings: 1", summary)
        self.assertIn("book=7 work=64805", summary)

    def test_cache_record_round_trip_is_json_serializable(self):
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            authors=("Example Author",),
            category="F/F, M/M",
            kudos=0,
        )

        decoded = ao3_backfill.AO3FetchRecord.from_dict(json.loads(json.dumps(record.to_dict())))

        self.assertEqual(decoded.authors, ("Example Author",))
        self.assertEqual(decoded.category, "F/F, M/M")
        self.assertEqual(decoded.kudos, 0)

    def test_cache_record_rejects_tampered_identity(self):
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
        ).to_dict()
        record["work_url"] = "https://archiveofourown.org/works/999"

        with self.assertRaises(ValueError):
            ao3_backfill.AO3FetchRecord.from_dict(record)

    def test_incomplete_final_cache_line_is_recovered(self):
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=68,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            path.write_text(json.dumps(record.to_dict()) + "\n{" , encoding="utf-8")
            cache = ao3_backfill.CacheStore(path)

            loaded = cache.records()
            recovered_contents = path.read_text(encoding="utf-8")

        self.assertEqual(loaded["64805"].kudos, 68)
        self.assertEqual(recovered_contents, json.dumps(record.to_dict()) + "\n")

    def test_non_empty_cache_without_context_is_not_adopted(self):
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=68,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            path.write_text(json.dumps(record.to_dict()) + "\n", encoding="utf-8")
            cache = ao3_backfill.CacheStore(path)

            with self.assertRaises(ao3_backfill.BackfillError):
                cache.bind(sample_report())

    def test_cache_rejects_boolean_counter_values(self):
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
        ).to_dict()
        record["kudos"] = False

        with self.assertRaises(ValueError):
            ao3_backfill.AO3FetchRecord.from_dict(record)

    def test_cache_rejects_non_finite_shared_request_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            cache.context_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "library": "/library",
                        "library_uuid": "library-uuid",
                        "last_request_at": float("nan"),
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ao3_backfill.BackfillError):
                with cache.operation_lock():
                    cache.last_request_at_unlocked()

    def test_environment_credentials_are_loaded_as_a_pair_without_output(self):
        username = "env-user"
        password = "env-password"
        output = io.StringIO()

        with redirect_stdout(output):
            actual = ao3_backfill.load_environment_credentials(
                {"AO3_USERNAME": username, "AO3_PASSWORD": password},
                dotenv_path=Path("/definitely-missing-ao3-backfill.env"),
            )

        self.assertEqual(actual, (username, password))
        self.assertNotIn(username, output.getvalue())
        self.assertNotIn(password, output.getvalue())

    def test_environment_credentials_reject_missing_or_partial_values_without_leaking(self):
        for environment in ({}, {"AO3_USERNAME": "env-user"}, {"AO3_PASSWORD": "env-password"}):
            with self.assertRaises(ao3_backfill.AuthenticationFailure) as raised:
                ao3_backfill.load_environment_credentials(
                    environment,
                    dotenv_path=Path("/definitely-missing-ao3-backfill.env"),
                )

            self.assertNotIn("env-user", str(raised.exception))
            self.assertNotIn("env-password", str(raised.exception))

    def test_dotenv_credentials_are_loaded_without_secret_output(self):
        with tempfile.TemporaryDirectory() as directory:
            dotenv_path = Path(directory) / ".env"
            dotenv_path.write_text(
                "AO3_USERNAME=env-user\nAO3_PASSWORD='env-password'\n",
                encoding="utf-8",
            )

            actual = ao3_backfill.load_environment_credentials(
                {},
                dotenv_path=dotenv_path,
            )

        self.assertEqual(actual, ("env-user", "env-password"))

    def test_login_uses_token_flow_and_does_not_print_credentials(self):
        token = FakeResponse(200, "", json_value={"token": "rotating-token"})
        logged_in = FakeResponse(
            200,
            '<html><body class="logged-in"><a href="/users/logout">Log out</a></body></html>',
        )
        session = FakeSession([token], [logged_in])
        output = io.StringIO()

        with redirect_stdout(output):
            ao3_backfill.login_authenticated_session(
                cast(requests.Session, session),
                "env-user",
                "env-password",
                delay_seconds=30,
                sleep_fn=lambda _: None,
            )

        self.assertEqual(session.urls, ["https://archiveofourown.org/token_dispenser.json"])
        self.assertEqual(session.post_urls, ["https://archiveofourown.org/users/login"])
        self.assertEqual(session.post_data[0]["user[login]"], "env-user")
        self.assertEqual(session.post_data[0]["user[password]"], "env-password")
        self.assertNotIn("env-user", output.getvalue())
        self.assertNotIn("env-password", output.getvalue())

    def test_login_token_failure_and_rejection_are_bounded(self):
        invalid_token_session = FakeSession([FakeResponse(200, "", json_value={})])
        with self.assertRaises(ao3_backfill.AuthenticationFailure):
            ao3_backfill.login_authenticated_session(
                cast(requests.Session, invalid_token_session),
                "env-user",
                "env-password",
                sleep_fn=lambda _: None,
            )
        self.assertEqual(invalid_token_session.post_urls, [])

        rejected_session = FakeSession(
            [FakeResponse(200, "", json_value={"token": "token"})],
            [FakeResponse(200, "Please log in; invalid username or password")],
        )
        with self.assertRaises(ao3_backfill.AuthenticationFailure):
            ao3_backfill.login_authenticated_session(
                cast(requests.Session, rejected_session),
                "env-user",
                "env-password",
                sleep_fn=lambda _: None,
            )

    def test_failed_login_persists_retry_after_from_a_403_challenge(self):
        token = FakeResponse(200, "", json_value={"token": "token"})
        challenge = FakeResponse(403, "challenge", {"Retry-After": "11"})
        session = FakeSession([token], [challenge])
        deferred: list[tuple[float, bool]] = []

        with self.assertRaises(ao3_backfill.AuthenticationFailure):
            ao3_backfill.login_authenticated_session(
                cast(requests.Session, session),
                "env-user",
                "env-password",
                sleep_fn=lambda _: None,
                request_deferred_callback=lambda seconds, exact: deferred.append((seconds, exact)),
            )

        self.assertEqual(deferred, [(11.0, True)])

    def test_login_post_redirect_persists_retry_after_before_following(self):
        token = FakeResponse(200, "", json_value={"token": "token"})
        redirect = FakeResponse(
            302,
            "",
            {
                "Location": "https://archiveofourown.org/works/64805",
                "Retry-After": "11",
            },
        )
        logged_in = FakeResponse(
            200,
            '<body class="logged-in"><a href="/users/logout">Log out</a></body>',
            url="https://archiveofourown.org/works/64805",
        )
        session = FakeSession([token, logged_in], [redirect])
        deferred: list[tuple[float, bool]] = []
        sleeps: list[float] = []

        ao3_backfill.login_authenticated_session(
            cast(requests.Session, session),
            "env-user",
            "env-password",
            sleep_fn=sleeps.append,
            request_deferred_callback=lambda seconds, exact: deferred.append((seconds, exact)),
        )

        self.assertEqual(deferred, [(11.0, True)])
        self.assertEqual(sleeps, [30.0, 11.0, 30.0])

    def follow(self, responses, url="https://archiveofourown.org/works/64805"):
        session = FakeSession(responses)
        paced: list[str] = []
        sleeps: list[float] = []
        response = ao3_backfill._get_with_ao3_redirects(
            cast(requests.Session, session),
            url,
            timeout_seconds=60,
            before_request=lambda: paced.append(session.urls[-1] if session.urls else url),
            request_started_callback=None,
            sleep_fn=sleeps.append,
        )
        return response, session, paced, sleeps

    def test_the_first_chapter_redirect_is_followed_without_a_second_wait(self):
        response, session, paced, _ = self.follow([
            FakeResponse(302, "", {"Location": "/works/64805/chapters/111"}),
            FakeResponse(200, "chapter", url="https://archiveofourown.org/works/64805/chapters/111"),
        ])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(session.urls), 2)
        self.assertEqual(len(paced), 1, "the 0.1s redirect must not cost a full delay")

    def test_a_redirect_to_another_works_chapter_is_still_paced(self):
        _, session, paced, _ = self.follow([
            FakeResponse(302, "", {"Location": "/works/99999/chapters/111"}),
            FakeResponse(200, "other"),
        ])

        self.assertEqual(len(session.urls), 2)
        self.assertEqual(len(paced), 2)

    def test_a_non_chapter_redirect_is_still_paced(self):
        _, session, paced, _ = self.follow([
            FakeResponse(302, "", {"Location": "/users/login"}),
            FakeResponse(200, "login"),
        ])

        self.assertEqual(len(paced), 2)

    def test_a_retry_after_on_the_chapter_redirect_is_still_served(self):
        _, _, paced, sleeps = self.follow([
            FakeResponse(
                302, "", {"Location": "/works/64805/chapters/111", "Retry-After": "7"}
            ),
            FakeResponse(200, "chapter"),
        ])

        self.assertEqual(sleeps, [7.0])
        self.assertEqual(len(paced), 1)

    def test_only_a_chapter_hop_skips_pacing_not_the_hop_after_it(self):
        _, session, paced, _ = self.follow([
            FakeResponse(302, "", {"Location": "/works/64805/chapters/111"}),
            FakeResponse(302, "", {"Location": "/works/64805/chapters/222"}),
            FakeResponse(200, "chapter"),
        ])

        self.assertEqual(len(session.urls), 3)
        self.assertEqual(len(paced), 2)

    def test_ao3_redirect_allowlist_requires_https_default_port_and_no_userinfo(self):
        self.assertTrue(ao3_backfill._is_ao3_url("https://archiveofourown.org/works/1"))
        self.assertFalse(ao3_backfill._is_ao3_url("http://archiveofourown.org/works/1"))
        self.assertFalse(ao3_backfill._is_ao3_url("https://archiveofourown.org:8443/works/1"))
        self.assertFalse(ao3_backfill._is_ao3_url("https://user:pass@archiveofourown.org/works/1"))

    def test_successful_login_with_login_history_is_not_rejected(self):
        token = FakeResponse(200, "", json_value={"token": "token"})
        prior_login_redirect = FakeResponse(302, "", url="https://archiveofourown.org/users/login")
        logged_in = FakeResponse(
            200,
            '<body class="logged-in"><a href="/users/logout">Log out</a></body>',
            url="https://archiveofourown.org/works/64805",
            history=[prior_login_redirect],
        )
        session = FakeSession([token], [logged_in])

        ao3_backfill.login_authenticated_session(
            cast(requests.Session, session),
            "env-user",
            "env-password",
            sleep_fn=lambda _: None,
        )

    def test_cross_origin_work_redirect_is_rejected_before_following(self):
        session = FakeSession(
            [
                FakeResponse(
                    302,
                    "",
                    {"Location": "https://example.invalid/redirect"},
                )
            ]
        )
        fetcher = ao3_backfill.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        with self.assertRaises(ao3_backfill.NetworkStopError):
            fetcher.fetch("64805")

        self.assertEqual(len(session.urls), 1)

    def test_login_redirect_or_401_reauthenticates_once(self):
        good = FakeResponse(
            200,
            AO3_PAGE + '<a href="https://archiveofourown.org/works/64805">work</a>',
            {"Content-Type": "text/html"},
            "https://archiveofourown.org/works/64805",
        )
        response_sequences = (
            [
                FakeResponse(
                    302,
                    "",
                    {"Location": "https://archiveofourown.org/users/login"},
                ),
                FakeResponse(
                    200,
                    "Please log in",
                    url="https://archiveofourown.org/users/login",
                ),
                good,
            ],
            [FakeResponse(401, ""), good],
        )
        for responses in response_sequences:
            session = FakeSession(responses)
            reauthentications: list[str] = []
            fetcher = ao3_backfill.AO3Fetcher(
                cast(requests.Session, session),
                delay_seconds=30,
                sleep_fn=lambda _: None,
                reauthenticate=lambda: reauthentications.append("once"),
            )

            record = fetcher.fetch("64805")

            self.assertEqual(record.kudos, 68)
            self.assertEqual(reauthentications, ["once"])

    def test_reauthentication_is_bounded_and_failed_auth_stops(self):
        session = FakeSession([FakeResponse(403, ""), FakeResponse(403, "")])
        reauthentications: list[str] = []
        fetcher = ao3_backfill.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
            reauthenticate=lambda: reauthentications.append("once"),
        )

        with self.assertRaises(ao3_backfill.AuthenticationFailure):
            fetcher.fetch("64805")

        self.assertEqual(reauthentications, ["once"])
        self.assertEqual(len(session.urls), 2)

    def test_work_authentication_challenge_persists_retry_after_before_reauth(self):
        good = FakeResponse(
            200,
            AO3_PAGE + '<a href="https://archiveofourown.org/works/64805">work</a>',
            {"Content-Type": "text/html"},
            "https://archiveofourown.org/works/64805",
        )
        session = FakeSession([FakeResponse(403, "", {"Retry-After": "11"}), good])
        sleeps: list[float] = []
        reauthentications: list[str] = []
        fetcher = ao3_backfill.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=sleeps.append,
            reauthenticate=lambda: reauthentications.append("once"),
        )

        record = fetcher.fetch("64805")

        self.assertEqual(record.work_id, "64805")
        self.assertEqual(sleeps, [11.0])
        self.assertEqual(reauthentications, ["once"])

    def test_work_redirect_retry_after_is_applied_before_following_login(self):
        good = FakeResponse(
            200,
            AO3_PAGE + '<a href="https://archiveofourown.org/works/64805">work</a>',
            {"Content-Type": "text/html"},
            "https://archiveofourown.org/works/64805",
        )
        session = FakeSession(
            [
                FakeResponse(
                    302,
                    "",
                    {
                        "Location": "https://archiveofourown.org/users/login",
                        "Retry-After": "11",
                    },
                ),
                FakeResponse(
                    200,
                    "Please log in",
                    url="https://archiveofourown.org/users/login",
                ),
                good,
            ]
        )
        sleeps: list[float] = []
        reauthentications: list[str] = []
        fetcher = ao3_backfill.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=sleeps.append,
            reauthenticate=lambda: reauthentications.append("once"),
        )

        fetcher.fetch("64805")

        self.assertEqual(sleeps[0], 11.0)
        self.assertEqual(len(sleeps), 2)
        self.assertEqual(reauthentications, ["once"])

    def test_same_origin_non_work_redirect_is_not_cached(self):
        session = FakeSession(
            [
                FakeResponse(
                    302,
                    "",
                    {"Location": "https://archiveofourown.org/search"},
                ),
                FakeResponse(
                    200,
                    AO3_PAGE + '<a href="https://archiveofourown.org/works/64805">work</a>',
                    {"Content-Type": "text/html"},
                    "https://archiveofourown.org/search",
                ),
            ]
        )
        fetcher = ao3_backfill.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        with self.assertRaises(ao3_backfill.UnexpectedHTML):
            fetcher.fetch("64805")

    def test_cloudflare_403_does_not_trigger_reauthentication(self):
        challenge = '<html><title>Just a moment...</title><div id="cf-wrapper"></div></html>'
        session = FakeSession([FakeResponse(403, challenge, {"Content-Type": "text/html"})] * 2)
        reauthentications: list[str] = []
        fetcher = ao3_backfill.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
            reauthenticate=lambda: reauthentications.append("unexpected"),
        )

        with self.assertRaises(ao3_backfill.RepeatedCloudflare):
            fetcher.fetch("64805")

        self.assertEqual(reauthentications, [])

    def test_shared_scheduler_persists_delay_and_server_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            now = [100.0]
            sleeps: list[float] = []

            def sleep(seconds: float) -> None:
                sleeps.append(seconds)
                now[0] += seconds

            with cache.operation_lock():
                scheduler = ao3_backfill.CacheRequestScheduler(
                    cache,
                    30,
                    sleep_fn=sleep,
                    wall_time_fn=lambda: now[0],
                )
                scheduler.before_request()
                scheduler.request_started(now[0])
                now[0] = 101.0
                scheduler.defer_requests(40)
                scheduler.before_request()

            self.assertEqual(sleeps, [40.0])
            context = json.loads(cache.context_path.read_text(encoding="utf-8"))
            self.assertEqual(context["last_request_at"], 100.0)
            self.assertEqual(context["next_request_at"], 141.0)

    def test_exact_retry_after_overrides_default_delay_and_is_persisted(self):
        good = FakeResponse(
            200,
            AO3_PAGE + '<a href="https://archiveofourown.org/works/64805">work</a>',
            {"Content-Type": "text/html"},
            "https://archiveofourown.org/works/64805",
        )
        session = FakeSession([FakeResponse(429, "", {"Retry-After": "7"}), good])
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            now = [100.0]
            sleeps: list[float] = []
            deferred: list[tuple[float, bool, float | None]] = []

            def sleep(seconds: float) -> None:
                sleeps.append(seconds)
                now[0] += seconds

            with cache.operation_lock():
                scheduler = ao3_backfill.CacheRequestScheduler(
                    cache,
                    30,
                    sleep_fn=sleep,
                    wall_time_fn=lambda: now[0],
                )

                def defer(seconds: float, exact: bool) -> None:
                    scheduler.defer_requests(seconds, exact)
                    deferred.append((seconds, exact, cache.next_request_at_unlocked()))

                fetcher = ao3_backfill.AO3Fetcher(
                    cast(requests.Session, session),
                    delay_seconds=30,
                    sleep_fn=sleep,
                    wall_time_fn=lambda: now[0],
                    before_request=scheduler.before_request,
                    request_started_callback=scheduler.request_started,
                    request_deferred_callback=defer,
                )
                fetcher.fetch("64805")

        self.assertEqual(sleeps, [7.0])
        self.assertEqual(deferred, [(7.0, True, 107.0)])

    def test_refresh_appends_new_record_without_deleting_old_cache_record(self):
        old_record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=1,
        )
        refreshed = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-02T00:00:00+00:00",
            availability="ok",
            kudos=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            cache.append(old_record)
            snapshot = Path(directory) / "before-refresh.jsonl"
            cache.snapshot(snapshot)

            class Fetcher:
                def fetch(self, _work_id: str) -> ao3_backfill.AO3FetchRecord:
                    return refreshed

            with patch.object(ao3_backfill, "verify_report_library"), patch.object(
                ao3_backfill, "verify_report_inputs"
            ), patch.object(ao3_backfill, "verify_custom_columns"):
                result = ao3_backfill.fetch_pending(
                    sample_report(),
                    cache,
                    calibredb="calibredb",
                    library=Path("/library"),
                    limit=1,
                    refresh=True,
                    fetcher=cast(ao3_backfill.AO3Fetcher, Fetcher()),
                )

            records = cache.records()
            snapshot_records = [json.loads(line) for line in snapshot.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(result), 1)
        self.assertEqual(records["64805"].kudos, 2)
        self.assertEqual(len(snapshot_records), 1)
        self.assertEqual(snapshot_records[0]["kudos"], 1)

    def test_no_pending_work_does_not_load_environment_credentials(self):
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            cache.append(record)
            with patch.object(ao3_backfill, "verify_report_library"), patch.object(
                ao3_backfill, "verify_report_inputs"
            ), patch.object(ao3_backfill, "verify_custom_columns"), patch.object(
                ao3_backfill, "load_ao3_credential_pair"
            ) as loader:
                result = ao3_backfill.fetch_pending(
                    sample_report(),
                    cache,
                    calibredb="calibredb",
                    library=Path("/library"),
                    limit=1,
                    use_env_credentials=True,
                )

        self.assertEqual(result, ())
        loader.assert_not_called()

    def test_validation_output_contains_category_and_local_metrics(self):
        report = sample_report()
        mapping = report.mappings[0]
        report = ao3_backfill.ScanReport(
            **{
                **report.__dict__,
                "mappings": (
                    ao3_backfill.EpubMapping(
                        **{
                            **mapping.__dict__,
                            "local_words": 321,
                            "local_gfog": 8.5,
                            "local_metrics_calculated": True,
                        }
                    ),
                ),
            }
        )
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            title="Example Work",
            category="F/F, M/M",
            kudos=68,
            hits=1040,
            bookmarks=6,
            comments=0,
            words=1315,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            cache.append(record)
            validation = ao3_backfill.validate_cache(report, cache)
            output = io.StringIO()
            with redirect_stdout(output):
                ao3_backfill.print_cache_validation(validation, sample_size=1)

        rendered = output.getvalue()
        self.assertIn("category='F/F, M/M'", rendered)
        self.assertIn("ao3_words=1315", rendered)
        self.assertIn("local_words=321", rendered)
        self.assertIn("gfog=8.5", rendered)

    def test_calibre_write_preserves_unavailable_values_and_writes_zero_as_zero(self):
        report = ao3_backfill.ScanReport(
            **{
                **sample_report().__dict__,
                "mappings": (
                    ao3_backfill.EpubMapping(
                        **{
                            **sample_report().mappings[0].__dict__,
                            "local_words": 100,
                            "local_gfog": 5.5,
                            "local_metrics_calculated": True,
                        }
                    ),
                ),
            }
        )
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=0,
            hits=12,
            words=0,
            comments=None,
            status="",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            cache.append(record)
            calls: list[tuple[str, ...]] = []

            def fake_calibredb(_executable: str, _library: Path, *arguments: str) -> str:
                calls.append(arguments)
                return ""

            with patch.object(ao3_backfill, "require_calibre_closed"), patch.object(
                ao3_backfill, "verify_backup"
            ), patch.object(ao3_backfill, "verify_custom_columns"), patch.object(
                ao3_backfill, "verify_local_metric_columns"
            ), patch.object(ao3_backfill, "verify_report_inputs"), patch.object(
                ao3_backfill, "load_local_metric_values", return_value={7: {"words": None, "gfog": 8.0}}
            ), patch.object(ao3_backfill, "run_calibredb", side_effect=fake_calibredb):
                result = ao3_backfill.write_calibre_values(
                    report,
                    cache,
                    calibredb="calibredb",
                    library=Path("/library"),
                    backup_path=Path("/backup"),
                    write_local_metrics=True,
                )

        self.assertEqual(result["updated"], [7])
        self.assertIn(("set_custom", "ao3_kudos", "7", "0"), calls)
        self.assertIn(("set_custom", "ao3_hits", "7", "12"), calls)
        self.assertIn(("set_custom", "ao3_words", "7", "0"), calls)
        self.assertIn(("set_custom", "words", "7", "100"), calls)
        self.assertNotIn(("set_custom", "ao3_comments", "7", ""), calls)
        self.assertNotIn(("set_custom", "ao3_status", "7", ""), calls)
        self.assertNotIn(("set_custom", "gfog", "7", "5.5"), calls)

    def test_calibre_write_skips_incomplete_records(self):
        report = sample_report()
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="incomplete",
            kudos=99,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            cache.append(record)
            with patch.object(ao3_backfill, "require_calibre_closed"), patch.object(
                ao3_backfill, "verify_backup"
            ), patch.object(ao3_backfill, "verify_custom_columns"), patch.object(
                ao3_backfill, "verify_report_inputs"
            ), patch.object(ao3_backfill, "run_calibredb") as run_calibredb:
                result = ao3_backfill.write_calibre_values(
                    report,
                    cache,
                    calibredb="calibredb",
                    library=Path("/library"),
                    backup_path=Path("/backup"),
                )

        self.assertEqual(result["updated"], [])
        self.assertEqual(result["skipped_unavailable"], [7])
        run_calibredb.assert_not_called()


class LoginDiagnosticsTest(unittest.TestCase):
    """Every login failure must say which step failed and why."""

    def login(self, session, max_transient_attempts: int = 1):
        return ao3_backfill.login_authenticated_session(
            cast(requests.Session, session),
            "env-user",
            "env-password",
            source="/repo/.env",
            sleep_fn=lambda _: None,
            max_transient_attempts=max_transient_attempts,
        )

    def assert_login_fails_with(
        self, session, *expected: str, max_transient_attempts: int = 1
    ) -> str:
        with self.assertRaises(ao3_backfill.AuthenticationFailure) as raised:
            self.login(session, max_transient_attempts=max_transient_attempts)
        message = str(raised.exception)
        for fragment in expected:
            self.assertIn(fragment, message)
        self.assertNotIn("env-password", message)
        return message

    def token(self):
        return FakeResponse(200, "", json_value={"token": "token"})

    def test_a_token_transport_failure_names_the_token_step(self):
        class Session(FakeSession):
            def get(self, *args, **kwargs):
                raise requests.ConnectionError("no route to host")

        self.assert_login_fails_with(Session([]), "token dispenser", "ConnectionError")

    def test_a_token_http_error_reports_its_status(self):
        session = FakeSession([FakeResponse(503, "unavailable")])

        self.assert_login_fails_with(session, "token dispenser", "HTTP 503")

    def test_a_missing_token_value_is_distinct_from_an_http_error(self):
        session = FakeSession([FakeResponse(200, "", json_value={})])

        self.assert_login_fails_with(session, "without a usable token")

    def test_a_post_transport_failure_names_the_login_step(self):
        class Session(FakeSession):
            def post(self, *args, **kwargs):
                raise requests.Timeout("timed out")

        self.assert_login_fails_with(
            Session([self.token()]), "login POST", "Timeout"
        )

    def test_rejected_credentials_name_the_credential_source(self):
        session = FakeSession(
            [self.token()],
            [FakeResponse(200, "Please log in; that doesn't match our records")],
        )

        message = self.assert_login_fails_with(session, "rejected the credentials", "/repo/.env")
        rejected_again = FakeSession(
            [self.token()],
            [FakeResponse(200, "Please log in; that doesn't match our records")],
        )
        with self.assertRaises(ao3_backfill.CredentialsRejected):
            self.login(rejected_again)
        self.assertTrue(message)

    def test_a_cloudflare_challenge_is_not_reported_as_a_bad_password(self):
        challenge = FakeResponse(
            503,
            "<title>Just a moment...</title>",
            {"Content-Type": "text/html"},
        )
        session = FakeSession([self.token()], [challenge])

        message = self.assert_login_fails_with(session, "Cloudflare", "HTTP 503")
        self.assertNotIn("rejected the credentials", message)

    def test_a_cloudflare_origin_outage_is_not_reported_as_a_challenge(self):
        """HTTP 525 means AO3's origin is down, not that we were bot-blocked."""

        outage = FakeResponse(
            525,
            '<html><div id="cf-wrapper">Error 525</div></html>',
            {"Content-Type": "text/html"},
        )
        session = FakeSession([self.token()], [outage])

        message = self.assert_login_fails_with(
            session, "HTTP 525", "server-side failure", "TLS handshake", "the login form"
        )
        self.assertNotIn("challenged", message)

    def test_an_origin_outage_at_the_token_step_names_that_step(self):
        outage = FakeResponse(
            521,
            '<html><div id="cf-wrapper">Error 521</div></html>',
            {"Content-Type": "text/html"},
        )

        self.assert_login_fails_with(FakeSession([outage]), "token dispenser", "HTTP 521")

    def test_a_transient_origin_error_is_retried_and_can_then_succeed(self):
        """AO3 emits passing 5xx blips; the login should ride them out."""

        logged_in = FakeResponse(
            200,
            '<body class="logged-in"><a href="/users/logout">Log out</a></body>',
        )
        outage = FakeResponse(
            525,
            '<html><div id="cf-wrapper">Error 525</div></html>',
            {"Content-Type": "text/html"},
        )
        session = FakeSession([self.token()], [outage, outage, logged_in])

        self.login(session, max_transient_attempts=4)

        self.assertEqual(len(session.post_urls), 3)

    def test_the_transient_budget_is_finite(self):
        outage = FakeResponse(
            522,
            '<html><div id="cf-wrapper">Error 522</div></html>',
            {"Content-Type": "text/html"},
        )
        session = FakeSession([self.token()], [outage] * 3)

        message = self.assert_login_fails_with(
            session, "HTTP 522", "after 3 attempts", max_transient_attempts=3
        )
        self.assertNotIn("challenged", message)

    def test_a_cloudflare_challenge_is_never_retried(self):
        """Retrying a challenge is how an address gets blocked outright."""

        challenge = FakeResponse(
            503,
            "<title>Just a moment...</title>",
            {"Content-Type": "text/html"},
        )
        session = FakeSession([self.token()], [challenge, challenge, challenge])

        with self.assertRaises(ao3_backfill.AuthenticationFailure):
            self.login(session, max_transient_attempts=8)

        self.assertEqual(len(session.post_urls), 1)

    def test_being_returned_to_the_login_form_is_its_own_message(self):
        returned = FakeResponse(200, "<html>form</html>", url="https://archiveofourown.org/users/login")
        session = FakeSession([self.token()], [returned])

        self.assert_login_fails_with(session, "returned the login form again")

    def test_a_200_with_no_session_evidence_reports_the_destination(self):
        session = FakeSession(
            [self.token()],
            [FakeResponse(200, "<html>nothing useful</html>", url="https://archiveofourown.org/works/1")],
        )

        self.assert_login_fails_with(
            session, "no session cookie", "https://archiveofourown.org/works/1"
        )

    def test_the_generic_failure_message_is_gone(self):
        """The old code raised one opaque string from four unrelated causes."""

        messages = []
        for session in (
            FakeSession([FakeResponse(503, "unavailable")]),
            FakeSession([self.token()], [FakeResponse(200, "Please log in")]),
            FakeSession(
                [self.token()],
                [FakeResponse(200, "x", url="https://archiveofourown.org/users/login")],
            ),
        ):
            with self.assertRaises(ao3_backfill.AuthenticationFailure) as raised:
                self.login(session)
            messages.append(str(raised.exception))

        self.assertNotIn("AO3 login request failed", messages)
        self.assertEqual(len(set(messages)), len(messages), "each cause needs its own message")


class CacheLockingTest(unittest.TestCase):
    def test_the_operation_lock_can_be_nested_within_one_process(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())

            with cache.operation_lock():
                # A scheduler read while the caller already holds the lock must
                # not deadlock against this same process.
                self.assertEqual(cache.request_schedule(), (None, None))
                cache.set_request_started(100.0, 30.0)
                self.assertEqual(cache.request_schedule(), (100.0, 130.0))

    def test_a_second_fetch_lock_is_refused_rather_than_left_to_hang(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            first = ao3_backfill.CacheStore(path)
            second = ao3_backfill.CacheStore(path)
            first.bind(sample_report())

            with first.fetch_lock():
                with self.assertRaises(ao3_backfill.BackfillError) as raised:
                    with second.fetch_lock():
                        pass

            self.assertIn("Another fetch already holds", str(raised.exception))

    def test_the_fetch_lock_is_released_for_the_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())

            with cache.fetch_lock():
                pass
            with cache.fetch_lock():
                pass

    def test_the_cache_stays_readable_while_a_fetch_holds_its_run_lock(self):
        record = ao3_backfill.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            fetching = ao3_backfill.CacheStore(path)
            reader = ao3_backfill.CacheStore(path)
            fetching.bind(sample_report())

            with fetching.fetch_lock():
                fetching.append(record)
                # This is the whole point of splitting the locks: another
                # command can inspect progress during a multi-day fetch.
                self.assertEqual(list(reader.records()), ["64805"])


def ok_record(work_id: str) -> ao3_backfill.AO3FetchRecord:
    return ao3_backfill.AO3FetchRecord(
        work_id=work_id,
        work_url=f"https://archiveofourown.org/works/{work_id}",
        fetched_at="2026-01-01T00:00:00+00:00",
        availability="ok",
        kudos=1,
    )


def multi_work_report(*work_ids: str) -> ao3_backfill.ScanReport:
    base = sample_report()
    mappings = tuple(
        ao3_backfill.EpubMapping(
            book_id=100 + index,
            epub_path=f"Author/Work ({100 + index})/Work.epub",
            work_id=work_id,
            work_url=f"https://archiveofourown.org/works/{work_id}",
            preface_entry="preface.xhtml",
        )
        for index, work_id in enumerate(work_ids)
    )
    return ao3_backfill.ScanReport(**{**base.__dict__, "mappings": mappings})


class ScriptedFetcher:
    """Plays back a per-work list of outcomes: an exception to raise, or "ok"."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self.script = {work_id: list(outcomes) for work_id, outcomes in script.items()}
        self.calls: list[str] = []

    def fetch(self, work_id: str) -> ao3_backfill.AO3FetchRecord:
        self.calls.append(work_id)
        outcome = self.script[work_id].pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return ok_record(work_id)


class KeepGoingTest(unittest.TestCase):
    """An unattended run must do as much as it can without hammering a real problem."""

    def run_fetch(self, work_ids, fetcher=None, *, sleep_fn=None, **overrides):
        report = multi_work_report(*work_ids)
        sleeps: list[float] = []
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            options = {
                "calibredb": "calibredb",
                "library": Path("/library"),
                "limit": len(work_ids),
                "keep_going": True,
                "sleep_fn": sleep_fn or sleeps.append,
                **overrides,
            }
            if fetcher is not None:
                options["fetcher"] = cast(ao3_backfill.AO3Fetcher, fetcher)
            with patch.object(ao3_backfill, "verify_report_library"), patch.object(
                ao3_backfill, "verify_report_inputs"
            ), patch.object(ao3_backfill, "verify_custom_columns"):
                try:
                    ao3_backfill.fetch_pending(report, cache, **options)
                finally:
                    self.records = cache.records()
                    self.failures = cache.failed_work_ids()
        return sleeps

    def test_a_failing_work_is_recorded_and_the_run_carries_on(self):
        unrecognised = ao3_backfill.UnexpectedHTML("unrecognised page")
        fetcher = ScriptedFetcher({
            "1": ["ok"],
            "2": [unrecognised, unrecognised],
            "3": ["ok"],
        })

        sleeps = self.run_fetch(["1", "2", "3"], fetcher)

        self.assertEqual(sorted(self.records), ["1", "3"])
        self.assertIn("2", self.failures)
        self.assertIn("unrecognised page", self.failures["2"])
        self.assertEqual(sleeps, [], "one bad work is not a reason to pause")

    def test_failed_works_get_one_more_attempt_after_the_main_pass(self):
        fetcher = ScriptedFetcher({
            "1": ["ok"],
            "2": [ao3_backfill.NetworkStopError("blip"), "ok"],
            "3": ["ok"],
        })

        self.run_fetch(["1", "2", "3"], fetcher)

        self.assertEqual(fetcher.calls, ["1", "2", "3", "2"])
        self.assertEqual(sorted(self.records), ["1", "2", "3"])

    def test_a_cloudflare_block_cools_down_and_retries_the_same_work(self):
        fetcher = ScriptedFetcher({
            "1": [ao3_backfill.RepeatedCloudflare("challenged"), "ok"],
            "2": ["ok"],
        })

        sleeps = self.run_fetch(["1", "2"], fetcher)

        self.assertEqual(fetcher.calls, ["1", "1", "2"])
        self.assertEqual(sleeps, [ao3_backfill.COOLDOWN_SCHEDULE_SECONDS[0]])
        self.assertEqual(sorted(self.records), ["1", "2"])
        self.assertEqual(self.failures, {}, "a global problem is not the work's failure")

    def test_a_systemic_error_that_survives_recovery_is_blamed_on_the_work(self):
        """Otherwise one odd work could hold the whole run in cool-downs forever."""

        blocked = ao3_backfill.AuthenticationFailure("login redirect for this work only")
        fetcher = ScriptedFetcher({
            "1": [blocked, blocked, blocked, blocked],
            "2": ["ok"],
        })

        self.run_fetch(["1", "2"], fetcher)

        self.assertEqual(fetcher.calls[:3], ["1", "1", "2"])
        self.assertEqual(list(self.records), ["2"])
        self.assertIn("1", self.failures)

    def test_a_run_of_consecutive_failures_triggers_a_cool_down(self):
        work_ids = [str(n) for n in range(1, ao3_backfill.FAILURE_STREAK_COOLDOWN_THRESHOLD + 1)]
        fetcher = ScriptedFetcher({
            work_id: [ao3_backfill.NetworkStopError("AO3 down"), "ok"] for work_id in work_ids
        })

        sleeps = self.run_fetch(work_ids, fetcher)

        self.assertEqual(sleeps, [ao3_backfill.COOLDOWN_SCHEDULE_SECONDS[0]])
        # The retry pass recovers the works that failed during the outage.
        self.assertEqual(sorted(self.records), sorted(work_ids))

    def test_a_lasting_outage_escalates_even_though_sign_in_keeps_working(self):
        """Login recovering must not reset the pause while work pages still fail."""

        threshold = ao3_backfill.FAILURE_STREAK_COOLDOWN_THRESHOLD
        work_ids = [str(n) for n in range(1, threshold * 2 + 1)]
        down = ao3_backfill.NetworkStopError("work pages down")
        credentials = ao3_backfill.AO3Credentials("user", "pass", "test")
        fetcher = ScriptedFetcher({work_id: [down, down] for work_id in work_ids})
        with patch.object(
            ao3_backfill, "load_ao3_credential_pair", return_value=credentials
        ), patch.object(ao3_backfill, "login_authenticated_session"), patch.object(
            ao3_backfill, "AO3Fetcher", return_value=fetcher
        ):
            sleeps = self.run_fetch(work_ids, use_env_credentials=True)

        schedule = ao3_backfill.COOLDOWN_SCHEDULE_SECONDS
        self.assertEqual(sleeps[:2], [schedule[0], schedule[1]])

    def test_cool_downs_escalate_and_reset_after_a_success(self):
        cooldown = ao3_backfill._Cooldown(lambda _: None, schedule=(1.0, 2.0, 3.0))

        waits = []
        for _ in range(4):
            waits.append(cooldown.schedule[min(cooldown.level, 2)])
            cooldown.wait("test")
        cooldown.reset()

        self.assertEqual(waits, [1.0, 2.0, 3.0, 3.0])
        self.assertEqual(cooldown.level, 0)
        self.assertEqual(cooldown.total_seconds, 9.0)

    def test_an_unexpected_code_error_does_not_stop_the_run(self):
        fetcher = ScriptedFetcher({
            "1": [KeyError("parser surprise"), KeyError("parser surprise")],
            "2": ["ok"],
        })

        self.run_fetch(["1", "2"], fetcher)

        self.assertEqual(list(self.records), ["2"])
        self.assertIn("KeyError", self.failures["1"])

    def test_rejected_credentials_stop_even_an_unattended_run(self):
        credentials = ao3_backfill.AO3Credentials("user", "pass", "test")
        with patch.object(
            ao3_backfill, "load_ao3_credential_pair", return_value=credentials
        ), patch.object(
            ao3_backfill,
            "login_authenticated_session",
            side_effect=ao3_backfill.CredentialsRejected("wrong password"),
        ) as login:
            with self.assertRaises(ao3_backfill.CredentialsRejected):
                self.run_fetch(["1"], use_env_credentials=True)

        self.assertEqual(login.call_count, 1, "never resubmit known-bad credentials")

    def test_a_transient_sign_in_failure_cools_down_and_then_proceeds(self):
        credentials = ao3_backfill.AO3Credentials("user", "pass", "test")
        fetcher = ScriptedFetcher({"1": ["ok"]})
        with patch.object(
            ao3_backfill, "load_ao3_credential_pair", return_value=credentials
        ), patch.object(
            ao3_backfill,
            "login_authenticated_session",
            side_effect=[ao3_backfill.AuthenticationFailure("HTTP 525"), None],
        ), patch.object(ao3_backfill, "AO3Fetcher", return_value=fetcher):
            sleeps = self.run_fetch(["1"], use_env_credentials=True)

        self.assertEqual(sleeps, [ao3_backfill.COOLDOWN_SCHEDULE_SECONDS[0]])
        self.assertEqual(list(self.records), ["1"])

    def test_an_interrupt_during_a_cool_down_still_stops_the_run(self):
        fetcher = ScriptedFetcher({"1": [ao3_backfill.RepeatedCloudflare("challenged")]})

        def interrupted(_seconds: float) -> None:
            raise RunInterrupted("stopped by signal")

        with self.assertRaises(RunInterrupted):
            self.run_fetch(["1"], fetcher, sleep_fn=interrupted)

    def test_without_keep_going_the_first_failure_still_stops_the_run(self):
        fetcher = ScriptedFetcher({
            "1": ["ok"],
            "2": [ao3_backfill.UnexpectedHTML("unrecognised page")],
            "3": ["ok"],
        })

        with self.assertRaises(ao3_backfill.UnexpectedHTML):
            self.run_fetch(["1", "2", "3"], fetcher, keep_going=False)

        self.assertEqual(list(self.records), ["1"])
        self.assertEqual(self.failures, {})


class FailureLogTest(unittest.TestCase):
    def test_a_torn_final_line_is_skipped_rather_than_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.record_failure("1", "UnexpectedHTML", "first")
            with cache.failures_path.open("a", encoding="utf-8") as stream:
                stream.write('{"work_id": "2", "err')

            self.assertEqual(list(cache.failed_work_ids()), ["1"])

    def test_the_latest_failure_per_work_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.record_failure("1", "NetworkStopError", "older")
            cache.record_failure("1", "UnexpectedHTML", "newer")

            self.assertEqual(cache.failed_work_ids(), {"1": "UnexpectedHTML: newer"})

    def test_registered_secrets_are_scrubbed_before_a_failure_is_persisted(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            run = configure_logging(
                "unit-failure-redaction", log_dir=Path(directory), stream=stream, root_name="ao3"
            )
            try:
                register_secret("sekrit-user")
                cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
                cache.record_failure(
                    "1", "AuthenticationFailure", "HTTP 200 at https://archiveofourown.org/users/sekrit-user"
                )
                persisted = cache.failures_path.read_text(encoding="utf-8")
            finally:
                for handler in list(run.logger.handlers):
                    run.logger.removeHandler(handler)
                    handler.close()

        self.assertNotIn("sekrit-user", persisted)


class FetchProgressTest(unittest.TestCase):
    def test_each_cached_work_is_reported_with_its_position(self):
        report = sample_report()
        records = [
            ao3_backfill.AO3FetchRecord(
                work_id=work_id,
                work_url=f"https://archiveofourown.org/works/{work_id}",
                fetched_at="2026-01-01T00:00:00+00:00",
                availability="ok",
                kudos=7,
            )
            for work_id in ("64805",)
        ]

        class Fetcher:
            def __init__(self):
                self.calls = []

            def fetch(self, work_id):
                self.calls.append(work_id)
                return records[0]

        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            cache = ao3_backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            run = configure_logging(
                "unit-fetch", log_dir=Path(directory), stream=stream, root_name="ao3"
            )
            try:
                with patch.object(ao3_backfill, "verify_report_library"), patch.object(
                    ao3_backfill, "verify_report_inputs"
                ), patch.object(ao3_backfill, "verify_custom_columns"):
                    ao3_backfill.fetch_pending(
                        report,
                        cache,
                        calibredb="calibredb",
                        library=Path("/library"),
                        limit=1,
                        fetcher=cast(ao3_backfill.AO3Fetcher, Fetcher()),
                    )
                logged = run.log_path.read_text(encoding="utf-8")
            finally:
                for handler in list(run.logger.handlers):
                    run.logger.removeHandler(handler)
                    handler.close()

        output = stream.getvalue()
        self.assertIn("AO3 fetch", output)
        self.assertIn("this batch", output)
        self.assertIn("estimated finish", output)
        self.assertIn("[1/1]", output)
        self.assertIn("work=64805", output)
        self.assertIn("--- final: 1/1 works ---", output)
        self.assertIn("work=64805", logged)


if __name__ == "__main__":
    unittest.main()
