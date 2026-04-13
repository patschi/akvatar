"""
config.py - Load YAML configuration and set up application-wide logging.

Reads config.yml once at import time so every other module can simply
`from src.config import cfg`.
"""

import logging
import os
import ssl
import sys
from urllib.parse import urlparse

import yaml

from src import APP_NAME, APP_VERSION
from src.image_formats import FORMAT_MAP as _FORMAT_MAP


def _fatal(msg: str) -> None:
    """Print a FATAL error and exit immediately."""
    print(f"FATAL: {msg}", file=sys.stderr)
    sys.exit(1)


def _fatal_unless(condition: bool, msg: str) -> None:
    """Exit with a FATAL error if *condition* is false."""
    if not condition:
        _fatal(msg)


CONFIG_PATH = os.environ.get("CONFIG_PATH", "data/config/config.yml")

# Load configuration from YAML file
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as _f:
        cfg = yaml.safe_load(_f)
except FileNotFoundError:
    _fatal(f"Configuration file not found at {CONFIG_PATH!r}.")
except yaml.YAMLError as exc:
    _fatal(f"Failed to parse {CONFIG_PATH!r}: {exc}")

# Convenience references for each config section
dry_run = cfg.get("dry_run", False)
branding_cfg = cfg.get("branding", {})
app_cfg = cfg.get("app", {})
security_cfg = cfg.get("security", {})
web_cfg = cfg.get("webserver", {})
oidc_cfg = cfg.get("oidc", {})
ak_cfg = cfg.get("authentik", {})
ldap_cfg = cfg.get("ldap", {})  # May be absent if disabled
img_cfg = cfg.get("images", {})
cleanup_cfg = cfg.get("cleanup", {})
import_cfg = cfg.get("image_import", {})
sentry_cfg = cfg.get("sentry", {})
access_log = bool(web_cfg.get("access_log", False))
http2_cfg = web_cfg.get("http2", {})

# ---------------------------------------------------------------------------
# Internal defaults - hardcoded application constants not exposed via config.yml.
# Centralised here so every module imports from one place instead of scattering
# magic numbers across the codebase.
# ---------------------------------------------------------------------------

# Default timeout (in seconds) for all outbound HTTP requests: OIDC discovery,
# Authentik Admin API calls, remote image fetches, Sentry tunnel relay, etc.
# Every module that makes external requests should import and use this value
# (or derive its own timeout from it) instead of hardcoding a per-call number.
EXTERNAL_REQUEST_TIMEOUT: int = 10

# Maximum number of HTTP redirect hops to follow during remote image import.
# Each hop is individually validated against the SSRF private-IP filter.
MAX_REDIRECTS: int = 5

# OIDC scopes requested during authorization - always identity, profile, and email.
OIDC_SCOPES: str = "openid profile email"

# Fallback locale used when no match is found in the OIDC claim, session,
# cookie, or Accept-Language header.
DEFAULT_LOCALE: str = "en_US"

# Pillow decompression bomb pixel limit.  A small file on disk can expand to an
# enormous bitmap in memory; 25 MP at 4 bytes/pixel ≈ 100 MB of RAM.
MAX_IMAGE_PIXELS: int = 25_000_000

# ---------------------------------------------------------------------------
# Per-setting named exports - defaults resolved centrally; re-use in modules
# ---------------------------------------------------------------------------

# Application
public_base_url: str = app_cfg.get("public_base_url", "").rstrip("/")
public_avatar_url: str = app_cfg.get("public_avatar_url", "").rstrip("/")

# Both public URLs are required and must be absolute (scheme + netloc).
for _key, _val in (
    ("app.public_base_url", public_base_url),
    ("app.public_avatar_url", public_avatar_url),
):
    _fatal_unless(bool(_val), f"{_key} is required but not set in config.yml.")
    _p = urlparse(_val)
    _fatal_unless(
        bool(_p.scheme and _p.netloc),
        f"{_key}={_val!r} must be an absolute URL (e.g. 'https://example.com').",
    )

