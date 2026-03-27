"""
authentik_api.py – Authentik Admin API client.

Updates a configurable attribute on the authenticated user's Authentik account
so that other services reading Authentik can discover the new avatar.
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


def _resolve_user_pk(username: str) -> int:
    """Look up the Authentik integer PK for a given username via the core users search API."""
    log.debug('GET %s?username=%s – resolving user PK.', _users_url, username)
    resp = _session.get(_users_url, params={'username': username}, timeout=15)
    log.debug('User search response: HTTP %d, %d result(s).', resp.status_code, len(resp.json().get('results', [])))
    resp.raise_for_status()
    results = resp.json().get('results', [])
    if not results:
        raise ValueError(f'User {username!r} not found in Authentik.')
    pk = results[0]['pk']
    log.debug('Resolved username %r to pk=%s.', username, pk)
    return pk


def update_avatar_url(username: str, avatar_url: str) -> None:
    """
    Look up the Authentik user by `username`, then PATCH their
    `attributes.<avatar_attribute>` to `avatar_url`.

    Fetches the current attributes first so existing values are preserved.
    """
    if dry_run:
        log.info('[DRY-RUN] Would update Authentik %r for user %r to %s.', _avatar_attr, username, avatar_url)
        return

    pk = _resolve_user_pk(username)

    url = f'{_users_url}{pk}/'

    # Fetch current attributes so we only touch what we need
    log.debug('GET %s – fetching current user attributes.', url)
    resp = _session.get(url, timeout=15)
    log.debug('GET user response: HTTP %d.', resp.status_code)
    resp.raise_for_status()
    current_attrs = resp.json().get('attributes', {})
    old_avatar = current_attrs.get(_avatar_attr, '(not set)')
    log.debug('Current attributes: %s', current_attrs)
    log.debug('Previous %s: %s', _avatar_attr, old_avatar)

    # Merge and PATCH
    current_attrs[_avatar_attr] = avatar_url
    log.debug('PATCH %s – setting %s to: %s', url, _avatar_attr, avatar_url)
    patch_resp = _session.patch(url, json={'attributes': current_attrs}, timeout=15)
    log.debug('PATCH response: HTTP %d.', patch_resp.status_code)
    patch_resp.raise_for_status()
    log.info('Authentik %s updated for user %r (pk=%s): %s -> %s', _avatar_attr, username, pk, old_avatar, avatar_url)
