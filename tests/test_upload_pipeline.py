"""Tests for src/upload.py - the SSE upload pipeline.

The pipeline's contract is that every step reports its own status and that a
backend failure leaves nothing behind: the Authentik attribute is reverted and
the generated files are deleted.  A half-applied upload (files on disk, avatar
URL pointing at them, LDAP never updated) is the outcome these tests exist to
prevent.
"""

import json

import pytest

import src.upload as upload
from src.config import img_formats, img_sizes
from src.imaging import AVATAR_ROOT, METADATA_ROOT, generate_filename
from src.upload import (
    build_canonical_url,
    generate_sse,
    pending_avatar_file_exists,
)
from tests.helpers import make_image, sse_events

USER = {"pk": 42, "username": "testuser", "name": "Test User", "email": "t@example.com"}


@pytest.fixture
def backends(monkeypatch):
    """Stub Authentik, LDAP and webhook calls made by the pipeline."""
    state = {
        "patched": [],
        "reverted": [],
        "ldap": [],
        "webhooks": [],
        "ak_attrs": {"ldap_uniq": "S-1-5-21"},
        "ak_error": None,
        "ldap_error": None,
        "ldap_applied": True,
    }

    def fake_update(pk, url, avatar_id):
        if state["ak_error"]:
            raise state["ak_error"]
        state["patched"].append((pk, url, avatar_id))
        return state["ak_attrs"], "https://cdn/old.jpg", "old-id"

    def fake_ldap(image, urls, filename_base, ak_attrs, user_pk):
        if state["ldap_error"]:
            raise state["ldap_error"]
        state["ldap"].append(filename_base)
        return state["ldap_applied"]

    monkeypatch.setattr(upload, "update_avatar_url", fake_update)
    monkeypatch.setattr(
        upload, "revert_avatar_url", lambda *args: state["reverted"].append(args)
    )
    monkeypatch.setattr(upload, "sync_ldap_photos", fake_ldap)
    monkeypatch.setattr(
        upload, "fire_webhooks", lambda ctx: state["webhooks"].append(ctx)
    )
    return state


def run_pipeline(filename_base: str, image=None) -> list[dict]:
    """Drive the SSE generator to completion and return the decoded frames."""
    return [
        json.loads(frame[len("data: ") :])
        for frame in generate_sse(USER, image or make_image((300, 300)), filename_base)
    ]


def set_exists(base: str) -> bool:
    return any(
        (AVATAR_ROOT / f"{size}x{size}" / f"{base}.{ext}").exists()
        for size in img_sizes
        for ext in img_formats
    )


# ---------------------------------------------------------------------------
# Canonical URL helpers
# ---------------------------------------------------------------------------


def test_the_canonical_url_uses_the_authentik_size_and_format():
    from src.avatar_pipeline import CANONICAL_FORMAT, CANONICAL_SIZE_KEY
    from src.config import public_avatar_url

    url = build_canonical_url("abc")
    assert url == f"{public_avatar_url}/{CANONICAL_SIZE_KEY}/abc.{CANONICAL_FORMAT}"


def test_pending_file_detection_follows_the_filesystem(backends):
    base = generate_filename()
    assert pending_avatar_file_exists(base) is False
    run_pipeline(base)
    assert pending_avatar_file_exists(base) is True


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_a_successful_upload_reports_every_step_and_finishes(backends):
    base = generate_filename()
    events = run_pipeline(base)

    statuses = [event.get("status") for event in events if "status" in event]
    assert statuses == ["success"] * 5  # validated, prepare, processed, authentik, ldap
    assert events[-1]["done"] is True
    assert events[-1]["avatar_url"] == build_canonical_url(base)


def test_a_successful_upload_writes_all_files_and_the_metadata(backends):
    base = generate_filename()
    run_pipeline(base)

    assert set_exists(base)
    stored = json.loads((METADATA_ROOT / f"{base}.meta.json").read_text())
    assert stored["user_pk"] == 42
    # source="web" is what stops the Gravatar sync from ever overwriting it.
    assert stored["source"] == "web"


def test_a_successful_upload_pushes_the_canonical_url_to_authentik(backends):
    base = generate_filename()
    run_pipeline(base)
    assert backends["patched"] == [(42, build_canonical_url(base), base)]


def test_a_successful_upload_fires_the_webhooks(backends):
    base = generate_filename()
    run_pipeline(base)

    assert len(backends["webhooks"]) == 1
    context = backends["webhooks"][0]
    assert context["username"] == "testuser"
    assert context["avatar_id"] == base
    assert context["total_bytes"] > 0


def test_the_processed_step_reports_the_generated_geometry(backends):
    events = run_pipeline(generate_filename())
    processed = events[2]
    assert str(len(img_sizes)) in processed["detail"]
    assert str(len(img_formats)) in processed["detail"]


