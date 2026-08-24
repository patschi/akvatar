"""Tests for src/gravatar_sync.py - the bulk Gravatar backfill engine.

The value of this module is entirely in its per-user decision matrix: which
users get imported, which get updated, and - most importantly - which must be
left alone.  Overwriting an avatar a user deliberately set (or re-adding one
they deliberately removed) is the failure this suite is built to catch.
"""

import hashlib
import json

import pytest

import src.gravatar_sync as sync
from src.config import ak_avatar_id_attribute, img_sizes
from src.gravatar_sync import (
    FETCH_SIZE,
    HASH_PROBE_SIZE,
    _AvatarChangedConcurrently,
    _probe_hash,
    _process_user,
    run_gravatar_sync,
)
from src.image_import import FetchFailed, GravatarNotFound
from src.imaging import AVATAR_ROOT, METADATA_ROOT
from tests.helpers import image_bytes

GRAVATAR_JPEG = image_bytes((300, 300), "JPEG")
GRAVATAR_PROBE = image_bytes((HASH_PROBE_SIZE, HASH_PROBE_SIZE), "JPEG")
PROBE_HASH = hashlib.sha256(GRAVATAR_PROBE).hexdigest()


def empty_counts() -> dict[str, int]:
    return {
        "imported": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped_custom": 0,
        "skipped_removed": 0,
        "no_gravatar": 0,
        "unsupported": 0,
        "no_email": 0,
        "failed": 0,
    }


def user(pk=1, email="user@example.com", avatar_id=None, **overrides):
    attributes = dict(overrides.pop("attributes", {}))
    if avatar_id is not None:
        attributes[ak_avatar_id_attribute] = avatar_id
    record = {
        "pk": pk,
        "username": f"user{pk}",
        "name": f"User {pk}",
        "email": email,
        "is_active": True,
        "attributes": attributes,
    }
    record.update(overrides)
    return record


@pytest.fixture
def gravatar(monkeypatch):
    """Stub the Gravatar fetch and record the sizes requested."""
    state = {
        "probe": (GRAVATAR_PROBE, "hash.jpg"),
        "full": (GRAVATAR_JPEG, "avatar.jpg"),
        "requested_sizes": [],
        "content_type": "image/jpeg",
    }

    def fake_fetch(email, size=1024):
        state["requested_sizes"].append(size)
        if isinstance(state.get("raise"), Exception):
            raise state["raise"]
        if size == HASH_PROBE_SIZE:
            payload = state["probe"]
        else:
            payload = state["full"]
        if payload is None:
            raise GravatarNotFound()
        data, filename = payload
        return data, state["content_type"], filename

    monkeypatch.setattr(sync, "fetch_gravatar_image", fake_fetch)
    return state


@pytest.fixture
def backends(monkeypatch):
    """Stub every backend write the publish step performs."""
    state = {"live_avatar_id": None, "patched": [], "ldap": [], "webhooks": []}

    monkeypatch.setattr(
        sync,
        "get_user",
        lambda pk: {"attributes": {ak_avatar_id_attribute: state["live_avatar_id"]}},
    )

    def fake_update(pk, url, avatar_id):
        state["patched"].append((pk, url, avatar_id))
        return {"ldap_uniq": "S-1-5-21"}, None, None

    monkeypatch.setattr(sync, "update_avatar_url", fake_update)
    monkeypatch.setattr(
        sync, "revert_avatar_url", lambda *a: state.setdefault("reverted", True)
    )
    monkeypatch.setattr(
        sync, "sync_ldap_photos", lambda *a: state["ldap"].append(a) or True
    )
    monkeypatch.setattr(
        sync, "fire_webhooks", lambda ctx: state["webhooks"].append(ctx)
    )
    return state


