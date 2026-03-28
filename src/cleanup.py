"""
cleanup.py – Avatar cleanup.

Removes avatar files that belong to users who no longer exist in Authentik,
enforces per-user retention (keeping only the N most recent uploads), and
removes orphaned files left behind by configuration changes or incomplete
uploads.

How user matching works:
  - Each upload writes a .meta.json with the Authentik PK stored in the
    ``user_pk`` field.
  - Authentik's core users API returns the same PK for every active user.
  - A direct integer-equality check determines whether the user still exists.

Orphan cleanup:
  - Size directories that no longer appear in ``images.sizes`` are removed.
  - Image files whose format (extension) is no longer in ``images.formats``
    are deleted.
  - Image files whose filename base has no matching .meta.json are deleted.

Safety:
  - If the Authentik API returns zero active users (e.g. due to an expired
    token or network error), the cleanup aborts entirely to prevent accidental
    mass deletion.
  - Respects dry_run mode from config.yml — when enabled, only logs what
    would be deleted without touching the filesystem.

This module is used in two ways:
  1. Automatically via a background daemon thread started in run_app.py.
  2. Manually via ``python run_cleanup.py`` at the project root.
"""

import logging
import re
import shutil
import threading
import time
from collections import defaultdict
from datetime import datetime, timezone

from croniter import croniter

from src.config import app_cfg, img_cfg, dry_run
from src.imaging import AVATAR_ROOT, METADATA_ROOT, _FORMAT_MAP, get_all_avatar_metadata, cleanup_avatar_files
from src.authentik_api import list_all_user_pks

log = logging.getLogger('cleanup')

# Crontab schedule for the cleanup job (empty string = disabled).
# Read once at import time; config is immutable after startup.
_cron_expr = str(app_cfg.get('cleanup_interval', '0 2 * * *')).strip()
_run_on_startup = bool(app_cfg.get('cleanup_on_startup', False))
_retention_count = app_cfg.get('avatar_retention_count', 2)

# Currently configured sizes and on-disk file extensions (used to detect orphans).
# Formats are resolved through _FORMAT_MAP so that e.g. config "jpeg" matches ".jpg" files.
_configured_sizes = {f'{s}x{s}' for s in img_cfg['sizes']}
_configured_formats = {_FORMAT_MAP[f.lower()][1] for f in img_cfg['formats']}

# Regex to match size directory names like "128x128", "1024x1024"
_SIZE_DIR_RE = re.compile(r'^\d+x\d+$')


def _enforce_retention(per_user: dict[int, list[dict]], active_pks: set[int]) -> tuple[int, set[str]]:
    """
    For each active user with more than ``_retention_count`` avatar sets,
    delete the oldest uploads beyond the limit.

    Returns a tuple of (count removed, set of deleted filename bases).
    """
    if _retention_count <= 0:
        log.debug('Retention is disabled (avatar_retention_count=0).')
        return 0, set()

    removed = 0
    deleted: set[str] = set()
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
            deleted.add(filename)
            removed += 1

    return removed, deleted


