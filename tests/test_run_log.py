"""Tests for logging, progress, and interrupt handling."""

import io
import logging
from pathlib import Path
import tempfile
import unittest

from ao3archiver import run_log


def make_logger(name: str) -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    return logger, stream


class FormatDurationTest(unittest.TestCase):
    def test_spans_are_rendered_at_a_useful_scale(self):
        self.assertEqual(run_log.format_duration(45), "45s")
        self.assertEqual(run_log.format_duration(90), "1m30s")
        self.assertEqual(run_log.format_duration(3600 * 7 + 240), "7h04m")
        self.assertEqual(run_log.format_duration(86400 * 5 + 3600 * 3), "5d03h")

    def test_unknown_and_invalid_spans_do_not_raise(self):
        self.assertEqual(run_log.format_duration(None), "unknown")
        self.assertEqual(run_log.format_duration(-1), "unknown")
        self.assertEqual(run_log.format_duration(float("inf")), "unknown")


class CredentialFilterTest(unittest.TestCase):
    def test_secrets_are_replaced_in_emitted_records(self):
        logger, stream = make_logger("test.redaction")
        credential_filter = run_log.CredentialFilter()
        credential_filter.add_secret("hunter2-password")
        logger.handlers[0].addFilter(credential_filter)

        logger.info("posting login for user with hunter2-password now")

        self.assertNotIn("hunter2-password", stream.getvalue())
        self.assertIn(run_log.REDACTED, stream.getvalue())

    def test_longer_secrets_are_redacted_before_their_substrings(self):
        credential_filter = run_log.CredentialFilter()
        credential_filter.add_secret("abc")
        credential_filter.add_secret("abcdef")

        self.assertEqual(credential_filter.redact("abcdef"), run_log.REDACTED)

    def test_trivially_short_values_are_not_registered(self):
        credential_filter = run_log.CredentialFilter()
        credential_filter.add_secret("ab")

        self.assertEqual(credential_filter.redact("a tab in ab"), "a tab in ab")


class ProgressTrackerTest(unittest.TestCase):
    def test_item_lines_carry_position_rate_and_eta(self):
        logger, stream = make_logger("test.progress")
        clock = [0.0]
        tracker = run_log.ProgressTracker(
            logger, 100, unit="work", summary_every=0, monotonic_fn=lambda: clock[0]
        )

        clock[0] = 3600.0
        tracker.record("ok", "work=64805", "kudos=68")

        line = stream.getvalue().strip()
        self.assertIn("[  1/100]", line)
        self.assertIn("1.0%", line)
        self.assertIn("work=64805", line)
        self.assertIn("kudos=68", line)
        self.assertIn("1/h", line)
        self.assertIn("elapsed 1h00m", line)

    def test_rate_and_eta_are_unknown_before_the_first_item(self):
        logger, _ = make_logger("test.progress.empty")
        tracker = run_log.ProgressTracker(logger, 10, monotonic_fn=lambda: 0.0)

        self.assertIsNone(tracker.rate_per_hour())
        self.assertIsNone(tracker.eta())
        self.assertIn("--/h", tracker.item_line("ok"))

    def test_a_sub_second_first_item_does_not_report_an_absurd_rate(self):
        logger, _ = make_logger("test.progress.instant")
        clock = [0.0]
        tracker = run_log.ProgressTracker(
            logger, 100, summary_every=0, monotonic_fn=lambda: clock[0]
        )

        clock[0] = 0.000002
        tracker.record("ok", "work=1")

        self.assertIsNone(tracker.rate_per_hour())
        self.assertIsNone(tracker.eta())

    def test_summary_is_emitted_on_the_configured_interval_only(self):
        logger, stream = make_logger("test.progress.summary")
        clock = [0.0]
        tracker = run_log.ProgressTracker(
            logger, 10, summary_every=3, monotonic_fn=lambda: clock[0]
        )

        for index in range(4):
            clock[0] += 10.0
            tracker.record("ok", f"work={index}")

        self.assertEqual(stream.getvalue().count("--- progress:"), 1)

    def test_outcomes_are_tallied_for_the_final_summary(self):
        logger, stream = make_logger("test.progress.outcomes")
        tracker = run_log.ProgressTracker(logger, 3, summary_every=0)
        tracker.record("ok")
        tracker.record("ok")
        tracker.record("incomplete")

        tracker.log_summary(final=True)

        output = stream.getvalue()
        self.assertIn("incomplete 1", output)
        self.assertIn("ok 2", output)
        self.assertIn("--- final: 3/3 works ---", output)


class InterruptGuardTest(unittest.TestCase):
    def test_sleep_runs_to_completion_when_no_signal_arrived(self):
        logger, _ = make_logger("test.guard.clean")
        slept: list[float] = []
        clock = [0.0]

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            clock[0] += seconds

        guard = run_log.InterruptGuard(
            logger, sleep_fn=sleep, monotonic_fn=lambda: clock[0], slice_seconds=0.25
        )
        guard.sleep(1.0)

        self.assertAlmostEqual(sum(slept), 1.0)

    def test_sleep_raises_at_the_next_slice_after_a_signal(self):
        logger, _ = make_logger("test.guard.interrupted")
        clock = [0.0]
        guard = run_log.InterruptGuard(
            logger,
            unit="work",
            sleep_fn=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
            monotonic_fn=lambda: clock[0],
            slice_seconds=0.25,
        )
        guard.request()

        with self.assertRaises(run_log.RunInterrupted):
            guard.sleep(30.0)
        # The wait is abandoned rather than shortened, so no request follows it.
        self.assertEqual(clock[0], 0.0)

    def test_check_is_a_no_op_until_a_signal_arrives(self):
        logger, _ = make_logger("test.guard.check")
        guard = run_log.InterruptGuard(logger)

        guard.check()
        guard.request()

        with self.assertRaises(run_log.RunInterrupted):
            guard.check()


class ConfigureLoggingTest(unittest.TestCase):
    def test_records_reach_both_the_stream_and_a_run_log_file(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            run = run_log.configure_logging(
                "unit-test",
                log_dir=Path(directory),
                stream=stream,
                root_name="test.configure",
            )
            child = logging.getLogger("test.configure.child")
            child.info("progress from a module logger")

            self.assertTrue(run.log_path.is_file())
            contents = run.log_path.read_text(encoding="utf-8")

        self.assertIn("progress from a module logger", stream.getvalue())
        self.assertIn("progress from a module logger", contents)

    def test_registered_secrets_are_redacted_everywhere(self):
        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as directory:
            run = run_log.configure_logging(
                "unit-secret",
                log_dir=Path(directory),
                stream=stream,
                root_name="test.secret",
            )
            run_log.register_secret("s3cret-value")
            logging.getLogger("test.secret.child").info("sending s3cret-value upstream")
            contents = run.log_path.read_text(encoding="utf-8")

        self.assertNotIn("s3cret-value", stream.getvalue())
        self.assertNotIn("s3cret-value", contents)


if __name__ == "__main__":
    unittest.main()
