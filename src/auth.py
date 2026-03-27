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

from src.config import oidc_cfg

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
        server_metadata_url=oidc_cfg['issuer_url'] + '/.well-known/openid-configuration',
        client_kwargs={'scope': OIDC_SCOPES},
    )
    log.info('OAuth client registered (client_id=%s, scopes=%s).', oidc_cfg['client_id'], OIDC_SCOPES)


def login_required(f):
    """Redirect unauthenticated visitors to the public login page."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user' not in session:
            log.debug('Unauthenticated access to %s – redirecting to login page.', f.__name__)
            return redirect(url_for('routes.login_page'))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@auth_bp.route('/login')
def login():
    """Redirect the browser to Authentik's authorization endpoint."""
    log.debug('Starting OIDC authorization redirect.')
    return oauth.authentik.authorize_redirect(url_for('auth.auth_callback', _external=True))


@auth_bp.route('/callback')
def auth_callback():
    """OIDC callback – exchange the authorization code for tokens and store the user profile in the session."""
    log.debug('Received OIDC callback, exchanging authorization code.')
    try:
        token = oauth.authentik.authorize_access_token()
    except Exception:
        log.exception('Failed to exchange OIDC authorization code.')
        return redirect(url_for('routes.login_page'))

    userinfo = token.get('userinfo')
    if userinfo is None:
        log.debug('userinfo not in token response – calling userinfo endpoint.')
        userinfo = oauth.authentik.userinfo()

    session['user'] = {
        'sub':      userinfo['sub'],
        'username': userinfo.get(oidc_cfg['username_claim'], userinfo['sub']),
        'name':     userinfo.get('name', ''),
        'email':    userinfo.get('email', ''),
        'avatar':   userinfo.get('picture', ''),
    }
    log.info('User %r logged in successfully.', session['user']['username'])
    log.debug('Session user data: %s', session['user'])
    return redirect(url_for('routes.dashboard'))


@auth_bp.route('/logout')
def logout():
    """Clear the local session (no OIDC front-channel logout)."""
    username = session.get('user', {}).get('username', 'unknown')
    session.clear()
    log.info('User %r logged out.', username)
    return render_template('logged_out.html')
