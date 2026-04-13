"""
authentik.py - Authentik Admin API client.

Provides helpers to resolve users, update avatar attributes, and enumerate
all active users for cleanup.  All lookups use the Authentik integer PK as
the stable identifier (usernames can be renamed; PKs cannot).
"""

import logging
import time

import requests as http_requests
import urllib3

from src import USER_AGENT
from src.config import (
    EXTERNAL_REQUEST_TIMEOUT,
    ak_api_token,
    ak_avatar_attribute,
    ak_base_url,
    ak_skip_cert_verify,
    dry_run,
)

log = logging.getLogger("authentik")

# Module-level configuration

# Timeout in seconds for individual API requests, derived from the central
# EXTERNAL_REQUEST_TIMEOUT.  Paginated list calls use a longer timeout because
# Authentik may need extra time to serialize large result sets.
_TIMEOUT = EXTERNAL_REQUEST_TIMEOUT
_TIMEOUT_LIST = EXTERNAL_REQUEST_TIMEOUT * 2

# Pre-build a requests.Session for TCP connection pooling across API calls.
# Avoids a fresh TCP+TLS handshake for every request to the same Authentik host.
_session = http_requests.Session()
_session.headers.update(
    {
        "Authorization": f"Bearer {ak_api_token}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
    }
)

if ak_skip_cert_verify:
    # Disable TLS certificate verification for all requests on this session.
    # Also suppress urllib3's per-request InsecureRequestWarning - the startup
    # warning in config.py already informs the operator that verification is off.
    _session.verify = False
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Pre-compute base URLs used in every call.
_users_url = f"{ak_base_url}/api/v3/core/users/"
log.debug("Authentik users API URL: %s", _users_url)

# Retry configuration for transient network-layer failures (connection refused,
# timeout).  Only these error classes are retried - an HTTP error response
# (4xx/5xx) means the server processed the request and should not be retried.
_RETRY_MAX = 3
_RETRY_DELAYS = (1.0, 2.0)  # seconds before attempt 2 and attempt 3


def _retry_request(fn):
    """
    Call fn() up to _RETRY_MAX times, retrying only on transient network errors.

    Retries on ConnectionError and Timeout - these indicate the request did not
    reach the server (or the response was lost), so repeating is safe.
    HTTPError (raised after raise_for_status()) is NOT retried because the
    server already processed the request and returned a deliberate error.
    """
    last_exc = None
    for attempt in range(_RETRY_MAX):
        try:
            return fn()
        except (
            http_requests.exceptions.ConnectionError,
            http_requests.exceptions.Timeout,
        ) as exc:
            last_exc = exc
            if attempt < _RETRY_MAX - 1:
                delay = _RETRY_DELAYS[min(attempt, len(_RETRY_DELAYS) - 1)]
                log.warning(
                    "Authentik API transient error (attempt %d/%d, retrying in %.1fs): %s",
                    attempt + 1,
                    _RETRY_MAX,
                    delay,
                    exc,
                )
                time.sleep(delay)
    raise last_exc


# Response helpers


def _parse_json(resp: http_requests.Response) -> dict:
    """Parse JSON from a response, raising a clear error on failure."""
    try:
        data = resp.json()
    except (ValueError, TypeError) as exc:
        raise ValueError(
            f"Authentik API returned non-JSON response (HTTP {resp.status_code}, "
            f"Content-Type: {resp.headers.get('Content-Type', 'unknown')}): {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise TypeError(
            f"Authentik API returned unexpected JSON type {type(data).__name__} "
            f"(expected dict) from {resp.request.method} {resp.url}."
        )
    return data


