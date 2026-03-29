"""
app.py – Application entry point.

Creates the Flask app, registers blueprints, initialises OAuth, applies reverse-proxy
and subfolder middleware, and starts the development server when run directly.
"""

import hashlib
import logging
import mimetypes
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

import flask.cli

# Suppress Flask's default startup banner ("Serving Flask app ...")
# – we print our own startup info via the 'app' logger.
flask.cli.show_server_banner = lambda *a, **kw: None

from flask import Flask, Response, abort, request  # noqa: E402
from werkzeug.middleware.proxy_fix import ProxyFix  # noqa: E402

from src.config import app_cfg, web_cfg, branding_cfg, debug_full, access_log  # noqa: E402
from src.i18n import t, get_locale, get_js_translations  # noqa: E402
from src import APP_VERSION  # noqa: E402
from src.auth import auth_bp, init_oauth  # noqa: E402
from src.routes import routes_bp  # noqa: E402
from src.imaging import AVATAR_ROOT, METADATA_ROOT, ensure_size_directories_existence  # noqa: E402
from src.cleanup import start_cleanup_thread  # noqa: E402

log = logging.getLogger('app')

# Logger dedicated to HTTP request logging (keeps it separate from application logic)
http_log = logging.getLogger('http')

# Suppress Werkzeug's built-in access logging – we use our own http_log at DEBUG level
logging.getLogger('werkzeug').setLevel(logging.WARNING)

# In-memory static file cache
# All files in static/ are read once at import time and served from memory.
# With gunicorn --preload, workers inherit the cache via fork (shared pages).
_STATIC_DIR = Path(__file__).resolve().parent / 'static'


def _build_static_cache() -> dict[str, tuple[bytes, str, str]]:
    """Read every file under static/ into {rel_path: (data, mimetype, etag)}."""
    cache = {}
    for path in sorted(_STATIC_DIR.rglob('*')):
        if not path.is_file():
            continue
        log.debug('Caching static file: %s', path)
        rel = path.relative_to(_STATIC_DIR).as_posix()
        data = path.read_bytes()
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        etag = hashlib.sha256(data).hexdigest()[:16]
        cache[rel] = (data, mime, etag)
    log.debug('Static file cache built with %d file(s).', len(cache))
    return cache


_static_cache = _build_static_cache()
log.info('Static file cache: %d file(s), %.1f KB total.',
         len(_static_cache),
         sum(len(d) for d, _, _ in _static_cache.values()) / 1024)


# Periodic memory monitor

def _get_rss_mb() -> float | None:
    """Return current process RSS in MB, or None if unavailable."""
    # Linux: parse VmRSS from /proc/self/status (value in KB)
    try:
        with open('/proc/self/status') as f:
            for line in f:
                if line.startswith('VmRSS:'):
                    return int(line.split()[1]) / 1024
    except (FileNotFoundError, OSError):
        pass
    # Windows: query working set size via Win32 API
    try:
        import ctypes
        import ctypes.wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ('cb',                         ctypes.wintypes.DWORD),
                ('PageFaultCount',             ctypes.wintypes.DWORD),
                ('PeakWorkingSetSize',         ctypes.c_size_t),
                ('WorkingSetSize',             ctypes.c_size_t),
                ('QuotaPeakPagedPoolUsage',    ctypes.c_size_t),
                ('QuotaPagedPoolUsage',        ctypes.c_size_t),
                ('QuotaPeakNonPagedPoolUsage', ctypes.c_size_t),
                ('QuotaNonPagedPoolUsage',     ctypes.c_size_t),
                ('PagefileUsage',              ctypes.c_size_t),
                ('PeakPagefileUsage',          ctypes.c_size_t),
            ]

        pmc = _PMC()
        pmc.cb = ctypes.sizeof(pmc)
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(),
            ctypes.byref(pmc),
            pmc.cb,
        )
        return pmc.WorkingSetSize / (1024 * 1024)
    except Exception:
        return None


def _memory_log_loop() -> None:
    """Log process RSS every x seconds, but only when the value has changed."""
    last_mem = None
    while True:
        mem = _get_rss_mb()
        if mem is not None and mem != last_mem:
            log.debug('Monitor: Process memory: %.1f MB', mem)
            last_mem = mem
        time.sleep(5)


