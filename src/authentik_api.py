"""
authentik_api.py – Authentik Admin API client.

Provides helpers to resolve users, update avatar attributes, and enumerate
all active users for cleanup.  All lookups use the Authentik integer PK as
the stable identifier (usernames can be renamed; PKs cannot).
"""

import logging

import requests as http_requests

from src import USER_AGENT
from src.config import ak_cfg, dry_run

log = logging.getLogger('authentik_api')

# Which Authentik user attribute stores the avatar URL (configurable in config)
_avatar_attr = ak_cfg.get('avatar_attribute', 'avatar-url')

# Pre-build a requests.Session for TCP connection pooling across API calls.
# Avoids a fresh TCP+TLS handshake for every request to the same Authentik host.
_session = http_requests.Session()
_session.headers.update({
    'Authorization': f'Bearer {ak_cfg["api_token"]}',
    'Content-Type': 'application/json',
    'User-Agent': USER_AGENT,
})
_session.timeout = 15

# Pre-compute base URLs used in every call
_base_url = ak_cfg['base_url']
_users_url = f'{_base_url}/api/v3/core/users/'


def resolve_user_pk(username: str) -> int:
    """
    Look up the Authentik integer PK for a given username.

    Called once at login time so the PK can be stored in the session and
    reused for all subsequent API operations without another lookup.
    """
    log.debug('GET %s?username=%s – resolving user PK.', _users_url, username)
    resp = _session.get(_users_url, params={'username': username}, timeout=15)
    resp.raise_for_status()
    results = resp.json().get('results', [])
    log.debug('User search response: HTTP %d, %d result(s).', resp.status_code, len(results))
    if not results:
        raise ValueError(f'User {username!r} not found in Authentik.')
    pk = results[0]['pk']
    log.debug('Resolved username %r to pk=%s.', username, pk)
    return pk


def update_avatar_url(pk: int, avatar_url: str) -> dict:
    """
    PATCH the user's avatar attribute in Authentik and return the user's
    full ``attributes`` dict (useful for inspecting ``ldap_uniq``, etc.).

    Accepts the Authentik PK directly so no username→PK lookup is needed.

    The GET to fetch current attributes is always performed (even in dry-run
    mode) so callers can inspect the returned attributes.  Only the PATCH
    is skipped in dry-run mode.
    """
    url = f'{_users_url}{pk}/'

    # Always fetch current attributes — callers rely on the returned dict
    # (e.g. to check for ldap_uniq before attempting an LDAP update).
    log.debug('GET %s – fetching current user attributes.', url)
    resp = _session.get(url, timeout=15)
    log.debug('GET user response: HTTP %d.', resp.status_code)
    resp.raise_for_status()

    user_data = resp.json()
    current_attrs = user_data.get('attributes', {})
    old_avatar = current_attrs.get(_avatar_attr, '(not set)')
    log.debug('Current attributes: %s', current_attrs)
    log.debug('Previous %s: %s', _avatar_attr, old_avatar)

    if dry_run:
        log.info('[DRY-RUN] Would update Authentik %r for pk=%s to %s.', _avatar_attr, pk, avatar_url)
        return current_attrs

    # Merge the new avatar URL into the existing attributes and PATCH
    current_attrs[_avatar_attr] = avatar_url
    log.debug('PATCH %s – setting %s to: %s', url, _avatar_attr, avatar_url)
    patch_resp = _session.patch(url, json={'attributes': current_attrs}, timeout=15)
    log.debug('PATCH response: HTTP %d.', patch_resp.status_code)
    patch_resp.raise_for_status()
    log.info('Authentik %s updated for pk=%s: %s -> %s', _avatar_attr, pk, old_avatar, avatar_url)

    return current_attrs


def list_all_user_pks() -> set[int]:
    """
    Paginate through Authentik's core users API and return the PK of every
    active user.

    Used by the cleanup job: any avatar metadata whose ``user_pk``
    is not in this set belongs to a deleted/deactivated user.
    """
    pks: set[int] = set()
    url = _users_url
    # Only request active users — deactivated accounts should have their
    # avatars cleaned up just like deleted ones.
    params: dict | None = {'page_size': 100, 'is_active': 'true'}
    page = 0

    while url:
        page += 1
        log.debug('GET %s (page %d) – fetching user list.', url, page)
        resp = _session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        page_results = data.get('results', [])
        for user in page_results:
            pk = user.get('pk')
            if pk is not None:
                pks.add(pk)
        log.debug('Page %d: received %d user(s), running total %d.', page, len(page_results), len(pks))

        # Authentik embeds the full next-page URL including query params,
        # so we only pass our own params on the first request.
        url = data.get('pagination', {}).get('next')
        params = None

    log.info('Fetched %d active user PK(s) from Authentik (%d page(s)).', len(pks), page)
    return pks
