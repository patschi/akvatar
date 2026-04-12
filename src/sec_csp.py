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

from src.config import (
    csp_enabled,
    csp_report_only,
    csp_report_uri,
    public_avatar_url,
    sentry_browser_dsn,
    sentry_browser_enabled,
    sentry_browser_js_sdk_url,
    sentry_browser_tunnel_enabled,
)

log = logging.getLogger("csp")

# 16 bytes → 128 bits of entropy, base64url-encoded to ~22 characters.
# RFC 2397 / CSP Level 3 require base64 (no padding issues with urlsafe).
_NONCE_BYTES = 16

# Flask `g` key used to store the nonce for the duration of one request
_G_KEY = "csp_nonce"

# Master switch - set security.csp_enabled to false to suppress the CSP header entirely.
# Defaults to true; only disable when a reverse proxy or WAF owns the CSP header.
_CSP_ENABLED = csp_enabled

# Report-only mode: when true, the policy is sent as Content-Security-Policy-Report-Only
# instead of Content-Security-Policy so violations are reported (browser console / report-uri)
# but NOT enforced.  Useful for testing a new policy without breaking the live site.
# Ignored when csp_enabled is false.
_CSP_REPORT_ONLY = csp_report_only

# Optional CSP report-uri directive - URL where the browser sends violation reports.
# Leave empty to omit the directive (violations are logged to the browser console only).
_CSP_REPORT_URI = csp_report_uri

# The header name to use: the standard enforcing header, or the report-only variant.
# Exported so app.py can set the correct header without hard-coding the name.
CSP_HEADER_NAME = (
    "Content-Security-Policy-Report-Only"
    if (_CSP_ENABLED and _CSP_REPORT_ONLY)
    else "Content-Security-Policy"
)

# Extract the origin from public_avatar_url and add it to img-src so that
# avatars hosted on a separate origin are not blocked by the policy.
# Example: "https://cdn.example.com/user-avatars" → "https://cdn.example.com"
_parsed_avatar = urlparse(public_avatar_url)
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

# Derive the origin of the Sentry Browser JS SDK URL so it can be added to
# script-src.  When browser Sentry is disabled or no URL is configured the
# script-src stays at "'self'" (plus the per-request nonce).
_sentry_js_origin: str | None = None
if sentry_browser_enabled and sentry_browser_js_sdk_url:
    _parsed_sentry_js = urlparse(sentry_browser_js_sdk_url)
    if _parsed_sentry_js.scheme and _parsed_sentry_js.netloc:
        _sentry_js_origin = (
            f"{_parsed_sentry_js.scheme}://{_parsed_sentry_js.netloc}"
        )

# Build script-src value - 'self' is always present; the Sentry JS origin is
# appended only when browser-side Sentry is configured.
_script_src_parts = ["'self'"]
if _sentry_js_origin:
    _script_src_parts.append(_sentry_js_origin)
_SCRIPT_SRC = " ".join(_script_src_parts)

# Derive the Sentry ingest origin for connect-src.  This is only needed when
# browser-side Sentry is enabled but the tunnel is DISABLED, because the
# browser SDK sends envelopes directly to the Sentry host and CSP must allow
# that connection.  When the tunnel IS enabled the browser only talks to 'self'
# (/api/sentry-event) so no extra connect-src entry is required.
_sentry_ingest_origin: str | None = None
if sentry_browser_enabled and sentry_browser_dsn and not sentry_browser_tunnel_enabled:
    _parsed_sentry_dsn = urlparse(sentry_browser_dsn)
    if _parsed_sentry_dsn.scheme and _parsed_sentry_dsn.hostname:
        _sentry_ingest_host = _parsed_sentry_dsn.hostname
        if _parsed_sentry_dsn.port:
            _sentry_ingest_host = f"{_sentry_ingest_host}:{_parsed_sentry_dsn.port}"
        _sentry_ingest_origin = (
            f"{_parsed_sentry_dsn.scheme}://{_sentry_ingest_host}"
        )

# Build connect-src value once at startup.
# 'self'    - API endpoints (upload, heartbeat, sentry tunnel when enabled).
# blob:     - Cropper.js calls fetch(blobUrl) to read the cropped canvas.
# <origin>  - Sentry ingest host (only when tunnel is disabled).
_connect_src_parts = ["'self'", "blob:"]
if _sentry_ingest_origin:
    _connect_src_parts.append(_sentry_ingest_origin)
_CONNECT_SRC = " ".join(_connect_src_parts)

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
    f"script-src {_SCRIPT_SRC} 'nonce-{{nonce}}'; "
    "style-src 'self'; "
    f"img-src {_IMG_SRC}; "
    "font-src 'self'; "
    f"connect-src {_CONNECT_SRC}; "
    "frame-ancestors 'none'; "
    "base-uri 'self'; "
    "form-action 'self'"
)

if _CSP_ENABLED:
    _mode = "report-only" if _CSP_REPORT_ONLY else "enforcing"
    log.debug(
        "CSP enabled (%s, header=%s). img-src: %s", _mode, CSP_HEADER_NAME, _IMG_SRC
    )
    if _CSP_REPORT_ONLY:
        log.info(
            "CSP is in report-only mode (%s) - violations are reported but NOT enforced.",
            CSP_HEADER_NAME,
        )
    if _CSP_REPORT_URI:
        log.debug("CSP report-uri: %s", _CSP_REPORT_URI)
else:
    log.debug("CSP disabled via security.csp_enabled=false.")


def generate_csp_nonce() -> str:
    """Return the CSP nonce for the current request, generating one if absent."""
    if not hasattr(g, _G_KEY):
        setattr(g, _G_KEY, secrets.token_urlsafe(_NONCE_BYTES))
    return getattr(g, _G_KEY)


def build_csp_header(nonce: str) -> str | None:
    """Return the Content-Security-Policy (or Report-Only) header value for the current nonce.

    Returns ``None`` when CSP is disabled via ``security.csp_enabled: false``,
    in which case the caller should omit the header entirely.
    The caller must set the header under ``CSP_HEADER_NAME`` (not hardcoded) so
    that report-only mode sends the correct header name.
    """
    if not _CSP_ENABLED:
        return None
    policy = _CSP_TEMPLATE.format(nonce=nonce)
    # Append report-uri directive when configured
    if _CSP_REPORT_URI:
        policy += f"; report-uri {_CSP_REPORT_URI}"
    return policy
