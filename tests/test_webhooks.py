"""Tests for src/webhooks.py - outgoing webhook rendering and delivery.

The interesting part is the template renderer: a value that is *exactly* one
placeholder keeps its native type (so ``user_pk`` serializes as a JSON number),
while a placeholder embedded in a larger string interpolates as text.  Delivery
itself must be fire-and-forget - a failing endpoint can never surface to the
user or roll back an otherwise successful upload.
"""

import time

import pytest

import src.webhooks as webhooks
from src.config import WEBHOOK_PLACEHOLDERS
from src.webhooks import (
    _DEFAULT_BODY,
    _render_value,
    fire_webhooks,
    wait_for_pending_deliveries,
)
from tests.helpers import FakeResponse

CONTEXT = {
    "username": "testuser",
    "name": "Test User",
    "email": "test.user@example.com",
    "user_pk": 42,
    "avatar_url": "https://cdn.test.example.com/user-avatars/256x256/abc.jpg",
    "avatar_id": "abc",
    "total_bytes": 12345,
    "timestamp": "2026-01-01T00:00:00+00:00",
    "app_name": "akvatar",
    "app_version": "0.9.0+testsha",
}


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------


def test_a_lone_placeholder_keeps_its_native_type():
    # "{user_pk}" must serialize as a JSON number, not as the string "42".
    assert _render_value("{user_pk}", CONTEXT) == 42
    assert _render_value("{total_bytes}", CONTEXT) == 12345


def test_an_embedded_placeholder_interpolates_as_text():
    assert (
        _render_value("avatar for {username} ({email})", CONTEXT)
        == "avatar for testuser (test.user@example.com)"
    )


def test_rendering_recurses_through_dicts_and_lists():
    template = {"a": ["{username}", {"b": "pk={user_pk}"}], "c": "{user_pk}"}
    assert _render_value(template, CONTEXT) == {
        "a": ["testuser", {"b": "pk=42"}],
        "c": 42,
    }


@pytest.mark.parametrize("value", [7, 1.5, True, None])
def test_non_string_scalars_pass_through_untouched(value):
    assert _render_value(value, CONTEXT) is value


def test_an_unknown_lone_token_falls_back_to_string_interpolation():
    # Config validation rejects unknown placeholders at startup, so reaching
    # here means a KeyError is the correct, loud outcome.
    with pytest.raises(KeyError):
        _render_value("{nope}", CONTEXT)


def test_the_default_body_only_uses_documented_placeholders():
    used = {
        value.strip("{}")
        for value in _DEFAULT_BODY.values()
        if isinstance(value, str) and value.startswith("{")
    }
    assert used <= set(WEBHOOK_PLACEHOLDERS)


def test_the_default_body_renders_completely():
    rendered = _render_value(_DEFAULT_BODY, CONTEXT)
    assert rendered["event"] == "avatar.updated"
    assert rendered["user_pk"] == 42
    assert rendered["total_bytes"] == 12345
    # No placeholder survives unrendered in any value.
    assert not [v for v in rendered.values() if isinstance(v, str) and "{" in v]
    assert set(rendered) == {"event", *WEBHOOK_PLACEHOLDERS}


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def test_fire_webhooks_delivers_the_configured_endpoint(webhook_session):
    fire_webhooks(CONTEXT)
    wait_for_pending_deliveries(timeout=5)

    assert len(webhook_session.calls) == 1
    call = webhook_session.calls[0]
    assert call.method == "POST"
    assert call.url == "https://hooks.test.invalid/avatar"
    assert call.kwargs["timeout"] == 5
    assert call.kwargs["headers"] == {"X-Akvatar-User": "testuser"}
    # The configured body template, rendered with typed substitution.
    assert call.json_body == {
        "event": "avatar.updated",
        "user_pk": 42,
        "avatar_url": CONTEXT["avatar_url"],
        "total_bytes": 12345,
        "note": "avatar for testuser (test.user@example.com)",
    }


def test_fire_webhooks_is_a_no_op_when_disabled(webhook_session, monkeypatch):
    monkeypatch.setattr(webhooks, "webhooks_enabled", False)
    fire_webhooks(CONTEXT)
    wait_for_pending_deliveries(timeout=5)
    assert webhook_session.calls == []


