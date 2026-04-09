"""
image_import.py – Remote image import routes (Gravatar and URL).

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
from flask import Blueprint, Response, jsonify, request

from src import USER_AGENT
from src.auth import login_required
from src.config import app_cfg, import_cfg
from src.csrf import validate_csrf_token

log = logging.getLogger('img_import')

import_bp = Blueprint('import', __name__)

# Remote image fetch limits (derived from the same config as direct uploads)
_MAX_FETCH_SIZE = app_cfg.get('max_upload_size_mb', 10) * 1024 * 1024  # MB -> bytes
_MAX_FETCH_SIZE_MB = app_cfg.get('max_upload_size_mb', 10)
_FETCH_TIMEOUT = 15  # seconds

# Content-Type to file extension mapping (for Gravatar filenames)
_MIME_TO_EXT: dict[str, str] = {
    'image/jpeg': 'jpg',
    'image/png':  'png',
    'image/webp': 'webp',
    'image/gif':  'gif',
}

# Config: per-source enable flags and URL security settings
GRAVATAR_ENABLED = import_cfg.get('gravatar', {}).get('enabled', True)
URL_ENABLED = import_cfg.get('url', {}).get('enabled', True)
RESTRICT_PRIVATE_IPS = import_cfg.get('url', {}).get('restrict_private_ips', True)


# Helpers

def _read_with_limit(resp: http_requests.Response) -> bytes | None:
    """
    Read a streamed response up to ``_MAX_FETCH_SIZE`` bytes.

    Returns the response body as bytes, or ``None`` if the size limit is
    exceeded.  Uses streaming so oversized responses are aborted early
    without downloading the entire body into memory.
    """
    # Early rejection via Content-Length header (if the remote server provides it)
    content_length = resp.headers.get('Content-Length')
    if content_length and content_length.isdigit() and int(content_length) > _MAX_FETCH_SIZE:
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
    """
    try:
        results = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        # DNS resolution failed — not a private IP issue; let the HTTP
        # request fail naturally with a more descriptive error.
        return False

    for result in results:
        # Strip IPv6 scope/zone ID (e.g. '%eth0') which ipaddress does not accept
        addr_str = result[4][0].split('%')[0]
        addr = ipaddress.ip_address(addr_str)
        if not addr.is_global:
            log.warning('Blocked URL import: hostname %r resolves to non-global IP %s.', hostname, addr)
            return True
    return False


# Gravatar import

@import_bp.route('/api/fetch-gravatar', methods=['POST'])
@login_required
def api_fetch_gravatar():
    """
    Fetch a Gravatar image for the provided email address.

    Expects JSON body: ``{"email": "user@example.com"}``.
    The email is hashed with MD5 (Gravatar's lookup key) and the image
    is fetched at 1024px.  Returns the raw image bytes on success, or
    JSON error on failure.
    """
    if not GRAVATAR_ENABLED:
        return jsonify({'error': 'Gravatar import is disabled.'}), 403

    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    body = request.get_json(silent=True) or {}
    email = (body.get('email', '') or '').strip().lower()
    if not email:
        return jsonify({'error': 'No email address provided.'}), 400

    # Gravatar keys images by the MD5 hash of the lowercase, trimmed email.
    # usedforsecurity=False signals this is not a cryptographic use (required
    # on FIPS-enabled systems where MD5 is otherwise blocked).
    md5_hash = hashlib.md5(email.encode('utf-8'), usedforsecurity=False).hexdigest()
    gravatar_url = f'https://www.gravatar.com/avatar/{md5_hash}?s=1024&d=404'

    log.debug('Fetching Gravatar: %s', gravatar_url)
    try:
        resp = http_requests.get(
            gravatar_url, timeout=_FETCH_TIMEOUT, stream=True,
            headers={'User-Agent': USER_AGENT},
        )
        # d=404 tells Gravatar to return HTTP 404 when no avatar exists
        # for this email, instead of serving a generic default image.
        if resp.status_code == 404:
            log.debug('No Gravatar found for the requested email.')
            resp.close()
            return jsonify({'error': 'not_found'}), 404
        resp.raise_for_status()

        content_type = resp.headers.get('Content-Type', 'image/jpeg')
        data = _read_with_limit(resp)
        if data is None:
            return jsonify({'error': 'image_too_large', 'max_size_mb': _MAX_FETCH_SIZE_MB}), 400

        # Build a filename from the hash + content-type extension so the
        # client can display a meaningful name (e.g. "d41d8cd9…f00d.jpg")
        ext = _MIME_TO_EXT.get(content_type.split(';')[0].strip(), 'jpg')
        filename = f'{md5_hash}.{ext}'

        return Response(data, mimetype=content_type,
                        headers={
                            'Cache-Control': 'no-store',
                            'Content-Disposition': f'inline; filename="{filename}"',
                        })
    except http_requests.RequestException as exc:
        log.warning('Gravatar fetch failed: %s', exc)
        return jsonify({'error': 'fetch_failed'}), 502


# Remote URL import

@import_bp.route('/api/fetch-url', methods=['POST'])
@login_required
def api_fetch_url():
    """
    Fetch an image from a user-provided remote URL.

    Expects JSON body: ``{"url": "https://example.com/photo.jpg"}``.
    Validates the URL scheme (HTTP/HTTPS only), optionally blocks private
    IP ranges (SSRF protection), and checks Content-Type (image/* only)
    before proxying the response.
    """
    if not URL_ENABLED:
        return jsonify({'error': 'URL import is disabled.'}), 403

    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    body = request.get_json(silent=True) or {}
    url = (body.get('url', '') or '').strip()

    if not url:
        return jsonify({'error': 'No URL provided.'}), 400

    # Only allow HTTP(S) to prevent file:// or other scheme abuse
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        return jsonify({'error': 'Only HTTP and HTTPS URLs are supported.'}), 400
    if not parsed.hostname:
        return jsonify({'error': 'Invalid URL.'}), 400

    # SSRF protection: block URLs that resolve to private/internal IP ranges
    if RESTRICT_PRIVATE_IPS and _resolves_to_private_ip(parsed.hostname):
        return jsonify({'error': 'url_not_allowed'}), 400

    log.debug('Fetching remote image from: %r', url)
    try:
        resp = http_requests.get(
            url, timeout=_FETCH_TIMEOUT, stream=True,
            headers={'User-Agent': USER_AGENT},
        )
        resp.raise_for_status()

        content_type = resp.headers.get('Content-Type', '')
        if not content_type.startswith('image/'):
            resp.close()
            return jsonify({'error': 'URL does not point to an image.'}), 400

        data = _read_with_limit(resp)
        if data is None:
            return jsonify({'error': 'image_too_large', 'max_size_mb': _MAX_FETCH_SIZE_MB}), 400

        return Response(data, mimetype=content_type,
                        headers={'Cache-Control': 'no-store'})
    except http_requests.RequestException as exc:
        log.warning('Remote image fetch failed for URL: %s', exc)
        return jsonify({'error': 'fetch_failed'}), 502
