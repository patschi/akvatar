"""
config.py – Load YAML configuration and set up application-wide logging.

Reads config.yml (or the path given by the CONFIG_PATH env var) once at import time
so every other module can simply `from src.config import cfg`.
"""

import os
import sys
import logging

import yaml

# ---------------------------------------------------------------------------
# Load configuration
# ---------------------------------------------------------------------------
CONFIG_PATH = os.environ.get('CONFIG_PATH', 'data/config/config.yml')

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

_configured_level = app_cfg.get('log_level', 'INFO').upper()
_level = _LOG_LEVELS.get(_configured_level, logging.INFO)

logging.basicConfig(
    level=_level,
    format='%(asctime)s [%(levelname)-8s] %(name)-24s | %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)

from src import APP_NAME, APP_VERSION

log = logging.getLogger('config')
log.info('Starting %s v%s...', APP_NAME, APP_VERSION)
log.debug('Configuration loaded from %r.', CONFIG_PATH)
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
    _ldap_thumb_size = ldap_cfg.get('thumbnail_size', 128)
    if _ldap_thumb_size not in _valid_sizes:
        print(f'FATAL: ldap.thumbnail_size={_ldap_thumb_size} is not in images.sizes={_valid_sizes}.', file=sys.stderr)
        sys.exit(1)
    _ldap_photo_attr = ldap_cfg.get('photo_attribute', 'thumbnailPhoto')
    _ldap_search_filter = ldap_cfg.get('search_filter', '(objectSid={ldap_uniq})')
    log.debug('LDAP will use %dx%d JPG for %s.', _ldap_thumb_size, _ldap_thumb_size, _ldap_photo_attr)
    log.debug(
        'LDAP user identification: will search base %r with filter %r on server %s:%s.',
        ldap_cfg.get('search_base', ''), _ldap_search_filter, ldap_cfg.get('server', ''), ldap_cfg.get('port', 636),
    )
