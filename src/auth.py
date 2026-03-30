"""
auth.py – OpenID Connect authentication via Authentik.

Registers the Authlib OAuth client, provides a `login_required` decorator,
and exposes the three auth routes: /login, /callback, /logout.

OIDC scopes are hardcoded to `openid profile email` and cannot be changed via config.
"""

import logging
from functools import wraps

from flask import Blueprint, redirect, url_for, session, render_template
from authlib.integrations.flask_client import OAuth

from src.config import oidc_cfg, app_cfg
from src.i18n import resolve_oidc_locale
from src.authentik import resolve_user_pk

log = logging.getLogger('auth')

# Hardcoded OIDC scopes – always request identity, profile, and email claims
OIDC_SCOPES = 'openid profile email'

# Blueprint so auth routes can be registered cleanly on the app
auth_bp = Blueprint('auth', __name__)

# OAuth / OIDC client (initialised later via init_oauth)
oauth = OAuth()


def init_oauth(app):
    """Bind the OAuth instance to the Flask `app` and register the Authentik provider. Must be called once at startup."""
    log.debug('Initialising OAuth client for Authentik.')
    oauth.init_app(app)
    oauth.register(
        name='authentik',
        client_id=oidc_cfg['client_id'],
        client_secret=oidc_cfg['client_secret'],
        server_metadata_url=oidc_cfg['issuer_url'].rstrip('/') + '/.well-known/openid-configuration',
        client_kwargs={'scope': OIDC_SCOPES},
    )
    log.info('OAuth client registered (client_id=%s, scopes=%s).', oidc_cfg['client_id'], OIDC_SCOPES)
    # Log the redirect URI that must be registered in the Authentik OAuth application.
    # This is derived from public_base_url – if it doesn't match what Authentik has
    # registered, logins will fail with "mismatching redirection URI".
    _expected_redirect_uri = app_cfg.get('public_base_url', '').rstrip('/') + '/callback'
    log.debug('Expected OIDC redirect URI (must match Authentik app config): %s', _expected_redirect_uri)


def login_required(f):
    """Redirect unauthenticated visitors to the public login page."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            log.debug('Unauthenticated access to %s – redirecting to login page.', f.__name__)
            return redirect(url_for('routes.login_page'))
        return f(*args, **kwargs)
    return decorated


@auth_bp.route('/login')
def login():
    """Redirect the browser to Authentik's authorization endpoint."""
    redirect_uri = url_for('auth.auth_callback', _external=True)
    log.debug('OIDC authorization redirect – redirect_uri sent to Authentik: %s', redirect_uri)
    return oauth.authentik.authorize_redirect(redirect_uri)


@auth_bp.route('/callback')
def auth_callback():
    """OIDC callback – exchange the authorization code for tokens and store the user profile in the session."""
    log.debug('Received OIDC callback. Exchanging code for token...')
    try:
        token = oauth.authentik.authorize_access_token()
    except Exception:
        log.exception('Failed to exchange OIDC authorization code.')
        return redirect(url_for('routes.login_page', error='oidc_failed'))

    userinfo = token.get('userinfo')
    if userinfo is None:
        log.debug('userinfo not in token response – calling userinfo endpoint.')
        userinfo = oauth.authentik.userinfo()

    username = userinfo.get(oidc_cfg['username_claim'], userinfo['sub'])

    # Resolve the Authentik PK (integer primary key) at login time so that
    # all downstream operations (API updates, metadata, cleanup) can use a
    # stable, opaque identifier instead of the mutable username.
    try:
        pk = resolve_user_pk(username)
    except Exception:
        log.exception('Failed to resolve Authentik PK for user %r – login aborted.', username)
        return redirect(url_for('routes.login_page', error='pk_failed'))

    # Mark session as permanent so PERMANENT_SESSION_LIFETIME is enforced.
    # Without this Flask uses a browser-session cookie with no server-side expiry.
    session.permanent = True

    session['user'] = {
        'pk':       pk,
        'username': username,
        'name':     userinfo.get('name', ''),
        'email':    userinfo.get('email', ''),
        'avatar':   userinfo.get('picture', ''),
    }

    # Resolve locale from OIDC claim
    oidc_locale_raw = userinfo.get('locale', '')
    session['locale'] = resolve_oidc_locale(oidc_locale_raw)
    log.debug('OIDC locale claim: %r -> resolved to %r.', oidc_locale_raw, session['locale'])

    log.info('User %r (pk=%s) logged in successfully.', username, pk)
    log.debug('User session data: %s', session)
    return redirect(url_for('routes.dashboard'))


@auth_bp.route('/logout')
def logout():
    """Clear the local session (no OIDC front-channel logout)."""
    username = session.get('user', {}).get('username', 'unknown')
    session.clear()
    log.info('User %r logged out.', username)
    return render_template('logged_out.html')