avatar_storage_path: str = app_cfg.get("avatar_storage_path", "/data/avatars")
max_upload_size_mb: int = int(app_cfg.get("max_upload_size_mb", 10))
branding_name: str = branding_cfg.get("name", "Avatar Updater")

# Images
img_sizes: list = img_cfg.get("sizes", [])
img_formats: list = img_cfg.get("formats", [])
img_jpeg_quality: int = int(img_cfg.get("jpeg_quality", 90))
img_webp_quality: int = int(img_cfg.get("webp_quality", 85))
img_avif_quality: int = int(img_cfg.get("avif_quality", 80))
img_png_compress_level: int = int(img_cfg.get("png_compress_level", 6))

# Authentik
ak_base_url: str = ak_cfg.get("base_url", "")
ak_api_token: str = ak_cfg.get("api_token", "")
ak_avatar_attribute: str = ak_cfg.get("avatar_attribute", "avatar")
ak_skip_cert_verify: bool = bool(ak_cfg.get("skip_cert_verify", False))

# OIDC
oidc_issuer_url: str = oidc_cfg.get("issuer_url", "")
oidc_client_id: str = oidc_cfg.get("client_id", "")
oidc_client_secret: str = oidc_cfg.get("client_secret", "")
oidc_username_claim: str = oidc_cfg.get("username_claim", "preferred_username")
oidc_end_provider_session: bool = bool(oidc_cfg.get("end_provider_session", False))
oidc_skip_cert_verify: bool = bool(oidc_cfg.get("skip_cert_verify", False))

# LDAP
ldap_enabled: bool = bool(ldap_cfg.get("enabled", False))
ldap_server_urls: list[str] = [
    s.strip() for s in ldap_cfg.get("servers", "").split(",") if s.strip()
]
ldap_port: int = int(ldap_cfg.get("port", 636))
ldap_use_ssl: bool = bool(ldap_cfg.get("use_ssl", False))
ldap_skip_cert_verify: bool = bool(ldap_cfg.get("skip_cert_verify", False))
ldap_bind_dn: str = ldap_cfg.get("bind_dn", "")
ldap_bind_password: str = ldap_cfg.get("bind_password", "")
ldap_search_base: str = ldap_cfg.get("search_base", "")
ldap_search_filter: str = ldap_cfg.get("search_filter", "(objectSid={ldap_uniq})")
ldap_photos: list = ldap_cfg.get("photos", [])

# Security - metadata_access validated here so all consumers get the corrected value
_METADATA_ACCESS_MODES = frozenset({"owner_only", "public"})
_raw_metadata_access: str = security_cfg.get("metadata_access", "owner_only")
metadata_access: str = (
    _raw_metadata_access
    if _raw_metadata_access in _METADATA_ACCESS_MODES
    else "owner_only"
)
session_cookie_secure = security_cfg.get("session_cookie_secure", None)
web_session_lifetime_seconds: int = int(
    security_cfg.get("web_session_lifetime_seconds", 1800)
)
csp_enabled: bool = bool(security_cfg.get("csp_enabled", True))
csp_report_only: bool = bool(security_cfg.get("csp_report_only", False))
csp_report_uri: str = security_cfg.get("csp_report_uri", "")

# Webserver
proxy_mode: bool = bool(web_cfg.get("proxy_mode", True))
# When trusted_hosts is null/omitted, auto-derive from the configured public URLs.
_trusted_hosts_raw = web_cfg.get("trusted_hosts", None)
if _trusted_hosts_raw:
    trusted_hosts: list[str] | None = [h.lower().strip() for h in _trusted_hosts_raw if h] or None
else:
    _auto_hosts = []
    for _url in (public_base_url, public_avatar_url):
        _hostname = urlparse(_url).hostname
        if _hostname and _hostname not in _auto_hosts:
            _auto_hosts.append(_hostname)
    trusted_hosts = _auto_hosts or None

