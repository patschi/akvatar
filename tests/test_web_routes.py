"""Tests for src/web_routes.py - the public pages and the upload API."""

import pytest

import src.upload as upload
import src.web_routes as web_routes
from src.config import img_formats, img_sizes
from src.imaging import AVATAR_ROOT
from tests.conftest import login
from tests.helpers import image_bytes, sse_events, upload_file


@pytest.fixture
def stub_backends(monkeypatch):
    """Stub the backend writes the upload pipeline performs."""
    state = {"patched": [], "webhooks": []}
    monkeypatch.setattr(
        upload,
        "update_avatar_url",
        lambda pk, url, avatar_id: (
            state["patched"].append((pk, url, avatar_id)),
            ({"ldap_uniq": "S-1-5-21"}, None, None),
        )[1],
    )
    monkeypatch.setattr(upload, "sync_ldap_photos", lambda *a: True)
    monkeypatch.setattr(
        upload, "fire_webhooks", lambda ctx: state["webhooks"].append(ctx)
    )
    return state


# ---------------------------------------------------------------------------
# Health and static-ish endpoints
# ---------------------------------------------------------------------------


def test_healthz_returns_plain_ok(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_data(as_text=True) == "OK"
    assert response.mimetype == "text/plain"


def test_healthz_needs_no_authentication(client):
    # Load balancers probe it without a session.
    assert client.get("/healthz").status_code == 200


def test_robots_txt_is_served_from_the_root(client):
    response = client.get("/robots.txt")
    assert response.status_code == 200
    assert b"User-agent" in response.get_data()


# ---------------------------------------------------------------------------
# Login page
# ---------------------------------------------------------------------------


def test_the_root_redirects_to_the_login_page(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_the_login_page_renders_for_anonymous_visitors(client):
    response = client.get("/login")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
    assert b"Test Avatars" in response.get_data()  # branding.name from config


def test_an_authenticated_visitor_is_sent_to_the_dashboard(authed_client):
    response = authed_client.get("/login")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")


@pytest.mark.parametrize("error_key", ["oidc_failed", "pk_failed", "session_expired"])
def test_known_error_keys_are_rendered(client, error_key):
    response = client.get(f"/login?error={error_key}")
    assert response.status_code == 200


def test_an_unknown_error_key_renders_no_error_message(client):
    # Only the three known keys are turned into a message; anything else is
    # dropped, so an attacker-supplied string can never reach the error slot.
    response = client.get("/login", query_string={"error": "not-a-real-key"})
    assert response.status_code == 200
    assert "result-error" not in response.get_data(as_text=True)


def test_a_reflected_error_key_is_escaped(client):
    # The og:url meta tag echoes request.url, so the payload does appear in the
    # page - it must be HTML-escaped and never rendered as markup.
    payload = "<script>alert(1)</script>"
    body = client.get("/login", query_string={"error": payload}).get_data(as_text=True)
    assert payload not in body
    assert "&lt;script&gt;" in body
    assert "result-error" not in body


def test_the_autologin_hint_redirects_straight_to_the_provider(client):
    response = client.get("/login?autologin")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login-start")


def test_autologin_is_ignored_when_an_error_is_being_shown(client):
    # Otherwise a failed login would bounce back into an endless redirect loop.
    response = client.get("/login?autologin&error=oidc_failed")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_the_dashboard_requires_authentication(client):
    response = client.get("/dashboard")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_the_dashboard_renders_for_an_authenticated_user(authed_client):
    response = authed_client.get("/dashboard")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Test User" in body
    assert "TU" in body  # initials


def test_the_dashboard_exposes_the_configured_upload_constraints(authed_client):
    body = authed_client.get("/dashboard").get_data(as_text=True)
    assert str(max(img_sizes)) in body
    for ext in img_formats:
        assert ext in body


def test_the_dashboard_carries_a_csrf_token_and_a_csp_nonce(authed_client):
    response = authed_client.get("/dashboard")
    body = response.get_data(as_text=True)
    assert "csrf" in body.lower()
    nonce = (
        response.headers["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
    )
    assert f'nonce="{nonce}"' in body


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------


def test_the_heartbeat_reports_a_live_session(authed_client):
    response = authed_client.get("/api/heartbeat")
    assert response.status_code == 200
    assert response.get_json() == {"alive": True}


def test_the_heartbeat_reports_json_401_not_an_html_redirect(client):
    # The dashboard polls this from JS, so it must never answer with a redirect.
    response = client.get("/api/heartbeat")
    assert response.status_code == 401
    assert response.get_json() == {"alive": False}


# ---------------------------------------------------------------------------
# Upload API - rejection paths
# ---------------------------------------------------------------------------


def test_uploading_requires_authentication(client):
    response = client.post("/api/upload")
    assert response.status_code == 302


def test_uploading_requires_a_csrf_token(authed_client):
    response = authed_client.post("/api/upload")
    assert response.status_code == 403
    assert response.get_json() == {"error": "csrf_failed"}


def test_a_request_without_a_file_part_is_rejected(authed_client, csrf_headers):
    response = authed_client.post("/api/upload", headers=csrf_headers, data={})
    assert response.status_code == 400
    assert "error" in response.get_json()


def test_an_invalid_image_is_rejected_before_the_stream_starts(
    authed_client, csrf_headers
):
    response = authed_client.post(
        "/api/upload",
        headers=csrf_headers,
        data={"file": upload_file(b"not an image at all", "evil.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert response.mimetype == "application/json"


def test_an_oversized_body_is_rejected_by_the_content_length_limit(
    authed_client, csrf_headers, flask_app
):
    # max_upload_size_mb is 2 in the test config.
    oversized = b"\x00" * (flask_app.config["MAX_CONTENT_LENGTH"] + 1024)
    response = authed_client.post(
        "/api/upload",
        headers=csrf_headers,
        data={"file": upload_file(oversized, "big.jpg")},
        content_type="multipart/form-data",
    )
    assert response.status_code == 413


# ---------------------------------------------------------------------------
# Upload API - success path
# ---------------------------------------------------------------------------


def do_upload(client, headers, data=None):
    """POST an image and fully consume the SSE stream.

    The response is a lazy generator - without reading the body the pipeline
    never runs and no files are written, so every caller needs the drained
    response anyway.
    """
    response = client.post(
        "/api/upload",
        headers=headers,
        data={"file": upload_file(data or image_bytes((300, 300), "JPEG"))},
        content_type="multipart/form-data",
    )
    response.get_data()  # drive the generator to completion
    return response


def test_a_valid_upload_streams_progress_as_server_sent_events(
    authed_client, csrf_headers, stub_backends
):
    response = do_upload(authed_client, csrf_headers)

    assert response.status_code == 200
    assert response.mimetype == "text/event-stream"
    assert response.headers["Cache-Control"] == "no-cache"
    # Disables proxy buffering so steps reach the browser as they happen.
    assert response.headers["X-Accel-Buffering"] == "no"

    events = sse_events(response.get_data())
    assert events[-1]["done"] is True
    assert events[-1]["avatar_url"].endswith(".jpg")


def test_a_valid_upload_produces_every_configured_file(
    authed_client, csrf_headers, stub_backends
):
    response = do_upload(authed_client, csrf_headers)
    avatar_url = sse_events(response.get_data())[-1]["avatar_url"]
    base = avatar_url.rsplit("/", 1)[-1].removesuffix(".jpg")

    for size in img_sizes:
        for ext in img_formats:
            assert (AVATAR_ROOT / f"{size}x{size}" / f"{base}.{ext}").is_file()


def test_the_pending_avatar_is_recorded_in_the_session_before_streaming(
    authed_client, csrf_headers, stub_backends
):
    # The cookie is committed before the generator runs, so the filename has to
    # be stored in the normal request cycle.
    do_upload(authed_client, csrf_headers)
    with authed_client.session_transaction() as sess:
        assert sess["_pending_avatar"]


# ---------------------------------------------------------------------------
# Upload commit
# ---------------------------------------------------------------------------


def test_committing_promotes_the_pending_avatar_into_the_session(
    authed_client, csrf_headers, stub_backends
):
    do_upload(authed_client, csrf_headers)

    response = authed_client.post("/api/upload/commit", headers=csrf_headers)

    assert response.status_code == 204
    with authed_client.session_transaction() as sess:
        assert sess["user"]["avatar"].endswith(".jpg")
        assert "_pending_avatar" not in sess


def test_committing_twice_is_rejected(authed_client, csrf_headers, stub_backends):
    do_upload(authed_client, csrf_headers)
    assert (
        authed_client.post("/api/upload/commit", headers=csrf_headers).status_code
        == 204
    )

    second = authed_client.post("/api/upload/commit", headers=csrf_headers)
    assert second.status_code == 400
    assert second.get_json() == {"error": "no_pending_avatar"}


def test_committing_without_an_upload_is_rejected(authed_client, csrf_headers):
    response = authed_client.post("/api/upload/commit", headers=csrf_headers)
    assert response.status_code == 400
    assert response.get_json() == {"error": "no_pending_avatar"}


def test_committing_a_rolled_back_upload_is_rejected(
    authed_client, csrf_headers, monkeypatch
):
    # The SSE rollback deletes the files after the pending entry is already in
    # the cookie; promoting it would publish a URL pointing at nothing.
    monkeypatch.setattr(
        upload,
        "update_avatar_url",
        lambda *a: (_ for _ in ()).throw(RuntimeError("backend down")),
    )
    do_upload(authed_client, csrf_headers)

    response = authed_client.post("/api/upload/commit", headers=csrf_headers)

    assert response.status_code == 400
    assert response.get_json() == {"error": "pending_avatar_missing"}


def test_committing_requires_authentication(client):
    assert client.post("/api/upload/commit").status_code == 302


def test_committing_requires_a_csrf_token(authed_client):
    assert authed_client.post("/api/upload/commit").status_code == 403


def test_a_forged_pending_filename_cannot_be_committed(authed_client, csrf_headers):
    # A client that sets its own _pending_avatar must not be able to point the
    # session avatar at an arbitrary (or nonexistent) file.
    with authed_client.session_transaction() as sess:
        sess["_pending_avatar"] = "../../etc/passwd"

    response = authed_client.post("/api/upload/commit", headers=csrf_headers)
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Upload cooldown
# ---------------------------------------------------------------------------


def test_the_upload_cooldown_returns_429_with_retry_after(
    authed_client, csrf_headers, monkeypatch
):
    monkeypatch.setattr(web_routes, "check_upload_cooldown", lambda pk: (False, 7))

    response = do_upload(authed_client, csrf_headers)

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "7"
    assert response.get_json()["retry_after"] == 7


def test_the_cooldown_is_checked_before_the_file_is_read(
    authed_client, csrf_headers, monkeypatch
):
    monkeypatch.setattr(web_routes, "check_upload_cooldown", lambda pk: (False, 7))
    monkeypatch.setattr(
        web_routes,
        "validate_upload",
        lambda _f: pytest.fail("validation must not run while rate limited"),
    )
    assert do_upload(authed_client, csrf_headers).status_code == 429


# ---------------------------------------------------------------------------
# Session isolation
# ---------------------------------------------------------------------------


def test_a_second_user_cannot_see_the_first_users_pending_upload(
    flask_app, stub_backends
):
    first = flask_app.test_client()
    first_token = login(first, {"pk": 1, "username": "one", "email": "", "name": ""})
    do_upload(first, {"X-CSRF-Token": first_token})

    second = flask_app.test_client()
    second_token = login(second, {"pk": 2, "username": "two", "email": "", "name": ""})

    response = second.post("/api/upload/commit", headers={"X-CSRF-Token": second_token})
    assert response.status_code == 400
