"""
sec_csp.py - Per-request Content Security Policy nonce generation and header construction.

A fresh cryptographically random nonce is generated once per request and
stored on Flask's ``g`` object so that both the template (which adds it to
inline <script> tags via ``nonce="{{ csp_nonce() }}"``) and the
``after_request`` hook (which embeds it in the CSP response header) use
the exact same value for the same request.

The img-src directive is extended at startup to include the origin derived
from ``app.public_avatar_url`` so that avatar images served from a separate
host (e.g. a CDN or a dedicated media server) are not blocked.

CSP can be disabled entirely via ``security.csp_enabled: false`` in config.yml.
This is useful when a reverse proxy or WAF is responsible for injecting
CSP headers and two competing policies would cause problems.

Usage:
  - ``generate_csp_nonce()`` is injected into all templates as ``csp_nonce``
    via the context processor in app.py, callable as ``{{ csp_nonce() }}``.
  - The ``after_request`` hook in app.py calls ``build_csp_header()`` to
    retrieve the nonce-aware header value.  When CSP is disabled the function
    returns ``None`` and the caller omits the header entirely.
"""

import logging
import secrets
from urllib.parse import urlparse

from flask import g

from src.config import app_cfg, security_cfg

log = logging.getLogger("csp")

# 16 bytes → 128 bits of entropy, base64url-encoded to ~22 characters.
# RFC 2397 / CSP Level 3 require base64 (no padding issues with urlsafe).
_NONCE_BYTES = 16

# Flask `g` key used to store the nonce for the duration of one request
_G_KEY = "csp_nonce"

# Master switch - set security.csp_enabled to false to suppress the CSP header entirely.
# Defaults to true; only disable when a reverse proxy or WAF owns the CSP header.
_CSP_ENABLED = bool(security_cfg.get("csp_enabled", True))

# Extract the origin from public_avatar_url and add it to img-src so that
# avatars hosted on a separate origin are not blocked by the policy.
# Example: "https://cdn.example.com/user-avatars" → "https://cdn.example.com"
_avatar_url = app_cfg.get("public_avatar_url", "")
_parsed_avatar = urlparse(_avatar_url)
_avatar_origin = (
    f"{_parsed_avatar.scheme}://{_parsed_avatar.netloc}"
    if _parsed_avatar.scheme and _parsed_avatar.netloc
    else None
)

# Build the img-src value once at startup.
# 'self'   - same-origin avatar files served by this application.
# data:    - base64-encoded preview images produced by Cropper.js in the browser.
# blob:    - object URLs created by Cropper.js for the canvas preview.
# <origin> - explicit avatar host when public_avatar_url is on a different origin.
_img_src_parts = ["'self'", "data:", "blob:"]
if _avatar_origin:
    _img_src_parts.append(_avatar_origin)

_IMG_SRC = " ".join(_img_src_parts)

# Pre-built CSP directive string.  The nonce placeholder is substituted per
# request in build_csp_header() to keep allocation minimal.
#
# default-src 'none'     - deny everything not explicitly listed below.
# nonce-based script-src - only <script> tags carrying the per-request nonce
#                          are executed; injected inline scripts are blocked.
# connect-src blob:      - Cropper.js calls fetch(blobUrl) to read the cropped
#                          canvas back as binary data before POSTing it; blob:
#                          must be listed here so that connection is not blocked.
# frame-ancestors 'none' - CSP3 equivalent of X-Frame-Options: DENY, respected
#                          by modern browsers that ignore the legacy header.
_CSP_TEMPLATE = (
    "default-src 'none'; "
    "script-src 'self' 'nonce-{nonce}'; "
    "style-src 'self'; "
    f"img-src {_IMG_SRC}; "
    "font-src 'self'; "
    "connect-src 'self' blob:; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

if _CSP_ENABLED:
    log.debug("CSP enabled. img-src: %s", _IMG_SRC)
else:
    log.debug("CSP disabled via security.csp_enabled=false.")


def generate_csp_nonce() -> str:
    """Return the CSP nonce for the current request, generating one if absent."""
    if not hasattr(g, _G_KEY):
        setattr(g, _G_KEY, secrets.token_urlsafe(_NONCE_BYTES))
    return getattr(g, _G_KEY)


def build_csp_header(nonce: str) -> str | None:
    """Return the full Content-Security-Policy header value for the current nonce.

    Returns ``None`` when CSP is disabled via ``security.csp_enabled: false``,
    in which case the caller should omit the header entirely.
    """
    if not _CSP_ENABLED:
        return None
    return _CSP_TEMPLATE.format(nonce=nonce)
