import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import calibre_sync
from ao3_metadata import AO3Metadata, enrich_epub
from test_ao3_metadata import create_epub


class CalibreSyncTest(unittest.TestCase):
    def test_calibredb_child_does_not_inherit_ao3_environment_credentials(self):
        completed = type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()
        with patch.dict(
            os.environ,
            {"AO3_USERNAME": "env-user", "AO3_PASSWORD": "env-password"},
            clear=False,
        ), patch.object(calibre_sync.subprocess, "run", return_value=completed) as run:
            calibre_sync.run_calibredb("calibredb", Path("/library"), "list")

        child_environment = run.call_args.kwargs["env"]
        self.assertNotIn("AO3_USERNAME", child_environment)
        self.assertNotIn("AO3_PASSWORD", child_environment)

    def test_multi_author_fallback_matches_calibre_display(self):
        book = {"title": "Example Work", "authors": "Alice & Bob"}

        self.assertEqual(
            calibre_sync._book_title_author_key(book),
            ("example work", ("alice", "bob")),
        )

    def test_sync_uses_numeric_column_labels_without_hash_prefix(self):
        metadata = AO3Metadata(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            kudos=68,
            hits=1040,
        )

        with tempfile.TemporaryDirectory() as directory:
            epub_path = Path(directory) / "64805.epub"
            create_epub(epub_path)
            enrich_epub(epub_path, metadata)

            calls = []

            def fake_run(_calibredb, _library, *arguments):
                calls.append(arguments)
                if arguments[:2] == ("list", "--for-machine"):
                    return json.dumps(
                        [{
                            "id": 1,
                            "title": "Example Work",
                            "authors": "Example Author",
                            "identifiers": {"isbn": "123"},
                        }]
                    )
                return ""

            with patch.object(calibre_sync, "run_calibredb", side_effect=fake_run):
                calibre_sync.sync_metadata(
                    "calibredb",
                    Path("/library"),
                    Path(directory),
                    create_columns=True,
                )

            self.assertIn(("set_custom", "ao3_kudos", "1", "68"), calls)
            self.assertIn(("set_custom", "ao3_hits", "1", "1040"), calls)
            self.assertNotIn(("set_custom", "#ao3_kudos", "1", "68"), calls)
            self.assertFalse(any(call[0] == "set_metadata" for call in calls))


if __name__ == "__main__":
    unittest.main()
