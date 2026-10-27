"""Signed, timestamped RAT Score share cards (PNG) plus optional X post.

HMAC-SHA256 over canonical JSON of the score breakdown + timestamp.
The signing secret is read from ``SCORE_CARD_SIGNING_SECRET`` — never hardcoded.
Missing secret or X credentials must not crash the UI.
"""

from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import load_dotenv

from .scorer import PILLARS, WEIGHTS
from .x_client import X_POST_UNAVAILABLE_MESSAGE, user_facing_x_skip_message

SIGNING_SECRET_ENV = "SCORE_CARD_SIGNING_SECRET"
UNSIGNED_FINGERPRINT = "UNSIGNED"
FINGERPRINT_LEN = 16
PNG_BUILD_FAILED_PREFIX = "Score card generation failed:"
# Per-request X HTTP timeout. 8s was too tight once simple-upload can
# fall back to initialize/append/finalize + tweet create.
X_SHARE_UI_TIMEOUT = 20.0
# X wraps every URL as a t.co link and counts that link as 23 characters.
X_TCO_URL_WEIGHT = 23
SHARE_CAPTION_LIMIT = 280
CARD_WIDTH = 1200
CARD_HEIGHT = 675
FOOTER_MARGIN_X = 56
FOOTER_MIN_GAP = 16.0
FOOTER_FONT_MAX = 16
FOOTER_FONT_MIN = 11
FOOTER_TOP_INSET = 62
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
MONOGRAM_CANDIDATES = (
    ASSETS_DIR / "rat-monogram.png",
    ASSETS_DIR / "rat-icon-192.png",
)
FONTS_DIR = ASSETS_DIR / "fonts"

BAND_COLORS = {
    "GREEN": (61, 220, 151),
    "YELLOW": (245, 197, 66),
    "ORANGE": (240, 138, 36),
    "RED": (229, 72, 77),
}

_FONT_PAIRS = (
    (
        FONTS_DIR / "Inter-Regular.ttf",
        FONTS_DIR / "Inter-Bold.ttf",
    ),
    (
        Path("/usr/share/fonts/truetype/macos/Inter-Regular.ttf"),
        Path("/usr/share/fonts/truetype/macos/Inter-Bold.ttf"),
    ),
    (
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"),
    ),
    (
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"),
        Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ),
)


def utc_timestamp(now: datetime | None = None) -> str:
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


def signing_secret_from_env() -> str:
    load_dotenv()
    return (os.getenv(SIGNING_SECRET_ENV) or "").strip()


def canonical_score_payload(report: dict[str, Any], timestamp: str) -> dict[str, Any]:
    """Stable, signed subset: ticker, score, band, pillars, timestamp."""
    raw_subs = report.get("subscores") or {}
    subscores: dict[str, float | None] = {}
    for key in WEIGHTS:
        raw = raw_subs.get(key) if key in raw_subs else None
        if raw is None:
            subscores[key] = None
        else:
            subscores[key] = round(float(raw), 1)
    return {
        "band": str(report.get("band") or ""),
        "score": round(float(report.get("score") or 0.0), 1),
        "subscores": subscores,
        "ticker": str(report.get("ticker") or "").upper(),
        "timestamp": timestamp,
    }


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def sign_canonical(canonical: str, secret: str) -> str:
    if not secret:
        return ""
    digest = hmac.new(secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256)
    return digest.hexdigest()


def signature_fingerprint(signature: str) -> str:
    if not signature:
        return UNSIGNED_FINGERPRINT
    return signature[:FINGERPRINT_LEN]


def verify_signature(canonical: str, signature: str, secret: str) -> bool:
    if not secret or not signature:
        return False
    expected = sign_canonical(canonical, secret)
    return hmac.compare_digest(expected, signature)


def caption_weighted_length(text: str, url: str | None = None) -> int:
    """Length against the X post cap. A trailing URL counts as 23 characters."""
    if url is None:
        from . import seo

        url = seo.CANONICAL_URL
    url = (url or "").strip()
    if url and text.endswith(url):
        return len(text) - len(url) + X_TCO_URL_WEIGHT
    return len(text)


