"""
ldap_client.py – Microsoft Active Directory LDAP client.

Writes the JPEG thumbnail into the `thumbnailPhoto` attribute of the AD user object
that matches the authenticated user's username.

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
    """Return True if LDAP/AD integration is turned on in the config."""
    return _enabled


def update_thumbnail(ldap_uniq: str, jpeg_bytes: bytes) -> None:
    """
    Connect to Active Directory and replace the ``thumbnailPhoto`` attribute
    for the user whose ``objectSid`` matches the given ``ldap_uniq``.

    ``ldap_uniq`` is the SID string stored in Authentik's user attributes
    (e.g. ``S-1-5-21-466132232-558606507-1367596332-3607``).  Using the SID
    instead of ``sAMAccountName`` guarantees a 100 % unique match that
    survives username renames.

    Raises ValueError if the user is not found or the image exceeds the size limit.
    Raises RuntimeError on LDAP modify failure.
    """
    if not _enabled:
        log.info('LDAP integration is disabled – skipping AD thumbnail update.')
        return

    if dry_run:
        log.info('[DRY-RUN] Would update AD thumbnailPhoto for objectSid=%s (%d bytes).', ldap_uniq, len(jpeg_bytes))
        return

    log.debug('Connecting to AD server %s:%s (SSL=%s, skip_cert_verify=%s).', ldap_cfg['server'], ldap_cfg['port'], ldap_cfg.get('use_ssl', False), _skip_verify)

    conn = ldap3.Connection(_server, user=ldap_cfg['bind_dn'], password=ldap_cfg['bind_password'], auto_bind=True)

    try:
        log.debug('LDAP bind successful as %r.', ldap_cfg['bind_dn'])

        # Search for the user by their SID — this is immutable and globally
        # unique within the AD forest, unlike sAMAccountName which can change.
        escaped = ldap3.utils.conv.escape_filter_chars(ldap_uniq)
        search_filter = f'(objectSid={escaped})'
        log.debug('Searching %s with filter %s.', ldap_cfg['search_base'], search_filter)

        conn.search(search_base=ldap_cfg['search_base'], search_filter=search_filter, attributes=['distinguishedName'])

        if not conn.entries:
            raise ValueError(f'AD user with objectSid={ldap_uniq!r} not found under {ldap_cfg["search_base"]}')

        user_dn = conn.entries[0].entry_dn
        log.info('Found AD user DN: %s (objectSid=%s)', user_dn, ldap_uniq)

        # Enforce the AD thumbnail size limit
        if len(jpeg_bytes) > _max_thumbnail_bytes:
            raise ValueError(f'Thumbnail JPEG is {len(jpeg_bytes)} bytes, exceeding the limit of {_max_thumbnail_bytes} bytes.')
        log.debug('Thumbnail size OK: %d bytes (limit %d).', len(jpeg_bytes), _max_thumbnail_bytes)

        # Write the thumbnailPhoto attribute
        log.debug('Replacing thumbnailPhoto on %r.', user_dn)
        conn.modify(user_dn, {'thumbnailPhoto': [(ldap3.MODIFY_REPLACE, [jpeg_bytes])]})

        if conn.result['result'] != 0:
            raise RuntimeError(f'LDAP modify failed: {conn.result}')

        log.info('AD thumbnailPhoto updated for objectSid=%s.', ldap_uniq)
    finally:
        conn.unbind()
