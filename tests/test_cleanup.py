"""Tests for src/cleanup.py - the scheduled avatar cleanup job.

This is the only code in the application that deletes user data, so the tests
cover both what it must remove (deleted users, retention overflow, orphans) and
- just as importantly - the guards that stop it removing anything: the
zero-users abort, dry-run mode, and the in-process / cross-process run locks.
"""

import json
import threading

import pytest

import src.cleanup as cleanup
from src.cleanup import (
    _apply_thread_priority,
    _cleanup_orphaned_files,
    _enforce_retention,
    run_cleanup,
    start_cleanup_thread,
)
from src.config import img_formats, img_sizes
from src.imaging import AVATAR_ROOT, METADATA_ROOT, generate_filename, process_image
from tests.helpers import make_image

FILES_PER_SET = len(img_sizes) * len(img_formats)


def create_avatar_set(
    user_pk: int, uploaded_at: str = "2026-01-01T00:00:00+00:00"
) -> str:
    """Generate a complete avatar set on disk plus its metadata sidecar."""
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    (METADATA_ROOT / f"{base}.meta.json").write_text(
        json.dumps(
            {
                "filename": base,
                "user_pk": user_pk,
                "uploaded_at": uploaded_at,
                "sizes": img_sizes,
                "formats": img_formats,
                "total_bytes": 1,
                "source": "web",
            }
        ),
        encoding="utf-8",
    )
    return base


def set_exists(base: str) -> bool:
    """True when at least one file of the avatar set is still on disk."""
    return any(
        (AVATAR_ROOT / f"{size}x{size}" / f"{base}.{ext}").exists()
        for size in img_sizes
        for ext in img_formats
    )


@pytest.fixture
def authentik_users(monkeypatch):
    """Control which user PKs Authentik reports back to the cleanup job."""
    state = {"all": set(), "active": set()}
    monkeypatch.setattr(cleanup, "list_all_user_pks", lambda: set(state["all"]))
    monkeypatch.setattr(cleanup, "list_active_user_pks", lambda: set(state["active"]))
    return state


# ---------------------------------------------------------------------------
# Phase 1: deleted and deactivated users
# ---------------------------------------------------------------------------


def test_avatars_of_a_deleted_user_are_removed(authentik_users):
    survivor = create_avatar_set(user_pk=1)
    deleted = create_avatar_set(user_pk=999)
    authentik_users["all"] = {1}

    run_cleanup()

    assert set_exists(survivor)
    assert not set_exists(deleted)
    assert not (METADATA_ROOT / f"{deleted}.meta.json").exists()


def test_deactivated_users_are_kept_by_default(authentik_users):
    base = create_avatar_set(user_pk=1)
    authentik_users["all"] = {1}
    authentik_users["active"] = set()  # user 1 exists but is deactivated

    run_cleanup()

    assert set_exists(base)


def test_deactivated_users_are_cleaned_when_the_flag_is_on(
    authentik_users, monkeypatch
):
    monkeypatch.setattr(cleanup, "_cleanup_when_deactivated", True)
    monkeypatch.setattr(cleanup, "_cleanup_when_deleted", False)
    base = create_avatar_set(user_pk=1)
    authentik_users["all"] = {1}
    authentik_users["active"] = set()

    run_cleanup()

    assert not set_exists(base)


def test_with_both_flags_only_active_users_survive(authentik_users, monkeypatch):
    monkeypatch.setattr(cleanup, "_cleanup_when_deactivated", True)
    active = create_avatar_set(user_pk=1)
    inactive = create_avatar_set(user_pk=2)
    gone = create_avatar_set(user_pk=3)
    # Both flags on: the job only asks for active PKs.
    authentik_users["active"] = {1}

    run_cleanup()

    assert set_exists(active)
    assert not set_exists(inactive)
    assert not set_exists(gone)


def test_phase_one_is_skipped_when_both_flags_are_off(authentik_users, monkeypatch):
    monkeypatch.setattr(cleanup, "_cleanup_when_deleted", False)
    monkeypatch.setattr(cleanup, "_cleanup_when_deactivated", False)
    monkeypatch.setattr(
        cleanup,
        "list_all_user_pks",
        lambda: pytest.fail("Authentik must not be queried when phase 1 is off"),
    )
    base = create_avatar_set(user_pk=999)

    run_cleanup()

    assert set_exists(base)


