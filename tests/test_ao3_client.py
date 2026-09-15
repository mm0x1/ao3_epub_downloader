"""Tests for the AO3 client: login, request policy, page classification, downloads, and the run queue."""

from contextlib import redirect_stdout
import io
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import cast
import unittest
from unittest.mock import patch

import requests

from ao3archiver import ao3_client
from ao3archiver.run_log import ProgressTracker, RunInterrupted
from tests.support import (
    AO3_PAGE,
    CREDENTIALS,
    epub_reply,
    FakeResponse,
    FakeSession,
    html,
    make_client,
    MYSTERY_PAGE,
    redirect,
    Reply,
    RoutedSession,
    serve_work,
    sign_in_replies,
    work_page,
)


class AO3ClientTest(unittest.TestCase):
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

    # Trimmed from the live chapter-view response for work 36776911.
    MYSTERY_CHAPTER_PAGE = """<html><head><title>Mystery Work - Chapter 1 | Archive of Our Own</title></head>
<body class="logged-in"><div id="inner" class="wrapper">
<div id="main" class="chapters-show region" role="main">
<div class="flash"></div>
  <p class="notice">
  This work is part of an ongoing challenge and will be revealed soon!
</p>
<!-- BEGIN revealed -->
<!-- END revealed -->
<div id="chapter-1" class="chapter"></div>
</div></div></body></html>"""

    def follow(self, responses, url="https://archiveofourown.org/works/64805"):
        session = FakeSession(responses)
        paced: list[str] = []
        sleeps: list[float] = []
        response = ao3_client._get_with_ao3_redirects(
            cast(requests.Session, session),
            url,
            timeout_seconds=60,
            before_request=lambda: paced.append(session.urls[-1] if session.urls else url),
            request_started_callback=None,
            sleep_fn=sleeps.append,
        )
        return response, session, paced, sleeps

    def test_a_counter_left_off_a_real_stats_block_is_zero(self):
        """AO3 omits Comments/Kudos/Bookmarks rows at zero; Hits marks a real block.

        This replaced "missing counter is not coerced to zero" by explicit
        decision, after 14,392 live records showed no literal zero and the 47
        partial ones lacked only comments or bookmarks.
        """

        html = """
        <html><title>Archive of Our Own</title>
        <h2 class="title heading">Partial</h2>
        <a href="https://archiveofourown.org/works/64805">Partial</a>
        <dd class="words">900</dd><dd class="kudos">3</dd><dd class="hits">12</dd></html>
        """

        record = ao3_client._metadata_from_page(
            html,
            "https://archiveofourown.org/works/64805",
            200,
        )

        self.assertEqual(record.kudos, 3)
        self.assertEqual(record.hits, 12)
        self.assertEqual(record.comments, 0)
        self.assertEqual(record.bookmarks, 0)
        self.assertEqual(record.availability, "ok")

    def test_missing_counter_is_not_coerced_to_zero_without_a_real_stats_block(self):
        """Without Hits the block is not a genuine one, so nothing is invented."""

        html = """
        <html><title>Archive of Our Own</title>
        <h2 class="title heading">Partial</h2>
        <a href="https://archiveofourown.org/works/64805">Partial</a>
        <dd class="kudos">0</dd><dd class="words">12</dd></html>
        """

        record = ao3_client._metadata_from_page(
            html,
            "https://archiveofourown.org/works/64805",
            200,
        )

        self.assertEqual(record.kudos, 0)
        self.assertIsNone(record.hits)
        self.assertIsNone(record.comments)
        self.assertEqual(record.availability, "incomplete")
        self.assertEqual(record.value_for("comments"), "")

    def test_words_are_never_defaulted_even_with_a_real_stats_block(self):
        html = """
        <html><title>Archive of Our Own</title>
        <h2 class="title heading">Partial</h2>
        <a href="https://archiveofourown.org/works/64805">Partial</a>
        <dd class="hits">12</dd></html>
        """

        record = ao3_client._metadata_from_page(
            html,
            "https://archiveofourown.org/works/64805",
            200,
        )

        self.assertIsNone(record.words)
        self.assertEqual(record.availability, "incomplete")

    def test_preface_structure_allows_a_legitimate_incomplete_work_page(self):
        record = ao3_client._metadata_from_page(
            '<html><div id="preface"><a href="https://archiveofourown.org/works/64805">'
            'work</a></div></html>',
            "https://archiveofourown.org/works/64805",
            200,
            "https://archiveofourown.org/works/64805",
        )

        self.assertEqual(record.availability, "incomplete")
        self.assertIsNone(record.kudos)

    def test_same_work_chapter_redirect_can_supply_work_stats(self):
        record = ao3_client._metadata_from_page(
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
        record = ao3_client._metadata_from_page(
            '<html><div class="works-show"><div class="chapters-show region">'
            '<div class="userstuff">chapter text</div></div></div></html>',
            "https://archiveofourown.org/works/64805",
            200,
            "https://archiveofourown.org/works/64805/chapters/150732991",
        )

        self.assertEqual(record.availability, "incomplete")
        self.assertEqual(record.error, "AO3 page did not expose a statistics block")

    def test_unstructured_chapter_html_is_not_cached_as_incomplete(self):
        with self.assertRaises(ao3_client.UnexpectedHTML):
            ao3_client._metadata_from_page(
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
        fetcher = ao3_client.AO3Fetcher(
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
        with patch.object(ao3_client.time, "time", return_value=100.0):
            self.assertEqual(
                ao3_client._parse_retry_after("Thu, 01 Jan 1970 00:01:47 GMT"),
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
        fetcher = ao3_client.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        with self.assertRaises(ao3_client.UnexpectedHTML):
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
        fetcher = ao3_client.AO3Fetcher(
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
        fetcher = ao3_client.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        with self.assertRaises(ao3_client.AuthenticationFailure):
            fetcher.fetch("64805")

    def test_repeated_cloudflare_responses_stop_the_batch(self):
        challenge = '<html><title>Just a moment...</title><div id="cf-wrapper"></div></html>'
        session = FakeSession([
            FakeResponse(503, challenge, {"Content-Type": "text/html"}),
            FakeResponse(503, challenge, {"Content-Type": "text/html"}),
        ])
        sleeps: list[float] = []
        fetcher = ao3_client.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=sleeps.append,
            monotonic_fn=lambda: 0,
        )

        with self.assertRaises(ao3_client.RepeatedCloudflare):
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
        fetcher = ao3_client.AO3Fetcher(
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
        fetcher = ao3_client.AO3Fetcher(
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
        fetcher = ao3_client.AO3Fetcher(
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
        fetcher = ao3_client.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=10,
            sleep_fn=lambda _: None,
            monotonic_fn=lambda: 0,
            max_attempts=2,
            max_rate_limit_attempts=3,
        )

        with self.assertRaises(ao3_client.RepeatedRateLimit) as raised:
            fetcher.fetch("64805")

        self.assertEqual(len(session.urls), 3)
        self.assertIn("--delay", str(raised.exception))

    def test_transient_backoff_is_capped(self):
        fetcher = ao3_client.AO3Fetcher(
            session=cast(requests.Session, FakeSession([])),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        self.assertEqual(fetcher._transient_backoff(1), 30.0)
        self.assertEqual(fetcher._transient_backoff(2), 60.0)
        self.assertEqual(
            fetcher._transient_backoff(20), ao3_client.TRANSIENT_MAX_BACKOFF_SECONDS
        )

    def test_an_origin_outage_does_not_count_toward_the_cloudflare_stop(self):
        outage = '<html><div id="cf-wrapper">Error 525</div></html>'
        session = FakeSession([
            FakeResponse(525, outage, {"Content-Type": "text/html"}),
            FakeResponse(525, outage, {"Content-Type": "text/html"}),
        ])
        fetcher = ao3_client.AO3Fetcher(
            session=cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
            monotonic_fn=lambda: 0,
            max_attempts=2,
            max_transient_attempts=2,
        )

        # It still stops, but as a transient failure rather than a bot block.
        with self.assertRaises(ao3_client.NetworkStopError) as raised:
            fetcher.fetch("64805")
        self.assertNotIsInstance(raised.exception, ao3_client.RepeatedCloudflare)

    def test_an_unrevealed_mystery_work_is_cached_as_unavailable(self):
        url = "https://archiveofourown.org/works/62373328"

        record = ao3_client._metadata_from_page(self.MYSTERY_WORK_PAGE, url, 200, url)

        self.assertEqual(record.availability, "unavailable")
        self.assertEqual(record.http_status, 200)
        self.assertIn("unrevealed", record.error)
        # The slug keeps its case; it is read from the original markup.
        self.assertIn("/collections/Fic_Prison", record.error)

    def test_the_chapter_view_of_a_mystery_work_is_unavailable_not_incomplete(self):
        """This layout passes the work-page check, so it was cached as incomplete."""

        url = "https://archiveofourown.org/works/36776911/chapters/91746943"

        record = ao3_client._metadata_from_page(
            self.MYSTERY_CHAPTER_PAGE, "https://archiveofourown.org/works/36776911", 200, url
        )

        self.assertEqual(record.availability, "unavailable")
        self.assertEqual(record.error, "AO3 work is unrevealed (Mystery Work); statistics are hidden")

    def test_a_fic_quoting_the_mystery_notice_is_still_a_normal_work(self):
        page = AO3_PAGE.replace(
            "<html>",
            '<html><div class="userstuff"><p>This work is part of an ongoing challenge and will '
            "be revealed soon! she read aloud.</p></div>",
        )
        url = "https://archiveofourown.org/works/64805"

        record = ao3_client._metadata_from_page(page, url, 200, url)

        self.assertEqual(record.availability, "ok")

    def test_a_mystery_work_does_not_stop_the_fetch(self):
        session = FakeSession([
            FakeResponse(
                200,
                self.MYSTERY_WORK_PAGE,
                {"Content-Type": "text/html"},
                url="https://archiveofourown.org/works/62373328",
            )
        ])
        fetcher = ao3_client.AO3Fetcher(
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
        with self.assertRaises(ao3_client.UnexpectedHTML):
            ao3_client._metadata_from_page(
                self.MYSTERY_WORK_PAGE,
                "https://archiveofourown.org/works/62373328",
                200,
                "https://archiveofourown.org/works/11111111",
            )

    def test_an_unknown_page_reports_its_title_in_the_stop_message(self):
        """The last stop said only 'not a recognized AO3 work page'."""

        url = "https://archiveofourown.org/works/64805"
        page = "<html><head><title>Something New | Archive of Our Own</title></head><body></body></html>"

        with self.assertRaises(ao3_client.UnexpectedHTML) as raised:
            ao3_client._metadata_from_page(page, url, 200, url)

        self.assertIn("Something New | Archive of Our Own", str(raised.exception))

    def test_story_text_quoting_a_login_prompt_is_not_an_auth_demand(self):
        """Work 25021798 quotes a newspaper paywall: "log in to continue reading"."""

        page = AO3_PAGE.replace(
            "<html>",
            '<html><body class="logged-in"><div class="userstuff"><p>Create your free account '
            "or log in to continue reading nytimes.com.</p></div>",
        )
        session = FakeSession([
            FakeResponse(
                200,
                page,
                {"Content-Type": "text/html"},
                url="https://archiveofourown.org/works/64805",
            )
        ])
        reauthentications: list[str] = []
        fetcher = ao3_client.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
            reauthenticate=lambda: reauthentications.append("unexpected"),
        )

        record = fetcher.fetch("64805")

        self.assertEqual(record.availability, "ok")
        self.assertEqual(reauthentications, [])

    def test_a_signed_out_page_asking_to_log_in_is_still_an_auth_demand(self):
        response = FakeResponse(
            200, '<body class="logged-out"><p>Please log in to view this work.</p></body>'
        )

        self.assertTrue(ao3_client._is_authentication_response(response))

    def test_wrong_work_response_is_rejected(self):
        with self.assertRaises(ao3_client.UnexpectedHTML):
            ao3_client._metadata_from_page(
                '<html><title>Archive of Our Own</title><dl class="stats">'
                '<dd class="kudos">4</dd></dl>'
                '<a href="https://archiveofourown.org/works/999">wrong</a></html>',
                "https://archiveofourown.org/works/64805",
                200,
                "https://archiveofourown.org/works/999",
            )

    def test_non_finite_delay_is_rejected(self):
        with self.assertRaises(ValueError):
            ao3_client.AO3Fetcher(delay_seconds=float("nan"))

    def test_cache_record_round_trip_is_json_serializable(self):
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
            authors=("Example Author",),
            category="F/F, M/M",
            kudos=0,
        )

        decoded = ao3_client.AO3FetchRecord.from_dict(json.loads(json.dumps(record.to_dict())))

        self.assertEqual(decoded.authors, ("Example Author",))
        self.assertEqual(decoded.category, "F/F, M/M")
        self.assertEqual(decoded.kudos, 0)

    def test_cache_record_rejects_tampered_identity(self):
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
        ).to_dict()
        record["work_url"] = "https://archiveofourown.org/works/999"

        with self.assertRaises(ValueError):
            ao3_client.AO3FetchRecord.from_dict(record)

    def test_cache_rejects_boolean_counter_values(self):
        record = ao3_client.AO3FetchRecord(
            work_id="64805",
            work_url="https://archiveofourown.org/works/64805",
            fetched_at="2026-01-01T00:00:00+00:00",
            availability="ok",
        ).to_dict()
        record["kudos"] = False

        with self.assertRaises(ValueError):
            ao3_client.AO3FetchRecord.from_dict(record)

    def test_login_uses_token_flow_and_does_not_print_credentials(self):
        token = FakeResponse(200, "", json_value={"token": "rotating-token"})
        logged_in = FakeResponse(
            200,
            '<html><body class="logged-in"><a href="/users/logout">Log out</a></body></html>',
        )
        session = FakeSession([token], [logged_in])
        output = io.StringIO()

        with redirect_stdout(output):
            ao3_client.login_authenticated_session(
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
        with self.assertRaises(ao3_client.AuthenticationFailure):
            ao3_client.login_authenticated_session(
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
        with self.assertRaises(ao3_client.AuthenticationFailure):
            ao3_client.login_authenticated_session(
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

        with self.assertRaises(ao3_client.AuthenticationFailure):
            ao3_client.login_authenticated_session(
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

        ao3_client.login_authenticated_session(
            cast(requests.Session, session),
            "env-user",
            "env-password",
            sleep_fn=sleeps.append,
            request_deferred_callback=lambda seconds, exact: deferred.append((seconds, exact)),
        )

        self.assertEqual(deferred, [(11.0, True)])
        self.assertEqual(sleeps, [30.0, 11.0, 30.0])

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
        self.assertTrue(ao3_client._is_ao3_url("https://archiveofourown.org/works/1"))
        self.assertFalse(ao3_client._is_ao3_url("http://archiveofourown.org/works/1"))
        self.assertFalse(ao3_client._is_ao3_url("https://archiveofourown.org:8443/works/1"))
        self.assertFalse(ao3_client._is_ao3_url("https://user:pass@archiveofourown.org/works/1"))

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

        ao3_client.login_authenticated_session(
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
        fetcher = ao3_client.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        with self.assertRaises(ao3_client.NetworkStopError):
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
            fetcher = ao3_client.AO3Fetcher(
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
        fetcher = ao3_client.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
            reauthenticate=lambda: reauthentications.append("once"),
        )

        with self.assertRaises(ao3_client.AuthenticationFailure):
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
        fetcher = ao3_client.AO3Fetcher(
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
        fetcher = ao3_client.AO3Fetcher(
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
        fetcher = ao3_client.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
        )

        with self.assertRaises(ao3_client.UnexpectedHTML):
            fetcher.fetch("64805")

    def test_cloudflare_403_does_not_trigger_reauthentication(self):
        challenge = '<html><title>Just a moment...</title><div id="cf-wrapper"></div></html>'
        session = FakeSession([FakeResponse(403, challenge, {"Content-Type": "text/html"})] * 2)
        reauthentications: list[str] = []
        fetcher = ao3_client.AO3Fetcher(
            cast(requests.Session, session),
            delay_seconds=30,
            sleep_fn=lambda _: None,
            reauthenticate=lambda: reauthentications.append("unexpected"),
        )

        with self.assertRaises(ao3_client.RepeatedCloudflare):
            fetcher.fetch("64805")

        self.assertEqual(reauthentications, [])


class LoginDiagnosticsTest(unittest.TestCase):
    """Every login failure must say which step failed and why."""

    def login(self, session, max_transient_attempts: int = 1, max_transport_attempts: int = 1):
        return ao3_client.login_authenticated_session(
            cast(requests.Session, session),
            "env-user",
            "env-password",
            source="/repo/.env",
            sleep_fn=lambda _: None,
            max_transient_attempts=max_transient_attempts,
            max_transport_attempts=max_transport_attempts,
        )

    def assert_login_fails_with(
        self, session, *expected: str, max_transient_attempts: int = 1
    ) -> str:
        with self.assertRaises(ao3_client.AuthenticationFailure) as raised:
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
        with self.assertRaises(ao3_client.CredentialsRejected):
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

        with self.assertRaises(ao3_client.AuthenticationFailure):
            self.login(session, max_transient_attempts=8)

        self.assertEqual(len(session.post_urls), 1)

    def test_a_timed_out_login_starts_over_with_a_fresh_token(self):
        """A resent form could reuse a token AO3 already consumed."""

        logged_in = FakeResponse(
            200, '<body class="logged-in"><a href="/users/logout">Log out</a></body>'
        )

        class Session(FakeSession):
            timeouts = 1

            def post(self, *args, **kwargs):
                if Session.timeouts:
                    Session.timeouts -= 1
                    self.post_urls.append(args[0])
                    raise requests.ReadTimeout("read timed out")
                return super().post(*args, **kwargs)

        session = Session([self.token(), self.token()], [logged_in])
        sleeps: list[float] = []

        ao3_client.login_authenticated_session(
            cast(requests.Session, session),
            "env-user",
            "env-password",
            delay_seconds=10,
            sleep_fn=sleeps.append,
        )

        self.assertEqual(len(session.urls), 2, "each attempt fetches its own token")
        self.assertEqual(len(session.post_urls), 2)
        self.assertIn(10.0, sleeps)

    def test_login_transport_retries_are_bounded(self):
        class Session(FakeSession):
            def post(self, *args, **kwargs):
                raise requests.ReadTimeout("read timed out")

        session = Session([self.token(), self.token()])

        with self.assertRaises(ao3_client.LoginTransportError):
            ao3_client.login_authenticated_session(
                cast(requests.Session, session),
                "env-user",
                "env-password",
                sleep_fn=lambda _: None,
                max_transport_attempts=2,
            )

        self.assertEqual(len(session.urls), 2)

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
            with self.assertRaises(ao3_client.AuthenticationFailure) as raised:
                self.login(session)
            messages.append(str(raised.exception))

        self.assertNotIn("AO3 login request failed", messages)
        self.assertEqual(len(set(messages)), len(messages), "each cause needs its own message")


class FindEpubDownloadUrlTest(unittest.TestCase):
    def test_the_page_link_is_used_with_its_cache_buster_unescaped(self):
        url = ao3_client.find_epub_download_url(work_page("111"), "111")

        self.assertEqual(
            url, "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full"
        )

    def test_an_absolute_link_on_the_download_host_is_accepted(self):
        page = work_page("111", epub_href="https://download.archiveofourown.org/downloads/111/Example.epub")

        self.assertEqual(
            ao3_client.find_epub_download_url(page, "111"),
            "https://download.archiveofourown.org/downloads/111/Example.epub",
        )

    def test_a_link_to_a_foreign_host_is_ignored(self):
        page = work_page("111", epub_href="https://evil.example/downloads/111/Example.epub")

        self.assertEqual(
            ao3_client.find_epub_download_url(page, "111"),
            "https://archiveofourown.org/downloads/111/111.epub",
        )

    def test_a_link_for_a_different_work_is_not_used(self):
        page = work_page("999")

        self.assertEqual(
            ao3_client.find_epub_download_url(page, "111"),
            "https://archiveofourown.org/downloads/111/111.epub",
        )


class RequestPacerTest(unittest.TestCase):
    def pacer(self):
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        return ao3_client.RequestPacer(10.0, sleep_fn=sleep, monotonic_fn=lambda: clock[0]), clock, sleeps

    def test_the_first_request_is_not_delayed_and_the_next_waits_the_delay(self):
        pacer, clock, sleeps = self.pacer()

        pacer.before_request()
        pacer.request_started(0.0)
        clock[0] += 1.5
        pacer.before_request()

        self.assertEqual(sleeps, [8.5])

    def test_an_exact_retry_after_replaces_the_schedule(self):
        pacer, _, sleeps = self.pacer()
        pacer.request_started(0.0)

        pacer.defer(3.0, exact=True)
        pacer.before_request()

        self.assertEqual(sleeps, [3.0])

    def test_a_backoff_never_shortens_the_existing_schedule(self):
        pacer, _, sleeps = self.pacer()
        pacer.request_started(0.0)

        pacer.defer(3.0, exact=False)
        pacer.before_request()

        self.assertEqual(sleeps, [10.0])


class FetchWorkPageTest(unittest.TestCase):
    def test_the_final_chapter_page_html_comes_back_with_its_record(self):
        session = RoutedSession()
        serve_work(session, "111")
        client, _ = make_client(session)

        page = client.fetch_work_page("111")

        self.assertEqual(page.record.availability, "ok")
        self.assertEqual(page.record.kudos, 68)
        self.assertIn("/downloads/111/Example.epub", page.html)
        self.assertEqual(session.requests, [
            "GET https://archiveofourown.org/works/111",
            "GET https://archiveofourown.org/works/111/chapters/9",
        ])

    def test_a_mystery_work_is_unavailable(self):
        session = RoutedSession({"https://archiveofourown.org/works/111": [html(200, MYSTERY_PAGE)]})
        client, _ = make_client(session)

        page = client.fetch_work_page("111")

        self.assertEqual(page.record.availability, "unavailable")
        self.assertIn("Secret_Fest", page.record.error)

    def test_a_deleted_work_is_unavailable_with_no_page(self):
        session = RoutedSession({"https://archiveofourown.org/works/111": [html(404, "gone")]})
        client, _ = make_client(session)

        page = client.fetch_work_page("111")

        self.assertEqual(page.record.availability, "unavailable")
        self.assertIsNone(page.html)

    def test_downloads_identify_themselves(self):
        client, _ = make_client(RoutedSession())

        self.assertEqual(client.session.headers["User-Agent"], ao3_client.USER_AGENT)


class DownloadEpubTest(unittest.TestCase):
    URL = "https://archiveofourown.org/downloads/111/Example.epub"

    def download(self, *replies: Reply, credentials=CREDENTIALS, extra=None):
        session = RoutedSession({self.URL: list(replies), **(extra or {})})
        client, sleeps = make_client(session, credentials)
        return client, session, sleeps

    def test_the_epub_bytes_are_returned(self):
        client, _, _ = self.download(epub_reply())

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))

    def test_a_redirect_to_the_download_host_is_followed(self):
        target = "https://download.archiveofourown.org/downloads/111/Example.epub"
        client, session, _ = self.download(redirect(target), extra={target: [epub_reply()]})

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))
        self.assertEqual(session.requests[-1], f"GET {target}")

    def test_a_redirect_to_a_foreign_host_is_refused(self):
        client, _, _ = self.download(redirect("https://evil.example/Example.epub"))

        with self.assertRaises(ao3_client.NetworkStopError):
            client.download_epub(self.URL, "111")

    def test_an_html_page_instead_of_an_epub_names_the_page(self):
        client, _, _ = self.download(html(200, "<title>Please try again later | Archive of Our Own</title>"))

        with self.assertRaises(ao3_client.UnexpectedHTML) as raised:
            client.download_epub(self.URL, "111")

        self.assertIn("Please try again later", str(raised.exception))

    def test_one_cloudflare_challenge_is_waited_out(self):
        challenge = html(503, "<title>Just a moment...</title>")
        client, _, _ = self.download(challenge, epub_reply())

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))

    def test_a_repeated_cloudflare_challenge_stops_the_work(self):
        challenge = "<title>Just a moment...</title>"
        client, _, _ = self.download(html(503, challenge), html(503, challenge))

        with self.assertRaises(ao3_client.RepeatedCloudflare):
            client.download_epub(self.URL, "111")

    def test_a_rate_limit_waits_the_exact_retry_after(self):
        client, _, sleeps = self.download(Reply(429, "slow down", headers={"Retry-After": "300"}), epub_reply())

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))
        self.assertIn(300.0, sleeps)

    def test_a_cloudflare_origin_error_is_retried(self):
        client, _, _ = self.download(html(525, '<div id="cf-wrapper">Error 525</div>'), epub_reply())

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))

    def test_a_login_redirect_signs_in_once_and_retries(self):
        login = "https://archiveofourown.org/users/login"
        session = RoutedSession({self.URL: [redirect(login), epub_reply()], login: [html(200, "log in form")]})
        sign_in_replies(session)
        client, _ = make_client(session)

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))
        self.assertEqual(session.requests.count("POST https://archiveofourown.org/users/login"), 1)

    def test_authentication_that_survives_a_sign_in_fails_the_work(self):
        client, _, _ = self.download(Reply(403, "forbidden"), credentials=None)

        with self.assertRaises(ao3_client.AuthenticationFailure):
            client.download_epub(self.URL, "111")

    def test_story_text_saying_please_log_in_is_not_mistaken_for_a_login_page(self):
        client, _, _ = self.download(epub_reply("Please log in, she said, or you must log in to continue."))

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))


