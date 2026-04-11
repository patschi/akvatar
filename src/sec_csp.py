"""
csp.py – Per-request Content Security Policy nonce generation.

A fresh cryptographically random nonce is generated once per request and
stored on Flask's ``g`` object so that both the template (which adds it to
inline <script> tags via ``nonce="{{ csp_nonce() }}"``) and the
``after_request`` hook (which embeds it in the CSP response header) use
the exact same value for the same request.

Usage:
  - ``generate_csp_nonce()`` is injected into all templates as ``csp_nonce``
    via the context processor in app.py, callable as ``{{ csp_nonce() }}``.
  - The ``after_request`` hook in app.py calls ``generate_csp_nonce()`` to
    retrieve the same nonce and embed it in the ``Content-Security-Policy``
    header.  If the template never called it (non-HTML response), a fresh
    nonce is generated — it simply won't match any inline tag, which is
    correct and harmless for non-HTML content types.
"""

import logging
import secrets

from flask import g

log = logging.getLogger("csp")

# 16 bytes → 128 bits of entropy, base64url-encoded to ~22 characters.
# RFC 2397 / CSP Level 3 require base64 (no padding issues with urlsafe).
_NONCE_BYTES = 16

# Flask `g` key used to store the nonce for the duration of one request
_G_KEY = "csp_nonce"


def generate_csp_nonce() -> str:
    """Return the CSP nonce for the current request, generating one if absent."""
    if not hasattr(g, _G_KEY):
        setattr(g, _G_KEY, secrets.token_urlsafe(_NONCE_BYTES))
    return getattr(g, _G_KEY)
