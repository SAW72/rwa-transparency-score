"""Live-mode health payload and Streamlit ``GET /health`` route.

Streamlit 1.39 serves unknown paths as the SPA (``index.html``), so a real
JSON ``/health`` has to be registered on Tornado *before* ``Server._create_app``
runs. ``python -m rwa_score.health`` does that, then launches Streamlit.
``app.py`` also installs the route and stops UI rendering if ``/health``
(or ``?health``) is requested after the script is already running.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .chainlink_por import HEALTH_POR_FEED, probe_chainlink_por, rpc_urls_for_chain
from .client import use_fixtures

HEALTH_POR_SYMBOL = HEALTH_POR_FEED.symbol
HEALTH_TIMEOUT_SECONDS = 2.0
HEALTH_PATH = "/health"

GetFn = Callable[..., Any]
PostFn = Callable[..., Any]


def backed_feed_url() -> str:
    """Primary RPC URL used to probe the canonical Backed Chainlink PoR feed."""
    urls = rpc_urls_for_chain(HEALTH_POR_FEED.chain)
    return urls[0] if urls else ""


def utc_timestamp(now: datetime | None = None) -> str:
    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    else:
        ts = ts.astimezone(timezone.utc)
    return ts.isoformat(timespec="seconds").replace("+00:00", "Z")


def probe_backed_feed(
    *,
    getter: GetFn | None = None,
    poster: PostFn | None = None,
    timeout: float = HEALTH_TIMEOUT_SECONDS,
) -> str:
    """Chainlink PoR reachability. Returns ``ok`` or ``down``. Never raises.

    ``poster`` is a ``requests.post``-compatible callable. ``getter`` is accepted
    for older callers and ignored — the probe is JSON-RPC POST only.
    """
    _ = getter
    try:
        if probe_chainlink_por(poster=poster, timeout=timeout):
            return "ok"
    except Exception:  # noqa: BLE001 — health must stay up if the feed is down
        pass
    return "down"


def build_health_payload(
    *,
    getter: GetFn | None = None,
    poster: PostFn | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """JSON body for ``GET /health``. Does not run scoring."""
    fixtures = use_fixtures()
    try:
        backed = probe_backed_feed(getter=getter, poster=poster)
    except Exception:  # noqa: BLE001
        backed = "down"
    return {
        "fixtures": fixtures,
        "verifiers_live": not fixtures,
        "backed_feed": backed,
        "timestamp": utc_timestamp(now),
    }


def is_health_request() -> bool:
    """True when this script run is serving ``/health`` or ``?health``."""
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
    except Exception:  # noqa: BLE001
        return False
    ctx = get_script_run_ctx()
    if ctx is None:
        return False
    qs = (getattr(ctx, "query_string", None) or "").lstrip("?")
    for part in qs.split("&"):
        if part.split("=", 1)[0] == "health":
            return True
    page = (getattr(ctx, "page_name", None) or "").strip("/")
    return page.lower() == "health"


def serve_health_if_requested() -> None:
    """Stop Streamlit UI rendering when this run is a health check."""
    if not is_health_request():
        return
    import streamlit as st

    st.json(build_health_payload())
    st.stop()


def _health_handler_class() -> type:
    from tornado.web import RequestHandler

    class HealthJSONHandler(RequestHandler):
        def set_default_headers(self) -> None:
            self.set_header("Content-Type", "application/json")
            self.set_header("Cache-Control", "no-store")

        def _write_payload(self) -> None:
            try:
                payload = build_health_payload()
            except Exception:  # noqa: BLE001 — never 500 the probe
                payload = {
                    "fixtures": use_fixtures(),
                    "verifiers_live": not use_fixtures(),
                    "backed_feed": "down",
                    "timestamp": utc_timestamp(),
                }
            self.set_header("Content-Type", "application/json")
            self.write(payload)

        def get(self) -> None:
            self._write_payload()

        def head(self) -> None:
            self._write_payload()

    return HealthJSONHandler


def _attach_health_handler(app: Any) -> None:
    if getattr(app, "_rwa_health_attached", False):
        return
    try:
        from tornado.routing import PathMatches, Rule
    except Exception:  # noqa: BLE001
        return
    handler = _health_handler_class()
    rule = Rule(PathMatches(r"/health/?"), handler)
    router = getattr(app, "default_router", None)
    rules = getattr(router, "rules", None) if router is not None else None
    if rules is not None:
        rules.insert(0, rule)
    else:
        app.add_handlers(r".*", [(r"/health/?", handler)])
    app._rwa_health_attached = True


def _attach_to_running_server() -> None:
    try:
        import gc

        from tornado.httpserver import HTTPServer
        from tornado.web import Application
    except Exception:  # noqa: BLE001
        return
    for obj in gc.get_objects():
        try:
            if not isinstance(obj, HTTPServer):
                continue
            callback = getattr(obj, "request_callback", None)
            if isinstance(callback, Application):
                _attach_health_handler(callback)
        except Exception:  # noqa: BLE001
            continue


def install_health_route() -> None:
    """Patch Streamlit so ``GET /health`` returns JSON, if Tornado is available."""
    try:
        from streamlit.web.server.server import Server
    except Exception:  # noqa: BLE001
        return
    if not getattr(Server, "_rwa_health_patched", False):
        original = Server._create_app

        def _create_app(self):  # type: ignore[no-untyped-def]
            app = original(self)
            _attach_health_handler(app)
            return app

        Server._create_app = _create_app  # type: ignore[method-assign]
        Server._rwa_health_patched = True
    _attach_to_running_server()


def main(argv: list[str] | None = None) -> None:
    """Patch ``/health``, then ``streamlit run app.py`` with the remaining args."""
    install_health_route()
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--":
        args = args[1:]
    app = str(Path(__file__).resolve().parents[1] / "app.py")
    sys.argv = ["streamlit", "run", app, *args]
    from streamlit.web import cli as stcli

    stcli.main()


if __name__ == "__main__":
    main()
