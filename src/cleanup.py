"""
cleanup.py – Orphan avatar cleanup.

Removes avatar files that belong to users who no longer exist in Authentik.

How user matching works:
  - Each upload writes a .meta.json with the Authentik PK stored in the
    ``user_pk`` field.
  - Authentik's core users API returns the same PK for every active user.
  - A direct integer-equality check determines whether the user still exists.

Safety:
  - If the Authentik API returns zero active users (e.g. due to an expired
    token or network error), the cleanup aborts entirely to prevent accidental
    mass deletion.
  - Respects dry_run mode from config.yml — when enabled, only logs what
    would be deleted without touching the filesystem.

This module is used in two ways:
  1. Automatically via a background daemon thread started in app.py.
  2. Manually via ``python cleanup.py`` at the project root.
"""

import logging
import threading
import time
from datetime import datetime, timezone

from croniter import croniter

from src.config import app_cfg, dry_run
from src.imaging import get_all_avatar_metadata, cleanup_avatar_files
from src.authentik_api import list_all_user_pks

log = logging.getLogger('cleanup')

# Crontab schedule for the cleanup job (empty string = disabled).
# Read once at import time; config is immutable after startup.
_cron_expr = str(app_cfg.get('cleanup_interval', '0 2 * * *')).strip()


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

def _cleanup_loop() -> None:
    """
    Sleep until the next cron-scheduled time, run cleanup, repeat.

    Called as the target of a daemon thread — it exits automatically when the
    main process shuts down.
    """
    cron = croniter(_cron_expr, datetime.now(timezone.utc))

    while True:
        next_run = cron.get_next(datetime)
        now = datetime.now(timezone.utc)
        delay = max((next_run - now).total_seconds(), 0)
        log.debug('Next cleanup scheduled at %s (in %.0f s).', next_run.isoformat(), delay)
        time.sleep(delay)

        try:
            run_orphan_cleanup()
        except Exception:
            # Log and continue — a transient API failure should not kill the
            # cleanup thread permanently.
            log.exception('Orphan cleanup iteration failed.')


def start_cleanup_thread() -> None:
    """
    Start the background orphan cleanup thread if a cron schedule is configured.

    Uses a daemon thread so it is automatically terminated when the main
    process exits — no explicit shutdown logic needed.
    """
    if not _cron_expr:
        log.info('Orphan cleanup is disabled (cleanup_interval is empty).')
        return

    if not croniter.is_valid(_cron_expr):
        log.error('Invalid cron expression %r for cleanup_interval – cleanup is disabled.', _cron_expr)
        return

    thread = threading.Thread(
        target=_cleanup_loop,
        name='orphan-cleanup',
        daemon=True,
    )
    thread.start()
    log.info('Orphan cleanup thread started (schedule: %s).', _cron_expr)
