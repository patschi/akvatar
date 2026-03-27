"""
ldap_client.py – LDAP server client.

Writes the JPEG thumbnail into a configurable binary attribute (default:
``thumbnailPhoto``) of the LDAP user object that matches the authenticated
user's unique identifier synced from Authentik.

Designed for any standards-compliant LDAP server.  Microsoft Active Directory
is the primary and only tested target, but the search filter and photo
attribute are fully configurable for other directories.

The entire module is a no-op when `ldap.enabled` is `false` in config.yml.
"""

import ssl
import logging

import ldap3
import ldap3.utils.conv

from src.config import ldap_cfg, dry_run

log = logging.getLogger('ldap_client')

# Pre-compute enabled state and config values at import time (config is immutable after startup)
_enabled = ldap_cfg.get('enabled', False)
_skip_verify = ldap_cfg.get('skip_cert_verify', False)
_max_thumbnail_bytes = ldap_cfg.get('max_thumbnail_kb', 100) * 1024
_search_filter_tpl = ldap_cfg.get('search_filter', '(objectSid={ldap_uniq})')
_photo_attribute = ldap_cfg.get('photo_attribute', 'thumbnailPhoto')

# Pre-build the ldap3.Server object once so it is reused across connections.
# The Server object holds DNS resolution, schema info, and TLS config — all of
# which are static for the lifetime of the process.
_server: ldap3.Server | None = None
if _enabled:
    _tls_config = None
    if ldap_cfg.get('use_ssl', False) and _skip_verify:
        _tls_config = ldap3.Tls(validate=ssl.CERT_NONE)
    _server = ldap3.Server(
        ldap_cfg['server'], port=ldap_cfg['port'],
        use_ssl=ldap_cfg.get('use_ssl', False), tls=_tls_config, get_info=ldap3.ALL,
    )
    log.debug('Pre-built LDAP Server object for %s:%s.', ldap_cfg['server'], ldap_cfg['port'])


def is_enabled() -> bool:
    """Return True if LDAP integration is turned on in the config."""
    return _enabled


def update_thumbnail(ldap_uniq: str, jpeg_bytes: bytes) -> None:
    """
    Connect to the LDAP server and replace the photo attribute for the user
    matching the given ``ldap_uniq`` value.

    ``ldap_uniq`` is the unique identifier stored in Authentik's user
    attributes (e.g. an Active Directory ``objectSid`` like
    ``S-1-5-21-466132232-558606507-1367596332-3607``).  The LDAP search
    filter and photo attribute are configurable in ``config.yml`` so the
    module works with any LDAP directory.

    Raises ValueError if the user is not found or the image exceeds the size limit.
    Raises RuntimeError on LDAP modify failure.
    """
    if not _enabled:
        log.info('LDAP integration is disabled – skipping thumbnail update.')
        return

    if dry_run:
        log.info('[DRY-RUN] Would update LDAP %s for ldap_uniq=%s (%d bytes).', _photo_attribute, ldap_uniq, len(jpeg_bytes))
        return

    log.debug('Connecting to LDAP server %s:%s (SSL=%s, skip_cert_verify=%s).', ldap_cfg['server'], ldap_cfg['port'], ldap_cfg.get('use_ssl', False), _skip_verify)

    conn = ldap3.Connection(_server, user=ldap_cfg['bind_dn'], password=ldap_cfg['bind_password'], auto_bind=True)

    try:
        log.debug('LDAP bind successful as %r.', ldap_cfg['bind_dn'])

        # Build the search filter by substituting the user's unique identifier.
        escaped = ldap3.utils.conv.escape_filter_chars(ldap_uniq)
        search_filter = _search_filter_tpl.replace('{ldap_uniq}', escaped)
        log.debug('Searching %s with filter %s.', ldap_cfg['search_base'], search_filter)

        conn.search(search_base=ldap_cfg['search_base'], search_filter=search_filter, attributes=['distinguishedName'])

        if not conn.entries:
            raise ValueError(f'LDAP user with ldap_uniq={ldap_uniq!r} not found under {ldap_cfg["search_base"]}')

        user_dn = conn.entries[0].entry_dn
        log.info('Found LDAP user DN: %s (ldap_uniq=%s)', user_dn, ldap_uniq)

        # Enforce the thumbnail size limit
        if len(jpeg_bytes) > _max_thumbnail_bytes:
            raise ValueError(f'Thumbnail JPEG is {len(jpeg_bytes)} bytes, exceeding the limit of {_max_thumbnail_bytes} bytes.')
        log.debug('Thumbnail size OK: %d bytes (limit %d).', len(jpeg_bytes), _max_thumbnail_bytes)

        # Write the photo attribute
        log.debug('Replacing %s on %r.', _photo_attribute, user_dn)
        conn.modify(user_dn, {_photo_attribute: [(ldap3.MODIFY_REPLACE, [jpeg_bytes])]})

        if conn.result['result'] != 0:
            raise RuntimeError(f'LDAP modify failed: {conn.result}')

        log.info('LDAP %s updated for ldap_uniq=%s.', _photo_attribute, ldap_uniq)
    finally:
        conn.unbind()
