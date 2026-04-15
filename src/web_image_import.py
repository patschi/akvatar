"""
web_image_import.py - Remote image import routes (Gravatar and URL).

Provides proxy endpoints that fetch images from external sources on behalf
of the authenticated user.  Proxying is required so that fetched images are
served from the same origin as the app - without this, the browser marks
cross-origin images drawn on an HTML canvas as "tainted", which prevents
Cropper.js from reading pixel data.

Core fetch helpers, configuration constants, and exception classes are in
image_import.py.

Both endpoints require authentication (``@login_required``) and CSRF
validation to prevent abuse and cross-site request forgery.

Routes:
  - POST /api/fetch-gravatar -> fetch and proxy a Gravatar image
  - POST /api/fetch-url      -> fetch and proxy an image from a user-provided URL
"""

import logging

from flask import Blueprint, Response, jsonify, request, session

from src.auth import login_required
from src.i18n import t
from src.image_import import (
    GRAVATAR_ENABLED,
    MAX_FETCH_SIZE_MB,
    URL_ENABLED,
    FetchFailed,
    GravatarNotFound,
    ImageTooLarge,
    UnsupportedContentType,
    fetch_gravatar_image,
    fetch_remote_image,
    validate_gravatar_email,
    validate_import_url,
)
from src.rate_limit import check_gravatar_import_cooldown, check_url_import_cooldown
from src.sec_csrf import validate_csrf_token

log = logging.getLogger("import_img")

import_bp = Blueprint("import", __name__)


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
    """
    if not GRAVATAR_ENABLED:
        return jsonify({"error": t("error.import.gravatar_disabled")}), 403

    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    user = session.get("user", {})

    # Per-user import cooldown - prevents abuse as an outbound HTTP proxy
    allowed, retry_after = check_gravatar_import_cooldown(user.get("pk", 0))
    if not allowed:
        log.warning(
            "Gravatar import cooldown: denied for user %r (retry_after=%ds).",
            user.get("username", "unknown"),
            retry_after,
        )
        return (
            jsonify({"error": t("error.rate_limited"), "retry_after": retry_after}),
            429,
            {"Retry-After": str(retry_after)},
        )

    body = request.get_json(silent=True) or {}
    email = (body.get("email", None) or "").strip().lower()
    if not email:
        return jsonify({"error": t("error.import.no_email")}), 400
    email_error = validate_gravatar_email(
        email, user.get("email", ""), user.get("username", "unknown")
    )
    if email_error:
        return jsonify({"error": email_error}), 403

    try:
        data, content_type, filename = fetch_gravatar_image(email)
    except GravatarNotFound:
        return jsonify({"error": "not_found"}), 404
    except ImageTooLarge:
        return jsonify(
            {"error": "image_too_large", "max_size_mb": MAX_FETCH_SIZE_MB}
        ), 400
    except UnsupportedContentType:
        return jsonify({"error": t("error.import.unsupported_type")}), 400
    except FetchFailed as exc:
        log.warning("Gravatar fetch failed: %s", exc)
        return jsonify({"error": "fetch_failed"}), 502

    return Response(
        data,
        mimetype=content_type,
        headers={
            "Cache-Control": "no-store",
            "Content-Disposition": f'inline; filename="{filename}"',
        },
    )


# Remote URL import


@import_bp.route("/api/fetch-url", methods=["POST"])
@login_required
def api_fetch_url():
    """
    Fetch an image from a user-provided remote URL.

    Expects JSON body: ``{"url": "https://example.com/photo.jpg"}``.
    Validates the URL scheme (HTTP/HTTPS only) and optionally blocks private
    IP ranges (SSRF protection) before proxying the response.

    Each redirect hop is re-validated against the SSRF filter inside
    fetch_remote_image, preventing DNS rebinding and redirect-based bypasses.
    """
    if not URL_ENABLED:
        return jsonify({"error": t("error.import.url_disabled")}), 403

    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    # Per-user import cooldown - prevents abuse as an outbound HTTP proxy
    user = session.get("user", {})
    allowed, retry_after = check_url_import_cooldown(user.get("pk", 0))
    if not allowed:
        log.warning(
            "URL import cooldown: denied for user %r (retry_after=%ds).",
            user.get("username", "unknown"),
            retry_after,
        )
        return (
            jsonify({"error": t("error.rate_limited"), "retry_after": retry_after}),
            429,
            {"Retry-After": str(retry_after)},
        )

    body = request.get_json(silent=True) or {}
    url = (body.get("url", None) or "").strip()
    if not url:
        return jsonify({"error": t("error.import.no_url")}), 400

    url_error = validate_import_url(url)
    if url_error:
        return jsonify({"error": url_error}), 400

    try:
        data, content_type = fetch_remote_image(url)
    except ValueError as exc:
        # Raised by safe_fetch when a redirect target is blocked or invalid
        log.warning("URL fetch blocked: %s", exc)
        return jsonify({"error": "url_not_allowed"}), 400
    except ImageTooLarge:
        return jsonify(
            {"error": "image_too_large", "max_size_mb": MAX_FETCH_SIZE_MB}
        ), 400
    except UnsupportedContentType:
        return jsonify({"error": t("error.import.unsupported_type")}), 400
    except FetchFailed as exc:
        log.warning("Remote image fetch failed for URL: %s", exc)
        return jsonify({"error": "fetch_failed"}), 502

    return Response(data, mimetype=content_type, headers={"Cache-Control": "no-store"})
