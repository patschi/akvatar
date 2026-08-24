"""Tests for src/avatar_pipeline.py - the shared publish building blocks.

These helpers are the reason the interactive upload and the background Gravatar
sync cannot drift: both resolve the canonical URL, build LDAP writes, and shape
the metadata sidecar through this module.
"""

import json
import os
import time

import pytest

import src.avatar_pipeline as pipeline
from src.avatar_pipeline import (
    CANONICAL_FORMAT,
    CANONICAL_SIZE_KEY,
    build_ldap_updates,
    build_webhook_context,
    exclusive_process_lock,
    resolve_canonical_url,
    save_avatar_metadata,
    sync_ldap_photos,
)
from src.config import (
    WEBHOOK_PLACEHOLDERS,
    ak_avatar_ext,
    ak_avatar_size,
    img_formats,
    img_sizes,
)
from src.imaging import METADATA_ROOT, generate_filename, process_image
from tests.helpers import make_image


def urls_for(base: str) -> dict:
    """Generate a real avatar set and return its URL map."""
    urls, _total = process_image(make_image((300, 300)), base)
    return urls


# ---------------------------------------------------------------------------
# Canonical URL
# ---------------------------------------------------------------------------


def test_canonical_identifiers_come_from_the_authentik_config():
    assert CANONICAL_SIZE_KEY == f"{ak_avatar_size}x{ak_avatar_size}"
    assert CANONICAL_FORMAT == ak_avatar_ext


def test_resolve_canonical_url_picks_the_authentik_size_and_format():
    base = generate_filename()
    urls = urls_for(base)
    assert resolve_canonical_url(urls) == urls[CANONICAL_SIZE_KEY][CANONICAL_FORMAT]


def test_resolve_canonical_url_raises_when_the_expected_output_is_missing():
    with pytest.raises(RuntimeError, match="Canonical avatar URL not found"):
        resolve_canonical_url({"1x1": {"jpg": "https://x/1x1/a.jpg"}})


def test_resolve_canonical_url_raises_on_an_empty_map():
    with pytest.raises(RuntimeError):
        resolve_canonical_url({})


# ---------------------------------------------------------------------------
# LDAP update construction
# ---------------------------------------------------------------------------


def test_ldap_updates_cover_every_configured_photo_attribute():
    base = generate_filename()
    urls = urls_for(base)

    updates = build_ldap_updates(make_image((300, 300)), urls, base)

    assert [u["attribute"] for u in updates] == ["thumbnailPhoto", "photoURL"]
    # binary -> encoded image bytes, url -> the pre-generated public URL
    assert isinstance(updates[0]["value"], bytes)
    assert updates[1]["value"] == urls["64x64"]["webp"]


def test_ldap_url_entry_raises_when_the_referenced_output_does_not_exist():
    with pytest.raises(ValueError, match="No pre-generated URL"):
        build_ldap_updates(make_image((300, 300)), {}, generate_filename())


def test_unknown_ldap_photo_types_are_skipped_with_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(
        pipeline,
        "_LDAP_PHOTOS",
        [
            {
                "attribute": "x",
                "type": "carrier-pigeon",
                "image_size": 64,
                "image_type": "jpeg",
            }
        ],
    )
    with caplog.at_level("WARNING", logger="avatar_pipeline"):
        assert build_ldap_updates(make_image((100, 100)), {}, "base") == []
    assert "Unknown LDAP photo type" in caplog.text


# ---------------------------------------------------------------------------
# LDAP publish gate
# ---------------------------------------------------------------------------


def test_ldap_sync_is_skipped_for_a_user_without_ldap_uniq(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pipeline, "update_ldap_photos", lambda *args: calls.append(args)
    )
    applied = sync_ldap_photos(make_image((300, 300)), {}, "base", {}, 42)
    assert applied is False
    assert calls == []


def test_ldap_sync_applies_updates_for_a_user_with_ldap_uniq(monkeypatch):
    calls = []
    monkeypatch.setattr(
        pipeline, "update_ldap_photos", lambda *args: calls.append(args)
    )
    base = generate_filename()
    urls = urls_for(base)

    applied = sync_ldap_photos(
        make_image((300, 300)), urls, base, {"ldap_uniq": "S-1-5-21"}, 42
    )

    assert applied is True
    ldap_uniq, updates = calls[0]
    assert ldap_uniq == "S-1-5-21"
    assert [u["attribute"] for u in updates] == ["thumbnailPhoto", "photoURL"]


def test_ldap_sync_is_skipped_entirely_when_ldap_is_inactive(monkeypatch):
    monkeypatch.setattr(pipeline, "LDAP_PHOTOS_ACTIVE", False)
    monkeypatch.setattr(
        pipeline,
        "update_ldap_photos",
        lambda *args: pytest.fail("LDAP must not be contacted when inactive"),
    )
    assert (
        sync_ldap_photos(make_image((100, 100)), {}, "b", {"ldap_uniq": "x"}, 1)
        is False
    )


def test_ldap_sync_tolerates_a_non_dict_attributes_payload(monkeypatch):
    monkeypatch.setattr(
        pipeline,
        "update_ldap_photos",
        lambda *args: pytest.fail("must not reach LDAP"),
    )
    assert sync_ldap_photos(make_image((100, 100)), {}, "b", None, 1) is False


# ---------------------------------------------------------------------------
# Metadata sidecar
# ---------------------------------------------------------------------------


