"""
routes.py - Flask route definitions.

Contains:
  - GET  /                              -> redirect to /login
  - GET  /login                         -> public login page (unauthenticated)
  - GET  /dashboard                     -> avatar upload / crop page (authenticated)
  - GET  /user-avatars/NxN/<file>       -> serve stored avatar images
  - GET  /user-avatars/_metadata/<file> -> serve avatar metadata JSON (access controlled by security.metadata_access)
  - GET  /api/heartbeat                 -> lightweight session liveness probe (JSON)
  - POST /api/upload                    -> accept cropped image, process, update backends
  - POST /api/upload/commit             -> commit pending avatar URL into the session cookie
"""

import logging
import re

from flask import (
    Blueprint,
    Response,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    stream_with_context,
    url_for,
)

from src.app_static import serve_static_file
from src.auth import build_user_initials, login_required
from src.config import img_formats, metadata_access
from src.i18n import t
from src.image_formats import ALLOWED_EXTENSIONS, NEGOTIATION_PREFERENCE
from src.image_import import (
    GRAVATAR_ENABLED,
    GRAVATAR_RESTRICT_EMAIL,
    URL_ENABLED,
    WEBCAM_ENABLED,
)
from src.image_validation import ValidationError, validate_upload
from src.imaging import (
    AVATAR_ROOT,
    MAX_SIZE,
    METADATA_ROOT,
    generate_filename,
    load_metadata_file,
)
from src.ldap_client import is_enabled as ldap_is_enabled
from src.rate_limit import check_upload_cooldown
from src.sec_csrf import validate_csrf_token
from src.upload import (
    build_canonical_url,
    generate_sse,
)

log = logging.getLogger("routes")

routes_bp = Blueprint("routes", __name__)

# Allowed error keys for the login page (reject arbitrary reflected strings)
_VALID_ERROR_KEYS = frozenset({"oidc_failed", "pk_failed", "session_expired"})


# robots.txt - serve from static cache (crawlers expect /robots.txt at the root)
@routes_bp.route("/robots.txt", methods=["GET"])
def robots_txt():
    """Serve robots.txt from the in-memory static cache."""
    return serve_static_file("robots.txt")


# Health check
@routes_bp.route("/healthz", methods=["GET"])
def healthz():
    """Lightweight health probe for load balancers or healthchecks."""
    return Response("OK", mimetype="text/plain")


# Root - forward to the login page
@routes_bp.route("/", methods=["GET"])
def root():
    """Redirect the root URL to the login page."""
    return redirect(url_for("routes.login_page"))


# Public login page
@routes_bp.route("/login", methods=["GET"])
def login_page():
    """Show the login page with a sign-in button, or redirect to dashboard if already authenticated."""
    if "user" in session:
        log.debug("User already authenticated - redirecting to dashboard.")
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
@routes_bp.route("/dashboard", methods=["GET"])
@login_required
def dashboard():
    """Serve the authenticated avatar upload / crop page."""
    user = session["user"]
    log.debug("Serving dashboard for user %r.", user["username"])
    return render_template(
        "dashboard.html",
        user=user,
        user_initials=build_user_initials(user),
        ldap_enabled=ldap_is_enabled(),
        max_size=MAX_SIZE,
        allowed_extensions=sorted(ALLOWED_EXTENSIONS),
        import_gravatar_enabled=GRAVATAR_ENABLED,
        import_gravatar_restrict_email=GRAVATAR_RESTRICT_EMAIL,
        import_url_enabled=URL_ENABLED,
        import_webcam_enabled=WEBCAM_ENABLED,
    )


# Serve stored avatar files
# Dimensions must be NxN (e.g. "256x256") - reject anything else before touching the filesystem
_DIMENSIONS_RE = re.compile(r"^\d{1,5}x\d{1,5}$")

# Build once at import time from the configured formats list.
# NEGOTIATION_PREFERENCE (mime, ext) order is imported from image_formats.
_CONFIGURED_EXTS: frozenset[str] = frozenset(fmt.lower() for fmt in img_formats)


def _negotiate_avatar_format() -> str:
    """
    Pick the best image format for the requesting client based on the Accept
    header, constrained to formats present in images.formats config.

    Returns the file extension string (e.g. "webp", "jpg").
    Preference order: AVIF > WebP > PNG > JPEG.
    Falls back to the first configured format in that preference order when
    the Accept header does not match any supported format, or "jpg" as a last resort.
    """
    accept = request.headers.get("Accept", "")
    # First pass: find the best format the client explicitly accepts
    for mime, ext in NEGOTIATION_PREFERENCE:
        if mime in accept and ext in _CONFIGURED_EXTS:
            return ext
    # Second pass: fall back to the best configured format regardless of Accept
    for _, ext in NEGOTIATION_PREFERENCE:
        if ext in _CONFIGURED_EXTS:
            return ext
    return "jpg"


