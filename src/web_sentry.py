"""
web_sentry.py - Sentry Browser Tunnel endpoint.

Provides POST /api/sentry-event which forwards browser Sentry envelopes to
the real Sentry ingest URL so the browser never needs a direct connection to
the Sentry host.  This avoids CSP connect-src issues and ad-blocker
interference.

The endpoint is unauthenticated because Sentry events may fire from public
pages (login, logged-out).  The DSN in the envelope header is validated
against the configured browser DSN to prevent open-relay abuse.
"""

import json
import logging
from urllib.parse import urlparse

import requests as http_requests
from flask import Blueprint, Response, abort, request

from src.config import (
    sentry_browser_dsn,
    sentry_browser_enabled,
    sentry_browser_tunnel_enabled,
)

log = logging.getLogger("sentry.proxy")

sentry_bp = Blueprint("sentry", __name__)

# Pre-compute the allowed Sentry host and project ID from the configured DSN
# so the per-request handler only needs a fast string comparison.
_SENTRY_TUNNEL_ENABLED = sentry_browser_enabled and sentry_browser_tunnel_enabled
_SENTRY_TUNNEL_DSN = sentry_browser_dsn if _SENTRY_TUNNEL_ENABLED else ""
_SENTRY_INGEST_URL: str = ""

if _SENTRY_TUNNEL_DSN:
    _parsed_dsn = urlparse(_SENTRY_TUNNEL_DSN)
    _dsn_project_id = _parsed_dsn.path.strip("/")
    _dsn_host = _parsed_dsn.hostname
    if _parsed_dsn.port:
        _dsn_host = f"{_dsn_host}:{_parsed_dsn.port}"
    _SENTRY_INGEST_URL = (
        f"{_parsed_dsn.scheme}://{_dsn_host}/api/{_dsn_project_id}/envelope/"
    )
    log.debug("Sentry tunnel active – forwarding to %s", _SENTRY_INGEST_URL)

# Maximum envelope size accepted by the tunnel (1 MB).  Larger payloads are
# rejected before being forwarded to keep memory usage bounded.
_SENTRY_TUNNEL_MAX_BYTES = 1 * 1024 * 1024

# Timeout for the outbound HTTP request to Sentry (seconds).
_SENTRY_REPORT_TIMEOUT = 5


@sentry_bp.route("/api/sentry-event", methods=["POST"])
def api_sentry_tunnel():
    """Forward a Sentry envelope from the browser SDK to the real Sentry ingest.

    Validates that the DSN in the envelope header matches the configured
    browser DSN so the endpoint cannot be used as an open relay.
    """
    if not _SENTRY_TUNNEL_ENABLED or not _SENTRY_INGEST_URL:
        abort(404)

    # Guard against oversized payloads
    content_length = request.content_length or 0
    if content_length > _SENTRY_TUNNEL_MAX_BYTES:
        log.debug(
            "Sentry tunnel: rejected oversized envelope (%d bytes).", content_length
        )
        abort(413)

    envelope = request.get_data()
    if not envelope:
        abort(400)

    # Secondary size guard – covers chunked requests where Content-Length is absent.
    if len(envelope) > _SENTRY_TUNNEL_MAX_BYTES:
        log.debug(
            "Sentry tunnel: rejected oversized envelope (%d bytes).", len(envelope)
        )
        abort(413)

    # The first line of a Sentry envelope is a JSON header containing the DSN.
    # Example: {"dsn":"https://key@sentry.example.com/1","sdk":{...}}
    try:
        header_end = envelope.index(b"\n")
        header = json.loads(envelope[:header_end])
    except (ValueError, json.JSONDecodeError):
        log.debug("Sentry tunnel: malformed envelope header.")
        abort(400)

    if not isinstance(header, dict):
        abort(400)

    envelope_dsn = header.get("dsn", "")
    if envelope_dsn != _SENTRY_TUNNEL_DSN:
        log.debug(
            "Sentry tunnel: DSN mismatch (got %r, expected %r).",
            envelope_dsn,
            _SENTRY_TUNNEL_DSN,
        )
        abort(403)

    # Forward the envelope to the real Sentry ingest endpoint
    try:
        upstream = http_requests.post(
            _SENTRY_INGEST_URL,
            data=envelope,
            headers={"Content-Type": "application/x-sentry-envelope"},
            timeout=_SENTRY_REPORT_TIMEOUT,
        )
        return Response(upstream.content, status=upstream.status_code)
    except http_requests.RequestException as exc:
        log.warning("Sentry tunnel: upstream request failed: %s", exc)
        return Response("", status=502)