web_host: str = web_cfg.get("host", "0.0.0.0")
web_port: int = int(web_cfg.get("port", 5000))
web_workers: int = int(web_cfg.get("workers", 2))
web_threads: int = int(web_cfg.get("threads", 4))
web_timeout: int = int(web_cfg.get("timeout", 120))
http2_enabled: bool = bool(http2_cfg.get("enabled", True))

# Cleanup
cleanup_interval: str = str(cleanup_cfg.get("interval", "0 2 * * *")).strip()
cleanup_on_startup: bool = bool(cleanup_cfg.get("on_startup", False))
cleanup_retention_count: int = int(cleanup_cfg.get("avatar_retention_count", 2))
cleanup_when_deleted: bool = bool(cleanup_cfg.get("when_user_deleted", True))
cleanup_when_deactivated: bool = bool(cleanup_cfg.get("when_user_deactivated", False))

# Image import
gravatar_enabled: bool = bool(import_cfg.get("gravatar", {}).get("enabled", True))
gravatar_restrict_email: bool = bool(
    import_cfg.get("gravatar", {}).get("restrict_email", True)
)
import_url_enabled: bool = bool(import_cfg.get("url", {}).get("enabled", True))
import_url_restrict_private_ips: bool = bool(
    import_cfg.get("url", {}).get("restrict_private_ips", True)
)
webcam_enabled: bool = bool(import_cfg.get("webcam", {}).get("enabled", True))

# Sentry
sentry_enabled: bool = bool(sentry_cfg.get("enabled", False))
sentry_dsn: str = sentry_cfg.get("dsn", "")
sentry_environment: str = sentry_cfg.get("environment", "")
sentry_capture_performance: bool = bool(sentry_cfg.get("capture_performance", False))
sentry_traces_sample_rate: float = float(sentry_cfg.get("traces_sample_rate", 0.2))
sentry_capture_errors: bool = bool(sentry_cfg.get("capture_errors", True))
sentry_sample_rate: float = float(sentry_cfg.get("sample_rate", 1.0))
sentry_send_default_pii: bool = bool(sentry_cfg.get("send_default_pii", False))

# Sentry - Browser (client-side)
_sentry_browser_cfg = sentry_cfg.get("browser", {})
sentry_browser_enabled: bool = bool(_sentry_browser_cfg.get("enabled", False))
sentry_browser_js_sdk_url: str = _sentry_browser_cfg.get("js_sdk_url", "")
sentry_browser_dsn: str = _sentry_browser_cfg.get("dsn", "") or sentry_dsn
sentry_browser_sample_rate: float = float(_sentry_browser_cfg.get("sample_rate", 1.0))
sentry_browser_traces_sample_rate: float = float(
    _sentry_browser_cfg.get("traces_sample_rate", 0.2)
)
sentry_browser_tunnel_enabled: bool = bool(
    _sentry_browser_cfg.get("tunnel_enabled", True)
)

# Rate limiting configuration (exported for use in rate_limit.py)
# The full section dict is passed to _RateLimitManager as-is; the named
# variables below are for the upload cooldown so rate_limit.py does not
# need to repeat the default values or the master-switch logic.
rate_limiting_cfg = cfg.get("rate_limiting", {})
_upload_rate_cfg = rate_limiting_cfg.get("upload", {})
# Master switch governs all rate limiting (IP limiter and upload cooldown).
# upload_cooldown_enabled is False when either the master or upload.enabled is off.
rate_limiting_enabled: bool = bool(rate_limiting_cfg.get("enabled", False))
upload_cooldown_enabled: bool = rate_limiting_enabled and bool(
    _upload_rate_cfg.get("enabled", True)
)
upload_cooldown_secs: int = int(_upload_rate_cfg.get("cooldown", 10))

# Logging setup
_LOG_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

