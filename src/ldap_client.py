"""
ldap_client.py - LDAP server client.

Writes photo data (binary images or URL strings) into configurable LDAP
attributes for the user object that matches the authenticated user's unique
identifier synced from Authentik.

Supports multiple photo attributes per user, each with independent format,
size, and type (binary or URL) settings via the ``ldap.photos`` config array.

Supports multiple LDAP servers via ``ldap.servers`` (comma-separated URLs).
Servers are tried in the order listed; the next server is used when a
connection or bind attempt fails or times out.

Designed for any standards-compliant LDAP server.  Microsoft Active Directory
is the primary and only tested target, but the search filter and photo
attributes are fully configurable for other directories.

The entire module is a no-op when ``ldap.enabled`` is ``false`` in config.yml.
"""

import logging
import ssl
from urllib.parse import urlparse

import ldap3
import ldap3.utils.conv

from src.config import dry_run, ldap_cfg

log = logging.getLogger("ldap")

# Module-level configuration (config is immutable after startup)
_enabled = ldap_cfg.get("enabled", False)
_server_urls: list[str] = [
    s.strip() for s in ldap_cfg.get("servers", "").split(",") if s.strip()
]
_default_port = ldap_cfg.get("port", 636)
_default_ssl = ldap_cfg.get("use_ssl", False)
_skip_verify = ldap_cfg.get("skip_cert_verify", False)
_bind_dn = ldap_cfg.get("bind_dn", "")
_bind_password = ldap_cfg.get("bind_password", "")
_search_base = ldap_cfg.get("search_base", "")
_search_filter_tpl = ldap_cfg.get("search_filter", "(objectSid={ldap_uniq})")
_photos = ldap_cfg.get("photos", [])

# Pre-build one ldap3.Server object per configured URL so they are reused
# across connections.  Each Server object holds DNS resolution, schema info,
# and TLS config — all of which are static for the lifetime of the process.
#
# Per-URL port and SSL are derived from the URL itself when present:
#   ldaps://host      → SSL=True,  port=_default_port
#   ldap://host:389   → SSL=False, port=389
# Unrecognised or absent scheme falls back to _default_ssl / _default_port.
_servers: list[ldap3.Server] = []
if _enabled:
    for _url in _server_urls:
        _p = urlparse(_url)
        _scheme = _p.scheme.lower()
        _host = _p.hostname or _url
        _port = _p.port if _p.port is not None else _default_port
        if _scheme == "ldaps":
            _ssl = True
        elif _scheme == "ldap":
            _ssl = False
        else:
            _ssl = _default_ssl
        _tls = ldap3.Tls(validate=ssl.CERT_NONE) if (_ssl and _skip_verify) else None
        _servers.append(
            ldap3.Server(_host, port=_port, use_ssl=_ssl, tls=_tls, get_info=ldap3.ALL)
        )
    log.debug(
        "Pre-built %d LDAP Server object(s): %s.",
        len(_servers),
        ", ".join(_server_urls),
    )


# Public helpers


def is_enabled() -> bool:
    """Return True if LDAP integration is turned on in the config."""
    return _enabled


def get_photos_config() -> list[dict]:
    """Return the configured ``ldap.photos`` list (may be empty)."""
    return _photos


# Internal helpers


def _describe_value(val) -> str:
    """Human-readable description of an LDAP attribute value for logging."""
    return f"{len(val)} bytes" if isinstance(val, bytes) else repr(val)


def _connect() -> ldap3.Connection:
    """
    Open and bind an LDAP connection, trying each configured server in order.

    Falls back to the next server when a connection or bind attempt raises an
    LDAPException (network unreachable, TLS handshake failure, timeout, etc.).
    Raises ConnectionError if all servers fail.
    """
    last_exc: Exception | None = None
    for url, server in zip(_server_urls, _servers, strict=True):
        log.debug(
            "Connecting to LDAP server %s (SSL=%s, skip_cert_verify=%s).",
            url,
            server.ssl,
            _skip_verify,
        )
        try:
            conn = ldap3.Connection(
                server,
                user=_bind_dn,
                password=_bind_password,
                auto_bind=True,
            )
            log.debug("LDAP bind successful on %s as %r.", url, _bind_dn)
            return conn
        except ldap3.core.exceptions.LDAPException as exc:
            log.warning("LDAP server %s failed (%s), trying next server.", url, exc)
            last_exc = exc
    raise ConnectionError(
        f"All LDAP servers failed ({', '.join(_server_urls)}). Last error: {last_exc}"
    ) from last_exc


