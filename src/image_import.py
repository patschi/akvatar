"""
image_import.py - Remote image import helpers.

Provides HTTP fetch utilities and configuration constants used by the
remote image import routes (web_image_import.py).

Handles:
  - Remote image fetch size limiting
  - SSRF protection (private IP blocking, per-hop redirect validation)
  - Configuration flags for Gravatar, URL, and webcam import sources
"""

import hashlib
import ipaddress
import logging
import socket
from urllib.parse import urlparse

import requests as http_requests

from src import USER_AGENT
from src.config import app_cfg, import_cfg
from src.i18n import t
from src.image_formats import ALLOWED_PROXY_MIMETYPES, MIME_TO_EXT

log = logging.getLogger("img_import")

# Remote image fetch limits (derived from the same config as direct uploads)
MAX_FETCH_SIZE_MB = app_cfg.get("max_upload_size_mb", 10)
_MAX_FETCH_SIZE = MAX_FETCH_SIZE_MB * 1024 * 1024  # MB -> bytes
FETCH_TIMEOUT = 15  # seconds
_MAX_REDIRECTS = 5  # maximum redirect hops to follow during URL import

# Config: per-source enable flags and URL security settings
GRAVATAR_ENABLED = import_cfg.get("gravatar", {}).get("enabled", True)
# When True, the Gravatar email input is locked to the session user's email and
# the backend enforces a strict match - preventing oracle lookups for arbitrary emails.
GRAVATAR_RESTRICT_EMAIL = import_cfg.get("gravatar", {}).get("restrict_email", True)
URL_ENABLED = import_cfg.get("url", {}).get("enabled", True)
# Webcam capture is handled entirely client-side via MediaDevices.getUserMedia,
# so no proxy endpoint is needed - this flag only controls UI visibility and
# the Permissions-Policy header sent with HTML responses.
WEBCAM_ENABLED = import_cfg.get("webcam", {}).get("enabled", True)
RESTRICT_PRIVATE_IPS = import_cfg.get("url", {}).get("restrict_private_ips", True)


def read_with_limit(resp: http_requests.Response) -> bytes | None:
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


def resolves_to_private_ip(hostname: str) -> bool:
    """
    Check if a hostname resolves to any non-globally-routable IP address.

    Prevents SSRF attacks where a user-supplied URL targets internal services
    (e.g. 127.0.0.1, 10.x.x.x, 192.168.x.x, link-local, loopback).

    Note: DNS rebinding is a known limitation of pre-request checks.  The
    caller (``safe_fetch``) performs this check on every redirect hop and
    uses ``allow_redirects=False`` so each hop's hostname is validated before
    the next connection is opened, which eliminates the TOCTOU window that
    would exist if redirects were followed automatically.
    """
    try:
        results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # DNS resolution failed - not a private IP issue; let the HTTP
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