def write_meta(filename: str, user_pk: int, source: str, gravatar_hash=None) -> None:
    payload = {"filename": filename, "user_pk": user_pk, "source": source}
    if gravatar_hash is not None:
        payload["gravatar_hash"] = gravatar_hash
    (METADATA_ROOT / f"{filename}.meta.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Fetch sizing
# ---------------------------------------------------------------------------


def test_the_full_fetch_asks_for_the_largest_configured_size():
    # Fetching smaller than the largest output would force an upscale.
    assert FETCH_SIZE == min(2048, max(img_sizes))


def test_the_probe_is_a_small_fixed_size_independent_of_config():
    # Re-checking thousands of unchanged users must cost a few KB each, not a
    # full-size download.  The size is deliberately a constant: deriving it from
    # images.sizes would invalidate every stored hash whenever sizes change.
    assert HASH_PROBE_SIZE == 80
    assert HASH_PROBE_SIZE < FETCH_SIZE


def test_the_probe_hash_is_the_sha256_of_the_probe_bytes(gravatar):
    assert _probe_hash("user@example.com") == PROBE_HASH
    assert gravatar["requested_sizes"] == [HASH_PROBE_SIZE]


def test_the_probe_hash_is_none_when_the_user_has_no_gravatar(gravatar):
    gravatar["probe"] = None
    assert _probe_hash("user@example.com") is None


# ---------------------------------------------------------------------------
# Decision matrix
# ---------------------------------------------------------------------------


def test_a_user_without_an_email_is_skipped(gravatar):
    counts = empty_counts()
    fetched = _process_user(user(email=""), {}, set(), False, counts)

    assert fetched is False  # no Gravatar request was made
    assert counts["no_email"] == 1
    assert gravatar["requested_sizes"] == []


def test_a_user_set_avatar_is_never_overwritten(gravatar):
    counts = empty_counts()
    # Metadata says "web": this avatar belongs to the user, not to the job.
    meta = {"user-avatar": {"filename": "user-avatar", "source": "web"}}

    fetched = _process_user(user(avatar_id="user-avatar"), meta, {1}, False, counts)

    assert fetched is False
    assert counts["skipped_custom"] == 1
    assert gravatar["requested_sizes"] == []


def test_an_avatar_with_no_metadata_at_all_is_treated_as_user_set(gravatar):
    counts = empty_counts()
    _process_user(user(avatar_id="unknown-to-us"), {}, set(), False, counts)
    assert counts["skipped_custom"] == 1


def test_a_user_who_removed_their_avatar_is_not_refilled(gravatar):
    counts = empty_counts()
    # No current avatar, but prior metadata exists for this PK.
    fetched = _process_user(user(pk=5), {}, {5}, False, counts)

    assert fetched is False
    assert counts["skipped_removed"] == 1
    assert gravatar["requested_sizes"] == []


def test_a_first_time_user_is_imported(gravatar, backends):
    counts = empty_counts()
    _process_user(user(pk=5), {}, set(), False, counts)

    assert counts["imported"] == 1
    assert backends["patched"][0][0] == 5
    # Probe first, then the full-size fetch.
    assert gravatar["requested_sizes"] == [HASH_PROBE_SIZE, FETCH_SIZE]


def test_a_sync_owned_avatar_with_an_unchanged_gravatar_is_left_alone(gravatar):
    counts = empty_counts()
    meta = {
        "sync-avatar": {
            "filename": "sync-avatar",
            "source": "gravatar_sync",
            "gravatar_hash": PROBE_HASH,
        }
    }
    _process_user(user(avatar_id="sync-avatar"), meta, {1}, False, counts)

    assert counts["unchanged"] == 1
    # Only the cheap probe was fetched - no full-size download.
    assert gravatar["requested_sizes"] == [HASH_PROBE_SIZE]


def test_a_sync_owned_avatar_is_updated_when_the_gravatar_changed(gravatar, backends):
    counts = empty_counts()
    backends["live_avatar_id"] = "sync-avatar"
    meta = {
        "sync-avatar": {
            "filename": "sync-avatar",
            "source": "gravatar_sync",
            "gravatar_hash": "a-different-old-hash",
        }
    }
    _process_user(user(avatar_id="sync-avatar"), meta, {1}, False, counts)

    assert counts["updated"] == 1
    assert gravatar["requested_sizes"] == [HASH_PROBE_SIZE, FETCH_SIZE]


def test_a_user_without_a_gravatar_is_counted_and_skipped(gravatar):
    gravatar["probe"] = None
    counts = empty_counts()
    _process_user(user(), {}, set(), False, counts)
    assert counts["no_gravatar"] == 1


def test_a_deleted_gravatar_never_removes_the_existing_avatar(gravatar, caplog):
    gravatar["probe"] = None
    counts = empty_counts()
    meta = {"sync-avatar": {"filename": "sync-avatar", "source": "gravatar_sync"}}

    with caplog.at_level("INFO", logger="grav_sync"):
        _process_user(user(avatar_id="sync-avatar"), meta, {1}, False, counts)

    assert counts["no_gravatar"] == 1
    assert "keeping current avatar" in caplog.text


def test_an_unsupported_gravatar_format_is_skipped(gravatar):
    # GIF passes the proxy MIME allowlist but is not an accepted upload format.
    gravatar["content_type"] = "image/gif"
    counts = empty_counts()
    _process_user(user(), {}, set(), False, counts)
    assert counts["unsupported"] == 1


def test_a_gravatar_fetch_failure_is_counted_as_a_failure(gravatar):
    gravatar["raise"] = FetchFailed("connection reset")
    counts = empty_counts()
    _process_user(user(), {}, set(), False, counts)
    assert counts["failed"] == 1


def test_an_empty_avatar_id_attribute_counts_as_no_avatar(gravatar, backends):
    counts = empty_counts()
    _process_user(user(pk=5, avatar_id=""), {}, set(), False, counts)
    assert counts["imported"] == 1


def test_a_non_string_avatar_id_attribute_counts_as_no_avatar(gravatar, backends):
    counts = empty_counts()
    _process_user(user(pk=5, avatar_id=12345), {}, set(), False, counts)
    assert counts["imported"] == 1


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------


def test_publishing_writes_files_metadata_and_the_backend_attribute(gravatar, backends):
    counts = empty_counts()
    _process_user(user(pk=5), {}, set(), False, counts)

    stored = json.loads(
        next(METADATA_ROOT.glob("*.meta.json")).read_text(encoding="utf-8")
    )
    assert stored["user_pk"] == 5
    assert stored["source"] == "gravatar_sync"
    # The stored hash is the probe hash, so it stays valid when images.sizes changes.
    assert stored["gravatar_hash"] == PROBE_HASH

    filename_base = stored["filename"]
    assert (AVATAR_ROOT / "256x256" / f"{filename_base}.jpg").is_file()
    assert backends["patched"][0][2] == filename_base


def test_an_avatar_set_during_the_run_is_not_overwritten(gravatar, backends, caplog):
    # The user list is a snapshot; a web upload may land mid-run.
    counts = empty_counts()
    backends["live_avatar_id"] = "set-by-the-user-just-now"
    meta = {
        "sync-avatar": {
            "filename": "sync-avatar",
            "source": "gravatar_sync",
            "gravatar_hash": "old",
        }
    }

    with caplog.at_level("INFO", logger="grav_sync"):
        _process_user(user(avatar_id="sync-avatar"), meta, {1}, False, counts)

    assert counts["skipped_custom"] == 1
    assert backends["patched"] == []
    assert "changed their avatar during this run" in caplog.text
    assert _AvatarChangedConcurrently  # the guard exception is the mechanism


def test_a_backend_failure_rolls_back_the_generated_files(
    gravatar, backends, monkeypatch
):
    def failing_patch(pk, url, avatar_id):
        raise RuntimeError("Authentik rejected the write")

    monkeypatch.setattr(sync, "update_avatar_url", failing_patch)
    counts = empty_counts()
    _process_user(user(pk=5), {}, set(), False, counts)

    assert counts["failed"] == 1
    # No files and no metadata left behind.
    assert list(METADATA_ROOT.glob("*.meta.json")) == []
    assert list((AVATAR_ROOT / "256x256").glob("*.jpg")) == []


def test_invalid_gravatar_bytes_are_reported_without_a_traceback(
    gravatar, backends, caplog
):
    gravatar["full"] = (b"\xff\xd8\xff" + b"not really a jpeg", "avatar.jpg")
    counts = empty_counts()

    with caplog.at_level("WARNING", logger="grav_sync"):
        _process_user(user(pk=5), {}, set(), False, counts)

    assert counts["failed"] == 1
    assert "rejected by validation" in caplog.text
    assert "Traceback" not in caplog.text


def test_webhooks_are_off_by_default_for_bulk_runs(gravatar, backends):
    counts = empty_counts()
    _process_user(user(pk=5), {}, set(), False, counts)
    assert backends["webhooks"] == []


def test_webhooks_fire_when_explicitly_requested(gravatar, backends):
    counts = empty_counts()
    _process_user(user(pk=5), {}, set(), True, counts)
    assert len(backends["webhooks"]) == 1
    assert backends["webhooks"][0]["username"] == "user5"


def test_full_dry_run_writes_nothing(gravatar, backends, monkeypatch, caplog):
    monkeypatch.setattr(sync, "dry_run", True)
    counts = empty_counts()

    with caplog.at_level("INFO", logger="grav_sync"):
        _process_user(user(pk=5), {}, set(), False, counts)

    assert counts["imported"] == 1
    assert backends["patched"] == []
    assert list(METADATA_ROOT.glob("*.meta.json")) == []
    assert "[DRY-RUN] Would import" in caplog.text


def test_backend_dry_run_generates_files_but_writes_no_metadata(
    gravatar, backends, monkeypatch, caplog
):
    # Metadata without a matching Authentik avatar_id would make the next real
    # run believe the user removed their avatar and skip them forever.
    monkeypatch.setattr(sync, "skip_backend_writes", True)
    counts = empty_counts()

    with caplog.at_level("INFO", logger="grav_sync"):
        _process_user(user(pk=5), {}, set(), False, counts)

    assert counts["imported"] == 1
    assert list(METADATA_ROOT.glob("*.meta.json")) == []
    assert list((AVATAR_ROOT / "256x256").glob("*.jpg"))  # files were generated
    assert "metadata not written" in caplog.text


# ---------------------------------------------------------------------------
# Full run orchestration
# ---------------------------------------------------------------------------


@pytest.fixture
def user_list(monkeypatch):
    """Control the user list run_gravatar_sync() iterates over."""
    state = {"users": []}
    monkeypatch.setattr(
        sync, "list_users", lambda active_only=False: list(state["users"])
    )
    return state


def test_a_run_processes_every_user_and_returns_counts(user_list, gravatar, backends):
    user_list["users"] = [user(pk=1, email="a@example.com"), user(pk=2, email="")]

    counts = run_gravatar_sync()

    assert counts["imported"] == 1
    assert counts["no_email"] == 1


def test_an_empty_user_list_aborts_the_run(user_list, caplog):
    user_list["users"] = []
    with caplog.at_level("WARNING", logger="grav_sync"):
        assert run_gravatar_sync() == {}
    assert "zero users" in caplog.text


def test_a_user_list_failure_aborts_the_run(monkeypatch, caplog):
    def boom(active_only=False):
        raise ConnectionError("Authentik unreachable")

    monkeypatch.setattr(sync, "list_users", boom)
    with caplog.at_level("ERROR", logger="grav_sync"):
        assert run_gravatar_sync() == {}
    assert "Failed to fetch users" in caplog.text


def test_include_deactivated_controls_the_user_query(monkeypatch, gravatar, backends):
    seen = {}

    def fake_list(active_only=False):
        seen["active_only"] = active_only
        return [user(pk=1)]

    monkeypatch.setattr(sync, "list_users", fake_list)

    run_gravatar_sync(include_deactivated=False)
    assert seen["active_only"] is True

    run_gravatar_sync(include_deactivated=True)
    assert seen["active_only"] is False


def test_one_user_raising_never_aborts_the_run(
    user_list, gravatar, backends, monkeypatch
):
    user_list["users"] = [user(pk=1), user(pk=2), user(pk=3)]
    seen = []
    real_process = sync._process_user

    def flaky(u, *args):
        seen.append(u["pk"])
        if u["pk"] == 2:
            raise RuntimeError("unexpected")
        return real_process(u, *args)

    monkeypatch.setattr(sync, "_process_user", flaky)
    counts = run_gravatar_sync()

    assert seen == [1, 2, 3]
    assert counts["failed"] == 1
    assert counts["imported"] == 2


def test_a_run_stands_down_while_cleanup_holds_its_lock(user_list, caplog):
    from src.avatar_pipeline import CLEANUP_LOCKFILE, exclusive_process_lock

    user_list["users"] = [user(pk=1)]
    with exclusive_process_lock(CLEANUP_LOCKFILE, "Cleanup") as held:
        assert held
        with caplog.at_level("WARNING", logger="grav_sync"):
            assert run_gravatar_sync() == {}
    assert "a cleanup run is in progress" in caplog.text


def test_a_second_sync_stands_down(user_list):
    from src.avatar_pipeline import GRAVATAR_SYNC_LOCKFILE, exclusive_process_lock

    user_list["users"] = [user(pk=1)]
    with exclusive_process_lock(GRAVATAR_SYNC_LOCKFILE, "Gravatar sync") as held:
        assert held
        assert run_gravatar_sync() == {}


def test_the_throttle_delay_is_applied_between_fetching_users(
    user_list, gravatar, backends, monkeypatch
):
    sleeps = []
    monkeypatch.setattr(sync.time, "sleep", lambda seconds: sleeps.append(seconds))
    user_list["users"] = [user(pk=1), user(pk=2), user(pk=3, email="")]

    run_gravatar_sync(request_delay_ms=250)

    # Only users that actually hit Gravatar cost idle time, and never after the last.
    assert sleeps == [0.25, 0.25]


def test_no_delay_is_applied_when_the_throttle_is_off(
    user_list, gravatar, backends, monkeypatch
):
    monkeypatch.setattr(
        sync.time, "sleep", lambda _s: pytest.fail("no throttle expected")
    )
    user_list["users"] = [user(pk=1), user(pk=2)]
    run_gravatar_sync(request_delay_ms=0)


def test_the_summary_reports_every_outcome_bucket(
    user_list, gravatar, backends, caplog
):
    user_list["users"] = [user(pk=1)]
    with caplog.at_level("INFO", logger="grav_sync"):
        run_gravatar_sync()
    assert "Gravatar sync complete" in caplog.text
    assert "1 imported" in caplog.text


def test_pending_webhook_deliveries_are_drained_before_returning(
    user_list, gravatar, backends, monkeypatch
):
    # The CLI process exits right after this returns; abandoning in-flight
    # deliveries would silently drop notifications.
    drained = []
    monkeypatch.setattr(
        sync, "wait_for_pending_deliveries", lambda timeout: drained.append(timeout)
    )
    user_list["users"] = [user(pk=1)]

    run_gravatar_sync(fire_webhooks_enabled=True)

    assert drained == [sync._WEBHOOK_DRAIN_TIMEOUT_S]


def test_existing_metadata_is_indexed_for_ownership_and_removal_checks(
    user_list, gravatar, backends
):
    # A sync-owned avatar for pk=1 and a removed avatar for pk=2.
    write_meta("sync-avatar", 1, "gravatar_sync", gravatar_hash=PROBE_HASH)
    write_meta("removed-avatar", 2, "web")
    user_list["users"] = [user(pk=1, avatar_id="sync-avatar"), user(pk=2)]

    counts = run_gravatar_sync()

    assert counts["unchanged"] == 1  # pk=1: job-owned, hash matches
    assert counts["skipped_removed"] == 1  # pk=2: had one, removed it