# Environment variable DEBUG_MODE=true overrides the config file setting
debug_full = os.environ.get("DEBUG_MODE", "").lower() == "true" or bool(
    app_cfg.get("debug_full", False)
)

_configured_level = app_cfg.get("log_level", "INFO").upper()
_level = (
    logging.DEBUG if debug_full else _LOG_LEVELS.get(_configured_level, logging.INFO)
)

logging.basicConfig(
    level=_level,
    format="[%(asctime)s] [%(levelname)-7s] [%(name)-11.11s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S %z",
)

# Suppress hpack's per-header DEBUG spam - it floods logs when HTTP/2 is active
logging.getLogger("hpack").setLevel(logging.WARNING)
# Suppress PIL.Image's plugin registration DEBUG spam ("Importing XxxImagePlugin")
logging.getLogger("PIL").setLevel(logging.WARNING)

log = logging.getLogger("config")
log.info("Starting %s v%s...", APP_NAME, APP_VERSION)
log.debug("Configuration loaded from %r.", CONFIG_PATH)
if debug_full:
    log.warning(
        "FULL DEBUG MODE is enabled - Flask debugger, template auto-reload, and verbose logging are active. Disable in production."
    )
    log.debug("Log level forced to DEBUG by debug_full.")
log.debug("Log level set to %s.", _configured_level)
if trusted_hosts:
    if _trusted_hosts_raw:
        log.debug("Trusted hosts (explicit): %s", trusted_hosts)
    else:
        log.debug(
            "Trusted hosts (auto-derived from public URLs): %s", trusted_hosts
        )
if dry_run:
    log.warning(
        "DRY-RUN MODE is enabled - no changes will be pushed to Authentik or LDAP."
    )

# Warn when security.metadata_access had an unrecognized value and fell back to owner_only
if metadata_access != _raw_metadata_access:
    log.warning(
        "Unknown security.metadata_access value %r - falling back to owner_only.",
        _raw_metadata_access,
    )

# Startup SSL warnings (logged once at import time)
# TLS configuration (exported for use by app.py and run_app.py)
tls_cfg = web_cfg.get("tls", {})
tls_cert = tls_cfg.get("cert", "")
tls_key = tls_cfg.get("key", "")
tls_configured = bool(tls_cert and tls_key)

# Validate that configured TLS file paths actually exist on disk
if tls_cert:
    _fatal_unless(
        os.path.isfile(tls_cert),
        f"webserver.tls.cert={tls_cert!r} does not exist or is not a file.",
    )
if tls_key:
    _fatal_unless(
        os.path.isfile(tls_key),
        f"webserver.tls.key={tls_key!r} does not exist or is not a file.",
    )

# Minimum TLS version: dynamically resolved against ssl.TLSVersion by name so
# any TLS version added to Python's ssl module in the future is valid without
# requiring code changes.
_tls_min_ver_str = tls_cfg.get("min_version", "TLSv1_2")
_valid_tls_versions = [v.name for v in ssl.TLSVersion]
_fatal_unless(
    _tls_min_ver_str in _valid_tls_versions,
    f"webserver.tls.min_version={_tls_min_ver_str!r} is not a valid TLS version. "
    f"Valid values: {', '.join(_valid_tls_versions)}.",
)
tls_minimum_version: ssl.TLSVersion = ssl.TLSVersion[_tls_min_ver_str]

if not tls_configured:
    log.warning(
        "Server TLS is NOT configured - the built-in server will run over plain HTTP."
    )
log.debug("TLS minimum version: %s.", tls_minimum_version.name)

# HTTP/2 startup status
if http2_enabled and tls_configured:
    log.info(
        "HTTP/2 support enabled (TLS configured, ALPN negotiation will advertise h2)."
    )
elif http2_enabled and not tls_configured:
    log.warning(
        "HTTP/2 is enabled in config but TLS is not configured - HTTP/2 requires TLS and will not be used."
    )

