"""
cleanup.py – Avatar cleanup.

Removes avatar files that belong to users who no longer exist in Authentik
and enforces per-user retention (keeping only the N most recent uploads).

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
  1. Automatically via a background daemon thread started in run.py.
  2. Manually via ``python cleanup.py`` at the project root.
"""

import logging
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from croniter import croniter

from src.config import app_cfg, dry_run
from src.imaging import get_all_avatar_metadata, cleanup_avatar_files
from src.authentik_api import list_all_user_pks

log = logging.getLogger('cleanup')

# Crontab schedule for the cleanup job (empty string = disabled).
# Read once at import time; config is immutable after startup.
_cron_expr = str(app_cfg.get('cleanup_interval', '0 2 * * *')).strip()
_run_on_startup = bool(app_cfg.get('cleanup_on_startup', False))
_retention_count = app_cfg.get('avatar_retention_count', 2)


def _enforce_retention(per_user: dict[int, list[dict]], active_pks: set[int]) -> int:
    """
    For each active user with more than ``_retention_count`` avatar sets,
    delete the oldest uploads beyond the limit.

    Returns the number of avatar sets removed (or that would be removed in
    dry-run mode).
    """
    if _retention_count <= 0:
        log.debug('Retention is disabled (avatar_retention_count=0).')
        return 0

    removed = 0
    for user_pk, entries in per_user.items():
        if user_pk not in active_pks:
            continue  # already handled by stale-user removal
        if len(entries) <= _retention_count:
            continue

        # Sort newest-first by uploaded_at (ISO 8601 sorts lexicographically)
        entries.sort(key=lambda e: e.get('uploaded_at', ''), reverse=True)
        to_delete = entries[_retention_count:]

        for meta in to_delete:
            filename = meta.get('filename', '')
            if dry_run:
                log.info('[DRY-RUN] Would remove old avatar set %s (user_pk=%s).', filename, user_pk)
            else:
                log.info('Retention: removing old avatar set %s (user_pk=%s).', filename, user_pk)
                cleanup_avatar_files(filename)
            removed += 1

    return removed


def run_cleanup() -> int:
    """
    Run both cleanup phases:
      1. Remove avatar sets whose owner no longer exists in Authentik.
      2. Enforce per-user retention for active users.

    Matching is done by ``user_pk`` (Authentik's integer primary key), which
    is immutable — unlike usernames, it survives renames and reveals no PII.

    Returns the total number of avatar sets removed (or that would be removed
    in dry-run mode).
    """
    log.info('Starting avatar cleanup...')

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

    # Group metadata by user_pk for both stale-user removal and retention.
    per_user: dict[int, list[dict]] = defaultdict(list)
    skipped = 0
    for meta in all_metadata:
        user_pk = meta.get('user_pk')
        filename = meta.get('filename', '')
        if user_pk is None or not filename:
            log.debug('Skipping metadata entry with missing user_pk or filename: %s', filename)
            skipped += 1
            continue
        per_user[user_pk].append(meta)

    # Phase 1: remove avatar sets for users that no longer exist.
    removed = 0
    for user_pk, entries in per_user.items():
        if user_pk in active_pks:
            continue
        for meta in entries:
            filename = meta.get('filename', '')
            if dry_run:
                log.info('[DRY-RUN] Would remove avatar set %s (user_pk=%s).', filename, user_pk)
            else:
                log.info('Removing avatar set %s (user_pk=%s no longer active).', filename, user_pk)
                cleanup_avatar_files(filename)
            removed += 1

    # Phase 2: enforce per-user retention for active users.
    retained = _enforce_retention(per_user, active_pks)
    removed += retained

    if removed:
        log.info('Cleanup complete: %s %d avatar set(s).', 'would remove' if dry_run else 'removed', removed)
    else:
        log.info('Cleanup complete: nothing to remove.')

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
    if _run_on_startup:
        log.info('cleanup_on_startup is enabled – running cleanup in 60 s.')
        time.sleep(60)
        try:
            run_cleanup()
        except Exception:
            log.exception('Startup cleanup failed.')

    cron = croniter(_cron_expr, datetime.now(timezone.utc))

    while True:
        next_run = cron.get_next(datetime)
        now = datetime.now(timezone.utc)
        delay = max((next_run - now).total_seconds(), 0)
        log.debug('Next cleanup scheduled at %s (in %.0f s).', next_run.isoformat(), delay)
        time.sleep(delay)

        try:
            run_cleanup()
        except Exception:
            # Log and continue — a transient API failure should not kill the
            # cleanup thread permanently.
            log.exception('Cleanup iteration failed.')


def start_cleanup_thread() -> None:
    """
    Start the background cleanup thread if a cron schedule is configured.

    Uses a daemon thread so it is automatically terminated when the main
    process exits — no explicit shutdown logic needed.
    """
    if not _cron_expr:
        log.info('Cleanup is disabled (cleanup_interval is empty).')
        return

    if not croniter.is_valid(_cron_expr):
        log.error('Invalid cron expression %r for cleanup_interval – cleanup is disabled.', _cron_expr)
        return

    thread = threading.Thread(
        target=_cleanup_loop,
        name='cleanup',
        daemon=True,
    )
    thread.start()
    log.info('Cleanup thread started (schedule: %s).', _cron_expr)
