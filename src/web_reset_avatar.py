"""
reset_avatar.py - Avatar removal route.

Provides the ``POST /api/remove-avatar`` endpoint that strips the custom
avatar attribute from the Authentik user and clears it from the Flask session.
"""

import logging

from flask import Blueprint, jsonify, session

from src.auth import login_required
from src.authentik import remove_avatar_url
from src.sec_csrf import validate_csrf_token

log = logging.getLogger("reset_img")

reset_avatar_bp = Blueprint("reset_avatar", __name__)


@reset_avatar_bp.route("/api/remove-avatar", methods=["POST"])
@login_required
def api_remove_avatar():
    """Remove the user's custom avatar attribute from Authentik and clear it from the session."""
    # CSRF token validation (returns JSON 403 on failure)
    csrf_rejection = validate_csrf_token()
    if csrf_rejection:
        return csrf_rejection

    user = session["user"]
    log.info(
        "Avatar removal requested by user %r (pk=%s).", user["username"], user["pk"]
    )

    try:
        remove_avatar_url(user["pk"])
    except Exception:
        log.exception(
            "Failed to remove avatar for user %r (pk=%s).", user["username"], user["pk"]
        )
        return jsonify({"error": "remove_failed"}), 500

    # Clear the avatar from the session so the UI reflects the change immediately
    session["user"]["avatar"] = ""
    session.modified = True
    log.info("Avatar removed for user %r (pk=%s).", user["username"], user["pk"])
    return jsonify({"success": True})
