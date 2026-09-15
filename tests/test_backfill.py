"""Tests for the one-time library backfill: scanning, the cache, fetching, and the Calibre write."""

from contextlib import redirect_stdout
import io
import json
import logging
import sqlite3
from pathlib import Path
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

import requests

from ao3archiver import ao3_client, backfill, calibre_library
from ao3archiver.credentials import AO3Credentials
from ao3archiver.run_log import configure_logging, register_secret, RunInterrupted
from tests.support import AO3_PAGE, create_epub, FakeResponse, FakeSession, sample_report


class BackfillTest(unittest.TestCase):
    def test_primary_preface_link_wins_over_related_work_link(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "work.epub"
            create_epub(
                path,
                '<html><body><a href="https://archiveofourown.org/works/123">primary</a>'
                '<a href="https://archiveofourown.org/works/999">related</a></body></html>',
            )

            work_id, work_ids, entry, malformed, reason = backfill._epub_preface_work_ids(path)

        self.assertEqual(work_id, "123")
        self.assertEqual(work_ids, ("123", "999"))
        self.assertEqual(entry, "preface.xhtml")
        self.assertFalse(malformed)
        self.assertIsNone(reason)

    def test_cache_resume_skips_work_ids_already_cached(self):
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=0,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.append(record)
            loaded = cache.records()

        pending = [
            work_id
            for work_id in backfill._unique_work_ids(sample_report(), include_ambiguous=True)
            if work_id not in loaded
        ]
        self.assertEqual(pending, [])
        self.assertEqual(loaded["64805"].kudos, 0)

    def test_chapter_link_is_not_used_as_the_preface_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "chapter-only.epub"
            create_epub(
                path,
                "<html><body>preface without an AO3 link</body></html>",
                '<html><body><a href="https://archiveofourown.org/works/999">chapter</a></body></html>',
            )

            work_id, _, entry, _, reason = backfill._epub_preface_work_ids(path)

        self.assertIsNone(work_id)
        self.assertEqual(entry, "preface.xhtml")
        self.assertEqual(reason, "no AO3 work URL in EPUB preface")

    def test_dry_run_summary_reports_counts_and_sample_mapping(self):
        summary = backfill.render_scan_summary(sample_report())

        self.assertIn("Calibre books: 1", summary)
        self.assertIn("Primary AO3 mappings: 1", summary)
        self.assertIn("book=7 work=64805", summary)

    def test_incomplete_final_cache_line_is_recovered(self):
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=68,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            path.write_text(json.dumps(record.to_dict()) + "\n{" , encoding="utf-8")
            cache = backfill.CacheStore(path)

            loaded = cache.records()
            recovered_contents = path.read_text(encoding="utf-8")

        self.assertEqual(loaded["64805"].kudos, 68)
        self.assertEqual(recovered_contents, json.dumps(record.to_dict()) + "\n")

    def test_non_empty_cache_without_context_is_not_adopted(self):
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=68,
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            path.write_text(json.dumps(record.to_dict()) + "\n", encoding="utf-8")
            cache = backfill.CacheStore(path)

            with self.assertRaises(backfill.BackfillError):
                cache.bind(sample_report())

    def test_cache_rejects_non_finite_shared_request_timestamp(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
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

            with self.assertRaises(backfill.BackfillError):
                with cache.operation_lock():
                    cache.last_request_at_unlocked()

    def test_shared_scheduler_persists_delay_and_server_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            now = [100.0]
            sleeps: list[float] = []

            def sleep(seconds: float) -> None:
                sleeps.append(seconds)
                now[0] += seconds

            with cache.operation_lock():
                scheduler = backfill.CacheRequestScheduler(
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
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            now = [100.0]
            sleeps: list[float] = []
            deferred: list[tuple[float, bool, float | None]] = []

            def sleep(seconds: float) -> None:
                sleeps.append(seconds)
                now[0] += seconds

            with cache.operation_lock():
                scheduler = backfill.CacheRequestScheduler(
                    cache,
                    30,
                    sleep_fn=sleep,
                    wall_time_fn=lambda: now[0],
                )

                def defer(seconds: float, exact: bool) -> None:
                    scheduler.defer_requests(seconds, exact)
                    deferred.append((seconds, exact, cache.next_request_at_unlocked()))

                fetcher = ao3_client.AO3Fetcher(
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
        old_record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            kudos=1,
        )
        refreshed = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-02T00:00:00+00:00",
            availability="ok",
            kudos=2,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            cache.append(old_record)
            snapshot = Path(directory) / "before-refresh.jsonl"
            cache.snapshot(snapshot)

            class Fetcher:
                def fetch(self, _work_id: str) -> ao3_client.AO3FetchRecord:
                    return refreshed

            with patch.object(backfill, "verify_report_library"), patch.object(
                backfill, "verify_report_inputs"
            ), patch.object(backfill, "verify_custom_columns"):
                result = backfill.fetch_pending(
                    sample_report(),
                    cache,
                    calibredb="calibredb",
                    library=Path("/library"),
                    limit=1,
                    refresh=True,
                    fetcher=cast(ao3_client.AO3Fetcher, Fetcher()),
                )

            records = cache.records()
            snapshot_records = [json.loads(line) for line in snapshot.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(len(result), 1)
        self.assertEqual(records["64805"].kudos, 2)
        self.assertEqual(len(snapshot_records), 1)
        self.assertEqual(snapshot_records[0]["kudos"], 1)

    def test_no_pending_work_does_not_load_environment_credentials(self):
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            cache.append(record)
            with patch.object(backfill, "verify_report_library"), patch.object(
                backfill, "verify_report_inputs"
            ), patch.object(backfill, "verify_custom_columns"), patch.object(
                backfill, "load_run_credentials"
            ) as loader:
                result = backfill.fetch_pending(
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
        report = backfill.ScanReport(
            **{
                **report.__dict__,
                "mappings": (
                    backfill.EpubMapping(
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
        record = ao3_client.AO3FetchRecord(
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
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            cache.append(record)
            validation = backfill.validate_cache(report, cache)
            output = io.StringIO()
            with redirect_stdout(output):
                backfill.print_cache_validation(validation, sample_size=1)

        rendered = output.getvalue()
        self.assertIn("category='F/F, M/M'", rendered)
        self.assertIn("ao3_words=1315", rendered)
        self.assertIn("local_words=321", rendered)
        self.assertIn("gfog=8.5", rendered)

    def test_calibre_write_preserves_unavailable_values_and_writes_zero_as_zero(self):
        report = backfill.ScanReport(
            **{
                **sample_report().__dict__,
                "mappings": (
                    backfill.EpubMapping(
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
        record = ao3_client.AO3FetchRecord(
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
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            cache.append(record)
            writes: list[dict[str, dict[int, object]]] = []

            def bulk_writer(_executable, _library, plan, datatypes):
                writes.append({label: dict(values) for label, values in plan.items()})
                self.assertEqual(datatypes["gfog"], "float")
                return tuple(plan)

            with patch.object(backfill, "require_calibre_closed"), patch.object(
                backfill, "verify_backup"
            ), patch.object(backfill, "verify_custom_columns"), patch.object(
                backfill, "verify_local_metric_columns"
            ), patch.object(backfill, "verify_report_inputs"), patch.object(
                backfill, "load_local_metric_values", return_value={7: {"words": None, "gfog": 8.0}}
            ):
                result = backfill.write_calibre_values(
                    report,
                    cache,
                    calibredb="calibredb",
                    library=Path("/library"),
                    backup_path=Path("/backup"),
                    write_local_metrics=True,
                    bulk_writer=bulk_writer,
                )

        self.assertEqual(result["updated"], [7])
        self.assertEqual(len(writes), 1, "everything goes out in one bulk write")
        plan = writes[0]
        self.assertEqual(plan["ao3_kudos"][7], 0)
        self.assertEqual(plan["ao3_hits"][7], 12)
        self.assertEqual(plan["ao3_words"][7], 0)
        self.assertEqual(plan["words"][7], 100)
        self.assertNotIn("ao3_comments", plan)
        self.assertNotIn("ao3_status", plan)
        self.assertNotIn(7, plan.get("gfog", {}), "an existing local Gfog is preserved")
        self.assertEqual(result["written_values"]["7"]["ao3_kudos"], "0")

    def test_calibre_write_skips_incomplete_records(self):
        report = sample_report()
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="incomplete",
            kudos=99,
        )
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            cache.append(record)
            writes: list[object] = []
            with patch.object(backfill, "require_calibre_closed"), patch.object(
                backfill, "verify_backup"
            ), patch.object(backfill, "verify_custom_columns"), patch.object(
                backfill, "verify_report_inputs"
            ), patch.object(calibre_library, "run_calibredb") as run_calibredb:
                result = backfill.write_calibre_values(
                    report,
                    cache,
                    calibredb="calibredb",
                    library=Path("/library"),
                    backup_path=Path("/backup"),
                    bulk_writer=lambda *arguments: writes.append(arguments) or (),
                )

        self.assertEqual(result["updated"], [])
        self.assertEqual(result["skipped_unavailable"], [7])
        run_calibredb.assert_not_called()
        self.assertEqual(writes, [], "nothing to write means no Calibre process at all")


class BackfillBulkWriteTest(unittest.TestCase):
    def ok_record(self, **values):
        return ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            **values,
        )

    def write(self, bulk_writer, record):
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())
            cache.append(record)
            with patch.object(backfill, "require_calibre_closed"), patch.object(
                backfill, "verify_backup"
            ), patch.object(backfill, "verify_custom_columns"), patch.object(
                backfill, "verify_report_inputs"
            ):
                return backfill.write_calibre_values(
                    sample_report(),
                    cache,
                    calibredb="calibredb",
                    library=Path("/library"),
                    backup_path=Path("/backup"),
                    bulk_writer=bulk_writer,
                )

    def test_a_stopped_bulk_write_reports_exactly_which_columns_landed(self):
        def bulk_writer(_executable, _library, plan, _datatypes):
            raise calibre_library.BulkWriteError("disk full", ["ao3_kudos"])

        result = self.write(bulk_writer, self.ok_record(kudos=5, hits=9))

        self.assertEqual(result["written_values"], {"7": {"ao3_kudos": "5"}})
        self.assertEqual(result["updated"], [7])
        self.assertEqual(result["failed"][0]["completed_columns"], ["ao3_kudos"])
        self.assertEqual(result["failed"][0]["incomplete_columns"], ["ao3_hits"])

    def test_a_bulk_write_that_lands_nothing_updates_no_books(self):
        def bulk_writer(_executable, _library, plan, _datatypes):
            raise calibre_library.BulkWriteError("could not open library", [])

        result = self.write(bulk_writer, self.ok_record(kudos=5))

        self.assertEqual(result["written_values"], {})
        self.assertEqual(result["updated"], [])

    def test_the_terminal_summary_stays_short_for_a_large_write(self):
        stream = io.StringIO()
        logger = logging.getLogger("test.write.summary")
        logger.handlers[:] = [logging.StreamHandler(stream)]
        logger.setLevel(logging.INFO)
        logger.propagate = False
        result = {
            "updated": list(range(15000)),
            "written_values": {str(book): {"ao3_kudos": "1"} for book in range(15000)},
            "skipped_unavailable": [], "skipped_without_values": [], "skipped_ambiguous": [],
            "not_cached": [], "failed": [],
            "post_write_verification": {"books": 15000, "fields": 15000},
        }

        backfill._log_write_summary(logger, result, Path("/tmp/write-result.json"))

        self.assertLess(len(stream.getvalue().splitlines()), 12)
        self.assertIn("books updated: 15000", stream.getvalue())


class BackupCommandTest(unittest.TestCase):
    """The documented procedure takes a backup before every write."""

    def make_library(self, root: Path) -> Path:
        library = root / "Calibre Library"
        library.mkdir()
        with sqlite3.connect(library / "metadata.db") as connection:
            connection.execute("CREATE TABLE books (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE custom_columns (id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TABLE library_id (uuid TEXT)")
            connection.execute("INSERT INTO library_id VALUES ('library-uuid')")
        return library

    def test_repeated_backups_each_get_a_new_file_instead_of_failing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            library = self.make_library(root)
            state = root / "state"
            state.mkdir()
            (state / "metadata.db.backup").write_bytes(b"an older backup that must survive")
            arguments = ["backup", "--library", str(library), "--cache-dir", str(state),
                         "--log-dir", str(root / "logs")]

            with patch.object(backfill, "create_backup", wraps=calibre_library.create_backup), patch.object(
                calibre_library, "require_calibre_closed"
            ):
                self.assertEqual(backfill.main(arguments), 0)
                self.assertEqual(backfill.main(arguments), 0)

            backups = sorted(path.name for path in state.glob("metadata.db*.backup"))
            preserved = (state / "metadata.db.backup").read_bytes()

        self.assertEqual(len(backups), 3, backups)
        self.assertEqual(preserved, b"an older backup that must survive")

    def test_a_name_taken_in_the_same_second_gets_a_suffix(self):
        from datetime import datetime, timezone

        moment = datetime(2026, 9, 15, 1, 2, 3, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            first = backfill._new_backup_path(state, moment)
            first.write_bytes(b"taken")
            second = backfill._new_backup_path(state, moment)

        self.assertEqual(first.name, "metadata.db.20260915T010203Z.backup")
        self.assertEqual(second.name, "metadata.db.20260915T010203Z-2.backup")


class CacheLockingTest(unittest.TestCase):
    def test_a_download_refuses_to_start_while_the_backfill_holds_the_lock(self):
        """The backfill's fetch lock and the downloader's account lock are one lock."""

        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "ao3-cache.jsonl")
            with cache.fetch_lock():
                self.assertTrue(ao3_client.ao3_run_active(cache.fetch_lock_path))
                with self.assertRaises(ao3_client.AnotherRunActive):
                    with ao3_client.account_lock(cache.fetch_lock_path):
                        pass

    def test_the_default_account_lock_is_the_backfill_fetch_lock(self):
        cache = backfill.CacheStore(backfill.DEFAULT_CACHE_DIR / backfill.CACHE_NAME)

        self.assertEqual(cache.fetch_lock_path, ao3_client.ACCOUNT_LOCK_PATH)

    def test_the_operation_lock_can_be_nested_within_one_process(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
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
            first = backfill.CacheStore(path)
            second = backfill.CacheStore(path)
            first.bind(sample_report())

            with first.fetch_lock():
                with self.assertRaises(ao3_client.AnotherRunActive) as raised:
                    with second.fetch_lock():
                        pass

            self.assertIn("Another AO3 run already holds", str(raised.exception))

    def test_the_fetch_lock_is_released_for_the_next_run(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(sample_report())

            with cache.fetch_lock():
                pass
            with cache.fetch_lock():
                pass

    def test_the_cache_stays_readable_while_a_fetch_holds_its_run_lock(self):
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.jsonl"
            fetching = backfill.CacheStore(path)
            reader = backfill.CacheStore(path)
            fetching.bind(sample_report())

            with fetching.fetch_lock():
                fetching.append(record)
                # This is the whole point of splitting the locks: another
                # command can inspect progress during a multi-day fetch.
                self.assertEqual(list(reader.records()), ["64805"])


def ok_record(work_id: str) -> ao3_client.AO3FetchRecord:
    return ao3_client.AO3FetchRecord(
        work_id=work_id,
        work_url=f"https://archiveofourown.org/works/{work_id}",
        fetched_at="2026-01-01T00:00:00+00:00",
        availability="ok",
        kudos=1,
    )


def multi_work_report(*work_ids: str) -> backfill.ScanReport:
    base = sample_report()
    mappings = tuple(
        backfill.EpubMapping(
            book_id=100 + index,
            epub_path=f"Author/Work ({100 + index})/Work.epub",
            work_id=work_id,
            work_url=f"https://archiveofourown.org/works/{work_id}",
            preface_entry="preface.xhtml",
        )
        for index, work_id in enumerate(work_ids)
    )
    return backfill.ScanReport(**{**base.__dict__, "mappings": mappings})


class ScriptedFetcher:
    """Plays back a per-work list of outcomes: an exception to raise, or "ok"."""

    def __init__(self, script: dict[str, list[object]]) -> None:
        self.script = {work_id: list(outcomes) for work_id, outcomes in script.items()}
        self.calls: list[str] = []

    def fetch(self, work_id: str) -> ao3_client.AO3FetchRecord:
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
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
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
                options["fetcher"] = cast(ao3_client.AO3Fetcher, fetcher)
            with patch.object(backfill, "verify_report_library"), patch.object(
                backfill, "verify_report_inputs"
            ), patch.object(backfill, "verify_custom_columns"):
                try:
                    backfill.fetch_pending(report, cache, **options)
                finally:
                    self.records = cache.records()
                    self.failures = cache.failed_work_ids()
        return sleeps

    def test_a_failing_work_is_recorded_and_the_run_carries_on(self):
        unrecognised = ao3_client.UnexpectedHTML("unrecognised page")
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
            "2": [ao3_client.NetworkStopError("blip"), "ok"],
            "3": ["ok"],
        })

        self.run_fetch(["1", "2", "3"], fetcher)

        self.assertEqual(fetcher.calls, ["1", "2", "3", "2"])
        self.assertEqual(sorted(self.records), ["1", "2", "3"])

    def test_a_cloudflare_block_cools_down_and_retries_the_same_work(self):
        fetcher = ScriptedFetcher({
            "1": [ao3_client.RepeatedCloudflare("challenged"), "ok"],
            "2": ["ok"],
        })

        sleeps = self.run_fetch(["1", "2"], fetcher)

        self.assertEqual(fetcher.calls, ["1", "1", "2"])
        self.assertEqual(sleeps, [ao3_client.COOLDOWN_SCHEDULE_SECONDS[0]])
        self.assertEqual(sorted(self.records), ["1", "2"])
        self.assertEqual(self.failures, {}, "a global problem is not the work's failure")

    def test_a_systemic_error_that_survives_recovery_is_blamed_on_the_work(self):
        """Otherwise one odd work could hold the whole run in cool-downs forever."""

        blocked = ao3_client.AuthenticationFailure("login redirect for this work only")
        fetcher = ScriptedFetcher({
            "1": [blocked, blocked, blocked, blocked],
            "2": ["ok"],
        })

        self.run_fetch(["1", "2"], fetcher)

        self.assertEqual(fetcher.calls[:3], ["1", "1", "2"])
        self.assertEqual(list(self.records), ["2"])
        self.assertIn("1", self.failures)

    def test_a_run_of_consecutive_failures_triggers_a_cool_down(self):
        work_ids = [str(n) for n in range(1, ao3_client.FAILURE_STREAK_COOLDOWN_THRESHOLD + 1)]
        fetcher = ScriptedFetcher({
            work_id: [ao3_client.NetworkStopError("AO3 down"), "ok"] for work_id in work_ids
        })

        sleeps = self.run_fetch(work_ids, fetcher)

        self.assertEqual(sleeps, [ao3_client.COOLDOWN_SCHEDULE_SECONDS[0]])
        # The retry pass recovers the works that failed during the outage.
        self.assertEqual(sorted(self.records), sorted(work_ids))

    def test_a_lasting_outage_escalates_even_though_sign_in_keeps_working(self):
        """Login recovering must not reset the pause while work pages still fail."""

        threshold = ao3_client.FAILURE_STREAK_COOLDOWN_THRESHOLD
        work_ids = [str(n) for n in range(1, threshold * 2 + 1)]
        down = ao3_client.NetworkStopError("work pages down")
        credentials = AO3Credentials("user", "pass", "test")
        fetcher = ScriptedFetcher({work_id: [down, down] for work_id in work_ids})
        with patch.object(
            backfill, "load_run_credentials", return_value=credentials
        ), patch.object(backfill, "login_authenticated_session"), patch.object(
            backfill, "AO3Fetcher", return_value=fetcher
        ):
            sleeps = self.run_fetch(work_ids, use_env_credentials=True)

        schedule = ao3_client.COOLDOWN_SCHEDULE_SECONDS
        self.assertEqual(sleeps[:2], [schedule[0], schedule[1]])

    def test_cool_downs_escalate_and_reset_after_a_success(self):
        cooldown = ao3_client.Cooldown(lambda _: None, schedule=(1.0, 2.0, 3.0))

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
        credentials = AO3Credentials("user", "pass", "test")
        with patch.object(
            backfill, "load_run_credentials", return_value=credentials
        ), patch.object(
            backfill, "login_authenticated_session",
            side_effect=ao3_client.CredentialsRejected("wrong password"),
        ) as login:
            with self.assertRaises(ao3_client.CredentialsRejected):
                self.run_fetch(["1"], use_env_credentials=True)

        self.assertEqual(login.call_count, 1, "never resubmit known-bad credentials")

    def test_a_transient_sign_in_failure_cools_down_and_then_proceeds(self):
        credentials = AO3Credentials("user", "pass", "test")
        fetcher = ScriptedFetcher({"1": ["ok"]})
        with patch.object(
            backfill, "load_run_credentials", return_value=credentials
        ), patch.object(
            backfill, "login_authenticated_session",
            side_effect=[ao3_client.AuthenticationFailure("HTTP 525"), None],
        ), patch.object(backfill, "AO3Fetcher", return_value=fetcher):
            sleeps = self.run_fetch(["1"], use_env_credentials=True)

        self.assertEqual(sleeps, [ao3_client.COOLDOWN_SCHEDULE_SECONDS[0]])
        self.assertEqual(list(self.records), ["1"])

    def test_an_interrupt_during_a_cool_down_still_stops_the_run(self):
        fetcher = ScriptedFetcher({"1": [ao3_client.RepeatedCloudflare("challenged")]})

        def interrupted(_seconds: float) -> None:
            raise RunInterrupted("stopped by signal")

        with self.assertRaises(RunInterrupted):
            self.run_fetch(["1"], fetcher, sleep_fn=interrupted)

    def test_without_keep_going_the_first_failure_still_stops_the_run(self):
        fetcher = ScriptedFetcher({
            "1": ["ok"],
            "2": [ao3_client.UnexpectedHTML("unrecognised page")],
            "3": ["ok"],
        })

        with self.assertRaises(ao3_client.UnexpectedHTML):
            self.run_fetch(["1", "2", "3"], fetcher, keep_going=False)

        self.assertEqual(list(self.records), ["1"])
        self.assertEqual(self.failures, {})


class FailureLogTest(unittest.TestCase):
    def test_a_torn_final_line_is_skipped_rather_than_fatal(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.record_failure("1", "UnexpectedHTML", "first")
            with cache.failures_path.open("a", encoding="utf-8") as stream:
                stream.write('{"work_id": "2", "err')

            self.assertEqual(list(cache.failed_work_ids()), ["1"])

    def test_the_latest_failure_per_work_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
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
                cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
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
            ao3_client.AO3FetchRecord(
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
            cache = backfill.CacheStore(Path(directory) / "cache.jsonl")
            cache.bind(report)
            run = configure_logging(
                "unit-fetch", log_dir=Path(directory), stream=stream, root_name="ao3"
            )
            try:
                with patch.object(backfill, "verify_report_library"), patch.object(
                    backfill, "verify_report_inputs"
                ), patch.object(backfill, "verify_custom_columns"):
                    backfill.fetch_pending(
                        report,
                        cache,
                        calibredb="calibredb",
                        library=Path("/library"),
                        limit=1,
                        fetcher=cast(ao3_client.AO3Fetcher, Fetcher()),
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
