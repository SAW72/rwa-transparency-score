"""Crawler-visible ``<head>`` for the Streamlit SPA shell.

Streamlit serves one static ``index.html`` whose ``<title>`` is the literal
word ``Streamlit``. ``st.set_page_config(page_title=...)`` only changes the tab
title after the WebSocket script runs, so crawlers that do not run that
session still see ``Streamlit``. This module serves the same shell for
``GET /`` with the title/description/canonical injected. Fail-safe: if the
packaged shell no longer contains the exact ``<title>Streamlit</title>`` marker
(Streamlit upgrade), the unmodified shell is served.
"""

from __future__ import annotations

import html
from pathlib import Path

SEO_TITLE = "RAT Score | RWA Transparency Score for Tokenized Stocks"
SEO_DESCRIPTION = (
    "Educational risk radar for tokenized stocks: scores issuers 0-100 on "
    "backing, proof of reserves, redemption, price, disclosure and basis."
)
# Short product notice. The Streamlit page, legal pages, no-JS shell, and
# share-card PNG all render this same sentence large and high-contrast.
EDUCATIONAL_DISCLAIMER = "Educational demo — not financial advice."
DISCLAIMER_BANNER_BG = "#F5C542"
DISCLAIMER_BANNER_INK = "#0D1117"
CANONICAL_URL = "https://rwa-transparency-score.onrender.com/"
_TITLE_MARKER = "<title>Streamlit</title>"
_NOSCRIPT_MARKER = "<noscript>You need to enable JavaScript to run this app.</noscript>"

ROBOTS_TXT = (
    "User-agent: *\nAllow: /\n"
    f"Sitemap: {CANONICAL_URL}sitemap.xml\n"
)
SITEMAP_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
    + "".join(
        f"  <url><loc>{CANONICAL_URL}{p}</loc></url>\n" for p in ("", "privacy", "terms")
    )
    + "</urlset>\n"
)


def _shell_path() -> Path:
    from streamlit import file_util

    return Path(file_util.get_static_dir()) / "index.html"


def render_index_html(shell: str) -> str:
    """Return ``shell`` with SEO tags injected, or unchanged if not recognised."""
    if _TITLE_MARKER not in shell:
        return shell
    t = html.escape(SEO_TITLE)
    d = html.escape(SEO_DESCRIPTION, quote=True)
    head = (
        f"<title>{t}</title>"
        f'<meta name="description" content="{d}"/>'
        f'<link rel="canonical" href="{CANONICAL_URL}"/>'
        f'<meta property="og:title" content="{t}"/>'
        f'<meta property="og:description" content="{d}"/>'
        f'<meta property="og:type" content="website"/>'
        f'<meta property="og:url" content="{CANONICAL_URL}"/>'
    )
    out = shell.replace(_TITLE_MARKER, head, 1)
    banner = html.escape(EDUCATIONAL_DISCLAIMER)
    return out.replace(
        _NOSCRIPT_MARKER,
        f"<noscript><h1>{t}</h1><p>{d}</p>"
        f'<p style="margin:1rem 0;padding:0.95rem 1.15rem;background:{DISCLAIMER_BANNER_BG};'
        f"color:{DISCLAIMER_BANNER_INK};font-size:1.75rem;font-weight:800;"
        f'line-height:1.25;text-align:center;">{banner}</p>'
        "<p>Not financial advice. This app needs JavaScript.</p></noscript>",
        1,
    )


def _handler_classes() -> dict[str, type]:
    from tornado.web import RequestHandler

    class IndexHandler(RequestHandler):
        def get(self) -> None:
            try:
                body = render_index_html(_shell_path().read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001 - never break the app shell
                self.redirect("/index.html", permanent=False)
                return
            self.set_header("Content-Type", "text/html; charset=UTF-8")
            self.set_header("Cache-Control", "no-cache")
            self.write(body)

        head = get

    class TextHandler(RequestHandler):
        def initialize(self, body: str, content_type: str) -> None:
            self._body, self._ct = body, content_type

        def get(self) -> None:
            self.set_header("Content-Type", self._ct)
            self.set_header("Cache-Control", "public, max-age=3600")
            self.write(self._body)

        head = get

    return {"index": IndexHandler, "text": TextHandler}


def seo_route_specs() -> list[tuple]:
    h = _handler_classes()
    return [
        (r"/", h["index"]),
        (r"/robots\.txt", h["text"], {"body": ROBOTS_TXT, "content_type": "text/plain; charset=UTF-8"}),
        (r"/sitemap\.xml", h["text"], {"body": SITEMAP_XML, "content_type": "application/xml; charset=UTF-8"}),
    ]