# ---------------------------------------------------------------------------
# Safety guards
# ---------------------------------------------------------------------------


def test_an_empty_user_list_aborts_the_whole_run(authentik_users, caplog):
    # An expired API token would otherwise look like "every user was deleted".
    base = create_avatar_set(user_pk=1)
    authentik_users["all"] = set()

    with caplog.at_level("WARNING", logger="cleanup"):
        assert run_cleanup() == 0

    assert set_exists(base)
    assert "prevent accidental mass deletion" in caplog.text


def test_an_authentik_failure_aborts_the_whole_run(monkeypatch, caplog):
    base = create_avatar_set(user_pk=1)

    def boom():
        raise ConnectionError("Authentik unreachable")

    monkeypatch.setattr(cleanup, "list_all_user_pks", boom)

    with caplog.at_level("ERROR", logger="cleanup"):
        assert run_cleanup() == 0

    assert set_exists(base)
    assert "aborting cleanup" in caplog.text


def test_dry_run_deletes_nothing(authentik_users, monkeypatch, caplog):
    monkeypatch.setattr(cleanup, "dry_run", True)
    base = create_avatar_set(user_pk=999)
    authentik_users["all"] = {1}

    with caplog.at_level("INFO", logger="cleanup"):
        run_cleanup()

    assert set_exists(base)
    assert "[DRY-RUN] Would remove avatar set" in caplog.text


def test_a_second_run_in_the_same_process_is_skipped(authentik_users, caplog):
    # The in-process lock keeps a startup run from racing a scheduled one.
    # threading.Lock is not reentrant, so holding it here is exactly what a
    # concurrent run sees: a non-blocking acquire that fails.
    base = create_avatar_set(user_pk=999)
    authentik_users["all"] = {1}

    assert cleanup._cleanup_lock.acquire(blocking=False) is True
    try:
        with caplog.at_level("WARNING", logger="cleanup"):
            assert run_cleanup() == 0
    finally:
        cleanup._cleanup_lock.release()

    assert "already in progress (same process)" in caplog.text
    assert set_exists(base)


def test_a_run_in_another_process_is_skipped(monkeypatch, authentik_users, caplog):
    # The flock guard makes a manual run_cleanup.py invocation stand down while
    # the background thread (or the Gravatar sync) holds the lock.
    from src.avatar_pipeline import CLEANUP_LOCKFILE, exclusive_process_lock

    base = create_avatar_set(user_pk=999)
    authentik_users["all"] = {1}

    with exclusive_process_lock(CLEANUP_LOCKFILE, "Cleanup") as held:
        assert held
        with caplog.at_level("WARNING", logger="avatar_pipeline"):
            assert run_cleanup() == 0

    assert set_exists(base)


def test_metadata_entries_missing_a_pk_or_filename_are_skipped(authentik_users):
    broken = METADATA_ROOT / "broken.meta.json"
    broken.write_text(json.dumps({"user_pk": None, "filename": ""}), encoding="utf-8")
    survivor = create_avatar_set(user_pk=1)
    authentik_users["all"] = {1}

    run_cleanup()

    # The malformed record never enters the per-user grouping, so it cannot
    # cause a valid user's avatars to be deleted - and it is swept up by the
    # orphaned-metadata phase rather than lingering forever.
    assert set_exists(survivor)
    assert not broken.exists()


# ---------------------------------------------------------------------------
# Phase 2: retention
# ---------------------------------------------------------------------------


def test_retention_keeps_only_the_newest_sets():
    metas = [
        {"filename": f"set{i}", "uploaded_at": f"2026-01-0{i}T00:00:00+00:00"}
        for i in range(1, 6)
    ]
    deleted_names = []
    # Retention deletes via cleanup_avatar_files; record instead of touching disk.
    original = cleanup.cleanup_avatar_files
    try:
        cleanup.cleanup_avatar_files = lambda name: (
            deleted_names.append(name),
            (1, 0),
        )[1]
        _deleted, _failed, deleted_sets = _enforce_retention({7: metas}, skip_pks=set())
    finally:
        cleanup.cleanup_avatar_files = original

    # avatar_retention_count is 2 in the test config: set5 and set4 survive.
    assert deleted_sets == {"set1", "set2", "set3"}
    assert set(deleted_names) == deleted_sets


