"""
csrf.py – Per-session CSRF token generation and validation.

Generates a cryptographically random token once per session and validates it
on state-changing requests via the ``X-CSRF-Token`` header.  This is
defence-in-depth on top of SameSite=Lax session cookies.

Usage:
  - ``generate_csrf_token()`` is injected into all templates as ``csrf_token``
    via the context processor in app.py, callable as ``{{ csrf_token() }}``.
  - ``validate_csrf_token()`` returns None on success or a JSON 403 response
    on failure.  Call it at the top of any POST/PUT/PATCH/DELETE route.
"""

import logging
import secrets

from flask import session, request, jsonify

log = logging.getLogger("csrf")

# Token length in bytes (32 bytes = 64 hex characters)
_TOKEN_BYTES = 32

# Session key where the token is stored
_SESSION_KEY = "csrf_token"

# HTTP header carrying the token from the client
_HEADER_NAME = "X-CSRF-Token"


def generate_csrf_token() -> str:
    """Return the CSRF token for the current session, generating one if absent."""
    if _SESSION_KEY not in session:
        session[_SESSION_KEY] = secrets.token_hex(_TOKEN_BYTES)
    return session[_SESSION_KEY]


def validate_csrf_token():
    """
    Validate the CSRF token on the current request.

    Returns None if the token is valid, or a (response, 403) tuple if
    validation fails.  Intended to be called at the start of POST handlers::

        rejection = validate_csrf_token()
        if rejection:
            return rejection
    """
    expected = session.get(_SESSION_KEY, None)
    provided = request.headers.get(_HEADER_NAME, "")
    # Explicitly reject when either value is absent — a falsy `expected` (e.g.
    # empty string or None) must never be treated as "validation passed".
    # secrets.compare_digest is only reached when both values are non-empty.
    if not expected or not provided or not secrets.compare_digest(expected, provided):
        log.warning(
            "CSRF validation failed on %s %s from %s (token present: %s).",
            request.method,
            request.path,
            request.remote_addr,
            bool(provided),
        )
        return jsonify({"error": "csrf_failed"}), 403
    return None
