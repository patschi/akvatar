"""
web_auth.py - OpenID Connect authentication routes.

Handles the OIDC login flow, callback, logout, and post-logout landing page.
Core OAuth client setup, session building, and logout URL logic live in auth.py.

Routes:
  - GET /login-start  -> redirect to Authentik's authorization endpoint
  - GET /callback     -> exchange OIDC code for token, store session
  - GET /logout       -> clear session, optionally perform RP-Initiated Logout
  - GET /logged-out   -> post-logout landing page
"""

import logging

from flask import Blueprint, redirect, render_template, session, url_for

from src.auth import build_provider_logout_url, oauth, process_oidc_callback

log = logging.getLogger("auth")

# Blueprint so auth routes can be registered cleanly on the app
auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/login-start", methods=["GET"])
def login():
    """Redirect the browser to Authentik's authorization endpoint."""
    redirect_uri = url_for("auth.auth_callback", _external=True)
    log.debug(
        "OIDC authorization redirect - redirect_uri sent to Authentik: %s", redirect_uri
    )
    return oauth.authentik.authorize_redirect(redirect_uri)


@auth_bp.route("/callback", methods=["GET"])
def auth_callback():
    """OIDC callback - exchange the authorization code for tokens and store the user profile in the session."""
    log.debug("Received OIDC callback. Exchanging code for token...")
    try:
        token = oauth.authentik.authorize_access_token()
    except Exception:
        log.exception("Failed to exchange OIDC authorization code.")
        return redirect(url_for("routes.login_page", error="oidc_failed"))

    userinfo = token.get("userinfo")
    if userinfo is None:
        log.debug("userinfo not in token response - calling userinfo endpoint.")
        userinfo = oauth.authentik.userinfo()

    try:
        user_dict, id_token, locale = process_oidc_callback(token, userinfo)
    except Exception:
        log.exception("Failed to process OIDC callback user data.")
        return redirect(url_for("routes.login_page", error="pk_failed"))

    # Mark session as permanent so PERMANENT_SESSION_LIFETIME is enforced.
    # Without this Flask uses a browser-session cookie with no server-side expiry.
    session.permanent = True
    session["user"] = user_dict
    if id_token:
        session["id_token"] = id_token
        log.debug("ID token stored (%d bytes).", len(id_token))
    session["locale"] = locale

    return redirect(url_for("routes.dashboard"))


@auth_bp.route("/logout", methods=["GET"])
def logout():
    """Clear the local session and optionally redirect to Authentik's end_session_endpoint (RP-Initiated Logout)."""
    username = session.get("user", {}).get("username", "unknown")
    id_token = session.get("id_token", None)
    session.clear()
    log.info("User %r logged out.", username)

    logout_url = build_provider_logout_url(id_token)
    return redirect(logout_url or url_for("auth.logged_out"))


@auth_bp.route("/logged-out", methods=["GET"])
def logged_out():
    """Post-logout landing page. Authentik redirects here after ending the SSO session."""
    return render_template("logged_out.html")