def test_retention_is_not_applied_to_users_already_handled_by_phase_one():
    metas = [
        {"filename": f"set{i}", "uploaded_at": f"2026-01-0{i}T00:00:00+00:00"}
        for i in range(1, 6)
    ]
    _d, _f, deleted = _enforce_retention({7: metas}, skip_pks={7})
    assert deleted == set()


def test_retention_leaves_a_user_at_or_below_the_limit_alone():
    metas = [
        {"filename": "a", "uploaded_at": "2026-01-01T00:00:00+00:00"},
        {"filename": "b", "uploaded_at": "2026-01-02T00:00:00+00:00"},
    ]
    _d, _f, deleted = _enforce_retention({7: metas}, skip_pks=set())
    assert deleted == set()


def test_retention_can_be_disabled(monkeypatch):
    monkeypatch.setattr(cleanup, "_retention_count", 0)
    metas = [
        {"filename": f"set{i}", "uploaded_at": f"2026-01-0{i}T00:00:00+00:00"}
        for i in range(1, 6)
    ]
    assert _enforce_retention({7: metas}, skip_pks=set()) == (0, 0, set())


def test_retention_runs_end_to_end_against_real_files(authentik_users):
    keep_new = create_avatar_set(1, uploaded_at="2026-03-01T00:00:00+00:00")
    keep_mid = create_avatar_set(1, uploaded_at="2026-02-01T00:00:00+00:00")
    drop_old = create_avatar_set(1, uploaded_at="2026-01-01T00:00:00+00:00")
    authentik_users["all"] = {1}

    run_cleanup()

    assert set_exists(keep_new) and set_exists(keep_mid)
    assert not set_exists(drop_old)


# ---------------------------------------------------------------------------
# Phases 3-4: orphans
# ---------------------------------------------------------------------------


def test_an_obsolete_size_directory_is_removed_whole():
    obsolete = AVATAR_ROOT / "999x999"
    obsolete.mkdir()
    (obsolete / "a.jpg").write_bytes(b"x")
    (obsolete / "b.jpg").write_bytes(b"x")

    expected, deleted, failed = _cleanup_orphaned_files(set())

    assert (expected, deleted, failed) == (2, 2, 0)
    assert not obsolete.exists()


def test_a_directory_that_is_not_a_size_is_left_alone():
    unrelated = AVATAR_ROOT / "some-other-folder"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_bytes(b"x")

    _cleanup_orphaned_files(set())

    assert (unrelated / "keep.txt").exists()


def test_a_file_in_an_unconfigured_format_is_removed():
    base = create_avatar_set(user_pk=1)
    stale = AVATAR_ROOT / "256x256" / f"{base}.bmp"
    stale.write_bytes(b"x")

    _expected, deleted, _failed = _cleanup_orphaned_files({base})

    assert deleted == 1
    assert not stale.exists()
    assert (AVATAR_ROOT / "256x256" / f"{base}.jpg").exists()


def test_an_image_without_metadata_is_removed():
    orphan = AVATAR_ROOT / "256x256" / "orphan-file.jpg"
    orphan.write_bytes(b"x")

    _expected, deleted, _failed = _cleanup_orphaned_files(set())

    assert deleted == 1
    assert not orphan.exists()


def test_metadata_without_images_is_removed():
    stray = METADATA_ROOT / "stray.meta.json"
    stray.write_text(json.dumps({"filename": "stray", "user_pk": 1}), encoding="utf-8")

    _expected, deleted, _failed = _cleanup_orphaned_files(set())

    assert deleted == 1
    assert not stray.exists()


def test_orphan_removal_respects_dry_run(monkeypatch):
    monkeypatch.setattr(cleanup, "dry_run", True)
    orphan = AVATAR_ROOT / "256x256" / "orphan-file.jpg"
    orphan.write_bytes(b"x")

    expected, deleted, failed = _cleanup_orphaned_files(set())

    assert (expected, deleted, failed) == (1, 1, 0)
    assert orphan.exists()


