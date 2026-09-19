"""Publish-ready Privacy Policy and Terms of Service (Tornado HTML routes)."""

from __future__ import annotations

from pathlib import Path

from rwa_score.legal import (
    CONTACT_EMAIL,
    FORBIDDEN_MARKERS,
    LEGAL_FILES,
    LEGAL_SLUGS,
    OPERATOR,
    PAGE_TITLES,
    assert_publish_ready,
    attach_legal_handlers,
    legal_markdown,
    markdown_to_html,
    render_legal_html,
)

ROOT = Path(__file__).resolve().parents[1]


def test_legal_markdown_files_are_publish_ready() -> None:
    for slug in LEGAL_SLUGS:
        path = LEGAL_FILES[slug]
        assert path.is_file()
        text = legal_markdown(slug)
        assert_publish_ready(text)
        assert CONTACT_EMAIL in text
        assert OPERATOR in text
        assert "Ohio" in text
        assert "September 14, 2026" in text
        lowered = text.lower()
        for marker in FORBIDDEN_MARKERS:
            assert marker.lower() not in lowered
        assert "[CONTACT EMAIL" not in text
        assert "TBD" not in text
        assert "DRAFT" not in text


def test_legal_html_serves_policy_body() -> None:
    for slug in LEGAL_SLUGS:
        page = render_legal_html(slug)
        md = legal_markdown(slug)
        assert page.startswith("<!DOCTYPE html>")
        assert PAGE_TITLES[slug] in page
        assert CONTACT_EMAIL in page
        assert OPERATOR in page
        assert "Disclaimer" in page
        assert "/privacy" in page and "/terms" in page
        html_body = markdown_to_html(md)
        assert "<h1>" in html_body
        assert "<h2>" in html_body
        assert "<strong>" in html_body
        assert "<ul>" in html_body


def test_streamlit_mpa_pages_dir_absent() -> None:
    """No pages/ tree — Streamlit 1.39 MPA v1 is what showed Page not found on Share."""
    pages = ROOT / "pages"
    assert not pages.exists()
    leftover = list(ROOT.glob("pages/*.py"))
    assert leftover == []


def test_legal_is_tornado_not_streamlit_mpa() -> None:
    health = (ROOT / "rwa_score" / "health.py").read_text(encoding="utf-8")
    legal = (ROOT / "rwa_score" / "legal.py").read_text(encoding="utf-8")
    assert "attach_legal_handlers" in health
    assert "_legal_handler_class" in legal
    assert 'r"/privacy/?"' in legal
    assert 'r"/terms/?"' in legal
    assert "pages/" not in legal or "no ``pages/``" in legal


def test_app_sidebar_and_footer_link_legal_pages() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert source.count("[Privacy Policy](/privacy)") >= 2
    assert source.count("[Terms of Service](/terms)") >= 2
    assert "st.write(DISCLAIMER)" in source
    assert "st.caption(DISCLAIMER)" in source


def test_readme_links_privacy_and_terms_and_keeps_disclaimer() -> None:
    from tests.test_app_import import EXPECTED_DISCLAIMER

    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert EXPECTED_DISCLAIMER in text
    assert "/privacy" in text
    assert "/terms" in text
    assert "PRIVACY.md" in text
    assert "TERMS.md" in text
    assert CONTACT_EMAIL in text
    assert OPERATOR in text
    assert "These do not replace the product Disclaimer" in text


def test_health_launcher_attaches_legal_routes() -> None:
    health = (ROOT / "rwa_score" / "health.py").read_text(encoding="utf-8")
    assert "attach_legal_handlers" in health
    assert "_attach_custom_routes" in health


def test_legal_http_handlers_return_200_with_body() -> None:
    from tornado.testing import AsyncHTTPTestCase
    from tornado.web import Application

    from rwa_score.legal import _legal_handler_class

    class _LegalHTTP(AsyncHTTPTestCase):
        def get_app(self) -> Application:
            return Application(
                [
                    (r"/privacy/?", _legal_handler_class("privacy")),
                    (r"/terms/?", _legal_handler_class("terms")),
                ]
            )

        def test_privacy(self) -> None:
            resp = self.fetch("/privacy")
            assert resp.code == 200
            assert b"text/html" in resp.headers["Content-Type"].encode()
            assert CONTACT_EMAIL.encode() in resp.body
            assert OPERATOR.encode() in resp.body
            assert b"Privacy Policy" in resp.body

        def test_terms(self) -> None:
            resp = self.fetch("/terms")
            assert resp.code == 200
            assert CONTACT_EMAIL.encode() in resp.body
            assert b"Terms of Service" in resp.body
            assert b"Ohio" in resp.body

    suite = _LegalHTTP("test_privacy")
    suite.run()
    suite = _LegalHTTP("test_terms")
    result = suite.run()
    assert result.wasSuccessful()


def test_attach_legal_handlers_is_idempotent() -> None:
    class _Router:
        def __init__(self) -> None:
            self.rules: list[object] = []

    class _App:
        def __init__(self) -> None:
            self.default_router = _Router()

    app = _App()
    attach_legal_handlers(app)
    assert len(app.default_router.rules) == 2
    attach_legal_handlers(app)
    assert len(app.default_router.rules) == 2
    assert app._rwa_legal_attached is True
