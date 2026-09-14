import io
import logging
from pathlib import Path
import tempfile
import unittest
import zipfile

import ao3_backfill
import download_client
from credentials import AO3Credentials
from run_log import ProgressTracker, RunInterrupted


CREDENTIALS = AO3Credentials("reader", "hunter22", "test")
TOKEN_URL = "https://archiveofourown.org/token_dispenser.json"


def work_page(work_id: str, *, epub_href: str | None = None, kudos: str = "68") -> str:
    href = epub_href or f"/downloads/{work_id}/Example.epub?updated_at=1700000000&amp;view=full"
    return f"""<html><head><title>Example - Author | Archive of Our Own</title></head>
<body class="logged-in"><div id="main" class="works-show region">
<ul class="work navigation actions"><li class="download"><a href="#">Download</a><ul>
<li><a href="/downloads/{work_id}/Example.azw3?updated_at=1700000000">AZW3</a></li>
<li><a href="{href}">EPUB</a></li></ul></li></ul>
<div id="workskin"><div class="preface group"><h2 class="title heading">Example</h2></div></div>
<dl class="stats">
<dt class="published">Published:</dt><dd class="published">2018-06-01</dd>
<dt class="status">Completed:</dt><dd class="status">2018-09-14</dd>
<dt class="words">Words:</dt><dd class="words">1,315</dd>
<dt class="chapters">Chapters:</dt><dd class="chapters">3/3</dd>
<dt class="comments">Comments:</dt><dd class="comments">4</dd>
<dt class="kudos">Kudos:</dt><dd class="kudos">{kudos}</dd>
<dt class="bookmarks">Bookmarks:</dt><dd class="bookmarks"><a>6</a></dd>
<dt class="hits">Hits:</dt><dd class="hits">1,040</dd>
</dl><dd class="category tags"><ul><li><a>F/F</a></li></ul></dd>
</div></body></html>"""


MYSTERY_PAGE = """<html><head><title>Mystery Work | Archive of Our Own</title></head>
<body class="logged-in"><div id="main" class="works-show region">
<p class="notice">This work is part of an ongoing challenge and will be revealed soon!
You can find details here: <a href="/collections/Secret_Fest">Secret Fest</a></p>
</div></body></html>"""