if ldap_enabled:
    for _srv in ldap_server_urls:
        _scheme = urlparse(_srv).scheme.lower()
        if _scheme == "ldap" or (not _scheme and not ldap_use_ssl):
            log.warning(
                "One or more LDAP servers are configured WITHOUT SSL - credentials and data will be sent in plain text."
            )
            break
    if ldap_skip_cert_verify:
        log.warning(
            "LDAP TLS certificate verification is DISABLED - connections are vulnerable to MITM attacks."
        )

if ak_skip_cert_verify:
    log.warning(
        "Authentik API TLS certificate verification is DISABLED - connections are vulnerable to MITM attacks."
    )

if oidc_skip_cert_verify:
    log.warning(
        "OIDC TLS certificate verification is DISABLED - connections are vulnerable to MITM attacks."
    )

# Validate configured image sizes for backends
_valid_sizes = img_cfg.get("sizes", [])

# Validated Authentik avatar settings (exported for use by upload.py)
ak_avatar_size = ak_cfg.get("avatar_size", 1024)
_fatal_unless(
    ak_avatar_size in _valid_sizes,
    f"authentik.avatar_size={ak_avatar_size} is not in images.sizes={_valid_sizes}.",
)

_valid_formats_lower = {f.lower() for f in img_cfg.get("formats", [])}

# Validate each entry in images.formats against known format keys.
# Catches typos (e.g. "jpge") and unsupported formats (e.g. "bmp") at
# startup rather than at runtime when the first upload triggers a KeyError
# inside process_image().
for _fmt in img_cfg.get("formats", []):
    _fatal_unless(
        _fmt.lower() in _FORMAT_MAP,
        f"images.formats contains unsupported format {_fmt!r}. "
        f"Supported values: {sorted(_FORMAT_MAP.keys())}.",
    )

_ak_avatar_format = ak_cfg.get("avatar_format", "jpg")
_fatal_unless(
    _ak_avatar_format.lower() in _FORMAT_MAP,
    f"authentik.avatar_format={_ak_avatar_format!r} is not a valid format. "
    f"Valid values: {sorted(_FORMAT_MAP.keys())}.",
)
# Resolve the canonical file extension ("jpeg" and "jpg" both resolve to "jpg")
ak_avatar_ext = _FORMAT_MAP[_ak_avatar_format.lower()][1]
_fatal_unless(
    ak_avatar_ext in _valid_formats_lower,
    f"authentik.avatar_format={_ak_avatar_format!r} (ext={ak_avatar_ext!r}) is not in "
    f"images.formats={list(img_cfg.get('formats', []))}.",
)
log.debug(
    "Authentik API will use %dx%d %s for avatar URL.",
    ak_avatar_size,
    ak_avatar_size,
    ak_avatar_ext.upper(),
)

if ldap_enabled:
    if not ldap_photos:
        log.warning(
            "LDAP is enabled but no photo attributes are configured (ldap.photos is empty)."
        )

    _valid_image_types = set(_FORMAT_MAP.keys())
    _REQUIRED_PHOTO_KEYS = ("attribute", "type", "image_type", "image_size")

    for _i, _photo in enumerate(ldap_photos):
        _pfx = f"ldap.photos[{_i}]"

        # Every photo entry must have the four required keys
        for _key in _REQUIRED_PHOTO_KEYS:
            if _key not in _photo:
                _fatal(f'{_pfx} is missing required key "{_key}".')

        # Validate type and image_type against known values
        _fatal_unless(
            _photo["type"] in ("binary", "url"),
            f'{_pfx}.type={_photo["type"]!r} must be "binary" or "url".',
        )
        _fatal_unless(
            _photo["image_type"] in _valid_image_types,
            f"{_pfx}.image_type={_photo['image_type']!r} must be one of {sorted(_valid_image_types)}.",
        )

        # URL-type photos reference pre-generated files, so the size and format
        # must exist in the images.sizes / images.formats lists.
        if _photo["type"] == "url":
            _fatal_unless(
                _photo["image_size"] in _valid_sizes,
                f"{_pfx}.image_size={_photo['image_size']} is not in images.sizes={_valid_sizes} (required for type=url).",
            )
            _ext = _FORMAT_MAP[_photo["image_type"]][1]
            _fatal_unless(
                _ext in _valid_formats_lower,
                f"{_pfx}.image_type={_photo['image_type']!r} (ext={_ext}) is not in images.formats (required for type=url).",
            )

        log.debug(
            "LDAP photo[%d]: attribute=%s, type=%s, image_type=%s, size=%d, max_file_size=%d KB.",
            _i,
            _photo["attribute"],
            _photo["type"],
            _photo["image_type"],
            _photo["image_size"],
            _photo.get("max_file_size", 0),
        )

    log.debug(
        "LDAP user lookup: base=%r, filter=%r, servers=%s (port %s).",
        ldap_search_base,
        ldap_search_filter,
        ", ".join(ldap_server_urls),
        ldap_port,
    )
    log.info("LDAP configured with %d photo attribute(s).", len(ldap_photos))

