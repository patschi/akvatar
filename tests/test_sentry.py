"""Tests for src/app_sentry.py, src/web_sentry.py and src/app_monitor.py.

Sentry is disabled in the test configuration (as it is for most deployments),
so the module-level switches are patched to reach the enabled code paths.  The
browser tunnel in particular is an unauthenticated POST endpoint, and the DSN
check is the only thing standing between it and being an open relay.
"""

import json

import pytest

import src.app_monitor as app_monitor
import src.app_sentry as app_sentry
import src.web_sentry as web_sentry
from tests.helpers import FakeResponse

DSN = "https://publickey@sentry.test.invalid/7"
INGEST_URL = "https://sentry.test.invalid/api/7/envelope/"


def envelope(dsn: str = DSN, body: bytes = b'{"type":"event"}') -> bytes:
    """Build a minimal Sentry envelope: a JSON header line plus an item."""
    return (
        json.dumps({"dsn": dsn, "sdk": {"name": "sentry.javascript"}}).encode()
        + b"\n"
        + body
    )


@pytest.fixture
def tunnel(monkeypatch):
    """Enable the browser Sentry tunnel and capture what it forwards."""
    state = {"forwarded": [], "response": FakeResponse(status_code=200, content=b"ok")}

    monkeypatch.setattr(web_sentry, "_SENTRY_TUNNEL_ENABLED", True)
    monkeypatch.setattr(web_sentry, "_SENTRY_TUNNEL_DSN", DSN)
    monkeypatch.setattr(web_sentry, "_SENTRY_INGEST_URL", INGEST_URL)

    def fake_post(url, data=None, headers=None, timeout=None):
        state["forwarded"].append({"url": url, "data": data, "headers": headers})
        if isinstance(state["response"], Exception):
            raise state["response"]
        return state["response"]

    monkeypatch.setattr(web_sentry.http_requests, "post", fake_post)
    return state


# ---------------------------------------------------------------------------
# Browser tunnel
# ---------------------------------------------------------------------------


def test_the_tunnel_forwards_a_matching_envelope(client, tunnel):
    payload = envelope()
    response = client.post("/api/sentry-event", data=payload)

    assert response.status_code == 200
    assert tunnel["forwarded"][0]["url"] == INGEST_URL
    assert tunnel["forwarded"][0]["data"] == payload
    assert (
        tunnel["forwarded"][0]["headers"]["Content-Type"]
        == "application/x-sentry-envelope"
    )


def test_the_tunnel_relays_the_upstream_status(client, tunnel):
    tunnel["response"] = FakeResponse(status_code=429, content=b"slow down")
    assert client.post("/api/sentry-event", data=envelope()).status_code == 429


def test_the_tunnel_rejects_a_foreign_dsn(client, tunnel):
    # Without this the endpoint would relay arbitrary payloads to any Sentry
    # project on behalf of this server.
    response = client.post(
        "/api/sentry-event", data=envelope(dsn="https://other@evil.invalid/1")
    )
    assert response.status_code == 403
    assert tunnel["forwarded"] == []


def test_the_tunnel_rejects_an_empty_body(client, tunnel):
    assert client.post("/api/sentry-event", data=b"").status_code == 400


def test_the_tunnel_rejects_a_malformed_header_line(client, tunnel):
    assert client.post("/api/sentry-event", data=b"not json\n{}").status_code == 400


def test_the_tunnel_rejects_an_envelope_without_a_header_line(client, tunnel):
    assert client.post("/api/sentry-event", data=b'{"dsn":"x"}').status_code == 400


def test_the_tunnel_rejects_a_non_object_header(client, tunnel):
    assert client.post("/api/sentry-event", data=b'["a"]\n{}').status_code == 400


def test_the_tunnel_rejects_an_oversized_envelope(client, tunnel):
    oversized = envelope(body=b"x" * (web_sentry._SENTRY_TUNNEL_MAX_BYTES + 1))
    assert client.post("/api/sentry-event", data=oversized).status_code == 413


def test_the_tunnel_reports_an_upstream_failure_as_502(client, tunnel):
    import requests

    tunnel["response"] = requests.exceptions.ConnectTimeout("sentry unreachable")
    assert client.post("/api/sentry-event", data=envelope()).status_code == 502


def test_the_tunnel_is_absent_when_disabled(client):
    assert client.post("/api/sentry-event", data=envelope()).status_code == 404


def test_the_tunnel_is_absent_without_a_resolved_ingest_url(client, monkeypatch):
    monkeypatch.setattr(web_sentry, "_SENTRY_TUNNEL_ENABLED", True)
    monkeypatch.setattr(web_sentry, "_SENTRY_INGEST_URL", "")
    assert client.post("/api/sentry-event", data=envelope()).status_code == 404


