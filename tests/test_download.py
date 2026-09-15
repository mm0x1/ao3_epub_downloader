"""Tests for the download workflow: planning, enrichment, failure records, and full offline runs."""

import io
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile

from ao3archiver import ao3_client, download
from ao3archiver.ao3_client import AO3FetchRecord
from ao3archiver.metadata import (
    AO3Metadata,
    enrich_epub,
    has_ao3_metadata,
    read_ao3_metadata,
    validate_epub_file,
)
from ao3archiver.run_log import configure_logging, register_secret, RunInterrupted
from tests.support import (
    CREDENTIALS,
    epub_bytes,
    epub_reply,
    html,
    MYSTERY_PAGE,
    RoutedSession,
    serve_work,
    sign_in_replies,
    work_page,
)


def write_links(directory, *urls, name="all.txt"):
    path = Path(directory) / name
    path.write_text("\n".join(urls) + "\n", encoding="utf-8")
    return path


def links_for(*work_ids):
    return [f"https://archiveofourown.org/works/{work_id}" for work_id in work_ids]


def calibre_columns(epub_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(epub_path) as archive:
        package = ET.fromstring(archive.read("content.opf"))
    metadata = next(child for child in package if child.tag.rsplit("}", 1)[-1] == "metadata")
    return {
        child.attrib["name"].rsplit(":#", 1)[-1]: json.loads(child.attrib["content"])["#value#"]
        for child in metadata
        if child.attrib.get("name", "").startswith("calibre:user_metadata:")
    }


class LinkInventoryTest(unittest.TestCase):
    def test_duplicate_links_collapse_to_one_target(self):
        with tempfile.TemporaryDirectory() as directory:
            write_links(
                directory,
                "https://archiveofourown.org/works/111",
                "https://archiveofourown.org/works/222",
                "https://archiveofourown.org/works/111",
                "https://archiveofourown.org/works/111/chapters/9",
            )
            inventory = download.load_link_inventory(Path(directory))

        self.assertEqual([target.work_id for target in inventory.targets], ["111", "222"])
        self.assertEqual(inventory.lines, 4)
        self.assertEqual(inventory.duplicates, 2)

    def test_unparseable_lines_are_reported_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            write_links(directory, "https://archiveofourown.org/works/111", "not-a-url",
                        "https://archiveofourown.org/series/5")
            inventory = download.load_link_inventory(Path(directory))

        self.assertEqual(len(inventory.targets), 1)
        self.assertEqual(inventory.invalid, ("not-a-url", "https://archiveofourown.org/series/5"))

    def test_links_from_several_files_are_merged_and_attributed(self):
        with tempfile.TemporaryDirectory() as directory:
            write_links(directory, "https://archiveofourown.org/works/111", name="a.txt")
            write_links(directory, "https://archiveofourown.org/works/222", name="b.txt")
            inventory = download.load_link_inventory(Path(directory))

        self.assertEqual(inventory.files, 2)
        self.assertEqual({target.source for target in inventory.targets}, {"a.txt", "b.txt"})

    def test_an_empty_directory_yields_no_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = download.load_link_inventory(Path(directory))

        self.assertEqual(inventory.targets, ())
        self.assertEqual(inventory.lines, 0)


class FailureLogTest(unittest.TestCase):
    def test_the_latest_entry_per_work_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            log = download.FailureLog(Path(directory) / "failures.jsonl")
            log.record("1", stage="download", error_type="NetworkStopError", message="older", permanent=False)
            log.record("1", stage="page", error_type="Unavailable", message="newer", permanent=True)

            entry = log.latest()["1"]

        self.assertEqual((entry.stage, entry.error, entry.permanent), ("page", "newer", True))

    def test_only_permanent_entries_are_skipped_by_later_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            log = download.FailureLog(Path(directory) / "failures.jsonl")
            log.record("1", stage="page", error_type="Unavailable", message="HTTP 404", permanent=True)
            log.record("2", stage="download", error_type="NetworkStopError", message="blip", permanent=False)

            self.assertEqual(log.permanently_unavailable(), frozenset({"1"}))

    def test_a_torn_final_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            log = download.FailureLog(Path(directory) / "failures.jsonl")
            log.record("1", stage="page", error_type="Unavailable", message="gone", permanent=True)
            with log.path.open("a", encoding="utf-8") as stream:
                stream.write('{"work_id": "2", "sta')

            self.assertEqual(list(log.latest()), ["1"])

    def test_a_missing_log_is_empty(self):
        self.assertEqual(download.FailureLog(Path("/definitely-missing.jsonl")).latest(), {})

    def test_registered_secrets_are_scrubbed_before_persisting(self):
        with tempfile.TemporaryDirectory() as directory:
            run = configure_logging("unit-dl-redaction", log_dir=Path(directory), stream=io.StringIO(),
                                    root_name="ao3")
            try:
                register_secret("sekrit-reader")
                log = download.FailureLog(Path(directory) / "failures.jsonl")
                log.record("1", stage="page", error_type="AuthenticationFailure",
                           message="redirected to /users/sekrit-reader", permanent=False)
                persisted = log.path.read_text(encoding="utf-8")
            finally:
                for handler in list(run.logger.handlers):
                    run.logger.removeHandler(handler)
                    handler.close()

        self.assertNotIn("sekrit-reader", persisted)


class PlanWorkTest(unittest.TestCase):
    def plan(self, directory, work_ids, **options):
        write_links(directory, *links_for(*work_ids))
        inventory = download.load_link_inventory(Path(directory))
        output = Path(directory) / "out"
        output.mkdir(exist_ok=True)
        return download.plan_work(inventory, output, **options), output

    def test_a_missing_file_is_planned_as_a_download(self):
        with tempfile.TemporaryDirectory() as directory:
            (planned, counts), _ = self.plan(directory, ["111"])

        self.assertEqual([(p.target.work_id, p.action) for p in planned], [("111", "download")])
        self.assertEqual(counts.complete, 0)

    def test_an_unenriched_existing_file_is_planned_as_an_enrich(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            (output / "111.epub").write_bytes(epub_bytes())
            (planned, _), _ = self.plan(directory, ["111"])

        self.assertEqual([p.action for p in planned], ["enrich"])

    def test_a_fully_enriched_file_is_skipped_until_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            epub = output / "111.epub"
            epub.write_bytes(epub_bytes())
            metadata = AO3Metadata("111", "https://archiveofourown.org/works/111", kudos=5)
            download.enrich_for_calibre(epub, download.with_local_metrics(metadata, epub))

            (planned, counts), _ = self.plan(directory, ["111"])
            (refreshed, _), _ = self.plan(directory, ["111"], refresh=True)

        self.assertEqual(planned, ())
        self.assertEqual(counts.complete, 1)
        self.assertEqual([p.action for p in refreshed], ["enrich"])

    def test_a_file_from_the_old_downloader_without_calibre_columns_is_re_enriched(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            epub = output / "111.epub"
            epub.write_bytes(epub_bytes())
            enrich_epub(epub, AO3Metadata("111", "https://archiveofourown.org/works/111", kudos=5))
            self.assertTrue(has_ao3_metadata(epub))

            (planned, _), _ = self.plan(directory, ["111"])

        self.assertEqual([p.action for p in planned], ["enrich"])

    def test_a_corrupt_existing_file_is_re_downloaded_rather_than_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            (output / "111.epub").write_bytes(b"not an epub")
            (planned, _), _ = self.plan(directory, ["111"])

        self.assertEqual([p.action for p in planned], ["download"])

    def test_unavailable_works_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            (planned, counts), _ = self.plan(directory, ["111", "222"], skip_work_ids=frozenset({"111"}))

        self.assertEqual([p.target.work_id for p in planned], ["222"])
        self.assertEqual(counts.unavailable, 1)

    def test_metadata_only_never_plans_a_download(self):
        with tempfile.TemporaryDirectory() as directory:
            (planned, counts), _ = self.plan(directory, ["111"], metadata_only=True)

        self.assertEqual(planned, ())
        self.assertEqual(counts.missing_file, 1)

    def test_the_limit_bounds_the_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            (planned, _), _ = self.plan(directory, ["111", "222", "333"], limit=2)

        self.assertEqual(len(planned), 2)


class EstimateTest(unittest.TestCase):
    def test_a_download_costs_two_paced_requests_and_an_enrich_one(self):
        target = download.WorkTarget("1", "u", "all.txt")
        planned = [
            download.PlannedWork(target, Path("1.epub"), "download"),
            download.PlannedWork(target, Path("1.epub"), "enrich"),
        ]

        self.assertEqual(download.estimate_seconds(planned, 10.0), 30.0)

    def test_nothing_planned_takes_no_time(self):
        self.assertEqual(download.estimate_seconds([], 10.0), 0.0)


class SaveEpubTest(unittest.TestCase):
    def test_invalid_bytes_are_not_saved_and_leave_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "111.epub"

            with self.assertRaises(Exception):
                download.save_epub_atomically(b"<html>Cloudflare challenge</html>", destination)

            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_valid_bytes_replace_the_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "111.epub"
            destination.write_bytes(b"old")

            download.save_epub_atomically(epub_bytes(), destination)

            validate_epub_file(destination)


class EnrichForCalibreTest(unittest.TestCase):
    def test_every_column_including_local_words_and_gfog_is_baked_in(self):
        with tempfile.TemporaryDirectory() as directory:
            epub = Path(directory) / "111.epub"
            epub.write_bytes(epub_bytes())
            page = ao3_client.FetchedWorkPage(
                record=AO3FetchRecord(
                    work_id="111", work_url="https://archiveofourown.org/works/111",
                    fetched_at="2026-01-01T00:00:00+00:00", availability="ok",
                ),
                html=work_page("111"),
            )
            metadata = download.with_local_metrics(download.metadata_from_page(page, "111"), epub)

            download.enrich_for_calibre(epub, metadata)
            columns = calibre_columns(epub)
            stored = read_ao3_metadata(epub)

        self.assertEqual(columns["ao3_kudos"], 68)
        self.assertEqual(columns["ao3_hits"], 1040)
        self.assertEqual(columns["ao3_bookmarks"], 6)
        self.assertEqual(columns["ao3_comments"], 4)
        self.assertEqual(columns["ao3_words"], 1315)
        self.assertEqual(columns["ao3_chapters"], "3/3")
        self.assertEqual(columns["ao3_category"], "F/F")
        self.assertEqual(columns["words"], metadata.local_words)
        self.assertEqual(columns["gfog"], metadata.local_gfog)
        self.assertIsNotNone(metadata.local_words)
        self.assertEqual(stored.published, "2018-06-01")


class RunDownloadsTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.links = self.root / "links"
        self.links.mkdir()
        self.output = self.root / "out"
        self.failure_log = self.root / "failures.jsonl"
        self.lock = self.root / "ao3-cache.jsonl.fetch.lock"
        self.session = RoutedSession()
        self.credentials_loaded = 0

    def tearDown(self):
        self._directory.cleanup()

    def load_credentials(self):
        self.credentials_loaded += 1
        return CREDENTIALS

    def run_downloads(self, **options):
        def factory(credentials, delay, sleep_fn):
            return ao3_client.AO3DownloadClient(
                credentials, delay_seconds=delay, sleep_fn=lambda _: None, session=self.session
            )

        return download.run_downloads(
            self.links,
            self.output,
            failure_log_path=self.failure_log,
            lock_path=self.lock,
            credentials_loader=self.load_credentials,
            client_factory=factory,
            sleep_fn=lambda _: None,
            **options,
        )

    def test_a_work_is_downloaded_enriched_and_ready_for_calibre(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)
        serve_work(self.session, "111")
        self.session.add(
            "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full",
            epub_reply(),
        )

        summary = self.run_downloads()

        epub = self.output / "111.epub"
        self.assertEqual(summary.outcomes, {"downloaded": 1})
        self.assertTrue(download.is_complete(epub))
        columns = calibre_columns(epub)
        self.assertEqual(columns["ao3_kudos"], 68)
        self.assertIn("words", columns)
        self.assertIn("gfog", columns)
        self.assertEqual(self.session.requests, [
            "GET https://archiveofourown.org/token_dispenser.json",
            "POST https://archiveofourown.org/users/login",
            "GET https://archiveofourown.org/works/111",
            "GET https://archiveofourown.org/works/111/chapters/9",
            "GET https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full",
        ])
        self.assertTrue(self.session.closed)

    def test_an_unavailable_work_is_never_downloaded_and_is_skipped_next_time(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)
        self.session.add("https://archiveofourown.org/works/111", html(200, MYSTERY_PAGE))

        summary = self.run_downloads()
        again = self.run_downloads(dry_run=True)
        retried = self.run_downloads(dry_run=True, retry_unavailable=True)

        self.assertEqual(summary.outcomes, {"unavailable": 1})
        self.assertFalse(any("/downloads/" in request for request in self.session.requests))
        self.assertTrue(download.FailureLog(self.failure_log).latest()["111"].permanent)
        self.assertEqual(again.planned, 0)
        self.assertEqual(retried.planned, 1)

    def test_an_existing_unenriched_file_is_enriched_without_downloading(self):
        write_links(self.links, *links_for("111"))
        self.output.mkdir()
        (self.output / "111.epub").write_bytes(epub_bytes())
        sign_in_replies(self.session)
        serve_work(self.session, "111")

        summary = self.run_downloads()

        self.assertEqual(summary.outcomes, {"enriched": 1})
        self.assertFalse(any("/downloads/" in request for request in self.session.requests))
        self.assertTrue(download.is_complete(self.output / "111.epub"))

    def test_a_failed_work_does_not_stop_the_others_and_is_retried_next_run(self):
        write_links(self.links, *links_for("111", "222"))
        sign_in_replies(self.session)
        bad_download = "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full"
        for _ in range(2):  # the main pass and the retry pass
            serve_work(self.session, "111")
            self.session.add(bad_download, html(200, "<title>Please try again later</title>"))
        serve_work(self.session, "222")
        self.session.add(
            "https://archiveofourown.org/downloads/222/Example.epub?updated_at=1700000000&view=full",
            epub_reply(),
        )

        summary = self.run_downloads()
        next_plan = self.run_downloads(dry_run=True)

        self.assertEqual(summary.outcomes, {"failed": 2, "downloaded": 1})
        self.assertEqual(list(summary.still_failed), ["111"])
        self.assertFalse((self.output / "111.epub").exists())
        entry = download.FailureLog(self.failure_log).latest()["111"]
        self.assertEqual((entry.stage, entry.permanent), ("download", False))
        self.assertEqual(next_plan.planned, 1, "a non-permanent failure is retried next run")

    def test_a_dry_run_makes_no_requests_and_needs_no_credentials(self):
        write_links(self.links, *links_for("111", "111", "222"))

        summary = self.run_downloads(dry_run=True)

        self.assertEqual(summary.planned, 2)
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.credentials_loaded, 0)

    def test_a_dry_run_reports_the_plan(self):
        write_links(self.links, *links_for("111", "111", "222"))
        stream = io.StringIO()
        run = configure_logging("unit-download-plan", log_dir=self.root, stream=stream, root_name="ao3")
        try:
            self.run_downloads(dry_run=True)
        finally:
            for handler in list(run.logger.handlers):
                run.logger.removeHandler(handler)
                handler.close()

        output = stream.getvalue()
        # Label padding depends on the longest label, so match label and value.
        self.assertRegex(output, r"unique works\s+2\n")
        self.assertRegex(output, r"duplicate lines\s+1\n")
        self.assertRegex(output, r"to download\s+2\n")
        self.assertIn("would download: work=111", output)

    def test_an_empty_links_directory_stops_before_signing_in(self):
        summary = self.run_downloads()

        self.assertEqual(summary.planned, 0)
        self.assertEqual(self.credentials_loaded, 0)

    def test_a_real_run_refuses_to_start_while_another_run_holds_the_account(self):
        write_links(self.links, *links_for("111"))

        with ao3_client.account_lock(self.lock):
            with self.assertRaises(ao3_client.AnotherRunActive):
                self.run_downloads()

        self.assertEqual(self.credentials_loaded, 0)
        self.assertEqual(self.session.requests, [])

    def test_an_interrupt_is_not_swallowed_as_a_work_failure(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)

        def interrupted(url, timeout=None, allow_redirects=True):
            if "/works/" in url:
                raise RunInterrupted("signal")
            return RoutedSession.get(self.session, url, timeout, allow_redirects)

        self.session.get = interrupted

        with self.assertRaises(RunInterrupted):
            self.run_downloads()

        self.assertFalse(self.failure_log.exists())


if __name__ == "__main__":
    unittest.main()
