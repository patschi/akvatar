"""
app.py - Application entry point.

Creates the Flask app, registers blueprints, initializes OAuth, applies reverse-proxy
and subfolder middleware, and starts the development server when run directly.
"""

import logging
from urllib.parse import urlparse

from flask import Flask, abort, request
from werkzeug.middleware.proxy_fix import ProxyFix

from src import APP_VERSION
from src.app_middleware import MinifyingTemplateLoader, PrefixMiddleware
from src.app_monitor import start_memory_monitor
from src.app_sentry import init_sentry
from src.app_static import serve_static_file
from src.auth import auth_bp, init_oauth
from src.cleanup import start_cleanup_thread
from src.config import (
    access_log,
    app_cfg,
    branding_cfg,
    debug_full,
    security_cfg,
    tls_cert,
    tls_configured,
    tls_key,
    tls_minimum_version,
    web_cfg,
)
from src.i18n import AVAILABLE_LANGUAGES, get_js_translations, get_locale, t
from src.image_import import WEBCAM_ENABLED, import_bp
from src.imaging import AVATAR_ROOT, METADATA_ROOT, ensure_size_directories_existence
from src.reset_avatar import reset_avatar_bp
from src.routes import routes_bp
from src.sec_csp import build_csp_header, generate_csp_nonce
from src.sec_csrf import generate_csrf_token

log = logging.getLogger("app")

# Logger dedicated to HTTP request logging (keeps it separate from application logic)
http_log = logging.getLogger("http")

# Sentry SDK initialisation - runs once at import time (before Flask is created)
# so the SDK can hook into framework internals.
init_sentry()

