"""
app.py – Application entry point.

Creates the Flask app, registers blueprints, initialises OAuth, applies reverse-proxy
and subfolder middleware, and starts the development server when run directly.
"""

import logging
from urllib.parse import urlparse

from flask import Flask, request
from werkzeug.middleware.proxy_fix import ProxyFix

from src import APP_VERSION
from src.app_middleware import MinifyingTemplateLoader, PrefixMiddleware
from src.app_monitor import start_memory_monitor
from src.app_sentry import init_sentry
from src.app_static import serve_static_file
from src.auth import auth_bp, init_oauth
from src.sec_csp import generate_csp_nonce, build_csp_header
from src.sec_csrf import generate_csrf_token
from src.cleanup import start_cleanup_thread
from src.config import app_cfg, security_cfg, web_cfg, branding_cfg, debug_full, access_log
from src.i18n import t, get_locale, get_js_translations, AVAILABLE_LANGUAGES
from src.imaging import AVATAR_ROOT, METADATA_ROOT, ensure_size_directories_existence
from src.image_import import import_bp
from src.reset_avatar import reset_avatar_bp
from src.routes import routes_bp

log = logging.getLogger("app")

# Logger dedicated to HTTP request logging (keeps it separate from application logic)
http_log = logging.getLogger("http")

# Sentry SDK initialisation – runs once at import time (before Flask is created)
# so the SDK can hook into framework internals.
init_sentry()

# Suppress Werkzeug's built-in access logging – we use our own http_log at DEBUG level
logging.getLogger("werkzeug").setLevel(logging.WARNING)


def create_app() -> Flask:
    """Application factory – build and configure the Flask instance."""

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
    #   providing a first line of defence against CSRF. POST endpoints additionally
    #   require a per-session CSRF token sent via X-CSRF-Token header (see csrf.py).
    #   Lax (not Strict) is required so the OIDC redirect from Authentik back to
    #   /callback still carries the session cookie for state verification.
    # Secure: instructs the browser to only transmit the cookie over HTTPS connections.
    #   The Secure flag is about what the *browser* sees, not the Flask-to-proxy link.
    #   When a reverse proxy terminates TLS, the browser talks to the proxy over HTTPS
    #   so it will honour the flag; the internal proxy→Flask link being plain HTTP is
    #   irrelevant.  We therefore set Secure whenever the public URL uses https://, not
    #   only when Flask's own built-in server has TLS configured.
    #   security.session_cookie_secure overrides this auto-detection when set explicitly.
    # Permanent + lifetime: enforce an absolute session expiry (default: 30 min).
    _secure_override = security_cfg.get("session_cookie_secure", None)
    _tls_active = (
        _secure_override
        if _secure_override is not None
        else app_cfg.get("public_base_url", "").startswith("https://")
    )
    app.config["SESSION_COOKIE_NAME"] = "akvatar_session"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = _tls_active
    app.config["PERMANENT_SESSION_LIFETIME"] = security_cfg.get(
        "web_session_lifetime_seconds", 1800
    )

    app.debug = debug_full
    app.config["TEMPLATES_AUTO_RELOAD"] = debug_full

    log.debug("Flask app created (max upload = %d MB).", app_cfg["max_upload_size_mb"])

    # Ensure the avatar storage directory tree exists (root + all size sub-dirs)
    AVATAR_ROOT.mkdir(parents=True, exist_ok=True)
    METADATA_ROOT.mkdir(parents=True, exist_ok=True)
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
        log.info("PrefixMiddleware applied – app is served under %r.", _public_path)

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
        log.info("Proxy mode disabled – ProxyFix middleware not applied.")

    # Initialise OIDC / OAuth
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
    @app.route("/static/<path:filename>", endpoint="static")
    def _serve_static(filename):
        return serve_static_file(filename)

    log.debug("Web routes registered.")
    log.info("App initialized.")

    # Template context processor – inject shared variables into all templates
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
            # Per-request CSP nonce – called as {{ csp_nonce() }} in templates.
            # Generates once per request and is stored on Flask `g` so the
            # same value appears in both inline <script nonce="…"> tags and
            # the Content-Security-Policy response header.
            "csp_nonce": generate_csp_nonce,
        }

    # Security response headers – applied to every response
    @app.after_request
    def _set_security_headers(response):
        # Prevent MIME-type sniffing (e.g. serving a JPEG as text/html)
        response.headers["X-Content-Type-Options"] = "nosniff"

        # HTML-only headers
        if response.content_type.startswith("text/html"):
            # Clickjacking protection – deny framing of HTML pages entirely
            response.headers["X-Frame-Options"] = "DENY"

            # Content Security Policy – policy is built in sec_csp.py.
            # build_csp_header() returns None when disabled via security.csp_enabled=false,
            # in which case the header is omitted entirely.
            nonce = generate_csp_nonce()
            csp = build_csp_header(nonce)
            if csp is not None:
                response.headers["Content-Security-Policy"] = csp

        # Limit referrer information sent to cross-origin destinations
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # HSTS – instruct browsers to always use HTTPS for this origin.
        # Only set when TLS is active so plain-HTTP deployments are not broken.
        # 63072000 s = 2 years (recommended minimum for preload eligibility).
        if _tls_active:
            response.headers["Strict-Transport-Security"] = (
                "max-age=63072000; includeSubDomains"
            )

        # Permissions Policy – disable browser APIs this app never uses
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=()"
        )

        return response

    # HTTP request logging (non-static requests only)
    if access_log:

        @app.after_request
        def _after_request(response):
            if not request.path.startswith("/static/"):
                http_log.debug(
                    "%s %s %s (client=%s)",
                    request.method,
                    request.path,
                    response.status_code,
                    request.remote_addr,
                )
            return response

    # Template cache warm-up – pre-compile all templates so workers forked
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
    tls_cert = web_cfg.get("tls_cert", "")
    tls_key = web_cfg.get("tls_key", "")
    ssl_context = (tls_cert, tls_key) if tls_cert and tls_key else None
    scheme = "https" if ssl_context else "http"
    host = web_cfg.get("host", "0.0.0.0")
    port = web_cfg.get("port", 5000)

    log.info("Webserver starting on %s://%s:%s...", scheme, host, port)
    app.run(host=host, port=port, debug=debug_full, ssl_context=ssl_context)