class AccountLockTest(unittest.TestCase):
    def test_the_lock_is_released_when_a_run_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "run.lock"
            with ao3_client.account_lock(lock):
                self.assertTrue(ao3_client.ao3_run_active(lock))

            self.assertFalse(ao3_client.ao3_run_active(lock))

    def test_a_second_run_is_refused_and_told_who_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "run.lock"
            with ao3_client.account_lock(lock):
                with self.assertRaises(ao3_client.AnotherRunActive) as raised:
                    with ao3_client.account_lock(lock):
                        pass

        self.assertIn(f"pid {os.getpid()}", str(raised.exception))

    def test_an_error_inside_the_run_is_not_reported_as_a_busy_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / "run.lock"
            with self.assertRaises(ValueError):
                with ao3_client.account_lock(lock):
                    raise ValueError("unrelated")

            self.assertFalse(ao3_client.ao3_run_active(lock))


class RunWorkQueueTest(unittest.TestCase):
    def run_queue(self, script: dict[str, list[object]]):
        calls: list[str] = []
        sleeps: list[float] = []
        failures: list[str] = []
        recoveries: list[str] = []
        pending = {work_id: list(outcomes) for work_id, outcomes in script.items()}

        def process(work_id: str) -> ao3_client.WorkOutcome:
            calls.append(work_id)
            outcome = pending[work_id].pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return ao3_client.WorkOutcome("downloaded")

        logger = logging.getLogger("test.queue")
        logger.addHandler(logging.NullHandler())
        still_failed = ao3_client.run_work_queue(
            list(script),
            process,
            progress=ProgressTracker(logger, len(script), summary_every=0),
            cooldown=ao3_client.Cooldown(sleeps.append),
            recover_session=lambda: recoveries.append("recover"),
            record_failure=lambda work_id, error: failures.append(work_id),
        )
        return calls, sleeps, failures, recoveries, still_failed

    def test_a_failing_work_is_recorded_and_the_queue_carries_on(self):
        bad = ao3_client.UnexpectedHTML("odd page")
        calls, sleeps, failures, _, still_failed = self.run_queue({"1": [bad, bad], "2": ["ok"]})

        self.assertEqual(calls, ["1", "2", "1"])
        self.assertEqual(failures, ["1", "1"])
        self.assertEqual(list(still_failed), ["1"])
        self.assertEqual(sleeps, [])

    def test_the_retry_pass_recovers_a_work(self):
        _, _, _, _, still_failed = self.run_queue(
            {"1": [ao3_client.NetworkStopError("blip"), "ok"], "2": ["ok"]}
        )

        self.assertEqual(still_failed, {})

    def test_an_ao3_wide_problem_pauses_signs_in_again_and_retries_the_same_work(self):
        calls, sleeps, failures, recoveries, _ = self.run_queue(
            {"1": [ao3_client.RepeatedRateLimit("slow down"), "ok"], "2": ["ok"]}
        )

        self.assertEqual(calls, ["1", "1", "2"])
        self.assertEqual(sleeps, [ao3_client.COOLDOWN_SCHEDULE_SECONDS[0]])
        self.assertEqual(recoveries, ["recover"])
        self.assertEqual(failures, [])

    def test_the_same_ao3_wide_error_twice_is_blamed_on_the_work(self):
        blocked = ao3_client.AuthenticationFailure("only this work")
        calls, _, failures, _, _ = self.run_queue({"1": [blocked] * 4, "2": ["ok"]})

        self.assertEqual(calls[:3], ["1", "1", "2"])
        self.assertIn("1", failures)

    def test_consecutive_failures_trigger_a_pause(self):
        threshold = ao3_client.FAILURE_STREAK_COOLDOWN_THRESHOLD
        script = {str(n): [ao3_client.NetworkStopError("down"), "ok"] for n in range(threshold)}

        _, sleeps, _, recoveries, still_failed = self.run_queue(script)

        self.assertEqual(sleeps, [ao3_client.COOLDOWN_SCHEDULE_SECONDS[0]])
        self.assertEqual(recoveries, ["recover"])
        self.assertEqual(still_failed, {})

    def test_rejected_credentials_stop_the_run(self):
        with self.assertRaises(ao3_client.CredentialsRejected):
            self.run_queue({"1": [ao3_client.CredentialsRejected("wrong password")]})

    def test_an_interrupt_stops_the_run(self):
        with self.assertRaises(RunInterrupted):
            self.run_queue({"1": [RunInterrupted("signal")]})


if __name__ == "__main__":
    unittest.main()
