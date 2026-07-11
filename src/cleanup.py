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

Deletion behavior (Phase 1) is controlled by two config flags:
  - ``cleanup.when_user_deleted`` (default true): remove avatar sets for users
    that no longer exist in Authentik at all.
  - ``cleanup.when_user_deactivated`` (default false): also remove avatar sets
    for users that exist but are marked as inactive in Authentik.

Orphan cleanup:
  - Size directories that no longer appear in ``images.sizes`` are removed.
  - Image files whose format (extension) is no longer in ``images.formats``
    are deleted.
  - Image files whose filename base has no matching .meta.json are deleted.

Backfill (the inverse of orphan cleanup):
  - For every surviving avatar set, any file missing for a configured
    size/format is regenerated from the largest image already on disk.  This
    heals existing avatars after a new size or format is added to config,
    without requiring users to re-upload.  Controlled by
    ``cleanup.backfill_missing_images`` (default true).

Priority:
  - The *scheduled* cleanup runs at a lowered scheduling priority (Linux
    ``nice``) so its filesystem scans and image regeneration yield CPU to the
    latency-sensitive request threads.  Manual ``run_cleanup.py`` runs are not
    deprioritized.  Controlled by ``cleanup.scheduler_priority``.

Safety:
  - If the Authentik API returns zero users (e.g. due to an expired token or
    network error), the cleanup aborts entirely to prevent accidental mass
    deletion.
  - Respects dry_run mode from config.yml - when enabled, only logs what
    would be deleted without touching the filesystem.

This module is used in two ways:
  1. Automatically via a background daemon thread started in run_app.py.
  2. Manually via ``python run_cleanup.py`` at the project root.