def test_metadata_records_ownership_and_the_generated_geometry():
    base = generate_filename()
    save_avatar_metadata(base, 42, 9999)

    stored = json.loads((METADATA_ROOT / f"{base}.meta.json").read_text())
    assert stored["filename"] == base
    assert stored["user_pk"] == 42
    assert stored["total_bytes"] == 9999
    assert stored["sizes"] == img_sizes
    assert stored["formats"] == img_formats
    assert stored["source"] == "web"
    assert stored["uploaded_at"].endswith("+00:00")
    # A web-set avatar carries no Gravatar hash - that is what tells the sync
    # job it must never overwrite it.
    assert "gravatar_hash" not in stored


def test_metadata_records_the_gravatar_hash_for_sync_owned_avatars():
    base = generate_filename()
    save_avatar_metadata(base, 42, 1, source="gravatar_sync", gravatar_hash="deadbeef")

    stored = json.loads((METADATA_ROOT / f"{base}.meta.json").read_text())
    assert stored["source"] == "gravatar_sync"
    assert stored["gravatar_hash"] == "deadbeef"


def test_metadata_is_published_atomically():
    # The temp file must be renamed into place, never left behind for the
    # cleanup job (or a concurrent reader) to trip over.
    base = generate_filename()
    save_avatar_metadata(base, 42, 1)
    assert list(METADATA_ROOT.glob("*.tmp")) == []


def test_rewriting_metadata_replaces_the_previous_record():
    base = generate_filename()
    save_avatar_metadata(base, 42, 1)
    save_avatar_metadata(base, 99, 2)
    stored = json.loads((METADATA_ROOT / f"{base}.meta.json").read_text())
    assert (stored["user_pk"], stored["total_bytes"]) == (99, 2)


# ---------------------------------------------------------------------------
# Webhook context
# ---------------------------------------------------------------------------


def test_webhook_context_supplies_exactly_the_documented_placeholders():
    context = build_webhook_context(
        {"pk": 42, "username": "u", "name": "N", "email": "e@x"},
        "https://cdn/x.jpg",
        "base",
        123,
    )
    assert set(context) == set(WEBHOOK_PLACEHOLDERS)


def test_webhook_context_normalizes_missing_optional_user_fields():
    context = build_webhook_context({"pk": 1, "username": "u"}, "url", "base", 0)
    assert context["name"] == ""
    assert context["email"] == ""


def test_webhook_context_normalizes_null_optional_user_fields():
    context = build_webhook_context(
        {"pk": 1, "username": "u", "name": None, "email": None}, "url", "base", 0
    )
    assert (context["name"], context["email"]) == ("", "")


# ---------------------------------------------------------------------------
# Cross-process lock
# ---------------------------------------------------------------------------


def test_the_lock_is_granted_when_nothing_else_holds_it(tmp_path):
    with exclusive_process_lock(tmp_path / "job.lock", "Job") as acquired:
        assert acquired is True


def test_a_second_holder_is_refused_rather_than_queued(tmp_path, caplog):
    lockfile = tmp_path / "job.lock"
    # flock is tied to the open file description, so a second open() of the same
    # path conflicts even inside one process - exactly what a second job does.
    with exclusive_process_lock(lockfile, "Job") as first:
        assert first is True
        with caplog.at_level("WARNING", logger="avatar_pipeline"):
            with exclusive_process_lock(lockfile, "Job") as second:
                assert second is False
    assert "already in progress" in caplog.text


def test_the_lock_is_released_after_the_block_exits(tmp_path):
    lockfile = tmp_path / "job.lock"
    with exclusive_process_lock(lockfile, "Job"):
        pass
    with exclusive_process_lock(lockfile, "Job") as acquired:
        assert acquired is True


def test_the_lock_is_released_even_when_the_block_raises(tmp_path):
    lockfile = tmp_path / "job.lock"
    with pytest.raises(RuntimeError):
        with exclusive_process_lock(lockfile, "Job"):
            raise RuntimeError("boom")
    with exclusive_process_lock(lockfile, "Job") as acquired:
        assert acquired is True


def test_an_unopenable_lockfile_yields_false_instead_of_raising(tmp_path, caplog):
    # A directory can never be opened for writing - stands in for an
    # unwritable/misconfigured storage path.
    unopenable = tmp_path / "as-a-directory"
    unopenable.mkdir()
    with caplog.at_level("WARNING", logger="avatar_pipeline"):
        with exclusive_process_lock(unopenable, "Job") as acquired:
            assert acquired is False
    assert "Could not open" in caplog.text


def test_the_job_lockfiles_live_under_the_avatar_root():
    # Every process (web app, run_cleanup.py, run_sync_gravatar.py) must see the
    # same files regardless of its working directory.
    from src.imaging import AVATAR_ROOT

    assert pipeline.CLEANUP_LOCKFILE.parent == AVATAR_ROOT
    assert pipeline.GRAVATAR_SYNC_LOCKFILE.parent == AVATAR_ROOT
    assert os.path.isabs(pipeline.CLEANUP_LOCKFILE)


def test_metadata_timestamps_move_forward_between_writes():
    first = generate_filename()
    save_avatar_metadata(first, 1, 1)
    time.sleep(0.01)
    second = generate_filename()
    save_avatar_metadata(second, 1, 1)

    stamps = [
        json.loads((METADATA_ROOT / f"{name}.meta.json").read_text())["uploaded_at"]
        for name in (first, second)
    ]
    # Retention sorts sets by this field lexicographically, so it must be
    # monotonic and ISO-8601 formatted.
    assert stamps[0] < stamps[1]