# Validate Flask secret key
secret_key: str = security_cfg.get("secret_key", "")
_SECRET_KEY_MIN_LENGTH = 32
_SECRET_KEY_HINT = (
    'Generate one with: python3 -c "import secrets; print(secrets.token_hex(32))"'
)

_fatal_unless(
    secret_key != "CHANGE-ME-to-a-random-secret-key",
    f"security.secret_key is still set to the default placeholder value. {_SECRET_KEY_HINT}",
)
_fatal_unless(
    len(secret_key) >= _SECRET_KEY_MIN_LENGTH,
    f"security.secret_key is too short ({len(secret_key)} chars, minimum {_SECRET_KEY_MIN_LENGTH}). {_SECRET_KEY_HINT}",
)

log.debug("Secret key validation passed (length=%d).", len(secret_key))

# Validate images.rgba_background_color - must be a 3-element RGB list
_rgba_bg = img_cfg.get("rgba_background_color", [255, 255, 255])
_fatal_unless(
    isinstance(_rgba_bg, list)
    and len(_rgba_bg) == 3
    and all(isinstance(v, int) and 0 <= v <= 255 for v in _rgba_bg),
    "images.rgba_background_color must be a list of three integers [R, G, B] "
    "each in the range 0-255 (e.g. [255, 255, 255] for white).",
)
# Exported as a typed tuple for modules that need the RGBA background color
img_rgba_background_color: tuple[int, int, int] = (
    _rgba_bg[0],
    _rgba_bg[1],
    _rgba_bg[2],
)

# Verify Pillow runtime support for every configured format by attempting a
# minimal encode.  A 1x1 RGB image is encoded to a BytesIO buffer for each
# format - if the encoder raises, the codec is missing or broken.  This gives
# a clear FATAL message at startup instead of a cryptic error on the first
# upload (e.g. Pillow built without libwebp or libavif).
try:
    import io as _io

    from PIL import Image as _PilImage

    log.debug("Verifying Pillow runtime support for configured image formats...")
    _test_img = _PilImage.new("RGB", (1, 1), (0, 0, 0))
    for _fmt in img_cfg.get("formats", []):
        _pillow_fmt = _FORMAT_MAP[_fmt.lower()][0]
        try:
            _buf = _io.BytesIO()
            _test_img.save(_buf, format=_pillow_fmt)
            log.debug("Pillow %s (%s) encode check passed.", _fmt, _pillow_fmt)
        except Exception as _enc_exc:
            _fatal(
                f"images.formats includes {_fmt!r} but Pillow cannot encode "
                f"{_pillow_fmt!r} on this system: {_enc_exc}. "
                f"Install Pillow with the required library support or remove "
                f"{_fmt!r} from images.formats."
            )
except ImportError:
    pass  # Defensive: PIL is a required dependency and should always be present
