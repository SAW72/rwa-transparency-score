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

from dotenv import load_dotenv

from .scorer import PILLARS, WEIGHTS

SIGNING_SECRET_ENV = "SCORE_CARD_SIGNING_SECRET"
UNSIGNED_FINGERPRINT = "UNSIGNED"
FINGERPRINT_LEN = 16
CARD_WIDTH = 1200
CARD_HEIGHT = 675
ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
MONOGRAM_CANDIDATES = (
    ASSETS_DIR / "rat-monogram.png",
    ASSETS_DIR / "rat-icon-192.png",
)

BAND_COLORS = {
    "GREEN": (61, 220, 151),
    "YELLOW": (245, 197, 66),
    "ORANGE": (240, 138, 36),
    "RED": (229, 72, 77),
}

_FONT_PAIRS = (
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
    subscores: dict[str, float] = {}
    for key in WEIGHTS:
        subscores[key] = round(float(raw_subs.get(key) or 0.0), 1)
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
            pillar_bits.append(f"{short} {subs[key]:.0f}")
    lines = [
        f"RAT Score · {ticker} · {score} {band}",
        " · ".join(pillar_bits),
        f"{timestamp} · sig {fingerprint}",
        "Not financial advice",
    ]
    text = "\n".join(line for line in lines if line)
    return text[:280]


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
    font_foot = _load_font(16, bold=False)

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
    footer_y = CARD_HEIGHT - 62
    step = min(46, max(36, (footer_y - 18 - y) // pillar_count))
    bar_x = 360
    bar_w = 520
    bar_h = 12
    for key in WEIGHTS:
        meta = PILLARS.get(key) or {"label": key}
        label = meta["label"]
        value = float(subs.get(key) or 0.0)
        draw.text((56, y - 4), label, font=font_pillar, fill=text)
        _rounded_rect(draw, (bar_x, y + 4, bar_x + bar_w, y + 4 + bar_h), 6, bar_bg)
        fill_w = max(0, min(bar_w, int(bar_w * (value / 100.0))))
        if fill_w > 0:
            _rounded_rect(draw, (bar_x, y + 4, bar_x + fill_w, y + 4 + bar_h), 6, accent)
        num = f"{value:.0f}"
        num_bbox = draw.textbbox((0, 0), num, font=font_pillar_num)
        draw.text((CARD_WIDTH - 56 - (num_bbox[2] - num_bbox[0]), y - 4), num, font=font_pillar_num, fill=text)
        y += step

    sig_label = f"sig {fingerprint}" if signed else "unsigned — set SCORE_CARD_SIGNING_SECRET to sign"
    foot_left = f"{timestamp}  ·  {sig_label}"
    foot_right = "Not financial advice  ·  RAT Score"
    draw.text((56, footer_y), foot_left, font=font_foot, fill=muted)
    right_bbox = draw.textbbox((0, 0), foot_right, font=font_foot)
    draw.text(
        (CARD_WIDTH - 56 - (right_bbox[2] - right_bbox[0]), footer_y),
        foot_right,
        font=font_foot,
        fill=muted,
    )

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
            x_message=f"Score card generation failed: {exc}",
        )

    if not post_to_x:
        card.x_message = "X post skipped (disabled)."
        return card

    try:
        client = x_client
        if client is None:
            from .x_client import XClient

            client = XClient.from_env()
        result = client.post_image(card.png_bytes, card.caption)
        card.x_posted = bool(getattr(result, "posted", False))
        card.x_url = getattr(result, "url", None)
        card.x_message = getattr(result, "message", "") or (
            f"Posted to X: {card.x_url}" if card.x_posted else "X post skipped."
        )
    except Exception as exc:  # noqa: BLE001 — never crash the demo
        card.x_posted = False
        card.x_url = None
        card.x_message = f"X post skipped: {exc}"
    return card