def _assemble_caption(parts: list[str], url: str) -> str:
    body = [part for part in parts if part]
    if url:
        body.append(url)
    return "\n".join(body)


def _shrink_caption_part(part: str, overflow: int) -> str:
    """Drop ``overflow`` weighted characters from the end of one caption line."""
    if overflow <= 0 or not part:
        return part
    if len(part) <= overflow:
        return ""
    keep = len(part) - overflow
    if keep <= 1:
        return ""
    trimmed = part[: keep - 1].rstrip(" ·")
    if not trimmed:
        return ""
    return trimmed + "…"


def share_caption(payload: dict[str, Any], fingerprint: str) -> str:
    ticker = payload.get("ticker") or "?"
    score = payload.get("score")
    band = payload.get("band") or "?"
    timestamp = payload.get("timestamp") or ""
    subs = payload.get("subscores") or {}
    pillar_bits = []
    for key in WEIGHTS:
        if key in subs:
            label = PILLARS.get(key, {}).get("label", key)
            short = label.split()[0]
            raw = subs.get(key)
            if raw is None:
                pillar_bits.append(f"{short} not available")
            else:
                pillar_bits.append(f"{short} {float(raw):.0f}")
    from . import seo

    url = (seo.CANONICAL_URL or "").strip()
    parts = [
        f"RAT Score · {ticker} · {score} {band}",
        " · ".join(pillar_bits),
        f"{timestamp} · sig {fingerprint}",
        "Not financial advice",
    ]
    text = _assemble_caption(parts, url)
    # Shrink pillars, then the timestamp line, then the headline. Keep the
    # disclaimer and the URL. The URL is weighted as a t.co link.
    for _ in range(4):
        weight = caption_weighted_length(text, url)
        if weight <= SHARE_CAPTION_LIMIT:
            return text
        overflow = weight - SHARE_CAPTION_LIMIT
        for index in (1, 2, 0):
            if not parts[index]:
                continue
            parts[index] = _shrink_caption_part(parts[index], overflow)
            text = _assemble_caption(parts, url)
            break
        else:
            break
    if caption_weighted_length(text, url) <= SHARE_CAPTION_LIMIT:
        return text
    if url and caption_weighted_length(url, url) <= SHARE_CAPTION_LIMIT:
        return url
    return parts[3][:SHARE_CAPTION_LIMIT]


@dataclass
class ScoreCardShare:
    png_bytes: bytes
    payload: dict[str, Any]
    canonical: str
    signature: str
    fingerprint: str
    signed: bool
    timestamp: str
    caption: str
    filename: str
    x_posted: bool = False
    x_url: str | None = None
    x_message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _load_font(size: int, *, bold: bool = False):
    from PIL import ImageFont

    for regular, bold_path in _FONT_PAIRS:
        path = bold_path if bold else regular
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), size)
            except OSError:
                continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _load_monogram(size: int):
    from PIL import Image

    for path in MONOGRAM_CANDIDATES:
        if path.is_file():
            try:
                img = Image.open(path).convert("RGBA")
                return img.resize((size, size), Image.Resampling.LANCZOS)
            except Exception:  # noqa: BLE001 — branding is optional
                continue
    return None


def _rounded_rect(draw, box, radius: int, fill) -> None:
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def score_card_site_label(url: str | None = None) -> str:
    """Public host for the card footer. Scheme, path, and port are stripped.

    Reads ``seo.CANONICAL_URL`` at call time unless ``url`` is passed.
    """
    if url is None:
        from . import seo

        url = seo.CANONICAL_URL
    return _host_from_url(url)


def _host_from_url(url: str) -> str:
    text = (url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else f"//{text}")
    return (parsed.hostname or "").strip().strip("/")


def footer_text(timestamp: str, fingerprint: str, *, signed: bool) -> tuple[str, str, str]:
    """Return footer segments: time + signature, site host, disclaimer."""
    sig_label = (
        f"sig {fingerprint}" if signed else "unsigned — set SCORE_CARD_SIGNING_SECRET to sign"
    )
    left = f"{timestamp}  ·  {sig_label}"
    return left, score_card_site_label(), "Not financial advice  ·  RAT Score"


