"""
routes.py – Flask route definitions.

Contains:
  - GET  /                              -> redirect to /login
  - GET  /login                         -> public login page (unauthenticated)
  - GET  /dashboard                     -> avatar upload / crop page (authenticated)
  - GET  /user-avatars/NxN/<file>       -> serve stored avatar images
  - GET  /user-avatars/_metadata/<file> -> serve avatar metadata JSON (access controlled by app.metadata_access)
  - GET  /api/session                   -> lightweight session liveness probe (JSON)
  - POST /api/upload                    -> accept cropped image, process, update backends
"""

import json
import logging
import re

from flask import (
    Blueprint,
    Response,
    abort,
    redirect,
    url_for,
    session,
    request,
    jsonify,
    send_from_directory,
    render_template,
    stream_with_context,
)

from src.app_static import serve_static_file
from src.auth import login_required
from src.config import app_cfg
from src.i18n import t
from src.sec_csrf import validate_csrf_token
from src.image_import import GRAVATAR_ENABLED, URL_ENABLED
from src.imaging import AVATAR_ROOT, METADATA_ROOT, MAX_SIZE, ALLOWED_EXTENSIONS
from src.ldap_client import is_enabled as ldap_is_enabled
from src.upload import validate_upload, generate_sse, ValidationError

log = logging.getLogger("routes")

routes_bp = Blueprint("routes", __name__)

# Allowed error keys for the login page (reject arbitrary reflected strings)
_VALID_ERROR_KEYS = frozenset({"oidc_failed", "pk_failed", "session_expired"})


# robots.txt – serve from static cache (crawlers expect /robots.txt at the root)
@routes_bp.route("/robots.txt")
def robots_txt():
    """Serve robots.txt from the in-memory static cache."""
    return serve_static_file("robots.txt")


# Health check
@routes_bp.route("/healthz")
def healthz():
    """Lightweight health probe for load balancers or healthchecks."""
    return Response("OK", mimetype="text/plain")


# Root – forward to the login page
@routes_bp.route("/")
def root():
    """Redirect the root URL to the login page."""
    return redirect(url_for("routes.login_page"))


# Public login page
@routes_bp.route("/login")
def login_page():
    """Show the login page with a sign-in button, or redirect to dashboard if already authenticated."""
    if "user" in session:
        log.debug("User already authenticated – redirecting to dashboard.")
        return redirect(url_for("routes.dashboard"))
    error_key = request.args.get("error", "")
    if not error_key and "autologin" in request.args:
        return redirect(url_for("auth.login"))
    if error_key not in _VALID_ERROR_KEYS:
        error_key = ""
    if error_key:
        log.debug("Login page rendered with error=%r.", error_key)
    return render_template("login.html", error_key=error_key)


# Dashboard (authenticated)
@routes_bp.route("/dashboard")
@login_required
def dashboard():
    """Serve the authenticated avatar upload / crop page."""
    user = session["user"]
    log.debug("Serving dashboard for user %r.", user["username"])

    # Build user initials: first letter of first name + first letter of last name.
    # Falls back to the first letter of the username if name parts are unavailable.
    name_parts = user.get("name", "").split()
    if len(name_parts) >= 2:
        initials = (name_parts[0][0] + name_parts[-1][0]).upper()
    else:
        initials = (user.get("username", "") or "?")[0].upper()

    return render_template(
        "dashboard.html",
        user=user,
        user_initials=initials,
        ldap_enabled=ldap_is_enabled(),
        max_size=MAX_SIZE,
        allowed_extensions=sorted(ALLOWED_EXTENSIONS),
        import_gravatar_enabled=GRAVATAR_ENABLED,
        import_url_enabled=URL_ENABLED,
    )


# Serve stored avatar files
# Dimensions must be NxN (e.g. "256x256") – reject anything else before touching the filesystem
_DIMENSIONS_RE = re.compile(r"^\d{1,5}x\d{1,5}$")