def test_fire_webhooks_is_a_no_op_with_no_endpoints(webhook_session, monkeypatch):
    monkeypatch.setattr(webhooks, "webhooks_endpoints", [])
    fire_webhooks(CONTEXT)
    wait_for_pending_deliveries(timeout=5)
    assert webhook_session.calls == []


def test_dry_run_logs_the_intent_without_sending(webhook_session, monkeypatch, caplog):
    monkeypatch.setattr(webhooks, "skip_backend_writes", True)
    with caplog.at_level("INFO", logger="webhooks"):
        fire_webhooks(CONTEXT)
    wait_for_pending_deliveries(timeout=5)

    assert webhook_session.calls == []
    assert "Would fire webhook" in caplog.text


def test_a_failing_endpoint_never_raises_and_is_logged(
    webhook_session, monkeypatch, caplog
):
    webhook_session.handler = lambda *a, **kw: FakeResponse(status_code=500)
    with caplog.at_level("WARNING", logger="webhooks"):
        fire_webhooks(CONTEXT)
        wait_for_pending_deliveries(timeout=5)
    assert "failed" in caplog.text


def test_delivery_continues_to_the_next_endpoint_after_a_failure(
    webhook_session, monkeypatch
):
    monkeypatch.setattr(
        webhooks,
        "webhooks_endpoints",
        [
            {"name": "broken", "url": "https://a.invalid/hook"},
            {"name": "healthy", "url": "https://b.invalid/hook"},
        ],
    )
    webhook_session.handler = lambda method, url, **kw: FakeResponse(
        status_code=500 if "a.invalid" in url else 200, url=url
    )

    fire_webhooks(CONTEXT)
    wait_for_pending_deliveries(timeout=5)

    assert [call.url for call in webhook_session.calls] == [
        "https://a.invalid/hook",
        "https://b.invalid/hook",
    ]


def test_a_method_override_is_honored(webhook_session, monkeypatch):
    monkeypatch.setattr(
        webhooks,
        "webhooks_endpoints",
        [{"url": "https://a.invalid/hook", "method": "put"}],
    )
    fire_webhooks(CONTEXT)
    wait_for_pending_deliveries(timeout=5)
    assert webhook_session.calls[0].method == "PUT"


def test_an_endpoint_without_a_body_gets_the_default_payload(
    webhook_session, monkeypatch
):
    monkeypatch.setattr(
        webhooks, "webhooks_endpoints", [{"url": "https://a.invalid/hook"}]
    )
    fire_webhooks(CONTEXT)
    wait_for_pending_deliveries(timeout=5)

    body = webhook_session.calls[0].json_body
    assert body["event"] == "avatar.updated"
    assert body["avatar_id"] == "abc"


def test_skip_cert_verify_is_passed_through_per_endpoint(webhook_session, monkeypatch):
    monkeypatch.setattr(
        webhooks,
        "webhooks_endpoints",
        [
            {"url": "https://a.invalid/hook", "skip_cert_verify": True},
            {"url": "https://b.invalid/hook"},
        ],
    )
    fire_webhooks(CONTEXT)
    wait_for_pending_deliveries(timeout=5)

    assert webhook_session.calls[0].kwargs["verify"] is False
    assert webhook_session.calls[1].kwargs["verify"] is True


def test_wait_for_pending_deliveries_returns_immediately_when_idle(monkeypatch):
    # Nothing in flight: the drain call the CLI makes before exiting must return
    # without waiting out its timeout.
    monkeypatch.setattr(webhooks, "_pending_threads", [])
    started = time.monotonic()
    wait_for_pending_deliveries(timeout=30)
    assert time.monotonic() - started < 1.0


def test_finished_delivery_threads_do_not_accumulate(webhook_session):
    for _ in range(5):
        fire_webhooks(CONTEXT)
        wait_for_pending_deliveries(timeout=5)
    # The tracking list is pruned of dead threads on every fire.
    assert len([t for t in webhooks._pending_threads if t.is_alive()]) == 0
    assert len(webhooks._pending_threads) <= 1
