"""Crawler-visible shell injection, robots.txt, and sitemap.xml."""

from __future__ import annotations

import html
import xml.etree.ElementTree as ET

import pytest

from rwa_score.seo import (
    CANONICAL_URL,
    ROBOTS_TXT,
    SEO_DESCRIPTION,
    SEO_TITLE,
    SITEMAP_XML,
    _TITLE_MARKER,
    render_index_html,
    seo_route_specs,
)

_NOSCRIPT = "<noscript>You need to enable JavaScript to run this app.</noscript>"
_SITEMAP_NS = "http://www.sitemaps.org/schemas/sitemap/0.9"
SHELL = (
    "<!doctype html><html><head>"
    f"{_TITLE_MARKER}"
    "</head><body>"
    f"{_NOSCRIPT}"
    '<div id="root"></div></body></html>'
)


def test_render_index_html_injects_title_description_canonical_og_and_noscript() -> None:
    out = render_index_html(SHELL)

    assert f"<title>{SEO_TITLE}</title>" in out
    assert f'<meta name="description" content="{SEO_DESCRIPTION}"/>' in out
    assert f'<link rel="canonical" href="{CANONICAL_URL}"/>' in out
    assert f'<meta property="og:title" content="{SEO_TITLE}"/>' in out
    assert f'<meta property="og:description" content="{SEO_DESCRIPTION}"/>' in out
    assert '<meta property="og:type" content="website"/>' in out
    assert f'<meta property="og:url" content="{CANONICAL_URL}"/>' in out
    assert f"<noscript><h1>{SEO_TITLE}</h1><p>{SEO_DESCRIPTION}</p>" in out
    assert "Not financial advice. This app needs JavaScript." in out
    assert "Educational demo — not financial advice." in out
    assert "background:#F5C542" in out
    assert "font-size:1.75rem" in out
    assert _TITLE_MARKER not in out
    assert "You need to enable JavaScript to run this app." not in out
    assert '<div id="root"></div>' in out


def test_render_index_html_escapes_title_and_description(monkeypatch: pytest.MonkeyPatch) -> None:
    title = "RAT <Score> & Co"
    description = 'Say "hello" & <b>'
    monkeypatch.setattr("rwa_score.seo.SEO_TITLE", title)
    monkeypatch.setattr("rwa_score.seo.SEO_DESCRIPTION", description)

    out = render_index_html(SHELL)
    escaped_title = html.escape(title)
    escaped_description = html.escape(description, quote=True)

    assert f"<title>{escaped_title}</title>" in out
    assert f"<h1>{escaped_title}</h1>" in out
    assert f'<meta name="description" content="{escaped_description}"/>' in out
    assert f'<meta property="og:title" content="{escaped_title}"/>' in out
    assert f'<meta property="og:description" content="{escaped_description}"/>' in out
    assert "<title>RAT <Score>" not in out
    assert 'content="Say "hello"' not in out
    assert "content=\"Say &quot;hello&quot; &amp; &lt;b&gt;\"" in out


def test_render_index_html_unchanged_when_title_marker_absent() -> None:
    """Fail-safe: a shell without the Streamlit title marker is returned as-is.

    The noscript marker is present on purpose. Dropping the title-marker check
    would still rewrite that block.
    """
    shell = (
        "<!doctype html><html><head><title>Already set</title></head><body>"
        f"{_NOSCRIPT}"
        '<div id="root"></div></body></html>'
    )
    assert "<title>Streamlit</title>" not in shell
    assert render_index_html(shell) == shell


def test_render_index_html_injects_only_once() -> None:
    doubled = SHELL.replace(_TITLE_MARKER, _TITLE_MARKER + _TITLE_MARKER, 1)
    out = render_index_html(doubled)
    assert out.count(f"<title>{SEO_TITLE}</title>") == 1
    assert out.count("<title>Streamlit</title>") == 1
    assert out.count("<noscript>") == 1

    once = render_index_html(SHELL)
    assert render_index_html(once) == once
    assert once.count(f"<title>{SEO_TITLE}</title>") == 1
    assert "<title>Streamlit</title>" not in once


def test_render_index_html_injects_packaged_streamlit_shell() -> None:
    from rwa_score.seo import _shell_path

    shell = _shell_path().read_text(encoding="utf-8")
    assert "<title>Streamlit</title>" in shell
    out = render_index_html(shell)
    assert out != shell
    assert out.count("<title>") == 1
    assert f"<title>{SEO_TITLE}</title>" in out
    assert "<title>Streamlit</title>" not in out
    assert f'<link rel="canonical" href="{CANONICAL_URL}"/>' in out


def test_robots_txt_and_sitemap_xml_bodies() -> None:
    assert "Sitemap: " in ROBOTS_TXT
    sitemap_lines = [line for line in ROBOTS_TXT.splitlines() if line.startswith("Sitemap:")]
    assert sitemap_lines == [f"Sitemap: {CANONICAL_URL}sitemap.xml"]
    assert ROBOTS_TXT.startswith("User-agent: *\nAllow: /\n")

    root = ET.fromstring(SITEMAP_XML)
    assert root.tag == f"{{{_SITEMAP_NS}}}urlset"
    locs = [node.text for node in root.findall(f"{{{_SITEMAP_NS}}}url/{{{_SITEMAP_NS}}}loc")]
    assert locs == [
        CANONICAL_URL,
        f"{CANONICAL_URL}privacy",
        f"{CANONICAL_URL}terms",
    ]
    assert locs[0].endswith("/")
    assert locs[1].endswith("/privacy")
    assert locs[2].endswith("/terms")
    assert SITEMAP_XML.startswith('<?xml version="1.0" encoding="UTF-8"?>')


