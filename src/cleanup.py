"""
cleanup.py - Avatar cleanup.

Removes avatar files that belong to users who no longer exist in Authentik
(and optionally users who are deactivated), enforces per-user retention
(keeping only the N most recent uploads), and removes orphaned files left
behind by configuration changes or incomplete uploads.

How user matching works:
  - Each upload writes a .meta.json with the Authentik PK stored in the
    ``user_pk`` field.
  - Authentik's core users API returns the same PK for every user.
  - A direct integer-equality check determines whether the user still exists
    and whether they are active.

Deletion behaviour (Phase 1) is controlled by two config flags:
  - ``cleanup.when_user_deleted`` (default true): remove avatar sets for users
    that no longer exist in Authentik at all.
  - ``cleanup.when_user_deactivated`` (default false): also remove avatar sets
    for users that exist but are marked as inactive in Authentik.

Orphan cleanup:
  - Size directories that no longer appear in ``images.sizes`` are removed.
  - Image files whose format (extension) is no longer in ``images.formats``
    are deleted.
  - Image files whose filename base has no matching .meta.json are deleted.

Safety:
  - If the Authentik API returns zero users (e.g. due to an expired token or
    network error), the cleanup aborts entirely to prevent accidental mass
    deletion.
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
from datetime import UTC, datetime

from croniter import croniter

from src.authentik import list_active_user_pks, list_all_user_pks
from src.config import cleanup_cfg, dry_run, img_cfg
from src.imaging import (
    AVATAR_ROOT,
    FORMAT_MAP,
    METADATA_ROOT,
    cleanup_avatar_files,
    get_all_avatar_metadata,
)

log = logging.getLogger("cleanup")

# Crontab schedule for the cleanup job (empty string = disabled).
# Read once at import time; config is immutable after startup.
_cron_expr = str(cleanup_cfg.get("interval", "0 2 * * *")).strip()
_run_on_startup = bool(cleanup_cfg.get("on_startup", False))
_retention_count = cleanup_cfg.get("avatar_retention_count", 2)
_cleanup_when_deleted = bool(cleanup_cfg.get("when_user_deleted", True))
_cleanup_when_deactivated = bool(cleanup_cfg.get("when_user_deactivated", False))

# Currently configured sizes and on-disk file extensions (used to detect orphans).
# Formats are resolved through FORMAT_MAP so that e.g. config "jpeg" matches ".jpg" files.
_configured_sizes = {f"{s}x{s}" for s in img_cfg["sizes"]}
_configured_formats = {FORMAT_MAP[f.lower()][1] for f in img_cfg["formats"]}

# Regex to match size directory names like "128x128", "1024x1024"
_SIZE_DIR_RE = re.compile(r"^\d+x\d+$")


def _try_unlink(path, label: str) -> tuple[int, int]:
    """
    Delete a single file, respecting dry_run mode.

    Returns (deleted, failed) counts.  ``label`` is used for log messages
    (e.g. "obsolete format file 128x128/abc.png").
    """
    if dry_run:
        log.info("[DRY-RUN] Would remove %s.", label)
        return 1, 0
    try:
        path.unlink()
        log.info("Removed %s.", label)
        return 1, 0
    except OSError as exc:
        log.warning("Failed to remove %s: %s", label, exc)
        return 0, 1


# Files per avatar set: one image per size x format combination, plus one metadata file.
# Used in dry-run to estimate how many files would be removed for each targeted set.
_FILES_PER_SET = len(img_cfg["sizes"]) * len(img_cfg["formats"]) + 1


def _enforce_retention(
    per_user: dict[int, list[dict]], skip_pks: set[int]
) -> tuple[int, int, set[str]]:
    """
    For each user not in ``skip_pks`` that has more than ``_retention_count``
    avatar sets, delete the oldest uploads beyond the limit.

    ``skip_pks`` contains user PKs already handled by Phase 1 (whose avatars
    are being removed entirely); retention is not applied to those users.

    Returns (file_deleted, file_failed, deleted_set_names).
    """
    if _retention_count <= 0:
        log.debug("Retention is disabled (cleanup.avatar_retention_count=0).")
        return 0, 0, set()

    file_deleted = 0
    file_failed = 0
    deleted_sets: set[str] = set()
    for user_pk, entries in per_user.items():
        if user_pk in skip_pks:
            continue  # Phase 1 already handles removal for this user
        if len(entries) <= _retention_count:
            continue

        # Sort newest-first by uploaded_at (ISO 8601 sorts lexicographically)
        entries.sort(key=lambda e: e.get("uploaded_at", ""), reverse=True)
        to_delete = entries[_retention_count:]

        for meta in to_delete:
            filename = meta.get("filename", "")
            if dry_run:
                log.info(
                    "[DRY-RUN] Would remove old avatar set %s (user_pk=%s).",
                    filename,
                    user_pk,
                )
            else:
                log.info(
                    "Retention: removing old avatar set %s (user_pk=%s).",
                    filename,
                    user_pk,
                )
                d, f = cleanup_avatar_files(filename)
                file_deleted += d
                file_failed += f
            deleted_sets.add(filename)

    return file_deleted, file_failed, deleted_sets


def _cleanup_orphaned_files(known_filenames: set[str]) -> tuple[int, int, int]:
    """
    Remove orphaned image files from the avatar storage directory.

    Targets three types of orphans:
      1. Entire size directories that are no longer in ``images.sizes``.
      2. Image files whose extension is no longer in ``images.formats``.
      3. Image files whose filename base has no matching .meta.json.

    ``known_filenames`` is the set of filename bases from all metadata files
    that are still on disk (i.e. not already deleted by earlier phases).

    Returns (expected, deleted, failed): files targeted, successfully removed,
    and files that could not be removed due to an OSError.
    """
    expected = 0
    deleted = 0
    failed = 0

    # Scan all subdirectories under the avatar root
    for entry in AVATAR_ROOT.iterdir():
        # Skip non-directories and the metadata directory
        if not entry.is_dir() or entry.name == "_metadata":
            continue

        # Phase A: remove entire directories for sizes no longer configured
        if _SIZE_DIR_RE.match(entry.name) and entry.name not in _configured_sizes:
            file_count = sum(1 for f in entry.iterdir() if f.is_file())
            expected += file_count
            if dry_run:
                log.info(
                    "[DRY-RUN] Would remove obsolete size directory %s/ (%d file(s)).",
                    entry.name,
                    file_count,
                )
                deleted += file_count
            else:
                try:
                    shutil.rmtree(entry)
                    log.info(
                        "Removed obsolete size directory %s/ (%d file(s)).",
                        entry.name,
                        file_count,
                    )
                    deleted += file_count
                except OSError as exc:
                    log.warning(
                        "Failed to remove obsolete size directory %s/: %s",
                        entry.name,
                        exc,
                    )
                    failed += file_count
            continue

        # Phase B+C: scan files inside configured size directories
        if entry.name not in _configured_sizes:
            continue

        for file_path in entry.iterdir():
            if not file_path.is_file():
                continue

            ext = file_path.suffix.lstrip(".").lower()

            # Phase B: remove files with formats no longer configured
            if ext not in _configured_formats:
                expected += 1
                d, f = _try_unlink(
                    file_path, f"obsolete format file {entry.name}/{file_path.name}"
                )
                deleted += d
                failed += f
                continue

            # Phase C: remove files with no matching metadata (orphaned)
            if file_path.stem not in known_filenames:
                expected += 1
                d, f = _try_unlink(
                    file_path,
                    f"orphaned file {entry.name}/{file_path.name} (no metadata)",
                )
                deleted += d
                failed += f

    # Phase D: remove orphaned metadata files with no matching images
    for meta_path in METADATA_ROOT.glob("*.meta.json"):
        filename_base = meta_path.name.removesuffix(".meta.json")
        if filename_base not in known_filenames:
            expected += 1
            d, f = _try_unlink(meta_path, f"orphaned metadata {meta_path.name}")
            deleted += d
            failed += f

    return expected, deleted, failed


def run_cleanup() -> int:
    """
    Run all cleanup phases:
      1. Remove avatar sets for deleted users (and optionally deactivated users).
      2. Enforce per-user retention for remaining users.
      3. Remove orphaned files (obsolete sizes/formats, images without metadata).

    Matching is done by ``user_pk`` (Authentik's integer primary key), which
    is immutable — unlike usernames, it survives renames and reveals no PII.

    Returns the total number of files removed (or that would be removed
    in dry-run mode).
    """
    log.info("Starting avatar cleanup...")

    all_metadata = get_all_avatar_metadata()

    # Group metadata by user_pk for both Phase 1 and retention.
    per_user: dict[int, list[dict]] = defaultdict(list)
    skipped = 0
    for meta in all_metadata:
        user_pk = meta.get("user_pk")
        filename = meta.get("filename", "")
        if user_pk is None or not filename:
            log.debug(
                "Skipping metadata entry with missing user_pk or filename: %s", filename
            )
            skipped += 1
            continue
        per_user[user_pk].append(meta)

    # Collect all known filename bases from metadata
    all_filenames = {
        meta.get("filename", "") for entries in per_user.values() for meta in entries
    }
    all_filenames.discard("")
    deleted_filenames: set[str] = set()

    total_deleted = 0
    total_failed = 0
    dry_run_sets = 0

    # Phase 1: remove avatar sets for deleted (and optionally deactivated) users.
    phase1_delete_pks: set[int] = set()

    if not (_cleanup_when_deleted or _cleanup_when_deactivated):
        log.debug(
            "Phase 1 skipped (cleanup.when_user_deleted=false, cleanup.when_user_deactivated=false)."
        )
    else:
        # Fetch user PKs from Authentik.  Minimise API calls based on which
        # flags are set:
        #   - both flags: only active PKs needed (clean everything not active)
        #   - deleted only: all PKs needed (clean those absent from Authentik)
        #   - deactivated only: both sets needed (clean in-Authentik-but-inactive)
        all_pks: set[int] = set()
        active_pks: set[int] = set()
        try:
            if _cleanup_when_deleted and _cleanup_when_deactivated:
                active_pks = list_active_user_pks()
                all_pks = active_pks  # deleted users are also absent from active
            elif _cleanup_when_deleted:
                all_pks = list_all_user_pks()
            else:  # only _cleanup_when_deactivated
                all_pks = list_all_user_pks()
                active_pks = list_active_user_pks()
        except Exception:
            log.exception(
                "Failed to fetch user list from Authentik - aborting cleanup."
            )
            return 0

        # Safety guard: if Authentik returned zero users the API is likely broken
        # or the token expired.  Aborting prevents catastrophic mass deletion.
        if not all_pks:
            log.warning(
                "Authentik returned zero users - aborting to prevent accidental mass deletion."
            )
            return 0

        log.info(
            "Found %d avatar set(s) on disk, %d user(s) in Authentik.",
            len(all_metadata),
            len(all_pks),
        )

        # Determine which PKs to clean up based on each user's status.
        # Short-circuit evaluation ensures active_pks is never accessed when
        # _cleanup_when_deactivated is false (and thus active_pks is not set).
        for user_pk in per_user:
            if _cleanup_when_deleted and user_pk not in all_pks:
                phase1_delete_pks.add(user_pk)
            elif (
                _cleanup_when_deactivated
                and user_pk in all_pks
                and user_pk not in active_pks
            ):
                phase1_delete_pks.add(user_pk)

        # Delete avatar sets for every targeted user PK.
        for user_pk in phase1_delete_pks:
            reason = "deleted" if user_pk not in all_pks else "deactivated"
            for meta in per_user[user_pk]:
                filename = meta.get("filename", "")
                if dry_run:
                    log.info(
                        "[DRY-RUN] Would remove avatar set %s (user_pk=%s, %s).",
                        filename,
                        user_pk,
                        reason,
                    )
                    dry_run_sets += 1
                else:
                    log.info(
                        "Removing avatar set %s (user_pk=%s, %s).",
                        filename,
                        user_pk,
                        reason,
                    )
                    d, f = cleanup_avatar_files(filename)
                    total_deleted += d
                    total_failed += f
                deleted_filenames.add(filename)

    # Phase 2: enforce per-user retention for users not handled in Phase 1.
    ret_deleted, ret_failed, retention_deleted = _enforce_retention(
        per_user, phase1_delete_pks
    )
    total_deleted += ret_deleted
    total_failed += ret_failed
    deleted_filenames |= retention_deleted
    if dry_run:
        dry_run_sets += len(retention_deleted)

    # Filenames that should still exist on disk after phases 1 and 2
    surviving_filenames = all_filenames - deleted_filenames

    # Phase 3: remove orphaned files (obsolete sizes, formats, no metadata).
    orph_expected, orph_deleted, orph_failed = _cleanup_orphaned_files(
        surviving_filenames
    )
    total_deleted += orph_deleted
    total_failed += orph_failed

    if dry_run:
        dry_run_total = dry_run_sets * _FILES_PER_SET + orph_expected
        if dry_run_total:
            log.info(
                "Cleanup complete: would remove ~%d file(s) (%d avatar set(s), %d orphan(s)).",
                dry_run_total,
                dry_run_sets,
                orph_expected,
            )
        else:
            log.info("Cleanup complete: nothing to remove.")
    elif total_deleted or total_failed:
        total_targeted = total_deleted + total_failed
        if total_failed:
            log.info(
                "Cleanup complete: %d deleted, %d failed (%d targeted).",
                total_deleted,
                total_failed,
                total_targeted,
            )
        else:
            log.info("Cleanup complete: %d file(s) deleted.", total_deleted)
    else:
        log.info("Cleanup complete: nothing to remove.")

    return total_deleted


# Background daemon thread
def _cleanup_loop() -> None:
    """
    Sleep until the next cron-scheduled time, run cleanup, repeat.

    Called as the target of a daemon thread — it exits automatically when the
    main process shuts down.
    """
    if _run_on_startup:
        log.info("cleanup.on_startup is enabled - running cleanup in 60 s.")
        time.sleep(60)
        try:
            run_cleanup()
        except Exception:
            log.exception("Startup cleanup failed.")

    cron = croniter(_cron_expr, datetime.now(UTC))

    while True:
        next_run = cron.get_next(datetime)
        now = datetime.now(UTC)
        delay = max((next_run - now).total_seconds(), 0)
        log.debug(
            "Next cleanup scheduled at %s (in %.0f seconds).",
            next_run.isoformat(),
            delay,
        )
        time.sleep(delay)

        try:
            run_cleanup()
        except Exception:
            # Log and continue — a transient API failure should not kill the
            # cleanup thread permanently.
            log.exception("Cleanup iteration failed.")


def start_cleanup_thread() -> None:
    """
    Start the background cleanup thread if a cron schedule is configured.

    Uses a daemon thread so it is automatically terminated when the main
    process exits — no explicit shutdown logic needed.
    """
    if not _cron_expr:
        log.info("Cleanup is disabled (cleanup.interval is empty).")
        return

    if not croniter.is_valid(_cron_expr):
        log.error(
            "Invalid cron expression %r for cleanup.interval - cleanup is disabled.",
            _cron_expr,
        )
        return

    thread = threading.Thread(
        target=_cleanup_loop,
        name="cleanup",
        daemon=True,
    )
    thread.start()
    log.info("Cleanup thread started (schedule: %s).", _cron_expr)