def test_unremovable_orphans_are_counted_as_failures(monkeypatch):
    orphan = AVATAR_ROOT / "256x256" / "orphan-file.jpg"
    orphan.write_bytes(b"x")

    from pathlib import Path

    def failing_unlink(self, *args, **kwargs):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    expected, deleted, failed = _cleanup_orphaned_files(set())

    assert (expected, deleted, failed) == (1, 0, 1)


# ---------------------------------------------------------------------------
# Phase 5: backfill
# ---------------------------------------------------------------------------


def test_missing_files_of_a_surviving_set_are_regenerated(authentik_users):
    base = create_avatar_set(user_pk=1)
    missing = AVATAR_ROOT / "64x64" / f"{base}.webp"
    missing.unlink()
    authentik_users["all"] = {1}

    run_cleanup()

    assert missing.is_file()


def test_backfill_can_be_disabled(authentik_users, monkeypatch):
    monkeypatch.setattr(cleanup, "_backfill_missing", False)
    base = create_avatar_set(user_pk=1)
    missing = AVATAR_ROOT / "64x64" / f"{base}.webp"
    missing.unlink()
    authentik_users["all"] = {1}

    run_cleanup()

    assert not missing.exists()


def test_a_set_deleted_in_phase_one_is_never_backfilled(authentik_users):
    base = create_avatar_set(user_pk=999)
    authentik_users["all"] = {1}

    run_cleanup()

    assert not set_exists(base)
    assert not (AVATAR_ROOT / "64x64" / f"{base}.webp").exists()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def test_the_run_reports_the_number_of_files_deleted(authentik_users):
    create_avatar_set(user_pk=999)
    authentik_users["all"] = {1}
    # One full avatar set: every size x format file plus the metadata sidecar.
    assert run_cleanup() == FILES_PER_SET + 1


def test_a_clean_run_reports_zero(authentik_users, caplog):
    create_avatar_set(user_pk=1)
    authentik_users["all"] = {1}
    with caplog.at_level("INFO", logger="cleanup"):
        assert run_cleanup() == 0
    assert "nothing to remove" in caplog.text


# ---------------------------------------------------------------------------
# Scheduling
# ---------------------------------------------------------------------------


def test_no_thread_is_started_when_cleanup_is_disabled(monkeypatch, caplog):
    monkeypatch.setattr(cleanup, "_cron_expr", "")
    monkeypatch.setattr(cleanup, "_run_on_startup", False)
    monkeypatch.setattr(
        threading, "Thread", lambda *a, **kw: pytest.fail("no thread expected")
    )
    with caplog.at_level("INFO", logger="cleanup"):
        start_cleanup_thread()
    assert "Cleanup is disabled" in caplog.text


def test_an_invalid_cron_expression_disables_cleanup(monkeypatch, caplog):
    monkeypatch.setattr(cleanup, "_cron_expr", "not a cron expression")
    monkeypatch.setattr(
        threading, "Thread", lambda *a, **kw: pytest.fail("no thread expected")
    )
    with caplog.at_level("ERROR", logger="cleanup"):
        start_cleanup_thread()
    assert "Invalid cron expression" in caplog.text


def test_a_valid_schedule_starts_a_daemon_thread(monkeypatch):
    monkeypatch.setattr(cleanup, "_cron_expr", "0 2 * * *")
    started = {}

    class FakeThread:
        def __init__(self, target, name, daemon):
            started.update(name=name, daemon=daemon)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(threading, "Thread", FakeThread)
    start_cleanup_thread()

    assert started == {"name": "cleanup", "daemon": True, "started": True}


def test_on_startup_without_a_schedule_runs_once(monkeypatch):
    monkeypatch.setattr(cleanup, "_cron_expr", "")
    monkeypatch.setattr(cleanup, "_run_on_startup", True)
    started = {}

    class FakeThread:
        def __init__(self, target, name, daemon):
            started.update(name=name, daemon=daemon)

        def start(self):
            started["started"] = True

    monkeypatch.setattr(threading, "Thread", FakeThread)
    start_cleanup_thread()

    assert started["name"] == "cleanup-startup"