def _find_user_dn(conn: ldap3.Connection, ldap_uniq: str) -> str:
    """
    Search for a user by their unique identifier and return the DN.

    Raises ValueError if the user is not found or the DN is empty.
    """
    # Guard against pathologically long identifiers that could cause excessive
    # LDAP filter strings or log line bloat
    if len(ldap_uniq) > 512:
        raise ValueError(
            f"ldap_uniq is unreasonably long ({len(ldap_uniq)} chars); refusing search."
        )
    escaped = ldap3.utils.conv.escape_filter_chars(ldap_uniq)
    search_filter = _search_filter_tpl.replace("{ldap_uniq}", escaped)
    log.debug("Searching base=%r with filter=%s.", _search_base, search_filter)

    found = conn.search(
        search_base=_search_base,
        search_filter=search_filter,
        attributes=["distinguishedName"],
    )
    if not found or not conn.entries:
        raise ValueError(
            f"LDAP user not found: ldap_uniq={ldap_uniq!r}, "
            f"base={_search_base!r}, filter={search_filter!r}."
        )

    user_dn = conn.entries[0].entry_dn
    if not user_dn:
        raise ValueError(
            f"LDAP search matched an entry for ldap_uniq={ldap_uniq!r} but the DN is empty."
        )

    log.info("Found LDAP user: %s (ldap_uniq=%s).", user_dn, ldap_uniq)
    return user_dn


def _apply_modifications(
    conn: ldap3.Connection, user_dn: str, updates: list[dict]
) -> None:
    """
    Apply all attribute changes in a single LDAP MODIFY operation.

    Raises RuntimeError if the server rejects the modification.
    """
    changes = {}
    for update in updates:
        attr = update["attribute"]
        val = update["value"]
        changes[attr] = [(ldap3.MODIFY_REPLACE, [val])]
        log.debug(
            "Queuing LDAP modify: %s = %s on %r.", attr, _describe_value(val), user_dn
        )

    log.info("Applying %d attribute change(s) to %r.", len(changes), user_dn)
    conn.modify(user_dn, changes)

    result_code = conn.result.get("result", -1)
    result_desc = conn.result.get("description", "unknown")
    if result_code != 0:
        raise RuntimeError(
            f"LDAP modify failed on {user_dn!r}: "
            f"code={result_code}, description={result_desc}, "
            f"message={conn.result.get('message', '')!r}."
        )

    log.debug(
        "LDAP modify succeeded (result=%d, description=%s).", result_code, result_desc
    )
    for update in updates:
        log.info(
            "LDAP %s updated for %r (%s).",
            update["attribute"],
            user_dn,
            _describe_value(update["value"]),
        )


# Public API


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
        log.info("LDAP integration is disabled - skipping photo updates.")
        return

    if not updates:
        log.debug("No LDAP photo updates to apply.")
        return

    # Validate update entries before making any network calls
    for i, update in enumerate(updates):
        if "attribute" not in update or "value" not in update:
            raise ValueError(
                f'LDAP update[{i}] is missing required key "attribute" or "value": {update!r}'
            )

    if dry_run:
        for update in updates:
            log.info(
                "[DRY-RUN] Would update LDAP %s for ldap_uniq=%s (%s).",
                update["attribute"],
                ldap_uniq,
                _describe_value(update["value"]),
            )
        return

    conn = _connect()
    try:
        user_dn = _find_user_dn(conn, ldap_uniq)
        _apply_modifications(conn, user_dn, updates)
    finally:
        conn.unbind()
