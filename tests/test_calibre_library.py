"""Tests for Calibre library access: columns, calibredb, process checks, bulk writes, and verification."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from ao3archiver import calibre_library
from tests.support import create_epub


class CalibreLibraryTest(unittest.TestCase):
    def test_multiline_calibre_details_are_parsed_and_datatype_is_verified(self):
        details = "\n\n".join(
            f"{label}\n\n{{'datatype': '{datatype}', 'name': '{name}', 'label': '{label}'}}"
            for label, name, datatype, _ in calibre_library.AO3_COLUMNS
        )
        parsed = calibre_library.parse_custom_column_details(details)

        self.assertEqual(parsed["ao3_kudos"]["datatype"], "int")
        self.assertEqual(parsed["ao3_status"]["name"], "AO3 Status")

        sqlite_columns = {
            label: {"id": index, "label": label, "name": name, "datatype": datatype}
            for index, (label, name, datatype, _) in enumerate(calibre_library.AO3_COLUMNS, start=4)
        }
        with patch.object(calibre_library, "run_calibredb", return_value=details), patch.object(
            calibre_library, "read_custom_columns_sqlite", return_value=sqlite_columns
        ):
            verified = calibre_library.verify_custom_columns("calibredb", Path("/library"))

        self.assertEqual(verified["ao3_kudos"]["sqlite_id"], 4)
        self.assertEqual(verified["ao3_category"]["datatype"], "text")


class VerifyLibraryValuesTest(unittest.TestCase):
    def test_the_numeric_search_count_reads_calibredbs_comma_separated_ids(self):
        """calibredb search prints "6,8,9" on one line; counting lines reported 1."""

        books = [{"id": n, "title": "t", "*ao3_kudos": 200} for n in (6, 8, 9)]
        calls: list[tuple[str, ...]] = []

        def fake_calibredb(_executable, _library, *arguments):
            calls.append(arguments)
            if arguments[0] == "search":
                return "6,8,9\n"
            return json.dumps(books)

        with patch.object(calibre_library, "verify_custom_columns"), patch.object(
            calibre_library, "run_calibredb", side_effect=fake_calibredb
        ):
            result = calibre_library.verify_library_values("calibredb", Path("/library"))

        self.assertEqual(result["numeric_search_result_count"], 3)
        search_call = next(call for call in calls if call[0] == "search")
        self.assertIn("--limit", search_call)


class CalibreBulkWriteRunnerTest(unittest.TestCase):
    def fake_calibre_debug(self, directory: Path, body: str) -> str:
        script = directory / "fake-calibre-debug"
        script.write_text(f"#!{sys.executable}\nimport sys\n{body}\n", encoding="utf-8")
        script.chmod(0o755)
        return str(script)

    def test_progress_events_are_followed_and_chatter_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.fake_calibre_debug(Path(directory), (
                "print('DeDRM v10: loading plugins')\n"
                "print('{\"event\": \"column\", \"label\": \"ao3_kudos\", \"requested\": 2, \"changed\": 2, \"seconds\": 0.1}')\n"
                "print('{\"event\": \"column\", \"label\": \"ao3_hits\", \"requested\": 2, \"changed\": 1, \"seconds\": 0.1}')\n"
                "print('{\"event\": \"done\"}')"
            ))

            completed = calibre_library.run_calibre_bulk_write(
                executable, Path(directory), {"ao3_kudos": {1: 5, 2: 6}, "ao3_hits": {1: 9}},
                {"ao3_kudos": "int", "ao3_hits": "int"},
            )

        self.assertEqual(completed, ("ao3_kudos", "ao3_hits"))

    def test_a_crash_part_way_raises_with_the_completed_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.fake_calibre_debug(Path(directory), (
                "print('{\"event\": \"column\", \"label\": \"ao3_kudos\", \"requested\": 1, \"changed\": 1, \"seconds\": 0.1}', flush=True)\n"
                "sys.stderr.write('sqlite3.OperationalError: database is locked\\n')\n"
                "sys.exit(1)"
            ))

            with self.assertRaises(calibre_library.BulkWriteError) as raised:
                calibre_library.run_calibre_bulk_write(
                    executable, Path(directory), {"ao3_kudos": {1: 5}, "ao3_hits": {1: 9}},
                    {"ao3_kudos": "int", "ao3_hits": "int"},
                )

        self.assertEqual(raised.exception.completed, ("ao3_kudos",))
        self.assertIn("database is locked", str(raised.exception))

    def test_an_exit_without_the_done_event_is_not_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = self.fake_calibre_debug(Path(directory), "pass")

            with self.assertRaises(calibre_library.BulkWriteError):
                calibre_library.run_calibre_bulk_write(
                    executable, Path(directory), {"ao3_kudos": {1: 5}}, {"ao3_kudos": "int"}
                )

    def test_a_missing_executable_is_a_bulk_write_error(self):
        with self.assertRaises(calibre_library.BulkWriteError):
            calibre_library.run_calibre_bulk_write(
                "/definitely/missing/calibre-debug", Path("/library"), {"ao3_kudos": {1: 5}},
                {"ao3_kudos": "int"},
            )


@unittest.skipUnless(shutil.which("calibre-debug") and shutil.which("calibredb"), "Calibre is not installed")
class CalibreBulkWriteIntegrationTest(unittest.TestCase):
    """Runs calibre_scripts/bulk_write.py under the real calibre-debug against a real library."""

    def test_values_written_through_the_api_read_back_through_calibredb(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "library"
            library.mkdir()
            epub = Path(directory) / "work.epub"
            create_epub(epub, "<html><body>preface</body></html>")
            env = calibre_library.sanitized_child_environment()
            for label, datatype in (("ao3_kudos", "int"), ("gfog", "float"), ("ao3_category", "text")):
                subprocess.run(["calibredb", "--library-path", str(library), "add_custom_column",
                                label, label, datatype], check=True, capture_output=True, env=env)
            subprocess.run(["calibredb", "--library-path", str(library), "add", str(epub)],
                           check=True, capture_output=True, env=env)

            completed = calibre_library.run_calibre_bulk_write(
                "calibre-debug",
                library,
                {"ao3_kudos": {1: 518}, "gfog": {1: 8.45}, "ao3_category": {1: "F/F"}},
                {"ao3_kudos": "int", "gfog": "float", "ao3_category": "text"},
            )
            values = calibre_library._read_custom_values_calibredb(
                "calibredb", library, ("ao3_kudos", "gfog", "ao3_category")
            )

        self.assertEqual(set(completed), {"ao3_kudos", "gfog", "ao3_category"})
        self.assertEqual(values[1], {"ao3_kudos": 518, "gfog": 8.45, "ao3_category": "F/F"})

    def test_a_datatype_mismatch_stops_before_writing_that_column(self):
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "library"
            library.mkdir()
            env = calibre_library.sanitized_child_environment()
            subprocess.run(["calibredb", "--library-path", str(library), "add_custom_column",
                            "ao3_kudos", "ao3_kudos", "int"], check=True, capture_output=True, env=env)

            with self.assertRaises(calibre_library.BulkWriteError) as raised:
                calibre_library.run_calibre_bulk_write(
                    "calibre-debug", library, {"ao3_kudos": {1: 5}}, {"ao3_kudos": "text"}
                )

        self.assertEqual(raised.exception.completed, ())


class CalibreProcessDetectionTest(unittest.TestCase):
    def detect(self, *argvs):
        return calibre_library.running_calibre_processes(
            lambda: [(100 + index, list(argv)) for index, argv in enumerate(argvs)]
        )

    def test_a_process_mentioning_the_library_path_is_not_calibre(self):
        """`/home/drifter/Calibre Library` used to split into a token named Calibre."""

        self.assertEqual(
            self.detect(
                ["python3", "backfill.py", "fetch", "--library", "/home/drifter/Calibre Library"],
                ["timeout", "600", "python3", "download.py", "--output", "/home/drifter/Calibre Library"],
                ["tail", "-f", "/home/drifter/Calibre Library/metadata.db"],
            ),
            (),
        )

    def test_calibre_programs_are_detected(self):
        found = self.detect(
            ["/opt/calibre/bin/calibre"],
            ["/usr/bin/calibredb", "list"],
            ["/opt/calibre/bin/calibre-server", "/library"],
            ["python3", "/opt/calibre-web/cps.py"],
            ["python3", "-m", "calibreweb"],
            ["/usr/bin/python3", "/usr/local/bin/calibre-web"],
        )

        self.assertEqual(len(found), 6)

    def test_this_process_is_never_reported(self):
        own = os.getpid()
        self.assertEqual(
            calibre_library.running_calibre_processes(lambda: [(own, ["/usr/bin/calibredb"])]), ()
        )

    def test_the_live_process_table_can_be_read(self):
        self.assertTrue(calibre_library._proc_command_lines())


class CalibredbEnvironmentTest(unittest.TestCase):
    def test_calibredb_child_does_not_inherit_ao3_environment_credentials(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.dict(
            os.environ,
            {"AO3_USERNAME": "env-user", "AO3_PASSWORD": "env-password"},
            clear=False,
        ), patch.object(calibre_library.subprocess, "run", return_value=completed) as run:
            calibre_library.run_calibredb("calibredb", Path("/library"), "list")

        child_environment = run.call_args.kwargs["env"]
        self.assertNotIn("AO3_USERNAME", child_environment)
        self.assertNotIn("AO3_PASSWORD", child_environment)


if __name__ == "__main__":
    unittest.main()