def _text_ink(draw, text: str, font) -> tuple[float, float]:
    """Left bearing and ink width for ``text`` drawn at x=0."""
    if not text:
        return 0.0, 0.0
    box = draw.textbbox((0, 0), text, font=font)
    return float(box[0]), float(box[2] - box[0])


def layout_footer_segments(
    draw,
    font,
    segments: list[str],
    *,
    width: int = CARD_WIDTH,
    margin: int = FOOTER_MARGIN_X,
    min_gap: float = FOOTER_MIN_GAP,
) -> list[tuple[str, float]] | None:
    """Place footer segments on one line. None when they cannot fit with ``min_gap``."""
    texts = [segment for segment in segments if segment]
    if not texts:
        return []
    bearings: list[float] = []
    inks: list[float] = []
    for text in texts:
        bearing, ink = _text_ink(draw, text, font)
        bearings.append(bearing)
        inks.append(ink)
    avail = float(width - 2 * margin)
    gaps = len(texts) - 1
    if sum(inks) + gaps * min_gap > avail + 0.01:
        return None
    if len(texts) == 1:
        return [(texts[0], float(margin) - bearings[0])]
    extra = avail - sum(inks)
    gap = extra / gaps
    placed: list[tuple[str, float]] = []
    ink_left = float(margin)
    for index, (text, bearing, ink) in enumerate(zip(texts, bearings, inks)):
        if index == len(texts) - 1:
            ink_left = float(width - margin) - ink
        placed.append((text, ink_left - bearing))
        ink_left += ink + gap
    return placed


def _ellipsize_to_ink(draw, font, text: str, max_ink: float) -> str:
    if not text or max_ink <= 0:
        return ""
    if _text_ink(draw, text, font)[1] <= max_ink:
        return text
    ellipsis = "…"
    if _text_ink(draw, ellipsis, font)[1] > max_ink:
        return ""
    best = ellipsis
    lo = 0
    hi = len(text)
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if _text_ink(draw, candidate, font)[1] <= max_ink:
            best = candidate
            lo = mid + 1
        else:
            hi = mid - 1
    return best


def _compress_footer(draw, font, left: str, site: str, right: str) -> list[tuple[str, float]]:
    """Fit a too-wide footer by shortening the left segment, then the host."""
    disclaimer = "Not financial advice"
    right_options = [right]
    if disclaimer in right and right != disclaimer:
        right_options.append(disclaimer)
    for right_text in right_options:
        placed = layout_footer_segments(draw, font, [left, site, right_text])
        if placed is not None:
            return placed

    right_text = right_options[-1]
    others = [part for part in (site, right_text) if part]
    other_ink = sum(_text_ink(draw, part, font)[1] for part in others)
    gaps = len(others)
    avail = float(CARD_WIDTH - 2 * FOOTER_MARGIN_X)
    max_left = avail - other_ink - gaps * FOOTER_MIN_GAP
    shrunk_left = _ellipsize_to_ink(draw, font, left, max_left)
    placed = layout_footer_segments(draw, font, [shrunk_left, site, right_text])
    if placed is not None:
        return placed

    right_ink = _text_ink(draw, right_text, font)[1] if right_text else 0.0
    max_site = avail - right_ink - (FOOTER_MIN_GAP if right_text else 0.0)
    shrunk_site = _ellipsize_to_ink(draw, font, site, max_site)
    placed = layout_footer_segments(draw, font, [shrunk_left, shrunk_site, right_text])
    if placed is not None:
        return placed

    packed = "  ".join(part for part in (shrunk_left, shrunk_site, right_text) if part)
    packed = _ellipsize_to_ink(draw, font, packed, avail)
    placed = layout_footer_segments(draw, font, [packed])
    return placed or []


def fit_footer(
    draw,
    left: str,
    site: str,
    right: str,
) -> tuple[Any, list[tuple[str, float]]]:
    """Pick a footer font and x positions so the three segments do not overlap."""
    for size in range(FOOTER_FONT_MAX, FOOTER_FONT_MIN - 1, -1):
        font = _load_font(size, bold=False)
        placed = layout_footer_segments(draw, font, [left, site, right])
        if placed is not None:
            return font, placed
    font = _load_font(FOOTER_FONT_MIN, bold=False)
    return font, _compress_footer(draw, font, left, site, right)


