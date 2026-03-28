"""
config.py – Load YAML configuration and set up application-wide logging.

Reads config.yml once at import time so every other module can simply
`from src.config import cfg`.
"""

import sys
import logging

import yaml

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = 'data/config/config.yml'

try:
    with open(CONFIG_PATH, 'r', encoding='utf-8') as _f:
        cfg = yaml.safe_load(_f)
except FileNotFoundError:
    print(f'FATAL: Configuration file not found at {CONFIG_PATH!r}.', file=sys.stderr)
    sys.exit(1)
except yaml.YAMLError as exc:
    print(f'FATAL: Failed to parse {CONFIG_PATH!r}: {exc}', file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Convenience references for each config section
# ---------------------------------------------------------------------------
dry_run      = cfg.get('dry_run', False)
branding_cfg = cfg.get('branding', {})
app_cfg      = cfg['app']
web_cfg      = cfg['webserver']
oidc_cfg     = cfg['oidc']
ak_cfg       = cfg['authentik_api']
ldap_cfg     = cfg.get('ldap', {})  # May be absent if disabled
img_cfg      = cfg['images']
access_log   = bool(web_cfg.get('access_log', False))

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
_LOG_LEVELS = {
    'DEBUG':    logging.DEBUG,
    'INFO':     logging.INFO,
    'WARNING':  logging.WARNING,
    'ERROR':    logging.ERROR,
    'CRITICAL': logging.CRITICAL,
}

debug_full = bool(app_cfg.get('debug_full', False))

_configured_level = app_cfg.get('log_level', 'INFO').upper()
_level = logging.DEBUG if debug_full else _LOG_LEVELS.get(_configured_level, logging.INFO)

logging.basicConfig(
    level=_level,
    format='[%(asctime)s] [%(levelname)-8s] [%(name)-24s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S %z',
)

from src import APP_NAME, APP_VERSION

log = logging.getLogger('config')
log.info('Starting %s v%s...', APP_NAME, APP_VERSION)
log.debug('Configuration loaded from %r.', CONFIG_PATH)
if debug_full:
    log.warning('FULL DEBUG MODE is enabled – Flask debugger, template auto-reload, and verbose logging are active. Disable in production.')
    log.debug('Log level forced to DEBUG by debug_full.')
log.debug('Log level set to %s.', _configured_level)
if dry_run:
    log.warning('DRY-RUN MODE is enabled – no changes will be pushed to Authentik or LDAP.')

# ---------------------------------------------------------------------------
# Startup SSL warnings (logged once at import time)
# ---------------------------------------------------------------------------
_tls_cert = web_cfg.get('tls_cert', '')
_tls_key = web_cfg.get('tls_key', '')
if not _tls_cert or not _tls_key:
    log.warning('Server TLS is NOT configured – the built-in server will run over plain HTTP.')

if ldap_cfg.get('enabled', False) and not ldap_cfg.get('use_ssl', False):
    log.warning('LDAP connection is configured WITHOUT SSL – credentials and data will be sent in plain text.')
if ldap_cfg.get('enabled', False) and ldap_cfg.get('skip_cert_verify', False):
    log.warning('LDAP TLS certificate verification is DISABLED – connections are vulnerable to MITM attacks.')

# ---------------------------------------------------------------------------
# Validate configured image sizes for backends
# ---------------------------------------------------------------------------
_valid_sizes = img_cfg.get('sizes', [])

_ak_avatar_size = ak_cfg.get('avatar_size', 1024)
if _ak_avatar_size not in _valid_sizes:
    print(f'FATAL: authentik_api.avatar_size={_ak_avatar_size} is not in images.sizes={_valid_sizes}.', file=sys.stderr)
    sys.exit(1)
log.debug('Authentik API will use %dx%d JPG for avatar URL.', _ak_avatar_size, _ak_avatar_size)

if ldap_cfg.get('enabled', False):
    _ldap_photos = ldap_cfg.get('photos', [])
    if not _ldap_photos:
        log.warning('LDAP is enabled but no photo attributes are configured (ldap.photos is empty).')

    from src.imaging import _FORMAT_MAP
    _valid_image_types = set(_FORMAT_MAP.keys())
    _valid_photo_types = {'binary', 'url'}
    _valid_formats_lower = [f.lower() for f in img_cfg.get('formats', [])]

    for _i, _photo in enumerate(_ldap_photos):
        _pfx = f'ldap.photos[{_i}]'
        for _key in ('attribute', 'type', 'image_type', 'image_size'):
            if _key not in _photo:
                print(f'FATAL: {_pfx} is missing required key "{_key}".', file=sys.stderr)
                sys.exit(1)
        if _photo['type'] not in _valid_photo_types:
            print(f'FATAL: {_pfx}.type={_photo["type"]!r} must be one of {sorted(_valid_photo_types)}.', file=sys.stderr)
            sys.exit(1)
        if _photo['image_type'] not in _valid_image_types:
            print(f'FATAL: {_pfx}.image_type={_photo["image_type"]!r} must be one of {sorted(_valid_image_types)}.', file=sys.stderr)
            sys.exit(1)
        if _photo['type'] == 'url':
            if _photo['image_size'] not in _valid_sizes:
                print(f'FATAL: {_pfx}.image_size={_photo["image_size"]} is not in images.sizes={_valid_sizes} (required for type=url).', file=sys.stderr)
                sys.exit(1)
            _ext = _FORMAT_MAP[_photo['image_type']][1]
            if _ext not in _valid_formats_lower:
                print(f'FATAL: {_pfx}.image_type={_photo["image_type"]!r} (ext={_ext}) is not in images.formats (required for type=url).', file=sys.stderr)
                sys.exit(1)
        log.debug('LDAP photo[%d]: attribute=%s, type=%s, image_type=%s, size=%d, max_file_size=%d KB.',
                   _i, _photo['attribute'], _photo['type'], _photo['image_type'],
                   _photo['image_size'], _photo.get('max_file_size', 0))

    _ldap_search_filter = ldap_cfg.get('search_filter', '(objectSid={ldap_uniq})')
    log.debug(
        'LDAP user identification: will search base %r with filter %r on server %s:%s.',
        ldap_cfg.get('search_base', ''), _ldap_search_filter, ldap_cfg.get('server', ''), ldap_cfg.get('port', 636),
    )
    log.info('LDAP configured with %d photo attribute(s).', len(_ldap_photos))

# ---------------------------------------------------------------------------
# Validate Flask secret key
# ---------------------------------------------------------------------------
_secret_key = app_cfg.get('secret_key', '')
_SECRET_KEY_MIN_LENGTH = 32

if _secret_key == 'CHANGE-ME-to-a-random-secret-key':
    print(
        'FATAL: app.secret_key is still set to the default placeholder value.\n'
        '       Generate a secure key with: python3 -c "import secrets; print(secrets.token_hex(32))"',
        file=sys.stderr,
    )
    sys.exit(1)

if len(_secret_key) < _SECRET_KEY_MIN_LENGTH:
    print(
        f'FATAL: app.secret_key is too short ({len(_secret_key)} characters, minimum {_SECRET_KEY_MIN_LENGTH}).\n'
        f'       Generate a secure key with: python3 -c "import secrets; print(secrets.token_hex(32))"',
        file=sys.stderr,
    )
    sys.exit(1)

log.debug('Secret key validation passed (length=%d).', len(_secret_key))