"""

import fcntl
import logging
import os
import re
import shutil
import sys
import threading
import time
from collections import defaultdict
from datetime import UTC, datetime

from croniter import croniter

from src.authentik import list_active_user_pks, list_all_user_pks
from src.config import (
    cleanup_backfill_missing,
    cleanup_interval,
    cleanup_on_startup,
    cleanup_retention_count,
    cleanup_scheduler_priority,
    cleanup_when_deactivated,
    cleanup_when_deleted,
    dry_run,
    img_formats,
    img_sizes,
)
from src.imaging import (
    AVATAR_ROOT,
    METADATA_ROOT,
    backfill_avatar_set,
    cleanup_avatar_files,
    get_all_avatar_metadata,
)

log = logging.getLogger("cleanup")

# In-process thread lock: prevents two threads in the same process from running
# cleanup concurrently (e.g. startup cleanup racing with a scheduled run).
# Non-blocking acquire: the second caller skips rather than waiting.
_cleanup_lock = threading.Lock()

# Filesystem advisory lock for cross-process protection: ensures that a manual
# `python run_cleanup.py` invocation cannot run while the background thread in
# the Flask process is already executing cleanup.  fcntl.flock() is enforced by
# the OS kernel and is automatically released when the fd closes (even on crash).
_CLEANUP_LOCKFILE = AVATAR_ROOT / ".cleanup.lock"

# Crontab schedule for the cleanup job (empty string = disabled).
# Read once at import time; config is immutable after startup.
_cron_expr = cleanup_interval
_run_on_startup = cleanup_on_startup
_retention_count = cleanup_retention_count
_cleanup_when_deleted = cleanup_when_deleted
_cleanup_when_deactivated = cleanup_when_deactivated
_scheduler_priority = cleanup_scheduler_priority
_backfill_missing = cleanup_backfill_missing

# Currently configured sizes and on-disk file extensions (used to detect orphans).
# img_formats entries are already canonical on-disk extensions (config.py
# resolves them through FORMAT_MAP, e.g. "jpeg" -> "jpg"), so they can be used
# directly - the same naming process_image() and backfill_avatar_set() use.
_configured_sizes = {f"{s}x{s}" for s in img_sizes}
_configured_formats = set(img_formats)

# Regex to match size directory names like "128x128", "1024x1024"
_SIZE_DIR_RE = re.compile(r"^\d+x\d+$")


def _apply_thread_priority() -> None:
    """
    Lower the scheduling priority ("niceness") of the calling thread so the
    scheduled cleanup yields CPU to the latency-sensitive web request threads.

    Called once from the background cleanup thread(s) - the cron loop and the
    one-shot startup runner - so only the *automated* cleanup is deprioritized.
    Manual ``run_cleanup.py`` invocations call ``run_cleanup()`` directly and
    keep normal priority.

    On Linux the nice value is a per-thread attribute, so adjusting it here
    affects only the cleanup thread; the gunicorn worker threads serving avatar
    requests keep their normal priority.  ``os.setpriority`` is used with an
    absolute target, so the call is idempotent (unlike ``os.nice()``, whose
    effect is cumulative).  This is therefore restricted to Linux - see the
    inline note below; other platforms run cleanup at normal priority.

    Raising the nice value (i.e. lowering priority) never requires elevated
    privileges, so this works inside the non-root, ``cap_drop: ALL`` container.
    A restricted syscall is logged at debug level and ignored; cleanup then
    simply runs at normal priority.
    """
    # 0 (or negative) means "leave priority unchanged" - raising priority would
    # need CAP_SYS_NICE, which the container does not grant.
    if _scheduler_priority <= 0:
        return

    # Linux only: nice is a per-thread attribute there, so this deprioritizes
    # only the cleanup thread.  On other Unix (macOS/BSD) PRIO_PROCESS would
    # lower the whole process, slowing the request threads too; on Windows
    # setpriority does not exist.  Both cases run cleanup at normal priority.
    if not sys.platform.startswith("linux"):
        log.debug("Per-thread nice is Linux-only - cleanup runs at normal priority.")
        return

    try:
        # who=0 targets the calling thread; PRIO_PROCESS + nice is per-thread on Linux.
        os.setpriority(os.PRIO_PROCESS, 0, _scheduler_priority)
        log.debug("Cleanup thread niceness set to %d.", _scheduler_priority)
    except OSError as exc:
        log.debug(
            "Could not lower cleanup priority to %d: %s", _scheduler_priority, exc
        )


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
_FILES_PER_SET = len(img_sizes) * len(img_formats) + 1


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
      3. Remove orphaned files for obsolete sizes and formats.
      4. Remove orphaned metadata files with no matching images on disk.
      5. Backfill files missing for a configured size/format, regenerated from
         the largest image on disk (cleanup.backfill_missing_images).

    Matching is done by ``user_pk`` (Authentik's integer primary key), which
    is immutable - unlike usernames, it survives renames and reveals no PII.

    Returns the total number of files removed (or that would be removed
    in dry-run mode).  Returns 0 immediately if another run is already in
    progress (concurrent runs are skipped, not queued).
    """
    if not _cleanup_lock.acquire(blocking=False):
        log.warning("Cleanup already in progress (same process) - skipping.")
        return 0

    try:
        # Cross-process guard: open (or create) the lockfile and attempt a
        # non-blocking exclusive lock.  A separate process running run_cleanup.py
        # concurrently will fail here and exit cleanly rather than corrupting
        # avatar files mid-cleanup.  The `with` block guarantees the fd is closed
        # (and the OS-level flock released) on every exit path, including
        # exceptions inside _run_cleanup_impl().
        try:
            lockfile_ctx = open(_CLEANUP_LOCKFILE, "w")
        except OSError as exc:
            log.warning(
                "Could not open cleanup lockfile %s: %s", _CLEANUP_LOCKFILE, exc
            )
            return 0
        with lockfile_ctx as lockfile:
            try:
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                log.warning("Cleanup already in progress (another process) - skipping.")
                return 0
            try:
                return _run_cleanup_impl()
            finally:
                fcntl.flock(lockfile.fileno(), fcntl.LOCK_UN)
    finally:
        _cleanup_lock.release()


