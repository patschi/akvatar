"""
auth.py - OpenID Connect authentication core.

Registers the Authlib OAuth client, provides the login_required decorator,
and exposes init_oauth() for application startup.
Route handlers for /login-start, /callback, /logout, and /logged-out are in web_auth.py.

OIDC scopes are hardcoded to `openid profile email` and cannot be changed via config.
"""

import logging
from functools import wraps
from urllib.parse import urlencode

from authlib.integrations.flask_client import OAuth
from flask import redirect, session, url_for

from src.authentik import retrieve_user
from src.config import (
    oidc_client_id,
    oidc_client_secret,
    oidc_end_provider_session,
    oidc_issuer_url,
    oidc_skip_cert_verify,
    oidc_username_claim,
    public_base_url,
)
from src.i18n import resolve_oidc_locale

log = logging.getLogger("auth")

# Hardcoded OIDC scopes - always request identity, profile, and email claims
OIDC_SCOPES = "openid profile email"

# OAuth / OIDC client (initialized later via init_oauth)
oauth = OAuth()


def init_oauth(app):
    """Bind the OAuth instance to the Flask `app` and register the Authentik provider. Must be called once at startup."""
    log.debug("Initialising OAuth client for Authentik.")
    oauth.init_app(app)
    # Build client_kwargs, disabling TLS verification when configured.
    # Authlib's OAuth2Session (requests backend) propagates the verify flag to
    # ALL requests it makes, including the server metadata (OIDC discovery) fetch.
    _client_kwargs = {"scope": OIDC_SCOPES}
    if oidc_skip_cert_verify:
        _client_kwargs["verify"] = False

    oauth.register(
        name="authentik",
        client_id=oidc_client_id,
        client_secret=oidc_client_secret,
        server_metadata_url=oidc_issuer_url.rstrip("/")
        + "/.well-known/openid-configuration",
        client_kwargs=_client_kwargs,
    )
    _cid_censored = oidc_client_id[:3] + "***" + oidc_client_id[-3:]
    log.info(
        "OAuth client registered (client_id=%s, scopes=%s).",
        _cid_censored,
        OIDC_SCOPES,
    )
    # Log the redirect URI that must be registered in the Authentik OAuth application.
    # This is derived from public_base_url - if it doesn't match what Authentik has
    # registered, logins will fail with "mismatching redirection URI".
    _expected_redirect_uri = public_base_url + "/callback"
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


def process_oidc_callback(token: dict, userinfo: dict) -> tuple[dict, str | None, str]:
    """
    Build session data from a completed OIDC token exchange and userinfo payload.

    Looks up the Authentik user to get the PK and current avatar URL, builds
    the session user dict, extracts the ID token when RP-Initiated Logout is
    configured, and resolves the locale from the OIDC claim.

    Returns (user_dict, id_token_or_None, locale).
    Raises on Authentik user lookup failure - caller should redirect to login.
    """
    username = userinfo.get(oidc_username_claim, userinfo["sub"])
    ak_user = retrieve_user(username)  # raises on failure

    user_dict = {
        "pk": ak_user["pk"],
        "username": username,
        "name": userinfo.get("name", ""),
        "email": userinfo.get("email", ""),
        "avatar": ak_user.get("avatar", ""),
    }

    # Only extract the ID token when RP-Initiated Logout is enabled; it is only
    # used as id_token_hint in RP-Initiated Logout and is dead weight otherwise.
    id_token = None
    if oidc_end_provider_session:
        id_token = token.get("id_token", None)

    locale = resolve_oidc_locale(userinfo.get("locale", ""))
    log.info("User %r (pk=%s) logged in successfully.", username, ak_user["pk"])
    return user_dict, id_token, locale


def build_provider_logout_url(id_token: str | None) -> str | None:
    """
    Build the provider end_session_endpoint URL for RP-Initiated Logout.

    Reads OIDC discovery metadata (cached for the process lifetime by Authlib) to
    resolve the endpoint, then constructs the redirect URL with
    post_logout_redirect_uri and optional id_token_hint.
    Returns None when RP-Initiated Logout is disabled or the endpoint is unavailable.
    """
    if not oidc_end_provider_session:
        log.debug("oidc.end_provider_session is disabled - skipping provider logout.")
        return None

    try:
        metadata = oauth.authentik.load_server_metadata()
        end_session_endpoint = metadata.get("end_session_endpoint", None)
    except Exception:
        log.warning(
            "Failed to load OIDC server metadata for logout redirect - falling back to local logout page."
        )
        return None

    if not end_session_endpoint:
        log.debug(
            "No end_session_endpoint in OIDC metadata - showing local logged-out page."
        )
        return None

    post_logout_uri = public_base_url + "/logged-out"
    params = {"post_logout_redirect_uri": post_logout_uri}
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
    return logout_url


def build_user_initials(user: dict) -> str:
    """
    Build a 1-2 character initials string from a user session dict.

    Uses the first letter of the first and last name parts when two or more
    name parts are present, falling back to the first letter of the username.
    """
    name_parts = user.get("name", "").split()
    if len(name_parts) >= 2:
        return (name_parts[0][0] + name_parts[-1][0]).upper()
    return (user.get("username", "") or "?")[0].upper()
