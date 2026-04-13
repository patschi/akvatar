"""
web_serve_avatar.py - Avatar and metadata serving routes.

Contains:
  - GET  /user-avatars/NxN/<file>       -> serve stored avatar images (with content negotiation)
  - GET  /user-avatars/_metadata/<file> -> serve avatar metadata JSON (access controlled by security.metadata_access)
"""

import logging
import re
from pathlib import Path

from flask import (
    Blueprint,
    abort,
    jsonify,
    redirect,
    request,
    send_from_directory,
    session,
    url_for,
)

from src.config import img_formats, img_sizes, metadata_access
from src.image_formats import NEGOTIATION_PREFERENCE
from src.imaging import AVATAR_ROOT, METADATA_ROOT, load_metadata_file

log = logging.getLogger("serve_avatar")

serve_avatar_bp = Blueprint("serve_avatar", __name__)

# ---------------------------------------------------------------------------
# Shared validation helpers
# ---------------------------------------------------------------------------

_DIMENSIONS_RE = re.compile(r"^\d{1,5}x\d{1,5}$")

# Build once at import time from the configured sizes / formats lists.
_CONFIGURED_SIZES: frozenset[int] = frozenset(img_sizes)
_CONFIGURED_EXTS: frozenset[str] = frozenset(fmt.lower() for fmt in img_formats)


def _validate_dimensions(dimensions: str) -> bool:
    """Return True if *dimensions* is a valid, enabled square size like '256x256'."""
    if not _DIMENSIONS_RE.match(dimensions):
        return False
    w, _, h = dimensions.partition("x")
    if w != h:
        return False
    return int(w) in _CONFIGURED_SIZES


def _check_path_traversal(root: Path, filename: str) -> bool:
    """Return True if *filename* resolves inside *root* (no '..' escapes)."""
    resolved = (root / filename).resolve()
    return resolved.is_relative_to(root.resolve())


# ---------------------------------------------------------------------------
# Content negotiation
# ---------------------------------------------------------------------------


def _negotiate_avatar_format() -> str:
    """Pick the best image format based on the Accept header and configured formats.

    Preference order: AVIF > WebP > PNG > JPEG.
    Falls back to the first configured format, or 'jpg' as a last resort.
    """
    accept = request.headers.get("Accept", "")
    for mime, ext in NEGOTIATION_PREFERENCE:
        if mime in accept and ext in _CONFIGURED_EXTS:
            return ext
    for _, ext in NEGOTIATION_PREFERENCE:
        if ext in _CONFIGURED_EXTS:
            return ext
    return "jpg"


# ---------------------------------------------------------------------------
# Avatar routes
# ---------------------------------------------------------------------------


@serve_avatar_bp.route("/user-avatars/<dimensions>/<basename>", methods=["GET"])
def negotiate_avatar(dimensions, basename):
    """Content-negotiate an extension-less avatar URL and redirect to the best format.

    Called for URLs like ``/user-avatars/256x256/abc123`` (no file extension).
    Selects the optimal format based on the Accept header and issues a 302 to
    the explicit ``<basename>.<ext>`` URL served by :func:`serve_avatar_file`.
    """
    if not _validate_dimensions(dimensions):
        log.debug(
            "Avatar request rejected - invalid or disabled dimensions: %r", dimensions
        )
        abort(404)

    ext = _negotiate_avatar_format()
    log.debug(
        "Content negotiation for %s/%s -> .%s (Accept: %s).",
        dimensions,
        basename,
        ext,
        request.headers.get("Accept", ""),
    )
    target_url = url_for(
        "serve_avatar.serve_avatar_file",
        dimensions=dimensions,
        basename=basename,
        ext=ext,
    )
    resp = redirect(target_url, code=302)
    resp.headers["Vary"] = "Accept"
    resp.headers["Cache-Control"] = "no-store"
    return resp


@serve_avatar_bp.route("/user-avatars/<dimensions>/<basename>.<ext>", methods=["GET"])
def serve_avatar_file(dimensions, basename, ext):
    """Serve an avatar image file directly.

    Called for URLs like ``/user-avatars/256x256/abc123.webp`` (explicit extension).
    Validates that both the dimensions and format are enabled in config before
    touching the filesystem.
    """
    if not _validate_dimensions(dimensions):
        log.debug(
            "Avatar request rejected - invalid or disabled dimensions: %r", dimensions
        )
        abort(404)

    if ext.lower() not in _CONFIGURED_EXTS:
        log.debug("Avatar request rejected - format %r not enabled.", ext)
        abort(404)

    filename = f"{basename}.{ext}"
    filepath = f"{dimensions}/{filename}"
    if not _check_path_traversal(AVATAR_ROOT, filepath):
        log.warning("Avatar path traversal blocked: %s", filepath)
        abort(404)

    log.debug("Serving avatar file: %s", filepath)
    resp = send_from_directory(AVATAR_ROOT, filepath)
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


# ---------------------------------------------------------------------------
# Metadata route
# ---------------------------------------------------------------------------

_METADATA_ACCESS_MODE = metadata_access


@serve_avatar_bp.route("/user-avatars/_metadata/<filename>", methods=["GET"])
def serve_avatar_metadata(filename):
    """Serve avatar metadata JSON from the storage directory.

    In owner_only mode the requesting session user must match the user_pk stored
    inside the metadata file.  A 404 is returned for both missing files and
    ownership mismatches so callers cannot distinguish the two cases.
    """
    if not _check_path_traversal(METADATA_ROOT, filename):
        log.warning("Metadata path traversal blocked: %s", filename)
        abort(404)

    if _METADATA_ACCESS_MODE == "owner_only":
        if "user" not in session:
            log.debug(
                "Unauthenticated metadata request for %r - redirecting to login.",
                filename,
            )
            return redirect(url_for("routes.login_page"))

        meta = load_metadata_file(filename)
        if meta is None or meta.get("user_pk", None) != session["user"].get("pk", None):
            if meta is not None:
                log.debug(
                    "Metadata access denied for %r - user pk mismatch (session pk=%r).",
                    filename,
                    session["user"].get("pk", None),
                )
            abort(404)

        log.debug("Serving metadata file: %s (access=owner_only)", filename)
        resp = jsonify(meta)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    log.debug("Serving metadata file: %s (access=public)", filename)
    resp = send_from_directory(METADATA_ROOT, filename, mimetype="application/json")
    resp.headers["Cache-Control"] = "no-store"
    return resp