def test_the_dry_run_status_is_reported_instead_of_success(backends, monkeypatch):
    monkeypatch.setattr(upload, "skip_backend_writes", True)
    events = run_pipeline(generate_filename())
    statuses = [event.get("status") for event in events if "status" in event]
    # validated / prepare / processed stay real; the two backend steps are dry-run.
    assert statuses == ["success", "success", "success", "dry-run", "dry-run"]


def test_ldap_is_reported_as_skipped_for_authentik_only_users(backends):
    backends["ldap_applied"] = False
    events = run_pipeline(generate_filename())
    assert events[4]["status"] == "skipped"


def test_the_ldap_step_is_omitted_when_ldap_is_inactive(backends, monkeypatch):
    monkeypatch.setattr(upload, "LDAP_PHOTOS_ACTIVE", False)
    events = run_pipeline(generate_filename())
    statuses = [event.get("status") for event in events if "status" in event]
    assert len(statuses) == 4


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_an_authentik_failure_rolls_back_and_reports_an_error(backends):
    backends["ak_error"] = RuntimeError("Authentik rejected the PATCH")
    base = generate_filename()

    events = run_pipeline(base)

    assert events[3]["status"] == "failed"
    # The rollback itself is reported as its own successful step.
    assert events[-2]["status"] == "success"
    assert events[-1]["done"] is True and events[-1]["error"]
    assert not set_exists(base)
    assert not (METADATA_ROOT / f"{base}.meta.json").exists()
    # Nothing was written to Authentik, so nothing may be reverted.
    assert backends["reverted"] == []


def test_an_ldap_failure_reverts_the_authentik_attribute(backends):
    backends["ldap_error"] = RuntimeError("LDAP modify rejected")
    base = generate_filename()

    events = run_pipeline(base)

    assert events[4]["status"] == "failed"
    assert backends["reverted"] == [(42, "https://cdn/old.jpg", "old-id")]
    assert not set_exists(base)
    assert events[-1]["error"]


def test_no_revert_is_attempted_in_dry_run(backends, monkeypatch):
    monkeypatch.setattr(upload, "skip_backend_writes", True)
    backends["ldap_error"] = RuntimeError("LDAP modify rejected")

    run_pipeline(generate_filename())

    # The PATCH never happened, so there is nothing to undo.
    assert backends["reverted"] == []


def test_a_failing_revert_does_not_break_the_rollback(backends, monkeypatch, caplog):
    backends["ldap_error"] = RuntimeError("LDAP modify rejected")
    monkeypatch.setattr(
        upload,
        "revert_avatar_url",
        lambda *a: (_ for _ in ()).throw(RuntimeError("revert failed too")),
    )
    base = generate_filename()

    with caplog.at_level("ERROR", logger="upload"):
        events = run_pipeline(base)

    assert not set_exists(base)  # files are still cleaned up
    assert events[-1]["error"]
    assert "Failed to revert" in caplog.text


def test_no_webhook_fires_after_a_rollback(backends):
    backends["ak_error"] = RuntimeError("boom")
    run_pipeline(generate_filename())
    assert backends["webhooks"] == []


def test_a_non_dict_authentik_response_is_treated_as_a_failure(backends, monkeypatch):
    monkeypatch.setattr(
        upload, "update_avatar_url", lambda *a: ("not-a-dict", None, None)
    )
    events = run_pipeline(generate_filename())
    assert events[3]["status"] == "failed"


def test_an_unexpected_error_is_reported_without_leaking_internals(
    backends, monkeypatch, caplog
):
    def exploding_process(_image, _base):
        raise RuntimeError("secret internal detail: /data/config/config.yml")

    monkeypatch.setattr(upload, "process_image", exploding_process)
    base = generate_filename()

    with caplog.at_level("ERROR", logger="upload"):
        events = run_pipeline(base)

    assert events[-1] == {"done": True, "error": "contact_admin"}
    body = json.dumps(events)
    assert "secret internal detail" not in body
    assert not set_exists(base)


def test_an_empty_processing_result_is_treated_as_a_failure(backends, monkeypatch):
    monkeypatch.setattr(upload, "process_image", lambda _i, _b: ({}, 0))
    events = run_pipeline(generate_filename())
    assert events[-1]["error"] == "contact_admin"


# ---------------------------------------------------------------------------
# Frame format
# ---------------------------------------------------------------------------


def test_every_frame_is_a_well_formed_sse_data_line(backends):
    raw = "".join(generate_sse(USER, make_image((300, 300)), generate_filename()))
    assert raw.endswith("\n\n")
    for frame in raw.split("\n\n"):
        if frame:
            assert frame.startswith("data: ")
    assert len(sse_events(raw)) >= 6