def test_seo_route_specs_registered_ahead_of_health_and_legal() -> None:
    from tornado.web import RequestHandler, URLSpec

    from rwa_score.health import _custom_route_specs

    specs = seo_route_specs()
    patterns = [spec[0] for spec in specs]
    assert patterns == [r"/", r"/robots\.txt", r"/sitemap\.xml"]
    assert specs[1][2]["body"] == ROBOTS_TXT
    assert specs[1][2]["content_type"].startswith("text/plain")
    assert specs[2][2]["body"] == SITEMAP_XML
    assert specs[2][2]["content_type"].startswith("application/xml")

    class _Probe(RequestHandler):
        def get(self) -> None:
            return None

    root_spec = URLSpec(r"/", _Probe)
    assert root_spec.regex.match("/") is not None
    assert root_spec.regex.match("/index.html") is None
    assert root_spec.regex.match("/health") is None

    custom = [spec[0] for spec in _custom_route_specs()]
    assert custom[:3] == [r"/", r"/robots\.txt", r"/sitemap\.xml"]
    assert custom[3:] == [r"/health/?", r"/privacy/?", r"/terms/?"]
    assert custom.index(r"/") < custom.index(r"/health/?")
    assert custom.index(r"/robots\.txt") < custom.index(r"/privacy/?")
    assert custom.index(r"/sitemap\.xml") < custom.index(r"/terms/?")


def test_page_title_uses_seo_title() -> None:
    import app as demo_app

    assert demo_app.PAGE_TITLE == SEO_TITLE
    assert demo_app.PAGE_TITLE == "RAT Score | RWA Transparency Score for Tokenized Stocks"


def _run_http_case(case_cls: type, method_name: str) -> None:
    suite = case_cls(method_name)
    result = suite.run()
    assert result.wasSuccessful(), f"{method_name} failed: {result.failures} {result.errors}"


def test_handlers_serve_seo_routes_and_leave_index_html_alone() -> None:
    """GET/HEAD / inject the shell. /index.html stays on the later static route."""
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application, RequestHandler

    from rwa_score.health import _custom_route_specs

    class StaticIndex(RequestHandler):
        def get(self) -> None:
            self.set_header("Content-Type", "text/html; charset=UTF-8")
            self.write("<title>Streamlit</title>static-index")

        head = get

    class SpaCatchAll(RequestHandler):
        def get(self, *_args: object) -> None:
            self.set_header("Content-Type", "text/html")
            self.write("<!doctype html>SPA")

    class _SeoHTTP(AsyncHTTPTestCase):
        def get_app(self) -> Application:
            return Application(
                [
                    *_custom_route_specs(),
                    (r"/index\.html", StaticIndex),
                    (r"/(.*)", SpaCatchAll),
                ]
            )

        def test_get_head_robots_sitemap_and_index_html(self) -> None:
            page = self.fetch("/")
            assert page.code == 200
            assert "text/html" in page.headers["Content-Type"]
            body = page.body.decode()
            assert f"<title>{SEO_TITLE}</title>" in body
            assert f'<meta name="description" content="{SEO_DESCRIPTION}"/>' in body
            assert f'<link rel="canonical" href="{CANONICAL_URL}"/>' in body
            assert "<title>Streamlit</title>" not in body

            head = self.fetch("/", method="HEAD")
            assert head.code == 200
            assert head.body == b""
            assert "text/html" in head.headers["Content-Type"]
            assert int(head.headers["Content-Length"]) > 0

            robots = self.fetch("/robots.txt")
            assert robots.code == 200
            assert "text/plain" in robots.headers["Content-Type"]
            assert robots.body.decode() == ROBOTS_TXT

            sitemap = self.fetch("/sitemap.xml")
            assert sitemap.code == 200
            assert "application/xml" in sitemap.headers["Content-Type"]
            assert sitemap.body.decode() == SITEMAP_XML

            index = self.fetch("/index.html", follow_redirects=False)
            assert index.code == 200
            assert index.headers.get("Location") is None
            assert b"static-index" in index.body
            assert SEO_TITLE.encode() not in index.body

            other = self.fetch("/not-a-real-route")
            assert other.code == 200
            assert b"SPA" in other.body

    _run_http_case(_SeoHTTP, "test_get_head_robots_sitemap_and_index_html")


def test_shell_read_failure_redirects_to_index_html_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application, RequestHandler

    missing = tmp_path / "missing-shell.html"
    monkeypatch.setattr("rwa_score.seo._shell_path", lambda: missing)

    class StaticIndex(RequestHandler):
        def get(self) -> None:
            self.set_header("Content-Type", "text/html; charset=UTF-8")
            self.write("<title>Streamlit</title>static-index")

    class _RedirectHTTP(AsyncHTTPTestCase):
        def get_app(self) -> Application:
            return Application(
                [
                    *seo_route_specs(),
                    (r"/index\.html", StaticIndex),
                ]
            )

        def test_redirect_does_not_loop(self) -> None:
            first = self.fetch("/", follow_redirects=False)
            assert first.code == 302
            location = first.headers["Location"]
            assert location.endswith("/index.html")

            second = self.fetch("/index.html", follow_redirects=False)
            assert second.code == 200
            assert second.headers.get("Location") is None
            assert b"static-index" in second.body
            assert b"<title>Streamlit</title>" in second.body

    _run_http_case(_RedirectHTTP, "test_redirect_does_not_loop")