@routes_bp.route("/user-avatars/<dimensions>/<filename>", methods=["GET"])
def serve_avatar(dimensions, filename):
    """Serve avatar image files from the storage directory.

    Requests with an explicit extension (e.g. ``photo.jpg``) are served directly.
    Requests with no extension (e.g. ``photo``) trigger content negotiation: the
    server selects the best available format based on the Accept header and issues
    a temporary redirect to the explicit URL.  This lets clients and CDNs use
    extension-less canonical URLs while still receiving the optimal format.

    ``send_from_directory`` prevents directory-traversal attacks.
    """
    if not _DIMENSIONS_RE.match(dimensions):
        log.debug("Avatar request rejected - invalid dimensions: %r", dimensions)
        abort(404)

    # Content negotiation: if no extension in the filename, pick the best format
    # and redirect to the explicit URL.  "." is a reliable separator since the
    # generated filename base never contains a dot.
    if "." not in filename:
        ext = _negotiate_avatar_format()
        log.debug(
            "Content negotiation for %s/%s -> .%s (Accept: %s).",
            dimensions,
            filename,
            ext,
            request.headers.get("Accept", ""),
        )
        target_url = url_for(
            "routes.serve_avatar", dimensions=dimensions, filename=f"{filename}.{ext}"
        )
        resp = redirect(target_url, code=302)
        # Vary: Accept tells CDNs and proxies that the redirect target depends on the
        # Accept header, so they must not serve the same redirect to all clients.
        resp.headers["Vary"] = "Accept"
        resp.headers["Cache-Control"] = "no-store"
        return resp

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
# Access control is governed by security.metadata_access in config.yml:
#   "owner_only" (default) - only the authenticated user who owns the file may access it
#   "public"               - no authentication required
# Validation and fallback are handled centrally in config.py at startup.
_METADATA_ACCESS_MODE = metadata_access


@routes_bp.route("/user-avatars/_metadata/<filename>", methods=["GET"])
def serve_avatar_metadata(filename):
    """Serve avatar metadata JSON from the storage directory.

    In owner_only mode the requesting session user must match the user_pk stored
    inside the metadata file.  A 404 is returned for both missing files and
    ownership mismatches so callers cannot distinguish the two cases.
    """
    if _METADATA_ACCESS_MODE == "owner_only":
        if "user" not in session:
            log.debug(
                "Unauthenticated metadata request for %r - redirecting to login.",
                filename,
            )
            return redirect(url_for("routes.login_page"))

        # Load and verify ownership - 404 for both missing files and pk mismatches
        # so callers cannot distinguish "not found" from "not yours".
        meta = load_metadata_file(filename)
        if meta is None or meta.get("user_pk", None) != session["user"].get("pk", None):
            if meta is not None:
                log.debug(
                    "Metadata access denied for %r - user pk mismatch (session pk=%r).",
                    filename,
                    session["user"].get("pk", None),
                )
            abort(404)

        # Serve the already-loaded dict directly - avoids a second disk read.
        log.debug("Serving metadata file: %s (access=owner_only)", filename)
        resp = jsonify(meta)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    log.debug("Serving metadata file: %s (access=public)", filename)
    resp = send_from_directory(METADATA_ROOT, filename, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp


# Session liveness probe (used by the dashboard for client-side expiry detection)
@routes_bp.route("/api/heartbeat", methods=["GET"])
def api_heartbeat():
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

    # Per-user upload rate limit - once every 10 seconds per user
    allowed, retry_after = check_upload_cooldown(user["pk"])
    if not allowed:
        log.warning(
            "Upload cooldown: denied for user %r (retry_after=%ds).",
            user["username"],
            retry_after,
        )
        return (
            jsonify({"error": t("error.rate_limited"), "retry_after": retry_after}),
            429,
            {"Retry-After": str(retry_after)},
        )

    # Synchronous validation (returns JSON 400 on failure)
    if "file" not in request.files:
        log.warning("Upload rejected - no file part in request.")
        return jsonify({"error": t("error.no_file")}), 400

    try:
        image = validate_upload(request.files["file"])
    except ValidationError as exc:
        log.warning("Upload rejected: %s", exc)
        return jsonify({"error": str(exc)}), 400

    log.info(
        "Image validated - mode=%s, size=%dx%d. Starting SSE stream.",
        image.mode,
        image.width,
        image.height,
    )

    # Pre-generate the filename and canonical URL here (before the SSE response
    # is returned) so they can be stored in the session cookie in this normal
    # request/response cycle.  The SSE generator commits response headers before
    # it runs, so any session mutation inside the generator would be lost for
    # cookie-based sessions.  The client calls /api/upload/commit after
    # receiving the done event to promote _pending_avatar to the active avatar.
    filename_base = generate_filename()
    session["_pending_avatar"] = build_canonical_url(filename_base)

    # Stream processing progress as SSE
    return Response(
        stream_with_context(generate_sse(user, image, filename_base)),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# Upload commit (called by the client after a successful SSE upload stream)
@routes_bp.route("/api/upload/commit", methods=["POST"])
@login_required
def api_upload_commit():
    """
    Commit the pending avatar URL from the session into the active user record.

    During api_upload, the canonical URL is stored under session["_pending_avatar"]
    before the SSE stream begins so it is captured in the cookie header of that
    request.  The client calls this endpoint (with no body) after the done event
    so the pending URL is promoted to session["user"]["avatar"] in a normal
    request/response cycle where Set-Cookie is properly written.
    """
    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    # Promote the pending avatar URL (stored before the SSE stream started) to
    # the active session avatar.  Using pop() atomically reads and removes the
    # key so a second call for the same upload returns 400 rather than a stale value.
    pending_url = session.pop("_pending_avatar", None)
    if not pending_url:
        log.warning(
            "Session avatar commit rejected - no pending avatar for user %r.",
            session["user"].get("username", "?"),
        )
        return jsonify({"error": "no_pending_avatar"}), 400

    session["user"] = {**session["user"], "avatar": pending_url}
    log.debug(
        "Session avatar committed for user %r.", session["user"].get("username", "?")
    )
    return "", 204