# Suppress Werkzeug's built-in access logging - we use our own http_log at DEBUG level
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app() -> Flask:
    """Application factory - build and configure the Flask instance."""

    # Start the memory monitor thread immediately when DEBUG enabled so
    # it runs during startup and continues in workers after fork.
    if log.isEnabledFor(logging.DEBUG):
        start_memory_monitor()

    # Initialize flask app
    app = Flask(__name__, template_folder="src/templates", static_folder=None)
    app.secret_key = security_cfg["secret_key"]
    app.config["MAX_CONTENT_LENGTH"] = (
        app_cfg["max_upload_size_mb"] * 1024 * 1024
    )  # MB -> bytes

    # Strip blank lines introduced by Jinja2 block tags in rendered HTML output.
    # trim_blocks:   removes the newline after a block tag ({% ... %})
    # lstrip_blocks: strips leading whitespace before block tags on their own line
    app.jinja_env.trim_blocks = True
    app.jinja_env.lstrip_blocks = True
    # Wrap the loader so HTML comments and excess blank lines are stripped from
    # template source at compile time (once per template, not per request).
    app.jinja_env.loader = MinifyingTemplateLoader(app.jinja_env.loader)

    # Session cookie hardening.
    # HttpOnly: cookie not accessible from JavaScript (mitigates XSS session theft).
    # SameSite=Lax: browser refuses to send the cookie on cross-site POST requests,
    #   providing a first line of defense against CSRF. POST endpoints additionally
    #   require a per-session CSRF token sent via X-CSRF-Token header (see csrf.py).
    #   Lax (not Strict) is required so the OIDC redirect from Authentik back to
    #   /callback still carries the session cookie for state verification.
    # Secure: instructs the browser to only transmit the cookie over HTTPS connections.
    #   The Secure flag is about what the *browser* sees, not the Flask-to-proxy link.
    #   When a reverse proxy terminates TLS, the browser talks to the proxy over HTTPS
    #   so it will honor the flag; the internal proxy→Flask link being plain HTTP is
    #   irrelevant.  We therefore set Secure whenever the public URL uses https://, not
    #   only when Flask's own built-in server has TLS configured.
    #   security.session_cookie_secure overrides this auto-detection when set explicitly.
    # Permanent + lifetime: enforce an absolute session expiry (default: 30 min).
    _secure_override = security_cfg.get("session_cookie_secure", None)
    _secure_cookie_option = (
        _secure_override
        if _secure_override is not None
        else app_cfg.get("public_base_url", "").startswith("https://")
    )
    app.config["SESSION_COOKIE_NAME"] = "akvatar_session"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _secure_cookie_option
    app.config["PERMANENT_SESSION_LIFETIME"] = security_cfg.get(
        "web_session_lifetime_seconds", 1800
    )

    app.debug = debug_full
    app.config["TEMPLATES_AUTO_RELOAD"] = debug_full

    log.debug("Flask app created (max upload = %d MB).", app_cfg["max_upload_size_mb"])

    # Ensure the avatar storage directory tree exists (root + all size sub-dirs + metadata).
    # ensure_size_directories_existence() creates AVATAR_ROOT, size sub-dirs, and METADATA_ROOT.
    ensure_size_directories_existence()
    log.debug("Avatar storage root: %s", AVATAR_ROOT.resolve())
    log.debug("Metadata storage root: %s", METADATA_ROOT.resolve())

    # Subfolder support
    # Derive the path prefix from public_base_url (e.g. "/avatar-update" from
    # "https://portal.example.com/avatar-update").  Apply PrefixMiddleware as
    # the inner middleware so it only fires when the reverse proxy has NOT
    # already set SCRIPT_NAME via X-Forwarded-Prefix (handled by ProxyFix).
    _public_path = urlparse(app_cfg.get("public_base_url", "")).path.rstrip("/")
    if _public_path:
        app.wsgi_app = PrefixMiddleware(app.wsgi_app, _public_path)
        log.info("PrefixMiddleware applied - app is served under %r.", _public_path)

    # Reverse proxy support
    # ProxyFix is the OUTER middleware (runs first on every request).  It reads
    # X-Forwarded-For/Proto/Host/Prefix so that url_for() generates correct
    # external URLs and remote_addr reflects the real client IP.
    # IMPORTANT: must wrap PrefixMiddleware so that when X-Forwarded-Prefix is
    # present, ProxyFix sets SCRIPT_NAME before PrefixMiddleware checks it.
    # Disable via webserver.proxy_mode: false when running without a reverse proxy.
    if web_cfg.get("proxy_mode", True):
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)
        log.debug(
            "ProxyFix middleware applied (x_for=1, x_proto=1, x_host=1, x_prefix=1)."
        )
    else:
        log.info("Proxy mode disabled - ProxyFix middleware not applied.")

    # wsgi.url_scheme - force the correct transport scheme for this deployment.
    # _set_url_scheme is the outermost WSGI wrapper so it runs first on every
    # request.  It seeds wsgi.url_scheme from the startup configuration so that
    # url_for() produces correctly-schemed URLs even before any X-Forwarded-Proto
    # header is consulted.  ProxyFix (when enabled) runs inside this wrapper and
    # overwrites wsgi.url_scheme on a per-request basis from X-Forwarded-Proto,
    # so proxy-terminated HTTPS is handled correctly without relying on the seed.
    _url_scheme = "https" if tls_configured else "http"
    _inner_wsgi = app.wsgi_app

    def _set_url_scheme(environ, start_response):
        # Seed wsgi.url_scheme so Flask generates the correct scheme in url_for() calls
        environ["wsgi.url_scheme"] = _url_scheme
        return _inner_wsgi(environ, start_response)

    app.wsgi_app = _set_url_scheme
    log.info(
        "Transport scheme: %s (wsgi.url_scheme=%r).", _url_scheme.upper(), _url_scheme
    )

    # Initialize OIDC / OAuth
    init_oauth(app)

    # Register route blueprints
    app.register_blueprint(auth_bp)  # /login, /callback, /logout, /logged-out
    app.register_blueprint(routes_bp)  # /, /dashboard, /api/upload, /user-avatars
    app.register_blueprint(reset_avatar_bp)  # /api/remove-avatar
    app.register_blueprint(import_bp)  # /api/fetch-gravatar, /api/fetch-url

    # Rate limiting on avatar/metadata serving endpoints (before_request hook).
    # Deferred import: rate_limit imports src.config at module level which triggers
    # config validation; importing here keeps the startup sequence predictable.
    from src.rate_limit import init_rate_limiting

    init_rate_limiting(app)

    # Serve static files from in-memory cache
    @app.route("/static/<path:filename>", endpoint="static", methods=["GET"])
    def _serve_static(filename):
        return serve_static_file(filename)

    log.debug("Web routes registered.")
    log.info("App initialized.")

    # Template context processor - inject shared variables into all templates
    _brand_name = branding_cfg.get("name", "Avatar Updater")

    @app.context_processor
    def _inject_globals():
        locale = get_locale()
        return {
            "brand_name": _brand_name,
            "app_version": APP_VERSION,
            "t": t,
            "lang": locale.split("_")[0],
            "locale": locale,
            "i18n": get_js_translations(locale),
            "languages": AVAILABLE_LANGUAGES,
            "csrf_token": generate_csrf_token,
            # Per-request CSP nonce - called as {{ csp_nonce() }} in templates.
            # Generates once per request and is stored on Flask `g` so the
            # same value appears in both inline <script nonce="…"> tags and
            # the Content-Security-Policy response header.
            "csp_nonce": generate_csp_nonce,
        }

    # Trusted host restriction
    # Flask validates Request.host against this list and raises SecurityError
    # (HTTP 400) automatically when it does not match.  Port is stripped before
    # comparison; prefix an entry with "." to match all subdomains.
    _trusted_hosts_raw = web_cfg.get("trusted_hosts", None)
    _trusted_hosts = (
        [h.lower().strip() for h in _trusted_hosts_raw if h]
        if _trusted_hosts_raw
        else None
    )
    if _trusted_hosts:
        app.config["TRUSTED_HOSTS"] = _trusted_hosts
        log.info("Trusted hosts restriction active: %s", _trusted_hosts)
    else:
        log.warning(
            "No trusted_hosts restriction configured - any Host header is accepted."
        )

    # Globally allowed HTTP methods - derived once from the URL map so the set
    # is always in sync with whatever routes the application actually handles.
    # Any verb absent from this set is rejected with 405 before Flask routing runs.
    # NOTE: Flask automatically adds HEAD (for every GET route) and OPTIONS
    # (for every route) to the map, so both will be present in the derived set.
    _ALLOWED_METHODS = frozenset(
        method for rule in app.url_map.iter_rules() for method in rule.methods
    )
    log.debug(
        "Allowed HTTP methods derived from route map: %s", sorted(_ALLOWED_METHODS)
    )

    @app.before_request
    def _reject_disallowed_methods():
        # Block any HTTP method that is not in the globally-allowed set
        if request.method not in _ALLOWED_METHODS:
            log.debug(
                "Rejected disallowed HTTP method %r for %r (client=%s).",
                request.method,
                request.path,
                request.remote_addr,
            )
            abort(405)

    # Security response headers - applied to every response
    @app.after_request
    def _set_security_headers(response):
        # Prevent MIME-type sniffing (e.g. serving a JPEG as text/html)
        response.headers["X-Content-Type-Options"] = "nosniff"

        # HTML-only headers
        if response.content_type.startswith("text/html"):
            # Clickjacking protection - deny framing of HTML pages entirely
            response.headers["X-Frame-Options"] = "DENY"

            # Content Security Policy - policy is built in sec_csp.py.
            # build_csp_header() returns None when disabled via security.csp_enabled=false,
            # in which case the header is omitted entirely.
            nonce = generate_csp_nonce()
            csp = build_csp_header(nonce)
            if csp is not None:
                response.headers["Content-Security-Policy"] = csp

        # Limit referrer information sent to cross-origin destinations
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # HSTS - instruct browsers to always use HTTPS for this origin.
        # Only set when TLS is active so plain-HTTP deployments are not broken.
        # 63072000 s = 2 years (recommended minimum for preload eligibility).
        if tls_configured:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )

        # Permissions Policy - disable browser APIs this app never uses.
        # camera=(self) is required when the webcam import feature is enabled,
        # otherwise getUserMedia() is hard-blocked by the browser regardless of
        # user consent.  When disabled, camera=() denies the API entirely.
        camera_policy = "camera=(self)" if WEBCAM_ENABLED else "camera=()"
        response.headers["Permissions-Policy"] = (
            f"{camera_policy}, microphone=(), geolocation=(), payment=()"
        )

        return response

    # HTTP request logging (non-static requests only)
    if access_log:

        @app.after_request
        def _after_request(response):
            # Skip static assets
            if request.path.startswith("/static/"):
                return response
            # Skip health check calls from local to reduce noise
            if request.path == "/healthz" and request.remote_addr == "127.0.0.1":
                return response
            # Otherwise, log.
            http_log.debug(
                "%s %s %s (client=%s)",
                request.method,
                request.path,
                response.status_code,
                request.remote_addr,
            )
            return response

    # Template cache warm-up - pre-compile all templates so workers forked
    # via --preload inherit them and the first request has zero disk I/O.
    all_templates = app.jinja_loader.list_templates()
    log.info("Warming up template cache by pre-compiling all templates...")
    for template_name in all_templates:
        log.debug("Pre-compiling template: %s", template_name)
        app.jinja_env.get_template(template_name)
    log.info("Template cache warmed: %d template(s) pre-compiled.", len(all_templates))

    return app


if __name__ == "__main__":
    app = create_app()

    # Start the background cleanup thread (respects config interval; 0 = disabled)
    start_cleanup_thread()

    # TLS support
    import ssl as _ssl

    if tls_configured:
        # Build a full SSLContext so minimum_version can be enforced;
        # Werkzeug accepts either a (cert, key) tuple or an SSLContext instance.
        ssl_context = _ssl.SSLContext(_ssl.PROTOCOL_TLS_SERVER)
        ssl_context.minimum_version = tls_minimum_version
        ssl_context.load_cert_chain(tls_cert, tls_key)
    else:
        ssl_context = None
    scheme = "https" if ssl_context else "http"
    host = web_cfg.get("host", "0.0.0.0")
    port = web_cfg.get("port", 5000)

    log.info("Webserver starting on %s://%s:%s...", scheme, host, port)
    app.run(host=host, port=port, debug=debug_full, ssl_context=ssl_context)