def test_lowering_thread_priority_never_raises(monkeypatch):
    # Inside the cap_drop:ALL container the syscall may be refused; that must be
    # logged and ignored rather than killing the cleanup thread.
    monkeypatch.setattr(cleanup, "_scheduler_priority", 10)

    def refused(*_args):
        raise OSError("operation not permitted")

    monkeypatch.setattr(cleanup.os, "setpriority", refused)
    _apply_thread_priority()


def test_thread_priority_is_left_alone_when_not_configured(monkeypatch):
    monkeypatch.setattr(cleanup, "_scheduler_priority", 0)
    monkeypatch.setattr(
        cleanup.os,
        "setpriority",
        lambda *a: pytest.fail("priority must not be touched when disabled"),
    )
    _apply_thread_priority()


# ---------------------------------------------------------------------------
# Scheduler loop
# ---------------------------------------------------------------------------


class _StopLoop(Exception):
    """Breaks out of the cleanup thread's `while True` inside a test."""


def test_the_cron_loop_runs_cleanup_on_every_tick(monkeypatch):
    monkeypatch.setattr(cleanup, "_cron_expr", "0 2 * * *")
    monkeypatch.setattr(cleanup, "_run_on_startup", False)
    runs = []
    monkeypatch.setattr(cleanup, "run_cleanup", lambda: runs.append("run"))

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) > 2:
            raise _StopLoop

    monkeypatch.setattr(cleanup.time, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        cleanup._cleanup_loop()

    assert len(runs) == 2
    # The delay is computed from the cron schedule, so it is never zero-ish.
    assert all(delay > 0 for delay in sleeps)


def test_a_failing_iteration_does_not_kill_the_cron_loop(monkeypatch, caplog):
    monkeypatch.setattr(cleanup, "_cron_expr", "0 2 * * *")
    monkeypatch.setattr(cleanup, "_run_on_startup", False)
    calls = []

    def flaky():
        calls.append(1)
        raise ConnectionError("Authentik unreachable")

    monkeypatch.setattr(cleanup, "run_cleanup", flaky)

    sleeps = []

    def fake_sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) > 2:
            raise _StopLoop

    monkeypatch.setattr(cleanup.time, "sleep", fake_sleep)

    with caplog.at_level("ERROR", logger="cleanup"):
        with pytest.raises(_StopLoop):
            cleanup._cleanup_loop()

    # A transient API failure must not permanently stop scheduled cleanups.
    assert len(calls) == 2
    assert "Cleanup iteration failed" in caplog.text


def test_the_startup_run_happens_before_the_first_scheduled_tick(monkeypatch):
    monkeypatch.setattr(cleanup, "_cron_expr", "0 2 * * *")
    monkeypatch.setattr(cleanup, "_run_on_startup", True)
    runs = []
    monkeypatch.setattr(cleanup, "run_cleanup", lambda: runs.append("run"))

    sleeps = []

    def fake_sleep(_seconds):
        sleeps.append(1)
        if len(sleeps) > 1:
            raise _StopLoop

    monkeypatch.setattr(cleanup.time, "sleep", fake_sleep)

    with pytest.raises(_StopLoop):
        cleanup._cleanup_loop()

    assert len(runs) == 1  # the startup run, before any scheduled tick


def test_the_one_shot_startup_runner_runs_cleanup_once(monkeypatch):
    runs = []
    monkeypatch.setattr(cleanup, "run_cleanup", lambda: runs.append("run"))
    monkeypatch.setattr(cleanup.time, "sleep", lambda _s: None)

    cleanup._startup_only_runner()

    assert runs == ["run"]


def test_a_failing_startup_run_is_logged_and_swallowed(monkeypatch, caplog):
    monkeypatch.setattr(
        cleanup,
        "run_cleanup",
        lambda: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(cleanup.time, "sleep", lambda _s: None)

    with caplog.at_level("ERROR", logger="cleanup"):
        cleanup._startup_only_runner()

    assert "Startup cleanup failed" in caplog.text