def _start_memory_monitor() -> None:
    """Start the memory monitor thread (once)."""
    threading.Thread(target=_memory_log_loop, name='memlog', daemon=True).start()
    log.debug('Memory monitor thread started.')


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

    # Start the memory monitor thread immediately so it runs during startup and continues in workers after fork.
    _start_memory_monitor()

    # Initialize flask app
    app = Flask(__name__, template_folder='src/templates', static_folder=None)
    app.secret_key = app_cfg['secret_key']
    app.config['MAX_CONTENT_LENGTH'] = app_cfg['max_upload_size_mb'] * 1024 * 1024  # MB -> bytes

    # Session cookie hardening.
    # HttpOnly: cookie not accessible from JavaScript (mitigates XSS session theft).
    # SameSite=Lax: browser refuses to send the cookie on cross-site POST requests,
    #   which effectively prevents CSRF on /api/upload without a token library.
    #   Lax (not Strict) is required so the OIDC redirect from Authentik back to
    #   /callback still carries the session cookie for state verification.
    # Secure: instructs the browser to only transmit the cookie over HTTPS connections.
    #   The Secure flag is about what the *browser* sees, not the Flask-to-proxy link.
    #   When a reverse proxy terminates TLS, the browser talks to the proxy over HTTPS
    #   so it will honour the flag; the internal proxy→Flask link being plain HTTP is
    #   irrelevant.  We therefore set Secure whenever the public URL uses https://, not
    #   only when Flask's own built-in server has TLS configured.
    #   app.session_cookie_secure overrides this auto-detection when set explicitly.
    # Permanent + lifetime: enforce an absolute session expiry (default: 30 min).
    _secure_override = app_cfg.get('session_cookie_secure', None)
    _tls_active = _secure_override if _secure_override is not None else app_cfg.get('public_base_url', '').startswith('https://')
    app.config['SESSION_COOKIE_NAME'] = 'akvatar_session'
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    app.config['SESSION_COOKIE_SECURE'] = _tls_active
    app.config['PERMANENT_SESSION_LIFETIME'] = app_cfg.get('web_session_lifetime_seconds', 1800)

    if debug_full:
        app.debug = True
        app.config['TEMPLATES_AUTO_RELOAD'] = True
    else:
        app.config['TEMPLATES_AUTO_RELOAD'] = False

    log.debug('Flask app created (max upload = %d MB).', app_cfg['max_upload_size_mb'])

    # Ensure the avatar storage directory tree exists (root + all size sub-dirs)
    AVATAR_ROOT.mkdir(parents=True, exist_ok=True)
    METADATA_ROOT.mkdir(parents=True, exist_ok=True)
    ensure_size_directories_existence()
    log.debug('Avatar storage root: %s', AVATAR_ROOT.resolve())
    log.debug('Metadata storage root: %s', METADATA_ROOT.resolve())

    # Subfolder support
    # Derive the path prefix from public_base_url (e.g. "/avatar-update" from
    # "https://portal.example.com/avatar-update").  Apply PrefixMiddleware as
    # the inner middleware so it only fires when the reverse proxy has NOT
    # already set SCRIPT_NAME via X-Forwarded-Prefix (handled by ProxyFix).
    _public_path = urlparse(app_cfg.get('public_base_url', '')).path.rstrip('/')
    if _public_path:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, _public_path)
        log.info('PrefixMiddleware applied – app is served under %r.', _public_path)

    # Reverse proxy support
    # ProxyFix is the OUTER middleware (runs first on every request).  It reads
    # X-Forwarded-For/Proto/Host/Prefix so that url_for() generates correct
    # external URLs and remote_addr reflects the real client IP.
    # IMPORTANT: must wrap PrefixMiddleware so that when X-Forwarded-Prefix is
    # present, ProxyFix sets SCRIPT_NAME before PrefixMiddleware checks it.
    # Disable via webserver.proxy_mode: false when running without a reverse proxy.
    if web_cfg.get('proxy_mode', True):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
        log.debug('ProxyFix middleware applied (x_for=1, x_proto=1, x_host=1, x_prefix=1).')
    else:
        log.info('Proxy mode disabled – ProxyFix middleware not applied.')

    # Initialise OIDC / OAuth
    init_oauth(app)

    # Register route blueprints
    app.register_blueprint(auth_bp)    # /login, /callback, /logout
    app.register_blueprint(routes_bp)  # /, /dashboard, /api/upload, /user-avatars

    # Serve static files from in-memory cache
    @app.route('/static/<path:filename>', endpoint='static')
    def _serve_static(filename):
        entry = _static_cache.get(filename)
        if entry is None:
            abort(404)
        data, mime, etag = entry
        headers = {'ETag': f'"{etag}"', 'Cache-Control': 'public, max-age=86400'}
        if request.if_none_match and etag in request.if_none_match:
            return Response(status=304, headers=headers)
        return Response(data, mimetype=mime, headers=headers)

    log.debug('Web routes registered.')
    log.info('OK! Ready to serve requests.')

    # Template context processor – inject shared variables into all templates
    _brand_name = branding_cfg.get('name', 'Avatar Updater')

    @app.context_processor
    def _inject_globals():
        locale = get_locale()
        return {
            'brand_name': _brand_name,
            'app_version': APP_VERSION,
            't': t,
            'lang': locale.split('_')[0],
            'i18n': get_js_translations(locale),
        }

    # Security response headers – applied to every non-static response
    @app.after_request
    def _set_security_headers(response):
        # Prevent MIME-type sniffing (e.g. serving a JPEG as text/html)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        # Deny framing to block clickjacking attacks
        response.headers['X-Frame-Options'] = 'DENY'
        # Limit referrer information sent to cross-origin destinations
        response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
        # Remove server identity header to reduce fingerprinting surface
        response.headers.pop('Server', None)
        return response

    # HTTP request logging (non-static requests only)
    if access_log:
        @app.after_request
        def _after_request(response):
            if not request.path.startswith('/static/'):
                http_log.debug('%s %s %s (client=%s)', request.method, request.path, response.status_code, request.remote_addr)
            return response

    # Template cache warm-up – pre-compile all templates so workers forked
    # via --preload inherit them and the first request has zero disk I/O.
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

    # TLS support
    tls_cert = web_cfg.get('tls_cert', '')
    tls_key = web_cfg.get('tls_key', '')
    ssl_context = (tls_cert, tls_key) if tls_cert and tls_key else None
    scheme = 'https' if ssl_context else 'http'
    host = web_cfg.get('host', '0.0.0.0')
    port = web_cfg.get('port', 5000)

    log.info('Webserver starting on %s://%s:%s...', scheme, host, port)
    app.run(host=host, port=port, debug=debug_full, ssl_context=ssl_context)