def safe_fetch(url: str) -> http_requests.Response:
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

        # Only HTTP(S) is allowed at every hop - not file:// or other schemes
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"Redirect to non-HTTP(S) scheme blocked: {parsed.scheme!r}"
            )
        if not parsed.hostname:
            raise ValueError("Redirect target has no hostname.")

        # SSRF check on this hop's hostname
        if RESTRICT_PRIVATE_IPS and resolves_to_private_ip(parsed.hostname):
            raise ValueError(
                f"Redirect to private/internal address blocked: {parsed.hostname!r}"
            )

        resp = http_requests.get(
            url,
            timeout=FETCH_TIMEOUT,
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

        # Non-redirect response - return it to the caller
        return resp

    # Unreachable, but satisfies static analysis
    raise ValueError(f"Too many redirects (max {_MAX_REDIRECTS}).")


# Exceptions raised by fetch helpers - allow route handlers to pattern-match
# on error type rather than inspecting strings.


class ImageFetchError(Exception):
    """Base class for remote image fetch errors."""


class GravatarNotFound(ImageFetchError):
    """Gravatar returned HTTP 404 - no avatar exists for this email."""


class FetchFailed(ImageFetchError):
    """Network-level failure during a remote image fetch."""


class ImageTooLarge(ImageFetchError):
    """Response body exceeds the configured per-request size limit."""


class UnsupportedContentType(ImageFetchError):
    """Remote server returned a MIME type outside the allowed set."""

    def __init__(self, content_type: str) -> None:
        self.content_type = content_type
        super().__init__(f"Unsupported content type: {content_type!r}")


# Validation helpers


def validate_gravatar_email(
    email: str, session_email: str, username: str
) -> str | None:
    """
    Validate a submitted email for Gravatar lookup against the session user's email.

    Enforces oracle-prevention: prevents using the endpoint to probe whether
    arbitrary email addresses have a Gravatar.

    Returns None if the email is acceptable, or a raw error key string if
    rejected ("email_mismatch").  The caller maps the key to a JSON 403 response.
    """
    session_email = session_email.strip().lower()
    if GRAVATAR_RESTRICT_EMAIL and not session_email:
        log.warning(
            "Gravatar fetch rejected: restrict_email is enabled but session has no email for user %r.",
            username,
        )
        return "email_mismatch"
    if session_email and email != session_email:
        log.warning(
            "Gravatar fetch rejected: provided email does not match session email for user %r.",
            username,
        )
        return "email_mismatch"
    return None


def build_gravatar_url(email: str, size: int = 1024) -> tuple[str, str]:
    """
    Build the Gravatar image URL and MD5 lookup hash for a given email.

    Gravatar keys images by the MD5 hash of the lowercase, trimmed email.
    ``usedforsecurity=False`` signals this is not a cryptographic use (required
    on FIPS-enabled systems where MD5 is otherwise blocked).

    Returns (gravatar_url, md5_hash).
    """
    md5_hash = hashlib.md5(email.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"https://www.gravatar.com/avatar/{md5_hash}?s={size}&d=404", md5_hash


def validate_import_url(url: str) -> str | None:
    """
    Validate a user-supplied URL before remote image fetch.

    Checks scheme (HTTP/HTTPS only), hostname presence, and optional SSRF
    protection (private IP block).

    Returns None if the URL is acceptable, or the error value to put in the
    JSON response body if rejected (translated for user-facing errors,
    raw machine key for security-sensitive rejections).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return t("error.import.https_only")
    if not parsed.hostname:
        return t("error.import.invalid_url")
    if RESTRICT_PRIVATE_IPS and resolves_to_private_ip(parsed.hostname):
        return "url_not_allowed"
    return None


# Fetch helpers


def _validate_and_read(resp: http_requests.Response) -> tuple[bytes, str]:
    """
    Normalize the response Content-Type, check it against the proxy allowlist,
    and read the body up to the configured size limit.

    Returns (body_bytes, normalized_content_type).

    Raises:
      UnsupportedContentType - MIME type is not in ALLOWED_PROXY_MIMETYPES.
      ImageTooLarge          - body exceeds MAX_FETCH_SIZE_MB.
    """
    raw_ct = resp.headers.get("Content-Type", "")
    content_type = raw_ct.split(";")[0].strip().lower()
    if content_type not in ALLOWED_PROXY_MIMETYPES:
        resp.close()
        log.warning(
            "Proxy response has unsupported Content-Type %r - rejecting.", content_type
        )
        raise UnsupportedContentType(content_type)
    data = read_with_limit(resp)
    if data is None:
        raise ImageTooLarge()
    return data, content_type


def fetch_gravatar_image(email: str) -> tuple[bytes, str, str]:
    """
    Fetch the Gravatar image for a given email address.

    Returns (image_bytes, content_type, filename).

    Raises:
      GravatarNotFound   - Gravatar returned HTTP 404 (no avatar for this email).
      ImageTooLarge      - response body exceeds MAX_FETCH_SIZE_MB.
      UnsupportedContentType - Content-Type is not in the proxy allowlist.
      FetchFailed        - network or HTTP error during the fetch.
    """
    gravatar_url, md5_hash = build_gravatar_url(email)
    log.debug("Fetching Gravatar: %s", gravatar_url)

    try:
        resp = http_requests.get(
            gravatar_url,
            timeout=FETCH_TIMEOUT,
            stream=True,
            headers={"User-Agent": USER_AGENT},
        )
        # d=404 tells Gravatar to return HTTP 404 when no avatar exists
        # for this email, instead of serving a generic default image.
        if resp.status_code == 404:
            resp.close()
            log.debug("No Gravatar found for the requested email.")
            raise GravatarNotFound()
        resp.raise_for_status()

        data, content_type = _validate_and_read(resp)

        # Build a filename from the hash + content-type extension so the
        # client can display a meaningful name (e.g. "d41d8cd9...f00d.jpg")
        ext = MIME_TO_EXT.get(content_type, "jpg")
        return data, content_type, f"{md5_hash}.{ext}"

    except (GravatarNotFound, ImageTooLarge, UnsupportedContentType):
        raise
    except http_requests.RequestException as exc:
        log.warning("Gravatar fetch failed: %s", exc)
        raise FetchFailed(str(exc)) from exc


def fetch_remote_image(url: str) -> tuple[bytes, str]:
    """
    Fetch an image from a remote URL with per-hop SSRF protection.

    Uses safe_fetch() to follow redirects manually, re-checking each hop
    against the private-IP filter to prevent DNS rebinding attacks.

    Returns (image_bytes, content_type).

    Raises:
      ValueError          - redirect target failed SSRF/scheme validation (from safe_fetch).
      ImageTooLarge       - response body exceeds MAX_FETCH_SIZE_MB.
      UnsupportedContentType - Content-Type is not in the proxy allowlist.
      FetchFailed         - network or HTTP error during the fetch.
    """
    try:
        resp = safe_fetch(url)
        resp.raise_for_status()
        data, content_type = _validate_and_read(resp)
        return data, content_type

    except (ValueError, ImageTooLarge, UnsupportedContentType):
        raise
    except http_requests.RequestException as exc:
        log.warning("Remote image fetch failed for URL: %s", exc)
        raise FetchFailed(str(exc)) from exc