@routes_bp.route("/user-avatars/<dimensions>/<filename>")
def serve_avatar(dimensions, filename):
    """Serve avatar image files from the storage directory. `send_from_directory` prevents directory-traversal attacks."""
    if not _DIMENSIONS_RE.match(dimensions):
        log.debug("Avatar request rejected – invalid dimensions: %r", dimensions)
        abort(404)
    filepath = f"{dimensions}/{filename}"
    log.debug("Serving avatar file: %s", filepath)
    # Avatar URLs are immutable: filenames are cryptographically random per upload
    # (uuid4 + token_urlsafe + nanosecond timestamp).  A new upload always produces
    # a new URL, so the content at any given URL never changes.  The immutable
    # directive tells supporting browsers not to revalidate even on explicit refresh.
    resp = send_from_directory(AVATAR_ROOT, filepath)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# Serve avatar metadata JSON files
# Access control is governed by app.metadata_access in config.yml:
#   "owner_only" (default) – only the authenticated user who owns the file may access it
#   "public"               – no authentication required
_METADATA_ACCESS_MODES = frozenset({"owner_only", "public"})


@routes_bp.route("/user-avatars/_metadata/<filename>")
def serve_avatar_metadata(filename):
    """Serve avatar metadata JSON from the storage directory.

    In owner_only mode the requesting session user must match the user_pk stored
    inside the metadata file.  A 404 is returned for both missing files and
    ownership mismatches so callers cannot distinguish the two cases.
    """
    metadata_access = app_cfg.get("metadata_access", None)
    if metadata_access not in _METADATA_ACCESS_MODES:
        if metadata_access is not None:
            log.warning(
                "Unknown app.metadata_access value %r – falling back to owner_only.",
                metadata_access,
            )
        metadata_access = "owner_only"

    if metadata_access == "owner_only":
        # Must be authenticated first
        if "user" not in session:
            log.debug(
                "Unauthenticated metadata request for %r – redirecting to login.", filename
            )
            return redirect(url_for("routes.login_page"))

        # Read the metadata to verify that the requesting user owns this file
        meta_path = METADATA_ROOT / filename
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # File not found or unreadable – return 404 without leaking details
            abort(404)

        # Reject access when the session user is not the file owner.
        # Return 404 (not 403) so callers cannot distinguish "not found" from "not yours".
        if meta.get("user_pk", None) != session["user"].get("pk", None):
            log.debug(
                "Metadata access denied for %r – user pk mismatch (session pk=%r).",
                filename,
                session["user"].get("pk", None),
            )
            abort(404)

    log.debug("Serving metadata file: %s (access=%s)", filename, metadata_access)
    resp = send_from_directory(METADATA_ROOT, filename, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# Session liveness probe (used by the dashboard for client-side expiry detection)
@routes_bp.route("/api/session")
def api_session_check():
    """Return 200 {"alive": true} while the session is valid, 401 {"alive": false} when expired.

    Called periodically by the dashboard JS so the user is redirected to the login
    page before they discover their session is gone only upon form submission.
    Not decorated with @login_required because that would return an HTML redirect
    instead of a JSON response.
    """
    if "user" in session:
        return jsonify({"alive": True})
    return jsonify({"alive": False}), 401


# Upload & process API (Server-Sent Events for real-time progress)
@routes_bp.route("/api/upload", methods=["POST"])
@login_required
def api_upload():
    """
    Accept a cropped image blob, validate it synchronously, then stream
    processing progress back to the client as Server-Sent Events.

    Validation failures return a normal JSON 400 response.
    Once validation passes the response switches to ``text/event-stream``
    and each processing step is pushed as it completes.
    """
    # CSRF token validation (returns JSON 403 on failure)
    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    user = session["user"]
    log.info("Upload request from user %r.", user["username"])

    # Synchronous validation (returns JSON 400 on failure)
    if "file" not in request.files:
        log.warning("Upload rejected – no file part in request.")
        return jsonify({"error": t("error.no_file")}), 400

    try:
        image = validate_upload(request.files["file"])
    except ValidationError as exc:
        log.warning("Upload rejected: %s", exc)
        return jsonify({"error": str(exc)}), 400

    log.info(
        "Image validated – mode=%s, size=%dx%d. Starting SSE stream.",
        image.mode,
        image.width,
        image.height,
    )

    # Stream processing progress as SSE
    return Response(
        stream_with_context(generate_sse(user, image)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
