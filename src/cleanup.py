"""
cleanup.py – Orphan avatar cleanup.

Removes avatar files that belong to users who no longer exist in Authentik.

How user matching works:
  - Each upload writes a .meta.json with the OIDC `preferred_username` claim
    stored in the `username` field.
  - Authentik's core users API returns the same `username` value (since
    Authentik is both the OIDC provider and the API source).
  - A direct string-equality check determines whether the user still exists.

Safety:
  - If the Authentik API returns zero active users (e.g. due to an expired
    token or network error), the cleanup aborts entirely to prevent accidental
    mass deletion.
  - Respects dry_run mode from config.yml — when enabled, only logs what
    would be deleted without touching the filesystem.

This module is used in two ways:
  1. Automatically via a background daemon thread started in app.py.
  2. Manually via `python cleanup.py` at the project root.
"""

import logging
import threading
import time

from src.config import app_cfg, dry_run
from src.imaging import get_all_avatar_metadata, cleanup_avatar_files
from src.authentik_api import list_all_user_pks

log = logging.getLogger('cleanup')

# How often the background thread runs (0 = disabled).
# Read once at import time; config is immutable after startup.
_interval_hours = app_cfg.get('orphan_cleanup_interval_hours', 24)


def run_orphan_cleanup() -> int:
    """
    Compare avatar metadata on disk against active Authentik users and delete
    any avatar sets whose owner no longer exists.

    Matching is done by ``user_pk`` (Authentik's integer primary key), which
    is immutable — unlike usernames, it survives renames and reveals no PII.

    Returns the number of avatar sets removed (or that would be removed in
    dry-run mode).
    """
    log.info('Starting orphan avatar cleanup...')

    # Fetch the full set of active user PKs from Authentik.
    try:
        active_pks = list_all_user_pks()
    except Exception:
        log.exception('Failed to fetch user list from Authentik – aborting cleanup.')
        return 0

    # Guard against an empty response — deleting everything would be
    # catastrophic if the API simply returned no results due to a bug
    # or authentication failure.
    if not active_pks:
        log.warning('Authentik returned zero active users – aborting to prevent accidental mass deletion.')
        return 0

    all_metadata = get_all_avatar_metadata()
    log.info('Found %d avatar set(s) on disk, %d active user(s) in Authentik.', len(all_metadata), len(active_pks))

    removed = 0
    for meta in all_metadata:
        user_pk = meta.get('user_pk')
        filename = meta.get('filename', '')
        if user_pk is None or not filename:
            log.debug('Skipping metadata entry with missing user_pk or filename: %s', filename)
            continue

        if user_pk in active_pks:
            log.debug('user_pk=%s still active – keeping avatar set %s.', user_pk, filename)
            continue

        # User not found among active Authentik users — this avatar set is orphaned.
        if dry_run:
            log.info('[DRY-RUN] Would remove orphaned avatar set %s (user_pk=%s).', filename, user_pk)
        else:
            log.info('Removing orphaned avatar set %s (user_pk=%s no longer active).', filename, user_pk)
            cleanup_avatar_files(filename)
        removed += 1

    if removed:
        log.info('Orphan cleanup complete: %s %d avatar set(s).', 'would remove' if dry_run else 'removed', removed)
    else:
        log.info('Orphan cleanup complete: no orphaned avatars found.')

    return removed


# ---------------------------------------------------------------------------
# Background daemon thread
# ---------------------------------------------------------------------------

def _cleanup_loop(interval_seconds: float) -> None:
    """
    Run orphan cleanup repeatedly at a fixed interval.

    Called as the target of a daemon thread — it exits automatically when the
    main process shuts down.
    """
    # Delay the first run to let the app fully initialise (OIDC discovery,
    # blueprint registration, etc.) before hitting the Authentik API.
    log.debug('Cleanup thread sleeping 60 s before first run...')
    time.sleep(60)

    while True:
        try:
            run_orphan_cleanup()
        except Exception:
            # Log and continue — a transient API failure should not kill the
            # cleanup thread permanently.
            log.exception('Orphan cleanup iteration failed.')

        log.debug('Next orphan cleanup in %d seconds.', interval_seconds)
        time.sleep(interval_seconds)


def start_cleanup_thread() -> None:
    """
    Start the background orphan cleanup thread if the configured interval is > 0.

    Uses a daemon thread so it is automatically terminated when the main
    process exits — no explicit shutdown logic needed.
    """
    if _interval_hours <= 0:
        log.info('Orphan cleanup is disabled (orphan_cleanup_interval_hours = 0).')
        return

    interval_seconds = _interval_hours * 3600
    thread = threading.Thread(
        target=_cleanup_loop,
        args=(interval_seconds,),
        name='orphan-cleanup',
        daemon=True,
    )
    thread.start()
    log.info('Orphan cleanup thread started (interval: every %g hour(s)).', _interval_hours)