def epub_bytes(body: str = "the quick brown fox jumped over the lazy dog. " * 60) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as epub:
        epub.writestr("mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED)
        epub.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        epub.writestr(
            "content.opf",
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Example</dc:title>'
            "<dc:creator>Author</dc:creator><dc:description>Summary.</dc:description></metadata>"
            '<manifest><item id="c1" href="c1.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c1"/></spine></package>',
        )
        epub.writestr("c1.xhtml", f"<html xmlns='http://www.w3.org/1999/xhtml'><body><p>{body}</p></body></html>")
    return buffer.getvalue()


class Reply:
    def __init__(self, status: int = 200, text: str = "", *, content: bytes | None = None,
                 headers: dict[str, str] | None = None, url: str | None = None,
                 json_value: object | None = None) -> None:
        self.status_code = status
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self.headers = headers or {}
        self.url = url
        self.history: list[Reply] = []
        self._json = json_value

    def json(self) -> object:
        return self._json


def html(status: int = 200, text: str = "", **kwargs) -> Reply:
    return Reply(status, text, headers={"Content-Type": "text/html; charset=utf-8", **kwargs.pop("headers", {})}, **kwargs)


def epub_reply(body: str | None = None) -> Reply:
    return Reply(200, "", content=epub_bytes(body) if body else epub_bytes(),
                 headers={"Content-Type": "application/epub+zip"})


def redirect(location: str, **headers: str) -> Reply:
    return Reply(302, "", headers={"Location": location, **headers})


class FakeJar(list):
    pass


class RoutedSession:
    """A fake AO3: each URL answers from its own queue, and every request is recorded."""

    def __init__(self, routes: dict[str, list[Reply]] | None = None, posts: list[Reply] | None = None) -> None:
        self.routes = {url: list(replies) for url, replies in (routes or {}).items()}
        self.posts = list(posts or [])
        self.requests: list[str] = []
        self.headers: dict[str, str] = {}
        self.cookies = FakeJar()
        self.closed = False

    def add(self, url: str, *replies: Reply) -> None:
        self.routes.setdefault(url, []).extend(replies)

    def get(self, url: str, timeout: float | None = None, allow_redirects: bool = True) -> Reply:
        self.requests.append(f"GET {url}")
        queue = self.routes.get(url)
        if not queue:
            raise AssertionError(f"unexpected GET {url}")
        reply = queue.pop(0)
        if reply.url is None:
            reply.url = url
        return reply

    def post(self, url: str, data: object = None, timeout: float | None = None, allow_redirects: bool = True) -> Reply:
        self.requests.append(f"POST {url}")
        if not self.posts:
            raise AssertionError(f"unexpected POST {url}")
        return self.posts.pop(0)

    def close(self) -> None:
        self.closed = True


def sign_in_replies(session: RoutedSession) -> None:
    session.add(TOKEN_URL, Reply(200, "", json_value={"token": "rotating"}))
    session.posts.append(html(200, '<body class="logged-in"><a href="/users/logout">Log out</a></body>',
                              url="https://archiveofourown.org/users/reader"))


def serve_work(session: RoutedSession, work_id: str, page: str | None = None) -> None:
    session.add(f"https://archiveofourown.org/works/{work_id}", redirect(f"/works/{work_id}/chapters/9"))
    session.add(f"https://archiveofourown.org/works/{work_id}/chapters/9", html(200, page or work_page(work_id)))


def make_client(session: RoutedSession, credentials: AO3Credentials | None = CREDENTIALS):
    clock = [1000.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(round(seconds, 3))
        clock[0] += seconds

    client = download_client.AO3DownloadClient(
        credentials, delay_seconds=10.0, sleep_fn=sleep, session=session, monotonic_fn=lambda: clock[0]
    )
    return client, sleeps


class FindEpubDownloadUrlTest(unittest.TestCase):
    def test_the_page_link_is_used_with_its_cache_buster_unescaped(self):
        url = download_client.find_epub_download_url(work_page("111"), "111")

        self.assertEqual(
            url, "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full"
        )

    def test_an_absolute_link_on_the_download_host_is_accepted(self):
        page = work_page("111", epub_href="https://download.archiveofourown.org/downloads/111/Example.epub")

        self.assertEqual(
            download_client.find_epub_download_url(page, "111"),
            "https://download.archiveofourown.org/downloads/111/Example.epub",
        )

    def test_a_link_to_a_foreign_host_is_ignored(self):
        page = work_page("111", epub_href="https://evil.example/downloads/111/Example.epub")

        self.assertEqual(
            download_client.find_epub_download_url(page, "111"),
            "https://archiveofourown.org/downloads/111/111.epub",
        )

    def test_a_link_for_a_different_work_is_not_used(self):
        page = work_page("999")

        self.assertEqual(
            download_client.find_epub_download_url(page, "111"),
            "https://archiveofourown.org/downloads/111/111.epub",
        )


class RequestPacerTest(unittest.TestCase):
    def pacer(self):
        clock = [0.0]
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            clock[0] += seconds

        return download_client.RequestPacer(10.0, sleep_fn=sleep, monotonic_fn=lambda: clock[0]), clock, sleeps

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

        self.assertEqual(client.session.headers["User-Agent"], download_client.USER_AGENT)


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

        with self.assertRaises(ao3_backfill.NetworkStopError):
            client.download_epub(self.URL, "111")

    def test_an_html_page_instead_of_an_epub_names_the_page(self):
        client, _, _ = self.download(html(200, "<title>Please try again later | Archive of Our Own</title>"))

        with self.assertRaises(ao3_backfill.UnexpectedHTML) as raised:
            client.download_epub(self.URL, "111")

        self.assertIn("Please try again later", str(raised.exception))

    def test_one_cloudflare_challenge_is_waited_out(self):
        challenge = html(503, "<title>Just a moment...</title>")
        client, _, _ = self.download(challenge, epub_reply())

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))

    def test_a_repeated_cloudflare_challenge_stops_the_work(self):
        challenge = "<title>Just a moment...</title>"
        client, _, _ = self.download(html(503, challenge), html(503, challenge))

        with self.assertRaises(ao3_backfill.RepeatedCloudflare):
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

        with self.assertRaises(ao3_backfill.AuthenticationFailure):
            client.download_epub(self.URL, "111")

    def test_story_text_saying_please_log_in_is_not_mistaken_for_a_login_page(self):
        client, _, _ = self.download(epub_reply("Please log in, she said, or you must log in to continue."))

        self.assertTrue(client.download_epub(self.URL, "111").startswith(b"PK"))