# ---------------------------------------------------------------------------
# SDK initialization
# ---------------------------------------------------------------------------


def test_init_is_a_no_op_when_sentry_is_disabled(monkeypatch):
    import sentry_sdk

    monkeypatch.setattr(app_sentry, "sentry_enabled", False)
    monkeypatch.setattr(
        sentry_sdk,
        "init",
        lambda **kwargs: pytest.fail("the SDK must not be configured when disabled"),
    )
    app_sentry.init_sentry()


def test_init_warns_and_skips_without_a_dsn(monkeypatch, caplog):
    monkeypatch.setattr(app_sentry, "sentry_enabled", True)
    monkeypatch.setattr(app_sentry, "sentry_dsn", "")
    with caplog.at_level("WARNING", logger="app.sentry"):
        app_sentry.init_sentry()
    assert "no DSN is configured" in caplog.text


def test_init_passes_the_configured_rates_to_the_sdk(monkeypatch):
    captured = {}
    monkeypatch.setattr(app_sentry, "sentry_enabled", True)
    monkeypatch.setattr(app_sentry, "sentry_dsn", DSN)
    monkeypatch.setattr(app_sentry, "sentry_capture_errors", True)
    monkeypatch.setattr(app_sentry, "sentry_sample_rate", 0.5)
    monkeypatch.setattr(app_sentry, "sentry_capture_performance", True)
    monkeypatch.setattr(app_sentry, "sentry_traces_sample_rate", 0.25)

    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    app_sentry.init_sentry()

    assert captured["dsn"] == DSN
    assert captured["sample_rate"] == 0.5
    assert captured["traces_sample_rate"] == 0.25


def test_disabling_error_capture_zeroes_the_sample_rate(monkeypatch):
    captured = {}
    monkeypatch.setattr(app_sentry, "sentry_enabled", True)
    monkeypatch.setattr(app_sentry, "sentry_dsn", DSN)
    monkeypatch.setattr(app_sentry, "sentry_capture_errors", False)
    monkeypatch.setattr(app_sentry, "sentry_capture_performance", False)

    import sentry_sdk

    monkeypatch.setattr(sentry_sdk, "init", lambda **kwargs: captured.update(kwargs))
    app_sentry.init_sentry()

    assert captured["sample_rate"] == 0.0
    assert captured["traces_sample_rate"] == 0.0


# ---------------------------------------------------------------------------
# Browser configuration handed to templates
# ---------------------------------------------------------------------------


def test_no_browser_config_when_disabled():
    assert app_sentry.get_browser_sentry_config() is None


def test_no_browser_config_without_a_js_sdk_url(monkeypatch, caplog):
    monkeypatch.setattr(app_sentry, "sentry_browser_enabled", True)
    monkeypatch.setattr(app_sentry, "sentry_browser_js_sdk_url", "")
    with caplog.at_level("WARNING", logger="app.sentry"):
        assert app_sentry.get_browser_sentry_config() is None
    assert "no JS SDK URL" in caplog.text


def test_no_browser_config_without_a_dsn(monkeypatch, caplog):
    monkeypatch.setattr(app_sentry, "sentry_browser_enabled", True)
    monkeypatch.setattr(app_sentry, "sentry_browser_js_sdk_url", "https://cdn/s.js")
    monkeypatch.setattr(app_sentry, "sentry_browser_dsn", "")
    with caplog.at_level("WARNING", logger="app.sentry"):
        assert app_sentry.get_browser_sentry_config() is None
    assert "no DSN is configured" in caplog.text


def test_the_browser_config_is_json_serializable(monkeypatch):
    # It is rendered straight into the page with |tojson.
    monkeypatch.setattr(app_sentry, "sentry_browser_enabled", True)
    monkeypatch.setattr(app_sentry, "sentry_browser_js_sdk_url", "https://cdn/s.js")
    monkeypatch.setattr(app_sentry, "sentry_browser_dsn", DSN)

    config = app_sentry.get_browser_sentry_config()

    assert config["enabled"] is True
    assert config["dsn"] == DSN
    assert json.dumps(config)


# ---------------------------------------------------------------------------
# Memory monitor
# ---------------------------------------------------------------------------


def test_the_monitor_thread_is_a_daemon(monkeypatch):
    started = {}

    class FakeThread:
        def __init__(self, target, name, daemon):
            started.update(name=name, daemon=daemon)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(app_monitor.threading, "Thread", FakeThread)
    app_monitor.start_memory_monitor()

    assert started == {"name": "memlog", "daemon": True, "started": True}


def test_rss_reading_survives_a_missing_proc_file(monkeypatch):
    def no_proc(*_args, **_kwargs):
        raise FileNotFoundError("/proc/self/status")

    monkeypatch.setattr("builtins.open", no_proc)
    assert app_monitor._get_rss_mb() is None