def render_score_card_png(
    report: dict[str, Any],
    *,
    timestamp: str,
    fingerprint: str,
    signed: bool,
) -> bytes:
    """Draw a clean 1200×675 PNG. Uses Pillow only."""
    from PIL import Image, ImageDraw

    ticker = str(report.get("ticker") or "UNKNOWN").upper()
    issuer = str(report.get("issuer") or "unknown")
    score = float(report.get("score") or 0.0)
    band = str(report.get("band") or "")
    band_label = str(report.get("band_label") or band)
    subs = report.get("subscores") or {}
    accent = BAND_COLORS.get(band, (139, 148, 158))
    bg = (14, 17, 23)
    panel = (22, 27, 34)
    text = (230, 237, 243)
    muted = (139, 148, 158)
    bar_bg = (33, 38, 45)

    img = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), bg)
    draw = ImageDraw.Draw(img)
    _rounded_rect(draw, (28, 28, CARD_WIDTH - 28, CARD_HEIGHT - 28), 28, panel)

    monogram = _load_monogram(72)
    if monogram is not None:
        img.paste(monogram, (56, 48), monogram)
        title_x = 148
    else:
        title_x = 56

    font_title = _load_font(34, bold=True)
    font_sub = _load_font(18, bold=False)
    font_ticker = _load_font(56, bold=True)
    font_issuer = _load_font(22, bold=False)
    font_score = _load_font(112, bold=True)
    font_band = _load_font(22, bold=True)
    font_pillar = _load_font(18, bold=False)
    font_pillar_num = _load_font(18, bold=True)

    draw.text((title_x, 54), "RAT Score", font=font_title, fill=text)
    draw.text((title_x, 96), "RWA Transparency Score", font=font_sub, fill=muted)

    pill = f" {band or '—'} "
    pill_bbox = draw.textbbox((0, 0), pill, font=font_band)
    pill_w = pill_bbox[2] - pill_bbox[0] + 28
    pill_h = pill_bbox[3] - pill_bbox[1] + 16
    pill_x1 = CARD_WIDTH - 56
    pill_x0 = pill_x1 - pill_w
    _rounded_rect(draw, (pill_x0, 58, pill_x1, 58 + pill_h), 999, accent)
    draw.text((pill_x0 + 14, 64), band or "—", font=font_band, fill=(14, 17, 23))

    draw.text((56, 150), ticker, font=font_ticker, fill=text)
    issuer_line = issuer if len(issuer) <= 42 else issuer[:39] + "…"
    draw.text((56, 218), issuer_line, font=font_issuer, fill=muted)

    score_text = f"{score:.1f}"
    score_bbox = draw.textbbox((0, 0), score_text, font=font_score)
    score_w = score_bbox[2] - score_bbox[0]
    score_x = CARD_WIDTH - 56 - score_w
    draw.text((score_x, 140), score_text, font=font_score, fill=accent)
    band_short = band_label.split("—")[0].strip() if band_label else band
    band_bbox = draw.textbbox((0, 0), band_short, font=font_band)
    band_w = band_bbox[2] - band_bbox[0]
    draw.text((CARD_WIDTH - 56 - band_w, 268), band_short, font=font_band, fill=accent)

    draw.line((56, 288, CARD_WIDTH - 56, 288), fill=(48, 54, 61), width=1)

    pillar_count = max(1, len(WEIGHTS))
    y = 304
    footer_y = CARD_HEIGHT - FOOTER_TOP_INSET
    step = min(46, max(36, (footer_y - 18 - y) // pillar_count))
    bar_x = 360
    bar_w = 520
    bar_h = 12
    for key in WEIGHTS:
        meta = PILLARS.get(key) or {"label": key}
        label = meta["label"]
        raw_value = subs.get(key) if key in subs else None
        value = None if raw_value is None else float(raw_value)
        draw.text((56, y - 4), label, font=font_pillar, fill=text)
        _rounded_rect(draw, (bar_x, y + 4, bar_x + bar_w, y + 4 + bar_h), 6, bar_bg)
        fill_w = 0 if value is None else max(0, min(bar_w, int(bar_w * (value / 100.0))))
        if fill_w > 0:
            _rounded_rect(draw, (bar_x, y + 4, bar_x + fill_w, y + 4 + bar_h), 6, accent)
        num = "not available" if value is None else f"{value:.0f}"
        num_bbox = draw.textbbox((0, 0), num, font=font_pillar_num)
        draw.text((CARD_WIDTH - 56 - (num_bbox[2] - num_bbox[0]), y - 4), num, font=font_pillar_num, fill=text)
        y += step

    foot_left, foot_site, foot_right = footer_text(timestamp, fingerprint, signed=signed)
    font_foot, placed = fit_footer(draw, foot_left, foot_site, foot_right)
    for segment, segment_x in placed:
        draw.text((segment_x, footer_y), segment, font=font_foot, fill=muted)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_signed_card(
    report: dict[str, Any],
    *,
    secret: str | None = None,
    timestamp: str | None = None,
    now: datetime | None = None,
) -> ScoreCardShare:
    """Build PNG + HMAC without touching the network."""
    ts = timestamp or utc_timestamp(now)
    payload = canonical_score_payload(report, ts)
    canonical = canonical_json(payload)
    key = secret if secret is not None else signing_secret_from_env()
    signature = sign_canonical(canonical, key)
    fingerprint = signature_fingerprint(signature)
    signed = bool(signature)
    png = render_score_card_png(
        report,
        timestamp=ts,
        fingerprint=fingerprint,
        signed=signed,
    )
    ticker = payload["ticker"] or "SCORE"
    stamp = ts.replace(":", "").replace("-", "")
    filename = f"rat-score-{ticker}-{stamp}.png"
    return ScoreCardShare(
        png_bytes=png,
        payload=payload,
        canonical=canonical,
        signature=signature,
        fingerprint=fingerprint,
        signed=signed,
        timestamp=ts,
        caption=share_caption(payload, fingerprint),
        filename=filename,
    )


def share_score_card(
    report: dict[str, Any],
    *,
    secret: str | None = None,
    timestamp: str | None = None,
    now: datetime | None = None,
    post_to_x: bool = True,
    x_client: Any | None = None,
) -> ScoreCardShare:
    """Generate a signed card, then optionally post to X. Never raises."""
    try:
        card = build_signed_card(report, secret=secret, timestamp=timestamp, now=now)
    except Exception as exc:  # noqa: BLE001 — UI must still render
        ts = timestamp or utc_timestamp(now)
        return ScoreCardShare(
            png_bytes=b"",
            payload={},
            canonical="",
            signature="",
            fingerprint=UNSIGNED_FINGERPRINT,
            signed=False,
            timestamp=ts,
            caption="",
            filename="rat-score-error.png",
            x_message=f"{PNG_BUILD_FAILED_PREFIX} {exc}",
        )

    if not post_to_x:
        card.x_message = "X post skipped (disabled)."
        return card

    return attach_x_share(card, x_client=x_client)


def attach_x_share(
    card: ScoreCardShare,
    *,
    x_client: Any | None = None,
) -> ScoreCardShare:
    """Post an already-built PNG. Never raises. Never rebuilds the image."""
    if not card.png_bytes:
        card.x_posted = False
        card.x_url = None
        if not str(card.x_message or "").startswith(PNG_BUILD_FAILED_PREFIX):
            card.x_message = "X post skipped: score card image was empty."
        return card
    try:
        client = x_client
        if client is None:
            from .x_client import XClient

            client = XClient.from_env(timeout=X_SHARE_UI_TIMEOUT)
        result = client.post_image(card.png_bytes, card.caption)
        card.x_posted = bool(getattr(result, "posted", False))
        card.x_url = getattr(result, "url", None)
        raw = getattr(result, "message", "") or (
            f"Posted to X: {card.x_url}" if card.x_posted else "X post skipped."
        )
        card.x_message = raw if card.x_posted else user_facing_x_skip_message(raw)
    except Exception as exc:  # noqa: BLE001 — never crash the demo
        from .x_client import log_x_share_failure

        log_x_share_failure("attach_x_share", exc)
        card.x_posted = False
        card.x_url = None
        card.x_message = X_POST_UNAVAILABLE_MESSAGE
    return card