def _run_cleanup_impl() -> int:
    """Internal cleanup implementation called under _cleanup_lock."""
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
        # Fetch user PKs from Authentik.  Minimize API calls based on which
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
            if _cleanup_when_deleted and _cleanup_when_deactivated:
                # When both flags are set, all_pks holds active PKs only -
                # cannot distinguish deleted from deactivated without a second API call.
                reason = "deleted or deactivated"
            else:
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

    # Phases 3-4: remove orphaned files (obsolete sizes/formats, images without
    # metadata, and metadata without images).
    orph_expected, orph_deleted, orph_failed = _cleanup_orphaned_files(
        surviving_filenames
    )
    total_deleted += orph_deleted
    total_failed += orph_failed

    # Phase 5: backfill missing sizes/formats for surviving avatar sets.
    # Runs after the orphan phases so obsolete sizes are already gone and we only
    # operate on sets that still have valid metadata (won't regenerate files for an
    # avatar that was just deleted).  This is the inverse of orphan cleanup:
    # where a new size/format was added to config, existing avatars are filled
    # in on-demand from the largest image already on disk.
    backfill_generated = 0
    backfill_failed = 0
    backfill_skipped = 0
    if _backfill_missing:
        # Backfill is enabled: check every surviving set for missing size/format
        # files.  Logged at debug so operators can confirm the phase ran and see
        # how many sets it scanned without cluttering normal INFO output.
        log.debug(
            "Phase 5 backfill active - checking %d surviving avatar set(s) for missing sizes/formats.",
            len(surviving_filenames),
        )
        for filename in surviving_filenames:
            gen, fail, skip = backfill_avatar_set(filename)
            backfill_generated += gen
            backfill_failed += fail
            backfill_skipped += skip
        # Real backfill write failures are genuine cleanup failures - fold them
        # into the run-level total so the final summary reflects them.  Skipped
        # sets (missing files with no readable source) are reported below but
        # never counted as failures: nothing the job does can resolve them.
        total_failed += backfill_failed
        if backfill_generated or backfill_failed or backfill_skipped:
            # Report all three counts the same way in dry-run and real runs so a
            # preview never hides unrecoverable or failed sets.  Failed/skipped
            # are only appended when non-zero to keep the common case terse.
            verb = "would generate" if dry_run else "generated"
            details = [f"{verb} {backfill_generated} missing file(s)"]
            if backfill_skipped:
                details.append(f"{backfill_skipped} unrecoverable (no source)")
            if backfill_failed:
                details.append(f"{backfill_failed} failed")
            log.info("Backfill: %s.", ", ".join(details))
    else:
        log.debug("Phase 5 backfill skipped (cleanup.backfill_missing_images=false).")

    # Backfill counts are reported on their own "Backfill: ..." line above; also
    # fold the generated count into the final summary so a run that only
    # generated files is never summarized as "nothing to remove" (LOG-01).
    gen_clause = ""
    if backfill_generated:
        gen_verb = "would generate" if dry_run else "generated"
        gen_clause = f", {gen_verb} {backfill_generated} file(s)"

    if dry_run:
        dry_run_total = dry_run_sets * _FILES_PER_SET + orph_expected
        if dry_run_total:
            log.info(
                "Cleanup complete: would remove ~%d file(s) (%d avatar set(s), %d orphan(s))%s.",
                dry_run_total,
                dry_run_sets,
                orph_expected,
                gen_clause,
            )
        elif gen_clause:
            log.info("Cleanup complete: nothing to remove%s.", gen_clause)
        else:
            log.info("Cleanup complete: nothing to remove.")
    elif total_deleted or total_failed:
        total_targeted = total_deleted + total_failed
        if total_failed:
            log.info(
                "Cleanup complete: %d deleted, %d failed (%d targeted)%s.",
                total_deleted,
                total_failed,
                total_targeted,
                gen_clause,
            )
        else:
            log.info(
                "Cleanup complete: %d file(s) deleted%s.", total_deleted, gen_clause
            )
    elif gen_clause:
        log.info("Cleanup complete: nothing to remove%s.", gen_clause)
    else:
        log.info("Cleanup complete: nothing to remove.")

    return total_deleted


# Background daemon thread
def _cleanup_loop() -> None:
    """
    Sleep until the next cron-scheduled time, run cleanup, repeat.

    Called as the target of a daemon thread - it exits automatically when the
    main process shuts down.
    """
    # Deprioritize this background thread so the scheduled cleanup yields to
    # request handling.  Applied per-thread here (not in run_cleanup) so manual
    # run_cleanup.py invocations keep normal priority.
    _apply_thread_priority()

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
            # Log and continue - a transient API failure should not kill the
            # cleanup thread permanently.
            log.exception("Cleanup iteration failed.")


def _startup_only_runner() -> None:
    """One-shot startup cleanup runner used when on_startup is set without a schedule."""
    # Same per-thread deprioritization as the cron loop: this is an automated
    # (startup-triggered) run, so it yields to request handling.
    _apply_thread_priority()
    log.info("cleanup.on_startup is enabled (no schedule) - running cleanup in 60 s.")
    time.sleep(60)
    try:
        run_cleanup()
    except Exception:
        log.exception("Startup cleanup failed.")


def start_cleanup_thread() -> None:
    """
    Start the background cleanup thread.

    Behavior depends on the configured combination of cleanup.interval and
    cleanup.on_startup:
      - schedule set:               run the regular cron loop (which itself
                                    honors on_startup before the first tick).
      - schedule empty, on_startup: run a one-shot cleanup at startup, then exit.
      - both empty:                 cleanup is fully disabled.

    Uses a daemon thread so it is automatically terminated when the main
    process exits - no explicit shutdown logic needed.
    """
    if not _cron_expr:
        if _run_on_startup:
            # Honor on_startup even when no recurring schedule is configured -
            # otherwise the flag would silently do nothing.
            thread = threading.Thread(
                target=_startup_only_runner,
                name="cleanup-startup",
                daemon=True,
            )
            thread.start()
            log.info("Cleanup thread started (one-shot startup run, no schedule).")
            return
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
