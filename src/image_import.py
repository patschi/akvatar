"""
image_import.py - Remote image import routes (Gravatar and URL).

Provides proxy endpoints that fetch images from external sources on behalf
of the authenticated user.  Proxying is required so that fetched images are
served from the same origin as the app — without this, the browser marks
cross-origin images drawn on an HTML canvas as "tainted", which prevents
Cropper.js from reading pixel data via ``getCroppedCanvas().toBlob()``.

Both endpoints require authentication (``@login_required``) and CSRF
validation to prevent abuse and cross-site request forgery.
"""

import hashlib
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import requests as http_requests
from flask import Blueprint, Response, jsonify, request, session

from src import USER_AGENT
from src.auth import login_required
from src.config import app_cfg, import_cfg
from src.i18n import t
from src.sec_csrf import validate_csrf_token

log = logging.getLogger("img_import")

import_bp = Blueprint("import", __name__)

# Remote image fetch limits (derived from the same config as direct uploads)
_MAX_FETCH_SIZE = app_cfg.get("max_upload_size_mb", 10) * 1024 * 1024  # MB -> bytes
_MAX_FETCH_SIZE_MB = app_cfg.get("max_upload_size_mb", 10)
_FETCH_TIMEOUT = 15  # seconds
_MAX_REDIRECTS = 5  # maximum redirect hops to follow during URL import

# Content-Type to file extension mapping (for Gravatar filenames)
_MIME_TO_EXT: dict[str, str] = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
}

# Allowlist of MIME types accepted from remote servers - derived from _MIME_TO_EXT
# so both stay in sync automatically: adding a new MIME type to _MIME_TO_EXT
# automatically permits it here too.  Types absent from _MIME_TO_EXT (e.g.
# image/svg+xml, which can carry embedded JavaScript) are excluded by design.
# The upload pipeline's magic-byte check and Pillow decode are the definitive
# gate; this is a first layer that prevents obviously wrong content from being
# proxied to the browser at all.
_ALLOWED_PROXY_MIMETYPES = frozenset(_MIME_TO_EXT.keys())

# Config: per-source enable flags and URL security settings
GRAVATAR_ENABLED = import_cfg.get("gravatar", {}).get("enabled", True)
URL_ENABLED = import_cfg.get("url", {}).get("enabled", True)
RESTRICT_PRIVATE_IPS = import_cfg.get("url", {}).get("restrict_private_ips", True)


# Helpers


def _read_with_limit(resp: http_requests.Response) -> bytes | None:
    """
    Read a streamed response up to ``_MAX_FETCH_SIZE`` bytes.

    Returns the response body as bytes, or ``None`` if the size limit is
    exceeded.  Uses streaming so oversized responses are aborted early
    without downloading the entire body into memory.
    """
    # Early rejection via Content-Length header (if the remote server provides it)
    content_length = resp.headers.get("Content-Length", None)
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > _MAX_FETCH_SIZE
    ):
        resp.close()
        return None

    buf = bytearray()
    for chunk in resp.iter_content(8192):
        buf += chunk
        if len(buf) > _MAX_FETCH_SIZE:
            resp.close()
            return None
    return bytes(buf)


def _resolves_to_private_ip(hostname: str) -> bool:
    """
    Check if a hostname resolves to any non-globally-routable IP address.

    Prevents SSRF attacks where a user-supplied URL targets internal services
    (e.g. 127.0.0.1, 10.x.x.x, 192.168.x.x, link-local, loopback).

    Note: DNS rebinding is a known limitation of pre-request checks.  The
    caller (``_safe_fetch``) performs this check on every redirect hop and
    uses ``allow_redirects=False`` so each hop's hostname is validated before
    the next connection is opened, which eliminates the TOCTOU window that
    would exist if redirects were followed automatically.
    """
    try:
        results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # DNS resolution failed — not a private IP issue; let the HTTP
        # request fail naturally with a more descriptive error.
        return False

    for result in results:
        # Strip IPv6 scope/zone ID (e.g. '%eth0') which ipaddress does not accept
        addr_str = result[4][0].split("%")[0]
        addr = ipaddress.ip_address(addr_str)
        if not addr.is_global:
            log.warning(
                "Blocked URL import: hostname %r resolves to non-global IP %s.",
                hostname,
                addr,
            )
            return True
    return False


