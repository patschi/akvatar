"""Tests for the operator-facing CLI entry points.

``run_sync_gravatar.py`` is meant to be driven from cron, so its exit codes are
part of its contract: an operator's alerting keys off them.  ``run_cleanup.py``
and ``run_app.py`` are thin wrappers whose import-time side effects (bytecode
suppression, storage directory creation) are what actually matter.
"""

import sys

import pytest
import run_sync_gravatar
from run_sync_gravatar import EXIT_ABORTED, EXIT_OK, EXIT_PARTIAL_FAILURE, main


@pytest.fixture
def sync_run(monkeypatch):
    """Capture the arguments run_gravatar_sync() is called with."""
    state = {"result": {"imported": 1, "failed": 0}, "kwargs": None}

    def fake(**kwargs):
        state["kwargs"] = kwargs
        return state["result"]

    monkeypatch.setattr(run_sync_gravatar, "run_gravatar_sync", fake)
    monkeypatch.setattr(
        run_sync_gravatar, "ensure_size_directories_existence", lambda: None
    )
    return state


def run_cli(monkeypatch, *argv) -> int:
    monkeypatch.setattr(sys, "argv", ["run_sync_gravatar.py", *argv])
    return main()


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def test_a_clean_run_exits_zero(monkeypatch, sync_run):
    assert run_cli(monkeypatch) == EXIT_OK


def test_a_run_with_failures_exits_one(monkeypatch, sync_run):
    sync_run["result"] = {"imported": 3, "failed": 2}
    assert run_cli(monkeypatch) == EXIT_PARTIAL_FAILURE


def test_a_run_that_never_started_exits_two(monkeypatch, sync_run):
    # An empty result means a lock was held, the user list failed, or Authentik
    # returned zero users - all "nothing happened", not "nothing to do".
    sync_run["result"] = {}
    assert run_cli(monkeypatch) == EXIT_ABORTED


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def test_the_default_run_is_active_users_only_without_webhooks(monkeypatch, sync_run):
    run_cli(monkeypatch)
    assert sync_run["kwargs"] == {
        "include_deactivated": False,
        "fire_webhooks_enabled": False,
        "request_delay_ms": 0,
    }


@pytest.mark.parametrize("flag", ["--include-deactivated", "--include-disabled"])
def test_both_spellings_enable_deactivated_users(monkeypatch, sync_run, flag):
    run_cli(monkeypatch, flag)
    assert sync_run["kwargs"]["include_deactivated"] is True


def test_webhooks_can_be_enabled_explicitly(monkeypatch, sync_run):
    run_cli(monkeypatch, "--fire-webhooks")
    assert sync_run["kwargs"]["fire_webhooks_enabled"] is True


def test_the_throttle_delay_is_passed_through(monkeypatch, sync_run):
    run_cli(monkeypatch, "--delay-ms", "250")
    assert sync_run["kwargs"]["request_delay_ms"] == 250


def test_an_unknown_flag_is_rejected(monkeypatch, sync_run):
    with pytest.raises(SystemExit) as excinfo:
        run_cli(monkeypatch, "--not-a-flag")
    assert excinfo.value.code == 2


def test_the_storage_tree_is_created_before_the_run(monkeypatch):
    # Run standalone (outside the Flask app) the directories may not exist yet.
    order = []
    monkeypatch.setattr(
        run_sync_gravatar,
        "ensure_size_directories_existence",
        lambda: order.append("mkdir"),
    )
    monkeypatch.setattr(
        run_sync_gravatar,
        "run_gravatar_sync",
        lambda **kw: order.append("sync") or {"failed": 0},
    )
    run_cli(monkeypatch)
    assert order == ["mkdir", "sync"]


# ---------------------------------------------------------------------------
# Cleanup entry point
# ---------------------------------------------------------------------------


def test_the_cleanup_entry_point_imports_cleanly():
    # The module is guarded by __main__, so importing it must not run cleanup.
    import run_cleanup

    assert run_cleanup.run_cleanup is not None
    assert run_cleanup.ensure_size_directories_existence is not None


def test_the_entry_points_suppress_bytecode_writes():
    # The container runs with a read-only root filesystem; a stray .pyc write
    # would fail at import time.
    import run_cleanup

    assert sys.dont_write_bytecode is True
    assert run_cleanup.os.environ["PYTHONDONTWRITEBYTECODE"] == "1"
