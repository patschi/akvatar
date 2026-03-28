"""
app.py – Application entry point.

Creates the Flask app, registers blueprints, initialises OAuth, applies reverse-proxy
and subfolder middleware, and starts the development server when run directly.
"""

import logging
import flask.cli
from urllib.parse import urlparse

# Suppress Flask's default startup banner ("Serving Flask app ...")
# – we print our own startup info via the 'app' logger.
flask.cli.show_server_banner = lambda *a, **kw: None

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from src.config import app_cfg, web_cfg, branding_cfg, debug_full, access_log
from src.i18n import t, get_locale, get_js_translations
from src.auth import auth_bp, init_oauth
from src.routes import routes_bp
from src.imaging import AVATAR_ROOT, METADATA_ROOT, ensure_size_directories
from src.cleanup import start_cleanup_thread

log = logging.getLogger('app')

# Logger dedicated to HTTP request logging (keeps it separate from application logic)
http_log = logging.getLogger('http')

# Suppress Werkzeug's built-in access logging – we use our own http_log at DEBUG level
logging.getLogger('werkzeug').setLevel(logging.WARNING)


class PrefixMiddleware:
    """
    WSGI middleware that sets SCRIPT_NAME to a static path prefix so the app
    can be hosted under a subfolder without a reverse proxy setting X-Forwarded-Prefix.

    ProxyFix is applied as the outer middleware (runs before this one), so when a
    reverse proxy *does* set X-Forwarded-Prefix, ProxyFix has already populated
    SCRIPT_NAME and this middleware is a no-op.
    """
    def __init__(self, wsgi_app, prefix):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        # Skip if ProxyFix already set SCRIPT_NAME from X-Forwarded-Prefix.
        # Applying the prefix twice would generate double-prefixed URLs (e.g.
        # /avatar-update/avatar-update/callback), causing redirect_uri mismatches.
        if not environ.get('SCRIPT_NAME'):
            environ['SCRIPT_NAME'] = self.prefix
            path_info = environ.get('PATH_INFO', '')
            if path_info.startswith(self.prefix):
                environ['PATH_INFO'] = path_info[len(self.prefix):]
        return self.wsgi_app(environ, start_response)


def create_app() -> Flask:
    """Application factory – build and configure the Flask instance."""
    app = Flask(__name__, template_folder='src/templates')
    app.secret_key = app_cfg['secret_key']
    app.config['MAX_CONTENT_LENGTH'] = app_cfg['max_upload_size_mb'] * 1024 * 1024  # MB -> bytes

    if debug_full:
        app.debug = True
        app.config['TEMPLATES_AUTO_RELOAD'] = True
    else:
        app.config['TEMPLATES_AUTO_RELOAD'] = False

    log.debug('Flask app created (max upload = %d MB).', app_cfg['max_upload_size_mb'])

    # Ensure the avatar storage directory tree exists (root + all size sub-dirs)
    AVATAR_ROOT.mkdir(parents=True, exist_ok=True)
    METADATA_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_size_directories()
    log.debug('Avatar storage root: %s', AVATAR_ROOT.resolve())
    log.debug('Metadata storage root: %s', METADATA_ROOT.resolve())

    # -- Subfolder support -------------------------------------------------
    # Derive the path prefix from public_base_url (e.g. "/avatar-update" from
    # "https://portal.example.com/avatar-update").  Apply PrefixMiddleware as
    # the inner middleware so it only fires when the reverse proxy has NOT
    # already set SCRIPT_NAME via X-Forwarded-Prefix (handled by ProxyFix).
    _public_path = urlparse(app_cfg.get('public_base_url', '')).path.rstrip('/')
    if _public_path:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, _public_path)
        log.info('PrefixMiddleware applied – app is served under %r.', _public_path)

    # -- Reverse proxy support ---------------------------------------------
    # ProxyFix is the OUTER middleware (runs first on every request).  It reads
    # X-Forwarded-For/Proto/Host/Prefix so that url_for() generates correct
    # external URLs and remote_addr reflects the real client IP.
    # IMPORTANT: must wrap PrefixMiddleware so that when X-Forwarded-Prefix is
    # present, ProxyFix sets SCRIPT_NAME before PrefixMiddleware checks it.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
    log.debug('ProxyFix middleware applied (x_for=1, x_proto=1, x_host=1, x_prefix=1).')

    # Initialise OIDC / OAuth
    init_oauth(app)

    # Register route blueprints
    app.register_blueprint(auth_bp)    # /login, /callback, /logout
    app.register_blueprint(routes_bp)  # /, /dashboard, /api/upload, /user-avatars
    log.debug('Web routes registered.')

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
    if access_log:
        @app.after_request
        def _after_request(response):
            if not request.path.startswith('/static/'):
                http_log.debug('%s %s %s (client=%s)', request.method, request.path, response.status_code, request.remote_addr)
            return response

    # -- Template cache warm-up --------------------------------------------
    # Pre-compile all templates so workers forked via --preload inherit them
    # and the first request per template has zero disk I/O.
    if not debug_full:
        log.debug('Warming up template cache by pre-compiling all templates...')
        for template_name in app.jinja_loader.list_templates():
            log.debug('Pre-compiling template: %s', template_name)
            app.jinja_env.get_template(template_name)
        log.debug('Template cache warmed: %d template(s) pre-compiled.', len(app.jinja_loader.list_templates()))

    return app


if __name__ == '__main__':
    app = create_app()

    # Start the background cleanup thread (respects config interval; 0 = disabled)
    start_cleanup_thread()

    # -- TLS support -------------------------------------------------------
    tls_cert = web_cfg.get('tls_cert', '')
    tls_key = web_cfg.get('tls_key', '')
    ssl_context = (tls_cert, tls_key) if tls_cert and tls_key else None
    scheme = 'https' if ssl_context else 'http'
    host = web_cfg.get('host', '0.0.0.0')
    port = web_cfg.get('port', 5000)

    log.info('Webserver starting on %s://%s:%s...', scheme, host, port)
    app.run(host=host, port=port, debug=debug_full, ssl_context=ssl_context)
