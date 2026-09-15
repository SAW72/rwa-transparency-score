"""Publish-ready Privacy Policy and Terms of Service for the Streamlit demo.

``PRIVACY.md`` / ``TERMS.md`` are the source of truth. Streamlit pages render
them in-app; Tornado handlers (installed with ``/health``) serve the same
bodies as HTML at ``GET /privacy`` and ``GET /terms`` so crawlers and
``curl`` see the text (Streamlit's SPA shell does not).
"""

from __future__ import annotations

import html
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTACT_EMAIL = "hello@stewardoftheking.com"
OPERATOR = "Steward of the King LLC"
LEGAL_SLUGS = ("privacy", "terms")
LEGAL_FILES = {
    "privacy": REPO_ROOT / "PRIVACY.md",
    "terms": REPO_ROOT / "TERMS.md",
}
PAGE_TITLES = {
    "privacy": "Privacy Policy — RWA Transparency Score (RAT)",
    "terms": "Terms of Service — RWA Transparency Score (RAT)",
}
# Draft / placeholder markers that must never ship.
FORBIDDEN_MARKERS = (
    "[CONTACT EMAIL TBD",
    "DRAFT for Spencer approval",
    "do not publish until unlocked",
)

_CODE_TOKEN = "\x00C{0}\x00"


def legal_path(slug: str) -> Path:
    try:
        return LEGAL_FILES[slug]
    except KeyError as exc:
        raise ValueError(f"unknown legal slug: {slug}") from exc


def legal_markdown(slug: str) -> str:
    """Return the publish-ready markdown body for ``privacy`` or ``terms``."""
    return legal_path(slug).read_text(encoding="utf-8")


def assert_publish_ready(text: str) -> None:
    lowered = text.lower()
    for marker in FORBIDDEN_MARKERS:
        if marker.lower() in lowered:
            raise ValueError(f"legal text is not publish-ready: {marker}")
    if CONTACT_EMAIL not in text:
        raise ValueError("legal text must include the contact email")
    if OPERATOR not in text:
        raise ValueError("legal text must name the operator")


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    codes: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        codes.append(f"<code>{match.group(1)}</code>")
        return _CODE_TOKEN.format(len(codes) - 1)

    escaped = re.sub(r"`([^`]+)`", _stash, escaped)
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)
    escaped = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", escaped)
    for index, snippet in enumerate(codes):
        escaped = escaped.replace(_CODE_TOKEN.format(index), snippet)
    return escaped


def markdown_to_html(md: str) -> str:
    """Small markdown subset used by PRIVACY.md / TERMS.md (no extra deps)."""
    lines = md.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.strip():
            index += 1
            continue
        if line.startswith("# "):
            out.append(f"<h1>{_inline(line[2:])}</h1>")
            index += 1
            continue
        if line.startswith("## "):
            out.append(f"<h2>{_inline(line[3:])}</h2>")
            index += 1
            continue
        if line.startswith("- "):
            items: list[str] = []
            while index < len(lines) and lines[index].startswith("- "):
                items.append(f"<li>{_inline(lines[index][2:])}</li>")
                index += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        para = [line]
        index += 1
        while index < len(lines) and lines[index].strip():
            nxt = lines[index]
            if nxt.startswith("# ") or nxt.startswith("## ") or nxt.startswith("- "):
                break
            para.append(nxt)
            index += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def render_legal_html(slug: str) -> str:
    """Standalone HTML document for ``GET /privacy`` or ``GET /terms``."""
    body_md = legal_markdown(slug)
    assert_publish_ready(body_md)
    title = html.escape(PAGE_TITLES[slug])
    article = markdown_to_html(body_md)
    other = "terms" if slug == "privacy" else "privacy"
    other_label = "Terms of Service" if other == "terms" else "Privacy Policy"
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <style>
    :root {{
      color-scheme: dark;
      --bg: #0E1117;
      --panel: #161B22;
      --text: #E6EDF3;
      --muted: #8B949E;
      --accent: #3DDC97;
    }}
    body {{
      margin: 0;
      font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.55;
    }}
    header, main {{
      max-width: 48rem;
      margin: 0 auto;
      padding: 1.25rem 1.25rem 0;
    }}
    header {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.75rem 1.25rem;
      align-items: baseline;
      justify-content: space-between;
      border-bottom: 1px solid var(--panel);
      padding-bottom: 1rem;
    }}
    a {{ color: var(--accent); }}
    nav {{ display: flex; gap: 1rem; }}
    main {{ padding-bottom: 3rem; }}
    h1 {{ font-size: 1.6rem; margin-top: 0.5rem; }}
    h2 {{ font-size: 1.15rem; margin-top: 1.75rem; }}
    code {{
      background: var(--panel);
      padding: 0.1rem 0.35rem;
      border-radius: 4px;
      font-size: 0.92em;
    }}
    ul {{ padding-left: 1.25rem; }}
    .note {{ color: var(--muted); font-size: 0.9rem; }}
  </style>
</head>
<body>
  <header>
    <a href="/">RAT Score</a>
    <nav>
      <a href="/privacy">Privacy Policy</a>
      <a href="/terms">Terms of Service</a>
    </nav>
  </header>
  <main>
    {article}
    <p class="note">Product Disclaimer lives in the app sidebar/footer and README.
    These pages do not replace it. Contact: {html.escape(CONTACT_EMAIL)}</p>
    <p class="note"><a href="/">Back to RAT Score</a> · <a href="/{other}">{other_label}</a></p>
  </main>
</body>
</html>
"""


def _legal_handler_class(slug: str) -> type:
    from tornado.web import RequestHandler

    class LegalHTMLHandler(RequestHandler):
        def set_default_headers(self) -> None:
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.set_header("Cache-Control", "no-store")

        def _write_page(self) -> None:
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.write(render_legal_html(slug))

        def get(self) -> None:
            self._write_page()

        def head(self) -> None:
            self._write_page()

    LegalHTMLHandler.__name__ = f"{slug.title()}HTMLHandler"
    return LegalHTMLHandler


def attach_legal_handlers(app: Any) -> None:
    """Register ``GET /privacy`` and ``GET /terms`` ahead of Streamlit's SPA."""
    if getattr(app, "_rwa_legal_attached", False):
        return
    try:
        from tornado.routing import PathMatches, Rule
    except Exception:  # noqa: BLE001
        return
    router = getattr(app, "default_router", None)
    rules = getattr(router, "rules", None) if router is not None else None
    for slug, pattern in (("privacy", r"/privacy/?"), ("terms", r"/terms/?")):
        handler = _legal_handler_class(slug)
        rule = Rule(PathMatches(pattern), handler)
        if rules is not None:
            rules.insert(0, rule)
        else:
            app.add_handlers(r".*", [(pattern, handler)])
    app._rwa_legal_attached = True
