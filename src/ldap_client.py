"""
ldap_client.py – LDAP server client.

Writes photo data (binary images or URL strings) into configurable LDAP
attributes for the user object that matches the authenticated user's unique
identifier synced from Authentik.

Supports multiple photo attributes per user, each with independent format,
size, and type (binary or URL) settings via the ``ldap.photos`` config array.

Designed for any standards-compliant LDAP server.  Microsoft Active Directory
is the primary and only tested target, but the search filter and photo
attributes are fully configurable for other directories.

The entire module is a no-op when ``ldap.enabled`` is ``false`` in config.yml.
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
_search_filter_tpl = ldap_cfg.get('search_filter', '(objectSid={ldap_uniq})')
_photos = ldap_cfg.get('photos', [])

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


def get_photos_config() -> list[dict]:
    """Return the configured ``ldap.photos`` list (may be empty)."""
    return _photos


def _describe_value(val) -> str:
    """Human-readable description of an LDAP attribute value for logging."""
    return f'{len(val)} bytes' if isinstance(val, bytes) else repr(val)


def update_photos(ldap_uniq: str, updates: list[dict]) -> None:
    """
    Connect to the LDAP server and replace photo attributes for the user
    matching the given ``ldap_uniq`` value.

    ``updates`` is a list of dicts, each with:
      - ``attribute``: LDAP attribute name (e.g. ``thumbnailPhoto``)
      - ``value``: ``bytes`` for binary attributes or ``str`` for URL attributes

    All attribute changes are applied in a single LDAP modify operation.

    Raises ValueError if the user is not found.
    Raises RuntimeError on LDAP modify failure.
    """
    if not _enabled:
        log.info('LDAP integration is disabled – skipping photo updates.')
        return

    if not updates:
        log.debug('No LDAP photo updates to apply.')
        return

    if dry_run:
        for u in updates:
            val_desc = _describe_value(u['value'])
            log.info('[DRY-RUN] Would update LDAP %s for ldap_uniq=%s (%s).', u['attribute'], ldap_uniq, val_desc)
        return

    log.debug('Connecting to LDAP server %s:%s (SSL=%s, skip_cert_verify=%s).',
              ldap_cfg['server'], ldap_cfg['port'], ldap_cfg.get('use_ssl', False), _skip_verify)

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

        # Build a single modify operation with all attribute changes
        changes = {}
        for u in updates:
            attr = u['attribute']
            val = u['value']
            changes[attr] = [(ldap3.MODIFY_REPLACE, [val])]
            val_desc = _describe_value(val)
            log.debug('Queuing LDAP modify: %s = %s on %r.', attr, val_desc, user_dn)

        log.info('Applying %d attribute change(s) to %r.', len(changes), user_dn)
        conn.modify(user_dn, changes)

        if conn.result['result'] != 0:
            raise RuntimeError(f'LDAP modify failed: {conn.result}')

        for u in updates:
            val = u['value']
            val_desc = _describe_value(val)
            log.info('LDAP %s updated for ldap_uniq=%s (%s).', u['attribute'], ldap_uniq, val_desc)
    finally:
        conn.unbind()
