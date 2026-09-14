"""Publish-ready Privacy Policy and Terms of Service (pages + HTML routes)."""

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


def test_streamlit_legal_pages_exist_and_render_source() -> None:
    for slug in LEGAL_SLUGS:
        page = ROOT / "pages" / f"{slug}.py"
        source = page.read_text(encoding="utf-8")
        assert page.is_file()
        assert f'legal_markdown("{slug}")' in source
        assert "st.markdown" in source
        assert CONTACT_EMAIL in source or "CONTACT_EMAIL" in source
        assert "Disclaimer" in source


def test_app_sidebar_and_footer_link_legal_pages() -> None:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    assert 'st.page_link("pages/privacy.py", label="Privacy Policy")' in source
    assert 'st.page_link("pages/terms.py", label="Terms of Service")' in source
    assert "[Privacy Policy](/privacy)" in source
    assert "[Terms of Service](/terms)" in source
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