def _safe_fetch(url: str) -> http_requests.Response:
    """
    Fetch *url* with manual redirect following and per-hop SSRF validation.

    Each redirect hop is checked against the private-IP filter before the
    next connection is opened.  This eliminates the TOCTOU window that exists
    when ``requests`` follows redirects automatically: with automatic
    redirects the SSRF check runs on the original URL only, and a DNS
    rebinding or server-controlled redirect to ``127.0.0.1`` bypasses it.

    Only HTTP and HTTPS redirect targets are followed.  Non-HTTP schemes
    (e.g. ``file://``) are rejected immediately.

    Raises ``ValueError`` when a redirect target fails validation.
    Raises ``http_requests.RequestException`` on network failure.
    """
    for hop in range(_MAX_REDIRECTS + 1):
        parsed = urlparse(url)

        # Only HTTP(S) is allowed at every hop — not file:// or other schemes
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Redirect to non-HTTP(S) scheme blocked: {parsed.scheme!r}"
            )
        if not parsed.hostname:
            raise ValueError("Redirect target has no hostname.")

        # SSRF check on this hop's hostname
        if RESTRICT_PRIVATE_IPS and _resolves_to_private_ip(parsed.hostname):
            raise ValueError(
                f"Redirect to private/internal address blocked: {parsed.hostname!r}"
            )

        resp = http_requests.get(
            url,
            timeout=_FETCH_TIMEOUT,
            stream=True,
            allow_redirects=False,  # Manual redirect following for per-hop SSRF checks
            headers={"User-Agent": USER_AGENT},
        )

        # Follow 3xx redirects manually (up to _MAX_REDIRECTS hops)
        if resp.status_code in (301, 302, 303, 307, 308):
            if hop == _MAX_REDIRECTS:
                resp.close()
                raise ValueError(f"Too many redirects (max {_MAX_REDIRECTS}).")
            location = resp.headers.get("Location", "").strip()
            if not location:
                resp.close()
                raise ValueError("Redirect response missing Location header.")
            resp.close()
            url = location
            log.debug(
                "Following redirect (hop %d/%d): %r", hop + 1, _MAX_REDIRECTS, url
            )
            continue

        # Non-redirect response — return it to the caller
        return resp

    # Unreachable, but satisfies static analysis
    raise ValueError(f"Too many redirects (max {_MAX_REDIRECTS}).")


# Gravatar import


@import_bp.route("/api/fetch-gravatar", methods=["POST"])
@login_required
def api_fetch_gravatar():
    """
    Fetch a Gravatar image for the authenticated user's email address.

    Expects JSON body: ``{"email": "user@example.com"}``.
    The provided email must match the session user's email to prevent this
    endpoint from being used as a Gravatar account-existence oracle for
    arbitrary email addresses.

    The email is hashed with MD5 (Gravatar's lookup key) and the image
    is fetched at 1024px.  Returns the raw image bytes on success, or
    JSON error on failure.
    """
    if not GRAVATAR_ENABLED:
        return jsonify({"error": t("error.import.gravatar_disabled")}), 403

    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    body = request.get_json(silent=True) or {}
    email = (body.get("email", None) or "").strip().lower()
    if not email:
        return jsonify({"error": t("error.import.no_email")}), 400

    # Restrict to the session user's own email to prevent using this endpoint
    # as a Gravatar account-existence oracle for arbitrary email addresses.
    session_email = (session.get("user", {}).get("email", None) or "").strip().lower()
    if session_email and email != session_email:
        log.warning(
            "Gravatar fetch rejected: provided email does not match session email for user %r.",
            session.get("user", {}).get("username", "unknown"),
        )
        return jsonify({"error": "email_mismatch"}), 403

    # Gravatar keys images by the MD5 hash of the lowercase, trimmed email.
    # usedforsecurity=False signals this is not a cryptographic use (required
    # on FIPS-enabled systems where MD5 is otherwise blocked).
    md5_hash = hashlib.md5(email.encode("utf-8"), usedforsecurity=False).hexdigest()
    gravatar_url = f"https://www.gravatar.com/avatar/{md5_hash}?s=1024&d=404"

    log.debug("Fetching Gravatar: %s", gravatar_url)
    try:
        resp = http_requests.get(
            gravatar_url,
            timeout=_FETCH_TIMEOUT,
            stream=True,
            headers={"User-Agent": USER_AGENT},
        )
        # d=404 tells Gravatar to return HTTP 404 when no avatar exists
        # for this email, instead of serving a generic default image.
        if resp.status_code == 404:
            log.debug("No Gravatar found for the requested email.")
            resp.close()
            return jsonify({"error": "not_found"}), 404
        resp.raise_for_status()

        # Normalise content-type to the bare MIME type (strip parameters)
        raw_ct = resp.headers.get("Content-Type", "image/jpeg")
        content_type = raw_ct.split(";")[0].strip().lower()

        # Only proxy MIME types we explicitly accept — image/svg+xml and
        # others that can carry scripts are rejected at this layer.
        if content_type not in _ALLOWED_PROXY_MIMETYPES:
            resp.close()
            log.warning(
                "Gravatar returned unexpected Content-Type %r - rejecting.",
                content_type,
            )
            return jsonify({"error": t("error.import.unsupported_type")}), 400

        data = _read_with_limit(resp)
        if data is None:
            return jsonify(
                {"error": "image_too_large", "max_size_mb": _MAX_FETCH_SIZE_MB}
            ), 400

        # Build a filename from the hash + content-type extension so the
        # client can display a meaningful name (e.g. "d41d8cd9…f00d.jpg")
        ext = _MIME_TO_EXT.get(content_type, "jpg")
        filename = f"{md5_hash}.{ext}"

        return Response(
            data,
            mimetype=content_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'inline; filename="{filename}"',
            },
        )
    except http_requests.RequestException as exc:
        log.warning("Gravatar fetch failed: %s", exc)
        return jsonify({"error": "fetch_failed"}), 502


