"""Shared fakes and fixtures: a stand-in AO3, sample EPUBs, and scan reports."""

import io
from pathlib import Path
import zipfile

from ao3archiver import ao3_client, backfill
from ao3archiver.credentials import AO3Credentials


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
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
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


def sample_report() -> backfill.ScanReport:
    return backfill.ScanReport(
        library="/library",
        library_uuid="library-uuid",
        generated_at="2026-01-01T00:00:00+00:00",
        book_count=1,
        epub_count=1,
        existing_ao3_identifier_count=0,
        mappings=(
            backfill.EpubMapping(
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

    client = ao3_client.AO3DownloadClient(
        credentials, delay_seconds=10.0, sleep_fn=sleep, session=session, monotonic_fn=lambda: clock[0]
    )
    return client, sleeps
