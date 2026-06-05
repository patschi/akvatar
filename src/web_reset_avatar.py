"""
reset_avatar.py - Avatar removal route.

Provides the ``POST /api/remove-avatar`` endpoint that strips the custom
avatar attribute from the Authentik user and clears it from the Flask session.
"""

import logging

from flask import Blueprint, jsonify, session

from src.auth import login_required
from src.authentik import remove_avatar_url
from src.sec_csrf import csrf_required

log = logging.getLogger("reset_img")

reset_avatar_bp = Blueprint("reset_avatar", __name__)


@reset_avatar_bp.route("/api/remove-avatar", methods=["POST"])
@login_required
@csrf_required
def api_remove_avatar():
    """Remove the user's custom avatar attribute from Authentik and clear it from the session."""
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

    # Clear the avatar from the session so the UI reflects the change immediately.
    # Reassign the whole user dict (rather than mutating it in place) so Flask's
    # session detects the change automatically, without needing session.modified.
    session["user"] = {**session["user"], "avatar": ""}
    log.info("Avatar removed for user %r (pk=%s).", user["username"], user["pk"])
    return jsonify({"success": True})
