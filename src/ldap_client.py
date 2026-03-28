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

# ---------------------------------------------------------------------------
# Module-level configuration (config is immutable after startup)
# ---------------------------------------------------------------------------
_enabled = ldap_cfg.get('enabled', False)
_server_host = ldap_cfg.get('server', '')
_server_port = ldap_cfg.get('port', 636)
_use_ssl = ldap_cfg.get('use_ssl', False)
_skip_verify = ldap_cfg.get('skip_cert_verify', False)
_bind_dn = ldap_cfg.get('bind_dn', '')
_bind_password = ldap_cfg.get('bind_password', '')
_search_base = ldap_cfg.get('search_base', '')
_search_filter_tpl = ldap_cfg.get('search_filter', '(objectSid={ldap_uniq})')
_photos = ldap_cfg.get('photos', [])

# Pre-build the ldap3.Server object once so it is reused across connections.
# The Server object holds DNS resolution, schema info, and TLS config — all of
# which are static for the lifetime of the process.
_server: ldap3.Server | None = None
if _enabled:
    _tls_config = None
    if _use_ssl and _skip_verify:
        _tls_config = ldap3.Tls(validate=ssl.CERT_NONE)
    _server = ldap3.Server(
        _server_host, port=_server_port,
        use_ssl=_use_ssl, tls=_tls_config, get_info=ldap3.ALL,
    )
    log.debug('Pre-built LDAP Server object for %s:%s.', _server_host, _server_port)


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def is_enabled() -> bool:
    """Return True if LDAP integration is turned on in the config."""
    return _enabled


def get_photos_config() -> list[dict]:
    """Return the configured ``ldap.photos`` list (may be empty)."""
    return _photos


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _describe_value(val) -> str:
    """Human-readable description of an LDAP attribute value for logging."""
    return f'{len(val)} bytes' if isinstance(val, bytes) else repr(val)


def _connect() -> ldap3.Connection:
    """
    Open and bind an LDAP connection.

    Raises ConnectionError with a descriptive message on failure (network
    unreachable, bad credentials, TLS handshake failure, etc.).
    """
    server_addr = f'{_server_host}:{_server_port}'
    log.debug('Connecting to LDAP server %s (SSL=%s, skip_cert_verify=%s).',
              server_addr, _use_ssl, _skip_verify)
    try:
        conn = ldap3.Connection(
            _server, user=_bind_dn, password=_bind_password, auto_bind=True,
        )
    except ldap3.core.exceptions.LDAPException as exc:
        raise ConnectionError(
            f'Failed to connect/bind to LDAP server {server_addr} as {_bind_dn!r}: {exc}'
        ) from exc
    log.debug('LDAP bind successful as %r.', _bind_dn)
    return conn


def _find_user_dn(conn: ldap3.Connection, ldap_uniq: str) -> str:
    """
    Search for a user by their unique identifier and return the DN.

    Raises ValueError if the user is not found or the DN is empty.
    """
    escaped = ldap3.utils.conv.escape_filter_chars(ldap_uniq)
    search_filter = _search_filter_tpl.replace('{ldap_uniq}', escaped)
    log.debug('Searching base=%r with filter=%s.', _search_base, search_filter)

    found = conn.search(
        search_base=_search_base, search_filter=search_filter,
        attributes=['distinguishedName'],
    )
    if not found or not conn.entries:
        raise ValueError(
            f'LDAP user not found: ldap_uniq={ldap_uniq!r}, '
            f'base={_search_base!r}, filter={search_filter!r}.'
        )

    user_dn = conn.entries[0].entry_dn
    if not user_dn:
        raise ValueError(
            f'LDAP search matched an entry for ldap_uniq={ldap_uniq!r} but the DN is empty.'
        )

    log.info('Found LDAP user: %s (ldap_uniq=%s).', user_dn, ldap_uniq)
    return user_dn


def _apply_modifications(conn: ldap3.Connection, user_dn: str, updates: list[dict]) -> None:
    """
    Apply all attribute changes in a single LDAP MODIFY operation.

    Raises RuntimeError if the server rejects the modification.
    """
    changes = {}
    for update in updates:
        attr = update['attribute']
        val = update['value']
        changes[attr] = [(ldap3.MODIFY_REPLACE, [val])]
        log.debug('Queuing LDAP modify: %s = %s on %r.', attr, _describe_value(val), user_dn)

    log.info('Applying %d attribute change(s) to %r.', len(changes), user_dn)
    conn.modify(user_dn, changes)

    result_code = conn.result.get('result', -1)
    result_desc = conn.result.get('description', 'unknown')
    if result_code != 0:
        raise RuntimeError(
            f'LDAP modify failed on {user_dn!r}: '
            f'code={result_code}, description={result_desc}, '
            f'message={conn.result.get("message", "")!r}.'
        )

    log.debug('LDAP modify succeeded (result=%d, description=%s).', result_code, result_desc)
    for update in updates:
        log.info('LDAP %s updated for %r (%s).', update['attribute'], user_dn, _describe_value(update['value']))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def update_photos(ldap_uniq: str, updates: list[dict]) -> None:
    """
    Connect to the LDAP server and replace photo attributes for the user
    matching the given ``ldap_uniq`` value.

    ``updates`` is a list of dicts, each with:
      - ``attribute``: LDAP attribute name (e.g. ``thumbnailPhoto``)
      - ``value``: ``bytes`` for binary attributes or ``str`` for URL attributes

    All attribute changes are applied in a single LDAP modify operation.

    Raises ConnectionError on bind failure, ValueError if the user is not
    found, and RuntimeError if the LDAP modify is rejected.
    """
    if not _enabled:
        log.info('LDAP integration is disabled – skipping photo updates.')
        return

    if not updates:
        log.debug('No LDAP photo updates to apply.')
        return

    # Validate update entries before making any network calls
    for i, update in enumerate(updates):
        if 'attribute' not in update or 'value' not in update:
            raise ValueError(f'LDAP update[{i}] is missing required key "attribute" or "value": {update!r}')

    if dry_run:
        for update in updates:
            log.info('[DRY-RUN] Would update LDAP %s for ldap_uniq=%s (%s).',
                     update['attribute'], ldap_uniq, _describe_value(update['value']))
        return

    conn = _connect()
    try:
        user_dn = _find_user_dn(conn, ldap_uniq)
        _apply_modifications(conn, user_dn, updates)
    finally:
        conn.unbind()
