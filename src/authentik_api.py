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

log = logging.getLogger('ak_api')

# ---------------------------------------------------------------------------
# Module-level configuration
# ---------------------------------------------------------------------------

# Which Authentik user attribute stores the avatar URL (configurable in config)
_avatar_attr = ak_cfg.get('avatar_attribute', 'avatar-url')

# Timeout in seconds for individual API requests (longer for paginated list)
_TIMEOUT = 15
_TIMEOUT_LIST = 30

# Pre-build a requests.Session for TCP connection pooling across API calls.
# Avoids a fresh TCP+TLS handshake for every request to the same Authentik host.
_session = http_requests.Session()
_session.headers.update({
    'Authorization': f'Bearer {ak_cfg["api_token"]}',
    'Content-Type': 'application/json',
    'User-Agent': USER_AGENT,
})

# Pre-compute base URLs used in every call
_base_url = ak_cfg['base_url']
_users_url = f'{_base_url}/api/v3/core/users/'


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------

def _parse_json(resp: http_requests.Response) -> dict:
    """Parse JSON from a response, raising a clear error on failure."""
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f'Authentik API returned non-JSON response (HTTP {resp.status_code}, '
            f'Content-Type: {resp.headers.get("Content-Type", "unknown")}): {exc}'
        ) from exc
    if not isinstance(data, dict):
        raise TypeError(
            f'Authentik API returned unexpected JSON type {type(data).__name__} '
            f'(expected dict) from {resp.request.method} {resp.url}.'
        )
    return data


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_user_pk(username: str) -> int:
    """
    Look up the Authentik integer PK for a given username.

    Called once at login time so the PK can be stored in the session and
    reused for all subsequent API operations without another lookup.
    """
    log.debug('GET %s?username=%s – resolving user PK.', _users_url, username)
    resp = _session.get(_users_url, params={'username': username}, timeout=_TIMEOUT)
    resp.raise_for_status()

    data = _parse_json(resp)
    results = data.get('results')
    if not isinstance(results, list) or not results:
        raise ValueError(f'User {username!r} not found in Authentik (got {len(results) if isinstance(results, list) else 0} result(s)).')

    pk = results[0].get('pk')
    if not isinstance(pk, int):
        raise TypeError(f'Authentik returned non-integer PK {pk!r} for user {username!r}.')

    log.debug('Resolved username %r to pk=%d.', username, pk)
    return pk


def update_avatar_url(pk: int, avatar_url: str) -> dict:
    """
    PATCH the user's avatar attribute in Authentik and return the user's
    full ``attributes`` dict (useful for inspecting ``ldap_uniq``, etc.).

    Accepts the Authentik PK directly so no username->PK lookup is needed.

    The GET to fetch current attributes is always performed (even in dry-run
    mode) so callers can inspect the returned attributes.  Only the PATCH
    is skipped in dry-run mode.
    """
    url = f'{_users_url}{pk}/'

    # -- Fetch current attributes ----------------------------------------------
    # Always performed — callers rely on the returned dict (e.g. to read
    # ldap_uniq before attempting an LDAP update).
    log.debug('GET %s – fetching current user attributes.', url)
    resp = _session.get(url, timeout=_TIMEOUT)
    resp.raise_for_status()

    user_data = _parse_json(resp)
    current_attrs = user_data.get('attributes')
    if not isinstance(current_attrs, dict):
        raise TypeError(
            f'Authentik user pk={pk} has unexpected attributes type '
            f'{type(current_attrs).__name__} (expected dict).'
        )

    old_avatar = current_attrs.get(_avatar_attr, '(not set)')
    log.debug('Current attributes: %s', current_attrs)
    log.debug('Previous %s: %s', _avatar_attr, old_avatar)

    if dry_run:
        log.info('[DRY-RUN] Would update Authentik %r for pk=%d to %s.', _avatar_attr, pk, avatar_url)
        return current_attrs

    # -- PATCH the avatar attribute --------------------------------------------
    current_attrs[_avatar_attr] = avatar_url
    log.debug('PATCH %s – setting %s to: %s', url, _avatar_attr, avatar_url)
    patch_resp = _session.patch(url, json={'attributes': current_attrs}, timeout=_TIMEOUT)
    patch_resp.raise_for_status()

    # Verify the API accepted the change by checking the response body
    patched_data = _parse_json(patch_resp)
    patched_attrs = patched_data.get('attributes', {})
    actual_value = patched_attrs.get(_avatar_attr)
    if actual_value != avatar_url:
        log.warning(
            'Authentik PATCH returned HTTP %d but %s is %r instead of expected %r.',
            patch_resp.status_code, _avatar_attr, actual_value, avatar_url,
        )

    log.info('Authentik %s updated for pk=%d: %s -> %s', _avatar_attr, pk, old_avatar, avatar_url)
    return patched_attrs


def list_all_user_pks() -> set[int]:
    """
    Paginate through Authentik's core users API and return the PK of every
    active user.

    Used by the cleanup job: any avatar metadata whose ``user_pk``
    is not in this set belongs to a deleted/deactivated user.
    """
    pks: set[int] = set()
    page = 0

    # Only request active users — deactivated accounts should have their
    # avatars cleaned up just like deleted ones.
    url: str | None = _users_url
    params: dict | None = {'page_size': 100, 'is_active': 'true'}

    while url:
        page += 1
        log.debug('GET %s (page %d) – fetching user list.', url, page)
        resp = _session.get(url, params=params, timeout=_TIMEOUT_LIST)
        resp.raise_for_status()

        data = _parse_json(resp)
        page_results = data.get('results')
        if not isinstance(page_results, list):
            raise TypeError(
                f'Authentik user list page {page} returned non-list results '
                f'(type={type(page_results).__name__}).'
            )

        for user in page_results:
            pk = user.get('pk')
            if isinstance(pk, int):
                pks.add(pk)

        log.debug('Page %d: received %d user(s), running total %d.', page, len(page_results), len(pks))

        # Authentik embeds the full next-page URL including query params,
        # so we only pass our own params on the first request.
        url = data.get('pagination', {}).get('next')
        params = None

    log.info('Fetched %d active user PK(s) from Authentik (%d page(s)).', len(pks), page)
    return pks