# Remote URL import


@import_bp.route("/api/fetch-url", methods=["POST"])
@login_required
def api_fetch_url():
    """
    Fetch an image from a user-provided remote URL.

    Expects JSON body: ``{"url": "https://example.com/photo.jpg"}``.
    Validates the URL scheme (HTTP/HTTPS only), optionally blocks private
    IP ranges (SSRF protection), and checks Content-Type (allowlisted MIME
    types only) before proxying the response.

    Each redirect hop is validated against the SSRF filter before following,
    preventing DNS rebinding and server-controlled redirect attacks.
    """
    if not URL_ENABLED:
        return jsonify({"error": t("error.import.url_disabled")}), 403

    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    body = request.get_json(silent=True) or {}
    url = (body.get("url", None) or "").strip()

    if not url:
        return jsonify({"error": t("error.import.no_url")}), 400

    # Only allow HTTP(S) to prevent file:// or other scheme abuse
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return jsonify({"error": t("error.import.https_only")}), 400
    if not parsed.hostname:
        return jsonify({"error": t("error.import.invalid_url")}), 400

    # SSRF protection: block URLs that resolve to private/internal IP ranges.
    # The actual fetch uses _safe_fetch() which re-checks each redirect hop,
    # but we validate here first for an early, user-facing error.
    if RESTRICT_PRIVATE_IPS and _resolves_to_private_ip(parsed.hostname):
        return jsonify({"error": "url_not_allowed"}), 400

    log.debug("Fetching remote image from: %r", url)
    try:
        # _safe_fetch follows redirects manually, re-applying the SSRF filter
        # at every hop to prevent DNS rebinding and redirect-based bypasses.
        resp = _safe_fetch(url)
        resp.raise_for_status()

        # Normalise content-type to the bare MIME type (strip parameters)
        content_type = (
            resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        )

        # Only proxy explicitly allowlisted MIME types.  image/svg+xml is
        # excluded because SVG can contain embedded JavaScript; accepting any
        # image/* would allow an attacker to craft a server that returns
        # malicious SVG with a spoofed content-type prefix.
        if content_type not in _ALLOWED_PROXY_MIMETYPES:
            resp.close()
            return jsonify({"error": t("error.import.unsupported_type")}), 400

        data = _read_with_limit(resp)
        if data is None:
            return jsonify(
                {"error": "image_too_large", "max_size_mb": _MAX_FETCH_SIZE_MB}
            ), 400

        return Response(
            data, mimetype=content_type, headers={"Cache-Control": "no-store"}
        )
    except ValueError as exc:
        # Raised by _safe_fetch when a redirect target is blocked or invalid
        log.warning("URL fetch blocked: %s", exc)
        return jsonify({"error": "url_not_allowed"}), 400
    except http_requests.RequestException as exc:
        log.warning("Remote image fetch failed for URL: %s", exc)
        return jsonify({"error": "fetch_failed"}), 502