def _patch_user(pk: int, data: dict) -> tuple[dict, dict]:
    """
    Fetch the current user object, merge *data* into it, and PATCH it back.

    Authentik replaces top-level fields wholesale on PATCH, so a naive
    ``{"attributes": {"avatar": "x"}}`` would wipe every other attribute.
    This helper GETs the current state first and merges *data* into it:

    - Top-level keys in *data* overwrite the corresponding keys in the
      current user object.
    - If both the current value and the incoming value for a key are dicts
      (e.g. ``attributes``), their contents are shallow-merged so that
      only the specific sub-keys in *data* are updated.

    Returns ``(pre_patch, post_patch)`` - the user data before and after the
    update.  In dry-run mode *post_patch* equals *pre_patch* (no PATCH is
    sent).  Respects dry-run mode.
    """
    url = f"{_users_url}{pk}/"

    # Fetch current user data (always performed - callers may rely on the
    # returned pre-patch dict even in dry-run mode).
    log.debug("GET %s - fetching current user data for patch.", url)
    get_resp = _retry_request(lambda: _session.get(url, timeout=_TIMEOUT))
    get_resp.raise_for_status()
    current = _parse_json(get_resp)

    # Merge incoming data into current user object
    payload = {}
    for key, value in data.items():
        current_value = current.get(key)
        # Shallow-merge dicts (e.g. attributes) to preserve sibling keys
        if isinstance(current_value, dict) and isinstance(value, dict):
            merged = {**current_value, **value}
            payload[key] = merged
        else:
            payload[key] = value

    log.debug("PATCH %s - merged payload: %s", url, payload)

    if dry_run:
        log.info("[DRY-RUN] Would PATCH %s with: %s", url, payload)
        return current, current

    patch_resp = _retry_request(
        lambda: _session.patch(url, json=payload, timeout=_TIMEOUT)
    )
    patch_resp.raise_for_status()

    result = _parse_json(patch_resp)
    log.debug(
        "PATCH %s - response (HTTP %d): %s",
        url,
        patch_resp.status_code,
        result.get("attributes", {}),
    )
    return current, result


# Public API


def retrieve_user(username: str) -> dict:
    """
    Retrieve the Authentik user for a given username.

    Called once at login time.  Returns a dict with ``pk`` (integer primary
    key) and ``avatar`` (current avatar URL from Authentik, or empty string).
    The PK is stored in the session for all subsequent API operations; the
    avatar is used to display the current profile picture on the dashboard.
    """
    log.debug("GET %s?username=%s - retrieving user.", _users_url, username)
    resp = _retry_request(
        lambda: _session.get(
            _users_url, params={"username": username}, timeout=_TIMEOUT
        )
    )
    resp.raise_for_status()

    data = _parse_json(resp)
    results = data.get("results")
    if not isinstance(results, list) or not results:
        raise ValueError(
            f"User {username!r} not found in Authentik (got {len(results) if isinstance(results, list) else 0} result(s))."
        )

    if len(results) > 1:
        # Authentik enforces unique usernames for its own users, but a custom
        # username_claim (e.g. email or a non-unique attribute) can produce
        # multiple matches.  Log a warning and continue with the first result.
        log.warning(
            "Authentik returned %d users for username %r - using the first result. "
            "Check oidc.username_claim if this is unexpected.",
            len(results),
            username,
        )

    user = results[0]

    pk = user.get("pk")
    if not isinstance(pk, int):
        raise TypeError(
            f"Authentik returned non-integer PK {pk!r} for user {username!r}."
        )

    # Read the custom avatar URL from the configured attribute (e.g. 'avatar')
    # rather than the top-level 'avatar' field, which is Authentik's computed avatar
    # (Gravatar, initials, etc.) and doesn't distinguish custom from default.
    attrs = user.get("attributes", {})
    avatar = attrs.get(ak_avatar_attribute, "") if isinstance(attrs, dict) else ""

    log.debug(
        "Retrieved user %r: pk=%d, %s=%s.",
        username,
        pk,
        ak_avatar_attribute,
        avatar or "(not set)",
    )
    return {"pk": pk, "avatar": avatar}


def update_avatar_url(pk: int, avatar_url: str) -> tuple[dict, str | None]:
    """
    PATCH the user's avatar attribute in Authentik and return the user's
    full ``attributes`` dict together with the previous avatar URL.

    Returns ``(attrs, old_url)`` where *old_url* is the attribute value
    before this call (``None`` if it was not set).  The caller can use
    *old_url* to revert the change via :func:`revert_avatar_url` if a
    later pipeline step fails.

    Accepts the Authentik PK directly so no username->PK lookup is needed.

    The GET is always performed (even in dry-run mode) so callers can
    inspect the returned attributes.  Only the PATCH is skipped in
    dry-run mode.
    """
    # Fetch current user, merge the avatar attribute, and PATCH back
    pre_patch, post_patch = _patch_user(
        pk, {"attributes": {ak_avatar_attribute: avatar_url}}
    )

    current_attrs = pre_patch.get("attributes")
    if not isinstance(current_attrs, dict):
        raise TypeError(
            f"Authentik user pk={pk} has unexpected attributes type "
            f"{type(current_attrs).__name__} (expected dict)."
        )

    old_url = current_attrs.get(ak_avatar_attribute)
    log.debug("Previous %s: %s", ak_avatar_attribute, old_url or "(not set)")

    # Verify the API accepted the change by checking the response body
    # (skipped in dry-run mode where post_patch == pre_patch)
    if not dry_run:
        patched_attrs = post_patch.get("attributes", {})
        actual_value = patched_attrs.get(ak_avatar_attribute)
        if actual_value != avatar_url:
            log.warning(
                "Authentik PATCH returned %s=%r instead of expected %r.",
                ak_avatar_attribute,
                actual_value,
                avatar_url,
            )
        log.info(
            "Authentik %s updated for pk=%d: %s -> %s",
            ak_avatar_attribute,
            pk,
            old_url or "(not set)",
            avatar_url,
        )

    result_attrs = post_patch.get("attributes", current_attrs)
    return result_attrs, old_url