class AccountLockTest(unittest.TestCase):
    def test_a_download_refuses_to_start_while_the_backfill_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ao3-cache.jsonl"
            with ao3_backfill.CacheStore(path).fetch_lock():
                self.assertTrue(download_client.ao3_run_active(path))
                with self.assertRaises(download_client.AnotherRunActive):
                    with download_client.exclusive_ao3_run(path):
                        pass

    def test_the_lock_is_released_when_a_run_ends(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ao3-cache.jsonl"
            with download_client.exclusive_ao3_run(path):
                pass

            self.assertFalse(download_client.ao3_run_active(path))

    def test_an_error_inside_the_run_is_not_reported_as_a_busy_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ao3-cache.jsonl"
            with self.assertRaises(ao3_backfill.BackfillError):
                with download_client.exclusive_ao3_run(path):
                    raise ao3_backfill.BackfillError("unrelated")


class RunWorkQueueTest(unittest.TestCase):
    def run_queue(self, script: dict[str, list[object]]):
        calls: list[str] = []
        sleeps: list[float] = []
        failures: list[str] = []
        recoveries: list[str] = []
        pending = {work_id: list(outcomes) for work_id, outcomes in script.items()}

        def process(work_id: str) -> download_client.WorkOutcome:
            calls.append(work_id)
            outcome = pending[work_id].pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return download_client.WorkOutcome("downloaded")

        logger = logging.getLogger("test.queue")
        logger.addHandler(logging.NullHandler())
        still_failed = download_client.run_work_queue(
            list(script),
            process,
            progress=ProgressTracker(logger, len(script), summary_every=0),
            cooldown=ao3_backfill._Cooldown(sleeps.append),
            recover_session=lambda: recoveries.append("recover"),
            record_failure=lambda work_id, error: failures.append(work_id),
        )
        return calls, sleeps, failures, recoveries, still_failed

    def test_a_failing_work_is_recorded_and_the_queue_carries_on(self):
        bad = ao3_backfill.UnexpectedHTML("odd page")
        calls, sleeps, failures, _, still_failed = self.run_queue({"1": [bad, bad], "2": ["ok"]})

        self.assertEqual(calls, ["1", "2", "1"])
        self.assertEqual(failures, ["1", "1"])
        self.assertEqual(list(still_failed), ["1"])
        self.assertEqual(sleeps, [])

    def test_the_retry_pass_recovers_a_work(self):
        _, _, _, _, still_failed = self.run_queue(
            {"1": [ao3_backfill.NetworkStopError("blip"), "ok"], "2": ["ok"]}
        )

        self.assertEqual(still_failed, {})

    def test_an_ao3_wide_problem_pauses_signs_in_again_and_retries_the_same_work(self):
        calls, sleeps, failures, recoveries, _ = self.run_queue(
            {"1": [ao3_backfill.RepeatedRateLimit("slow down"), "ok"], "2": ["ok"]}
        )

        self.assertEqual(calls, ["1", "1", "2"])
        self.assertEqual(sleeps, [ao3_backfill.COOLDOWN_SCHEDULE_SECONDS[0]])
        self.assertEqual(recoveries, ["recover"])
        self.assertEqual(failures, [])

    def test_the_same_ao3_wide_error_twice_is_blamed_on_the_work(self):
        blocked = ao3_backfill.AuthenticationFailure("only this work")
        calls, _, failures, _, _ = self.run_queue({"1": [blocked] * 4, "2": ["ok"]})

        self.assertEqual(calls[:3], ["1", "1", "2"])
        self.assertIn("1", failures)

    def test_consecutive_failures_trigger_a_pause(self):
        threshold = ao3_backfill.FAILURE_STREAK_COOLDOWN_THRESHOLD
        script = {str(n): [ao3_backfill.NetworkStopError("down"), "ok"] for n in range(threshold)}

        _, sleeps, _, recoveries, still_failed = self.run_queue(script)

        self.assertEqual(sleeps, [ao3_backfill.COOLDOWN_SCHEDULE_SECONDS[0]])
        self.assertEqual(recoveries, ["recover"])
        self.assertEqual(still_failed, {})

    def test_rejected_credentials_stop_the_run(self):
        with self.assertRaises(ao3_backfill.CredentialsRejected):
            self.run_queue({"1": [ao3_backfill.CredentialsRejected("wrong password")]})

    def test_an_interrupt_stops_the_run(self):
        with self.assertRaises(RunInterrupted):
            self.run_queue({"1": [RunInterrupted("signal")]})


if __name__ == "__main__":
    unittest.main()
