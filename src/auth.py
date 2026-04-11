"""
auth.py - OpenID Connect authentication via Authentik.

Registers the Authlib OAuth client, provides a `login_required` decorator,
and exposes the auth routes: /login, /callback, /logout, /logged-out.

OIDC scopes are hardcoded to `openid profile email` and cannot be changed via config.
"""

import logging
from functools import wraps
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from flask import Blueprint, redirect, render_template, session, url_for

from src.authentik import retrieve_user
from src.config import app_cfg, oidc_cfg
from src.i18n import resolve_oidc_locale

log = logging.getLogger("auth")

# Hardcoded OIDC scopes - always request identity, profile, and email claims
OIDC_SCOPES = "openid profile email"

# Blueprint so auth routes can be registered cleanly on the app
auth_bp = Blueprint("auth", __name__)

# OAuth / OIDC client (initialised later via init_oauth)
oauth = OAuth()


def init_oauth(app):
    """Bind the OAuth instance to the Flask `app` and register the Authentik provider. Must be called once at startup."""
    log.debug("Initialising OAuth client for Authentik.")
    oauth.init_app(app)
    oauth.register(
        name="authentik",
        client_id=oidc_cfg["client_id"],
        client_secret=oidc_cfg["client_secret"],
        server_metadata_url=oidc_cfg["issuer_url"].rstrip("/")
        + "/.well-known/openid-configuration",
        client_kwargs={"scope": OIDC_SCOPES},
    )
    log.info(
        "OAuth client registered (client_id=%s, scopes=%s).",
        oidc_cfg["client_id"],
        OIDC_SCOPES,
    )
    # Log the redirect URI that must be registered in the Authentik OAuth application.
    # This is derived from public_base_url - if it doesn't match what Authentik has
    # registered, logins will fail with "mismatching redirection URI".
    _expected_redirect_uri = (
        app_cfg.get("public_base_url", "").rstrip("/") + "/callback"
    )
    log.debug(
        "Expected OIDC redirect URI (must match Authentik app config): %s",
        _expected_redirect_uri,
    )


def login_required(f):
    """Redirect unauthenticated visitors to the public login page."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if "user" not in session:
            log.debug(
                "Unauthenticated access to %s - redirecting to login page.", f.__name__
            )
            return redirect(url_for("routes.login_page"))
        return f(*args, **kwargs)

    return decorated


@auth_bp.route("/login-start")
def login():
    """Redirect the browser to Authentik's authorization endpoint."""
    redirect_uri = url_for("auth.auth_callback", _external=True)
    log.debug(
        "OIDC authorization redirect - redirect_uri sent to Authentik: %s", redirect_uri
    )
    return oauth.authentik.authorize_redirect(redirect_uri)


@auth_bp.route("/callback")
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

    username = userinfo.get(oidc_cfg["username_claim"], userinfo["sub"])

    # Retrieve the Authentik user at login time so that the PK (stable,
    # opaque identifier) and the current avatar URL are available in the
    # session without additional API calls.
    try:
        ak_user = retrieve_user(username)
    except Exception:
        log.exception("Failed to retrieve Authentik user %r - login aborted.", username)
        return redirect(url_for("routes.login_page", error="pk_failed"))

    # Mark session as permanent so PERMANENT_SESSION_LIFETIME is enforced.
    # Without this Flask uses a browser-session cookie with no server-side expiry.
    session.permanent = True

    session["user"] = {
        "pk": ak_user["pk"],
        "username": username,
        "name": userinfo.get("name", ""),
        "email": userinfo.get("email", ""),
        "avatar": ak_user.get("avatar", ""),
    }

    # Store the raw ID token for RP-Initiated Logout (id_token_hint parameter)
    session["id_token"] = token.get("id_token", None)

    # Resolve locale from OIDC claim
    oidc_locale_raw = userinfo.get("locale", "")
    session["locale"] = resolve_oidc_locale(oidc_locale_raw)
    log.debug(
        "OIDC locale claim: %r -> resolved to %r.", oidc_locale_raw, session["locale"]
    )

    log.info("User %r (pk=%s) logged in successfully.", username, ak_user["pk"])
    # Redact sensitive session values (id_token, csrf_token) before logging
    _redacted_session = {
        k: ("[REDACTED]" if k in ("id_token", "csrf_token") else v)
        for k, v in session.items()
    }
    log.debug("User session data: %s", _redacted_session)
    return redirect(url_for("routes.dashboard"))


@auth_bp.route("/logout")
def logout():
    """Clear the local session and optionally redirect to Authentik's end_session_endpoint (RP-Initiated Logout)."""
    username = session.get("user", {}).get("username", "unknown")
    id_token = session.get("id_token", None)
    session.clear()
    log.info("User %r logged out.", username)

    # When oidc.end_provider_session is enabled, perform RP-Initiated Logout by
    # redirecting to the provider's end_session_endpoint. This terminates the
    # Authentik SSO session as well (i.e. the user is logged out of all apps).
    if not oidc_cfg.get("end_provider_session", False):
        log.debug(
            "oidc.end_provider_session is disabled - skipping provider logout, showing local logged-out page."
        )
        return redirect(url_for("auth.logged_out"))

    # Fetch the end_session_endpoint from OIDC discovery metadata
    try:
        metadata = oauth.authentik.load_server_metadata()
        end_session_endpoint = metadata.get("end_session_endpoint", None)
    except Exception:
        log.warning(
            "Failed to load OIDC server metadata for logout redirect - falling back to local logout page."
        )
        end_session_endpoint = None

    if not end_session_endpoint:
        log.debug(
            "No end_session_endpoint in OIDC metadata - showing local logged-out page."
        )
        return redirect(url_for("auth.logged_out"))

    # Build the full logout redirect URL with post_logout_redirect_uri and id_token_hint.
    # Use the configured public_base_url to build the redirect URI, ensuring it
    # matches exactly what the admin registered in Authentik's redirect URIs.
    # url_for(_external=True) can produce a wrong scheme/host behind a reverse proxy.
    post_logout_uri = app_cfg.get("public_base_url", "").rstrip("/") + "/logged-out"
    params = {
        "post_logout_redirect_uri": post_logout_uri,
    }
    # id_token_hint lets the provider skip the "are you sure?" confirmation page
    if id_token:
        params["id_token_hint"] = id_token
    logout_url = end_session_endpoint + "?" + urlencode(params)
    log.debug(
        "OIDC RP-Initiated Logout - end_session_endpoint: %s", end_session_endpoint
    )
    log.debug(
        "OIDC RP-Initiated Logout - post_logout_redirect_uri: %s", post_logout_uri
    )
    log.debug("OIDC RP-Initiated Logout - id_token_hint present: %s", bool(id_token))
    return redirect(logout_url)


@auth_bp.route("/logged-out")
def logged_out():
    """Post-logout landing page. Authentik redirects here after ending the SSO session."""
    return render_template("logged_out.html")
