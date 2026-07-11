"""
webhooks.py - Outgoing webhook notifications on successful avatar update.

Fires one or more operator-configured HTTP webhooks after an avatar upload
reaches full success (image processing + Authentik write + optional LDAP write
all succeeded, no rollback).  Delivery is best-effort and runs in a background
daemon thread so it never blocks or delays the user's upload response; a failing
webhook is logged but never surfaced to the user and never triggers a rollback.

Each endpoint's request body is an operator-defined mapping serialized to JSON.
String values support {placeholder} substitution from the event context (see
WEBHOOK_PLACEHOLDERS in config.py).  A value that is exactly one placeholder
token (e.g. "{user_pk}") is replaced with the native typed value so it
serializes as a JSON number instead of a string.
"""

import logging
import re
import threading

import requests as http_requests
import urllib3

from src import USER_AGENT
from src.config import (
    EXTERNAL_REQUEST_TIMEOUT,
    skip_backend_writes,
    webhooks_enabled,
    webhooks_endpoints,
)

log = logging.getLogger("webhooks")

# Pre-build a requests.Session for TCP connection pooling across webhook calls.
# The User-Agent identifies akvatar; per-endpoint headers are merged per request.
_session = http_requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

# If any endpoint disables TLS verification, suppress urllib3's per-request
# InsecureRequestWarning (the startup warning in config.py already informs the
# operator).  Verification is still applied per request via the verify= arg below.
if any(bool(_wh.get("skip_cert_verify", False)) for _wh in webhooks_endpoints):
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Matches a string that is exactly one {placeholder} token and nothing else.
_SINGLE_TOKEN = re.compile(r"^\{(\w+)\}$")

# Default request body used when an endpoint does not define its own "body".
# Every string is a lone placeholder token, so user_pk/total_bytes serialize as
# JSON numbers via the typed-substitution rule in _render_value.
_DEFAULT_BODY = {
    "event": "avatar.updated",
    "username": "{username}",
    "name": "{name}",
    "email": "{email}",
    "user_pk": "{user_pk}",
    "avatar_url": "{avatar_url}",
    "avatar_id": "{avatar_id}",
    "total_bytes": "{total_bytes}",
    "timestamp": "{timestamp}",
    "app_name": "{app_name}",
    "app_version": "{app_version}",
}


def _render_value(value, context: dict):
    """Substitute {placeholders} in a single template value.

    - A dict or list is rendered recursively.
    - A string equal to exactly one placeholder token (e.g. "{user_pk}") is
      replaced with the native typed context value (so ints stay JSON numbers).
    - Any other string is interpolated via str.format_map (result is a string).
    - Non-string scalars (numbers, booleans, None) pass through unchanged.
    """
    if isinstance(value, dict):
        return {_k: _render_value(_v, context) for _k, _v in value.items()}
    if isinstance(value, list):
        return [_render_value(_v, context) for _v in value]
    if isinstance(value, str):
        _single = _SINGLE_TOKEN.match(value)
        if _single and _single.group(1) in context:
            return context[_single.group(1)]
        return value.format_map(context)
    return value


def _deliver_all(context: dict) -> None:
    """Deliver every configured webhook sequentially (best-effort, never raises)."""
    for _i, _wh in enumerate(webhooks_endpoints):
        _name = _wh.get("name", f"endpoints[{_i}]")
        _url = _wh["url"]
        _method = str(_wh.get("method", "POST")).upper()
        _timeout = _wh.get("timeout", EXTERNAL_REQUEST_TIMEOUT)
        _verify = not bool(_wh.get("skip_cert_verify", False))
        _headers = _render_value(_wh.get("headers", {}), context)
        _template = _wh.get("body")
        _body = _render_value(
            _template if _template is not None else _DEFAULT_BODY, context
        )
        try:
            _resp = _session.request(
                _method,
                _url,
                json=_body,
                headers=_headers,
                timeout=_timeout,
                verify=_verify,
            )
            _resp.raise_for_status()
            log.info(
                "Webhook %r delivered to %s (HTTP %d).", _name, _url, _resp.status_code
            )
        except Exception as _exc:
            # Best-effort delivery: log and move on to the next endpoint.
            log.warning("Webhook %r to %s failed: %s", _name, _url, _exc)


def fire_webhooks(context: dict) -> None:
    """Trigger all configured webhooks for a successful avatar update.

    Non-blocking: spawns a background daemon thread so the caller (the SSE upload
    generator) returns immediately.  A no-op when webhooks are disabled or no
    endpoints are configured.  Honors dry-run (skip_backend_writes): the intent
    is logged and nothing is sent, matching how Authentik/LDAP writes are
    suppressed.

    ``context`` maps every WEBHOOK_PLACEHOLDERS key to its value for this event.
    """
    if not webhooks_enabled or not webhooks_endpoints:
        return

    if skip_backend_writes:
        for _i, _wh in enumerate(webhooks_endpoints):
            _name = _wh.get("name", f"endpoints[{_i}]")
            log.info(
                "[dry-run] Would fire webhook %r to %s (skip_backend_writes).",
                _name,
                _wh.get("url", ""),
            )
        return

    _thread = threading.Thread(
        target=_deliver_all, args=(context,), name="webhook-delivery", daemon=True
    )
    _thread.start()
