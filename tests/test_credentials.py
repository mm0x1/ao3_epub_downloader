"""Tests for credential loading."""

from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from ao3archiver import credentials


class BackfillCredentialLoadingTest(unittest.TestCase):
    def test_environment_credentials_are_loaded_as_a_pair_without_output(self):
        username = "env-user"
        password = "env-password"
        output = io.StringIO()

        with redirect_stdout(output):
            actual = credentials.load_run_credentials(
                environ={"AO3_USERNAME": username, "AO3_PASSWORD": password},
                dotenv_path=Path("/definitely-missing-ao3-backfill.env"),
            )

        self.assertEqual(actual.as_tuple(), (username, password))
        self.assertNotIn(username, output.getvalue())
        self.assertNotIn(password, output.getvalue())

    def test_environment_credentials_reject_missing_or_partial_values_without_leaking(self):
        for environment in ({}, {"AO3_USERNAME": "env-user"}, {"AO3_PASSWORD": "env-password"}):
            with self.assertRaises(credentials.CredentialError) as raised:
                credentials.load_run_credentials(
                    environ=environment,
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

            actual = credentials.load_run_credentials(
                environ={},
                dotenv_path=dotenv_path,
            )

        self.assertEqual(actual.as_tuple(), ("env-user", "env-password"))


class LoadDotenvTest(unittest.TestCase):
    def test_quoted_and_unquoted_values_are_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# comment\n\nAO3_USERNAME=plain-user\nAO3_PASSWORD='quoted pass'\nOTHER=ignored\n",
                encoding="utf-8",
            )
            values = credentials.load_dotenv_values(path)

        self.assertEqual(values, {"AO3_USERNAME": "plain-user", "AO3_PASSWORD": "quoted pass"})

    def test_a_missing_file_is_not_an_error(self):
        self.assertEqual(credentials.load_dotenv_values(Path("/definitely-missing.env")), {})

    def test_a_malformed_line_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("AO3_USERNAME\n", encoding="utf-8")
            with self.assertRaises(credentials.CredentialError):
                credentials.load_dotenv_values(path)

    def test_an_unbalanced_quote_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("AO3_PASSWORD='unterminated\n", encoding="utf-8")
            with self.assertRaises(credentials.CredentialError):
                credentials.load_dotenv_values(path)


class LoadAO3CredentialsTest(unittest.TestCase):
    def test_the_process_environment_overrides_the_dotenv_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("AO3_USERNAME=file-user\nAO3_PASSWORD=file-pass\n", encoding="utf-8")
            resolved = credentials.load_ao3_credentials(
                {"AO3_PASSWORD": "env-pass"}, dotenv_path=path
            )

        self.assertEqual(resolved.username, "file-user")
        self.assertEqual(resolved.password, "env-pass")
        self.assertIn("AO3_PASSWORD", resolved.source)

    def test_a_partial_pair_is_rejected_without_leaking_the_value(self):
        with self.assertRaises(credentials.CredentialError) as raised:
            credentials.load_ao3_credentials(
                {"AO3_USERNAME": "lonely-user"}, dotenv_path=Path("/definitely-missing.env")
            )

        self.assertNotIn("lonely-user", str(raised.exception))

    def test_the_source_names_the_dotenv_path_when_nothing_overrides_it(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("AO3_USERNAME=u\nAO3_PASSWORD=p\n", encoding="utf-8")
            resolved = credentials.load_ao3_credentials({}, dotenv_path=path)

        self.assertEqual(resolved.source, str(path))


class ResolveCredentialsTest(unittest.TestCase):
    def test_the_ini_file_is_used_only_when_the_env_pair_is_absent(self):
        with tempfile.TemporaryDirectory() as directory:
            ini = Path(directory) / "personal.ini"
            ini.write_text(
                "[archiveofourown.org]\nusername = ini-user\npassword = ini-pass\n",
                encoding="utf-8",
            )
            resolved = credentials.resolve_credentials(
                dotenv_path=Path("/definitely-missing.env"), ini_path=ini, environ={}
            )

        self.assertEqual(resolved.as_tuple(), ("ini-user", "ini-pass"))
        self.assertEqual(resolved.source, str(ini))

    def test_the_env_pair_wins_over_the_ini_file(self):
        with tempfile.TemporaryDirectory() as directory:
            ini = Path(directory) / "personal.ini"
            ini.write_text(
                "[archiveofourown.org]\nusername = ini-user\npassword = ini-pass\n",
                encoding="utf-8",
            )
            resolved = credentials.resolve_credentials(
                dotenv_path=Path("/definitely-missing.env"),
                ini_path=ini,
                environ={"AO3_USERNAME": "env-user", "AO3_PASSWORD": "env-pass"},
            )

        self.assertEqual(resolved.as_tuple(), ("env-user", "env-pass"))

    def test_an_empty_ini_section_is_not_treated_as_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            ini = Path(directory) / "personal.ini"
            ini.write_text("[archiveofourown.org]\nusername =\npassword =\n", encoding="utf-8")
            with self.assertRaises(credentials.CredentialError):
                credentials.resolve_credentials(
                    dotenv_path=Path("/definitely-missing.env"), ini_path=ini, environ={}
                )

    def test_the_failure_message_names_both_places_to_look(self):
        with self.assertRaises(credentials.CredentialError) as raised:
            credentials.resolve_credentials(
                dotenv_path=Path("/missing.env"), ini_path=Path("/missing.ini"), environ={}
            )

        self.assertIn("/missing.env", str(raised.exception))
        self.assertIn("/missing.ini", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