def remove_avatar_url(pk: int) -> None:
    """
    Reset the user's custom avatar attribute to null in Authentik.

    Sets the avatar attribute to null (rather than removing the key entirely)
    so Authentik falls back to its default avatar (e.g. Gravatar or initials).
    Respects dry-run mode.
    """
    # Partial update: set the avatar attribute to null
    _patch_user(pk, {"attributes": {ak_avatar_attribute: None}})

    if not dry_run:
        log.info("Authentik %s set to null for pk=%d.", ak_avatar_attribute, pk)


def revert_avatar_url(pk: int, old_url: str | None) -> None:
    """
    Restore the user's avatar attribute to *old_url* (or set it to null if
    *old_url* is ``None``).  Used during rollback when a later pipeline
    step fails after Authentik was already updated.
    """
    # Partial update: set the avatar attribute to the old value (or null)
    display = old_url or "(null)"
    log.info(
        "Rolling back Authentik %s for pk=%d to: %s", ak_avatar_attribute, pk, display
    )
    _patch_user(pk, {"attributes": {ak_avatar_attribute: old_url}})
    log.info("Authentik rollback successful for pk=%d.", pk)


def _list_user_pks(active_only: bool = False) -> set[int]:
    """
    Paginate through Authentik's core users API and return the collected PKs.

    ``active_only=True`` adds ``is_active=true`` to the request, returning
    only non-deactivated users.  The default returns every user regardless
    of active status.
    """
    pks: set[int] = set()
    page = 0

    url: str | None = _users_url
    params: dict | None = {"page_size": 100}
    if active_only:
        params["is_active"] = "true"

    # Paginate through all result pages, collecting PKs from each page
    while url:
        page += 1
        log.debug("GET %s (page %d) - fetching user list.", url, page)
        resp = _retry_request(
            lambda u=url, p=params: _session.get(u, params=p, timeout=_TIMEOUT_LIST)
        )
        resp.raise_for_status()

        data = _parse_json(resp)
        page_results = data.get("results")
        if not isinstance(page_results, list):
            raise TypeError(
                f"Authentik user list page {page} returned non-list results "
                f"(type={type(page_results).__name__})."
            )

        for user in page_results:
            pk = user.get("pk")
            if isinstance(pk, int):
                pks.add(pk)

        log.debug(
            "Page %d: received %d user(s), running total %d.",
            page,
            len(page_results),
            len(pks),
        )

        # Authentik embeds the full next-page URL including query params,
        # so we only pass our own params on the first request.
        url = data.get("pagination", {}).get("next")
        params = None

    return pks


def list_all_user_pks() -> set[int]:
    """
    Return the PK of every user in Authentik (both active and deactivated).

    Used by the cleanup job to detect truly deleted users: a metadata entry
    whose ``user_pk`` is not in this set belongs to a user that no longer
    exists in Authentik at all.
    """
    pks = _list_user_pks(active_only=False)
    log.debug("Fetched %d user PK(s) from Authentik (active + deactivated).", len(pks))
    return pks


def list_active_user_pks() -> set[int]:
    """
    Return the PK of every *active* user in Authentik.

    Used by the cleanup job when ``cleanup.when_user_deactivated`` is enabled,
    to identify deactivated users (present in ``list_all_user_pks`` but absent
    here) alongside deleted users (absent from ``list_all_user_pks`` entirely).
    When both deleted and deactivated cleanup are enabled, only this function
    is called - anything not in the active set is cleaned up.
    """
    pks = _list_user_pks(active_only=True)
    log.debug("Fetched %d active user PK(s) from Authentik.", len(pks))
    return pks