def _cleanup_orphaned_files(known_filenames: set[str]) -> int:
    """
    Remove orphaned image files from the avatar storage directory.

    Targets three types of orphans:
      1. Entire size directories that are no longer in ``images.sizes``.
      2. Image files whose extension is no longer in ``images.formats``.
      3. Image files whose filename base has no matching .meta.json.

    ``known_filenames`` is the set of filename bases from all metadata files
    that are still on disk (i.e. not already deleted by earlier phases).

    Returns the number of files removed (or that would be removed in dry-run).
    """
    removed = 0

    # Scan all subdirectories under the avatar root
    for entry in AVATAR_ROOT.iterdir():
        # Skip non-directories and the metadata directory
        if not entry.is_dir() or entry.name == '_metadata':
            continue

        # Phase A: remove entire directories for sizes no longer configured
        if _SIZE_DIR_RE.match(entry.name) and entry.name not in _configured_sizes:
            if dry_run:
                file_count = sum(1 for f in entry.iterdir() if f.is_file())
                log.info('[DRY-RUN] Would remove obsolete size directory %s/ (%d file(s)).', entry.name, file_count)
                removed += file_count
            else:
                try:
                    shutil.rmtree(entry)
                    log.info('Removed obsolete size directory %s/.', entry.name)
                except OSError as exc:
                    log.warning('Failed to remove obsolete size directory %s/: %s', entry.name, exc)
            continue

        # Phase B+C: scan files inside configured size directories
        if entry.name not in _configured_sizes:
            continue

        for file_path in entry.iterdir():
            if not file_path.is_file():
                continue

            ext = file_path.suffix.lstrip('.').lower()

            # Phase B: remove files with formats no longer configured
            if ext not in _configured_formats:
                if dry_run:
                    log.info('[DRY-RUN] Would remove obsolete format file %s/%s.', entry.name, file_path.name)
                else:
                    try:
                        file_path.unlink()
                        log.info('Removed obsolete format file %s/%s.', entry.name, file_path.name)
                    except OSError as exc:
                        log.warning('Failed to remove obsolete format file %s/%s: %s', entry.name, file_path.name, exc)
                removed += 1
                continue

            # Phase C: remove files with no matching metadata (orphaned)
            if file_path.stem not in known_filenames:
                if dry_run:
                    log.info('[DRY-RUN] Would remove orphaned file %s/%s (no metadata).', entry.name, file_path.name)
                else:
                    try:
                        file_path.unlink()
                        log.info('Removed orphaned file %s/%s (no metadata).', entry.name, file_path.name)
                    except OSError as exc:
                        log.warning('Failed to remove orphaned file %s/%s: %s', entry.name, file_path.name, exc)
                removed += 1

    # Phase D: remove orphaned metadata files with no matching images
    for meta_path in METADATA_ROOT.glob('*.meta.json'):
        filename_base = meta_path.name.removesuffix('.meta.json')
        if filename_base not in known_filenames:
            if dry_run:
                log.info('[DRY-RUN] Would remove orphaned metadata %s.', meta_path.name)
            else:
                try:
                    meta_path.unlink()
                    log.info('Removed orphaned metadata %s.', meta_path.name)
                except OSError as exc:
                    log.warning('Failed to remove orphaned metadata %s: %s', meta_path.name, exc)
            removed += 1

    return removed


def run_cleanup() -> int:
    """
    Run all cleanup phases:
      1. Remove avatar sets whose owner no longer exists in Authentik.
      2. Enforce per-user retention for active users.
      3. Remove orphaned files (obsolete sizes/formats, images without metadata).

    Matching is done by ``user_pk`` (Authentik's integer primary key), which
    is immutable — unlike usernames, it survives renames and reveals no PII.

    Returns the total number of files removed (or that would be removed
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

    # Collect all known filename bases from metadata
    all_filenames = {meta.get('filename', '') for entries in per_user.values() for meta in entries}
    all_filenames.discard('')
    deleted_filenames: set[str] = set()

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
            deleted_filenames.add(filename)
            removed += 1

    # Phase 2: enforce per-user retention for active users.
    retained, retention_deleted = _enforce_retention(per_user, active_pks)
    removed += retained
    deleted_filenames |= retention_deleted

    # Filenames that should still exist on disk after phases 1 and 2
    surviving_filenames = all_filenames - deleted_filenames

    # Phase 3: remove orphaned files (obsolete sizes, formats, no metadata).
    orphans = _cleanup_orphaned_files(surviving_filenames)
    removed += orphans

    if removed:
        log.info('Cleanup complete: %s %d file(s).', 'would remove' if dry_run else 'removed', removed)
    else:
        log.info('Cleanup complete: nothing to remove.')

    return removed


# Background daemon thread
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
        log.debug('Next cleanup scheduled at %s (in %.0f seconds).', next_run.isoformat(), delay)
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
