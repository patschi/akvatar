"""
app.py – Application entry point.

Creates the Flask app, registers blueprints, initialises OAuth, applies reverse-proxy
and subfolder middleware, and starts the development server when run directly.
"""

import os
import logging

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from src.config import app_cfg, web_cfg, branding_cfg
from src.i18n import t, get_locale, get_js_translations
from src.auth import auth_bp, init_oauth
from src.routes import routes_bp
from src.imaging import AVATAR_ROOT, ensure_size_directories

log = logging.getLogger('app')

# Logger dedicated to HTTP request logging (keeps it separate from application logic)
http_log = logging.getLogger('http')

# Suppress Werkzeug's built-in access logging – we use our own http_log at DEBUG level
logging.getLogger('werkzeug').setLevel(logging.WARNING)


class PrefixMiddleware:
    """
    WSGI middleware that prepends a static path prefix (SCRIPT_NAME) so the app
    can be hosted under a subfolder without a reverse proxy setting X-Forwarded-Prefix.

    When a reverse proxy *does* set X-Forwarded-Prefix, ProxyFix handles it instead
    and this middleware is not applied.
    """
    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        environ['SCRIPT_NAME'] = self.prefix + environ.get('SCRIPT_NAME', '')
        path_info = environ.get('PATH_INFO', '')
        if path_info.startswith(self.prefix):
            environ['PATH_INFO'] = path_info[len(self.prefix):]
        return self.wsgi_app(environ, start_response)


def create_app() -> Flask:
    """Application factory – build and configure the Flask instance."""
    app = Flask(__name__)
    app.secret_key = app_cfg['secret_key']
    app.config['MAX_CONTENT_LENGTH'] = app_cfg['max_upload_size_mb'] * 1024 * 1024  # MB -> bytes

    log.debug('Flask app created (max upload = %d MB).', app_cfg['max_upload_size_mb'])

    # Ensure the avatar storage directory tree exists (root + all size sub-dirs)
    AVATAR_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_size_directories()
    log.debug('Avatar storage root: %s', AVATAR_ROOT.resolve())

    # -- Reverse proxy support ---------------------------------------------
    # Trust X-Forwarded-For, X-Forwarded-Proto, X-Forwarded-Host, X-Forwarded-Prefix
    # so that url_for() generates correct external URLs and request.remote_addr
    # reflects the real client IP.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    log.debug('ProxyFix middleware applied (x_for=1, x_proto=1, x_host=1, x_prefix=1).')

    # -- Subfolder support -------------------------------------------------
    # If base_path is set, apply a SCRIPT_NAME prefix so the app can be
    # hosted at e.g. /avatar without the reverse proxy needing to set
    # X-Forwarded-Prefix.
    base_path = web_cfg.get('base_path', '').rstrip('/')
    if base_path:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, base_path)
        log.info('PrefixMiddleware applied – app is served under %r.', base_path)

    # Initialise OIDC / OAuth
    init_oauth(app)

    # Register route blueprints
    app.register_blueprint(auth_bp)    # /login, /callback, /logout
    app.register_blueprint(routes_bp)  # /, /dashboard, /api/upload, /user-avatars
    log.debug('Blueprints registered.')

    # -- Template context processor -----------------------------------------
    _brand_name = branding_cfg.get('name', 'Avatar Updater')

    @app.context_processor
    def _inject_globals():
        locale = get_locale()
        return {
            'brand_name': _brand_name,
            't': t,
            'lang': locale.split('_')[0],
            'i18n': get_js_translations(locale),
        }

    # -- HTTP response headers & logging ------------------------------------
    @app.after_request
    def _after_request(response):
        # Log non-static requests at DEBUG level
        if not request.path.startswith('/static/'):
            http_log.debug('%s %s %s (client=%s)', request.method, request.path, response.status_code, request.remote_addr)
        return response

    return app


if __name__ == '__main__':
    app = create_app()
    debug = os.environ.get('FLASK_DEBUG', '0') == '1'

    # -- TLS support -------------------------------------------------------
    tls_cert = web_cfg.get('tls_cert', '')
    tls_key = web_cfg.get('tls_key', '')
    ssl_context = (tls_cert, tls_key) if tls_cert and tls_key else None
    scheme = 'https' if ssl_context else 'http'

    log.info('Webserver starting on %s://%s:%s (debug=%s).', scheme, web_cfg['host'], web_cfg['port'], debug)
    app.run(host=web_cfg['host'], port=web_cfg['port'], debug=debug, ssl_context=ssl_context)
