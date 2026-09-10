"""Band-crossing detection and webhook delivery.

v1 behavior: fire in the same scoring cycle as the request that observed the
new band (``GET /v1/score``, compare, watchlist) or ``python -m rwa_score.api.poll``.
POSTs are synchronous with a short timeout. First observation of a ticker
**for that API key** is stored and does not fire (no prior band to cross).

Band-crossing state and delivery are scoped per API key. A score under key A
never reads key B's last band and never POSTs key B's webhooks.

Delivery does not follow HTTP redirects (SSRF: an allowlisted host must not
bounce to localhost / metadata / RFC1918).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .store import Store

BAND_RANK = {"GREEN": 3, "YELLOW": 2, "ORANGE": 1, "RED": 0}

DeliverFn = Callable[[str, str, dict[str, str]], tuple[int, bool]]

_BLOCKED_HOSTS = {
    "localhost",
    "metadata.google.internal",
    "metadata.internal",
}


def _ip_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_https_url(url: str, *, resolve: bool = True) -> str:
    """Allowlist public HTTPS webhook targets. Rejects SSRF-shaped hosts."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("webhook URL must be https")
    if parsed.username or parsed.password:
        raise ValueError("webhook URL must not include credentials")
    host = (parsed.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise ValueError("webhook URL missing host")
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        raise ValueError("webhook URL host is not allowed")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and _ip_blocked(literal):
        raise ValueError("webhook URL must not target a private or metadata address")
    if resolve and literal is None:
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror:
            infos = []
        for info in infos:
            addr = info[4][0]
            try:
                resolved = ipaddress.ip_address(addr)
            except ValueError:
                continue
            if _ip_blocked(resolved):
                raise ValueError("webhook URL resolved to a private or metadata address")
    return url


def dropped_below_orange(new_band: str) -> bool:
    return BAND_RANK.get(new_band, 0) < BAND_RANK["ORANGE"]


def crossing_events(old_band: str | None, new_band: str) -> list[str]:
    if old_band is None or old_band == new_band:
        return []
    events = ["band_cross"]
    if dropped_below_orange(new_band):
        events.append("below_orange")
    return events


def sign_body(secret: str, body: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def default_poster(url: str, body: str, headers: dict[str, str], *, timeout: float = 5.0) -> tuple[int, bool]:
    try:
        assert_public_https_url(url)
        # Never follow redirects — the allowlist is only valid for this URL.
        resp = requests.post(
            url,
            data=body.encode("utf-8"),
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )
        code = int(resp.status_code)
        return code, 200 <= code < 300
    except Exception:  # noqa: BLE001 — delivery failure is recorded, not raised
        return 0, False


def _payload(report: dict[str, Any], *, old_band: str, event: str) -> dict[str, Any]:
    return {
        "event": event,
        "ticker": report["ticker"],
        "from_band": old_band,
        "to_band": report["band"],
        "score": report["score"],
        "score_hash": report.get("attestation", {}).get("score_hash"),
    }


def notify_crossings(
    store: Store,
    report: dict[str, Any],
    *,
    key_id: int,
    poster: DeliverFn | None = None,
    timeout: float = 5.0,
) -> list[dict[str, Any]]:
    """Compare to this tenant's last band, persist the new one, fire that key's hooks."""
    ticker = str(report["ticker"]).upper()
    new_band = str(report["band"])
    prev = store.get_last_band(key_id, ticker)
    old_band = prev[0] if prev else None
    store.set_last_band(key_id, ticker, new_band, float(report["score"]))
    events = crossing_events(old_band, new_band)
    if not events or old_band is None:
        return []

    send = poster or (lambda url, body, headers: default_poster(url, body, headers, timeout=timeout))
    deliveries: list[dict[str, Any]] = []
    for hook in store.active_webhooks(key_id):
        if hook.key_id != key_id:
            continue
        if hook.ticker and hook.ticker != ticker:
            continue
        matched = [e for e in events if hook.trigger == e or hook.trigger == "band_cross"]
        if hook.trigger == "below_orange" and "below_orange" not in events:
            continue
        if not matched:
            continue
        event = matched[0]
        body_obj = _payload(report, old_band=old_band, event=event)
        body = json.dumps(body_obj, sort_keys=True, separators=(",", ":"))
        headers = {
            "Content-Type": "application/json",
            "X-RAT-Signature": sign_body(hook.secret, body),
        }
        status, ok = send(hook.url, body, headers)
        store.record_delivery(
            webhook_id=hook.id,
            ticker=ticker,
            event=event,
            status_code=status,
            ok=ok,
        )
        deliveries.append(
            {"webhook_id": hook.id, "event": event, "status_code": status, "ok": ok}
        )
    return deliveries


def apply_score_side_effects(
    store: Store,
    report: dict[str, Any],
    *,
    key_id: int,
    poster: DeliverFn | None = None,
    timeout: float = 5.0,
) -> None:
    from .attest import history_json, score_hash

    digest = score_hash(report)
    store.record_history(
        ticker=report["ticker"],
        score=float(report["score"]),
        band=str(report["band"]),
        payload_json=history_json(report),
        payload_hash=digest,
    )
    notify_crossings(store, report, key_id=key_id, poster=poster, timeout=timeout)
