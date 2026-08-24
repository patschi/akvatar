"""Tests for src/web_image_import.py and src/web_reset_avatar.py.

The import endpoints proxy an outbound HTTP request on behalf of a signed-in
user, so every rejection path (feature disabled, wrong email, blocked URL,
oversized body, wrong content type, upstream failure) needs to map to a stable
status code the browser JS can act on.
"""

import pytest

import src.web_image_import as web_import
import src.web_reset_avatar as web_reset
from src.image_import import (
    FetchFailed,
    GravatarNotFound,
    ImageTooLarge,
    UnsupportedContentType,
)
from tests.helpers import image_bytes

GRAVATAR_BYTES = image_bytes((256, 256), "JPEG")


@pytest.fixture
def stub_gravatar(monkeypatch):
    """Replace the Gravatar fetch with a scripted stub."""
    state = {"result": (GRAVATAR_BYTES, "image/jpeg", "hash.jpg"), "raise": None}

    def fake(email, size=1024):
        if state["raise"]:
            raise state["raise"]
        return state["result"]

    monkeypatch.setattr(web_import, "fetch_gravatar_image", fake)
    return state


@pytest.fixture
def stub_url_fetch(monkeypatch):
    """Replace the remote URL fetch with a scripted stub."""
    state = {"result": (GRAVATAR_BYTES, "image/jpeg"), "raise": None}

    def fake(url):
        if state["raise"]:
            raise state["raise"]
        return state["result"]

    monkeypatch.setattr(web_import, "fetch_remote_image", fake)
    # The route validates the URL before fetching; allow public-looking hosts.
    monkeypatch.setattr(web_import, "validate_import_url", lambda _url: None)
    return state


# ---------------------------------------------------------------------------
# Gravatar import
# ---------------------------------------------------------------------------


def test_gravatar_import_requires_authentication(client):
    assert (
        client.post("/api/fetch-gravatar", json={"email": "a@b.com"}).status_code == 302
    )


def test_gravatar_import_requires_a_csrf_token(authed_client):
    response = authed_client.post("/api/fetch-gravatar", json={"email": "a@b.com"})
    assert response.status_code == 403
    assert response.get_json() == {"error": "csrf_failed"}


def test_gravatar_import_proxies_the_image(authed_client, csrf_headers, stub_gravatar):
    response = authed_client.post(
        "/api/fetch-gravatar",
        headers=csrf_headers,
        json={"email": "test.user@example.com"},
    )
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.get_data() == GRAVATAR_BYTES
    # Proxied bytes must never be cached by the browser or an intermediary.
    assert response.headers["Cache-Control"] == "no-store"
    assert 'filename="hash.jpg"' in response.headers["Content-Disposition"]


def test_gravatar_import_is_refused_when_the_feature_is_off(
    authed_client, csrf_headers, monkeypatch
):
    monkeypatch.setattr(web_import, "GRAVATAR_ENABLED", False)
    response = authed_client.post(
        "/api/fetch-gravatar", headers=csrf_headers, json={"email": "a@b.com"}
    )
    assert response.status_code == 403


def test_gravatar_import_requires_an_email(authed_client, csrf_headers):
    response = authed_client.post("/api/fetch-gravatar", headers=csrf_headers, json={})
    assert response.status_code == 400


def test_gravatar_import_rejects_a_foreign_email(authed_client, csrf_headers):
    # Prevents using the endpoint as a "does this address have a Gravatar" oracle.
    response = authed_client.post(
        "/api/fetch-gravatar",
        headers=csrf_headers,
        json={"email": "someone.else@example.com"},
    )
    assert response.status_code == 403
    assert response.get_json() == {"error": "email_mismatch"}


def test_gravatar_import_normalizes_the_submitted_email(
    authed_client, csrf_headers, stub_gravatar
):
    response = authed_client.post(
        "/api/fetch-gravatar",
        headers=csrf_headers,
        json={"email": "  Test.User@EXAMPLE.com  "},
    )
    assert response.status_code == 200


def test_gravatar_import_reports_a_missing_avatar_as_404(
    authed_client, csrf_headers, stub_gravatar
):
    stub_gravatar["raise"] = GravatarNotFound()
    response = authed_client.post(
        "/api/fetch-gravatar",
        headers=csrf_headers,
        json={"email": "test.user@example.com"},
    )
    assert response.status_code == 404
    assert response.get_json() == {"error": "not_found"}


def test_gravatar_import_reports_an_oversized_image(
    authed_client, csrf_headers, stub_gravatar
):
    stub_gravatar["raise"] = ImageTooLarge()
    response = authed_client.post(
        "/api/fetch-gravatar",
        headers=csrf_headers,
        json={"email": "test.user@example.com"},
    )
    assert response.status_code == 400
    body = response.get_json()
    assert body["error"] == "image_too_large"
    assert body["max_size_mb"] == web_import.MAX_FETCH_SIZE_MB


def test_gravatar_import_reports_an_unsupported_content_type(
    authed_client, csrf_headers, stub_gravatar
):
    stub_gravatar["raise"] = UnsupportedContentType("text/html")
    response = authed_client.post(
        "/api/fetch-gravatar",
        headers=csrf_headers,
        json={"email": "test.user@example.com"},
    )
    assert response.status_code == 400


def test_gravatar_import_reports_an_upstream_failure_as_502(
    authed_client, csrf_headers, stub_gravatar
):
    stub_gravatar["raise"] = FetchFailed("connection reset")
    response = authed_client.post(
        "/api/fetch-gravatar",
        headers=csrf_headers,
        json={"email": "test.user@example.com"},
    )
    assert response.status_code == 502
    assert response.get_json() == {"error": "fetch_failed"}


def test_gravatar_import_honors_the_per_user_cooldown(
    authed_client, csrf_headers, monkeypatch
):
    monkeypatch.setattr(
        web_import, "check_gravatar_import_cooldown", lambda pk: (False, 3)
    )
    response = authed_client.post(
        "/api/fetch-gravatar",
        headers=csrf_headers,
        json={"email": "test.user@example.com"},
    )
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "3"


# ---------------------------------------------------------------------------
# URL import
# ---------------------------------------------------------------------------


def test_url_import_requires_authentication(client):
    assert (
        client.post("/api/fetch-url", json={"url": "https://x/a.png"}).status_code
        == 302
    )


def test_url_import_requires_a_csrf_token(authed_client):
    assert (
        authed_client.post(
            "/api/fetch-url", json={"url": "https://x/a.png"}
        ).status_code
        == 403
    )


def test_url_import_proxies_the_image(authed_client, csrf_headers, stub_url_fetch):
    response = authed_client.post(
        "/api/fetch-url",
        headers=csrf_headers,
        json={"url": "https://images.example.com/a.jpg"},
    )
    assert response.status_code == 200
    assert response.get_data() == GRAVATAR_BYTES
    assert response.headers["Cache-Control"] == "no-store"


def test_url_import_is_refused_when_the_feature_is_off(
    authed_client, csrf_headers, monkeypatch
):
    monkeypatch.setattr(web_import, "URL_ENABLED", False)
    response = authed_client.post(
        "/api/fetch-url", headers=csrf_headers, json={"url": "https://x/a.png"}
    )
    assert response.status_code == 403


def test_url_import_requires_a_url(authed_client, csrf_headers):
    assert (
        authed_client.post("/api/fetch-url", headers=csrf_headers, json={}).status_code
        == 400
    )


def test_url_import_rejects_a_private_target(authed_client, csrf_headers, monkeypatch):
    monkeypatch.setattr(web_import, "validate_import_url", lambda _u: "url_not_allowed")
    response = authed_client.post(
        "/api/fetch-url", headers=csrf_headers, json={"url": "http://127.0.0.1/admin"}
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "url_not_allowed"}


def test_url_import_rejects_a_redirect_into_a_private_target(
    authed_client, csrf_headers, stub_url_fetch
):
    # safe_fetch raises ValueError when a later hop fails the SSRF check.
    stub_url_fetch["raise"] = ValueError("Redirect to private/internal address blocked")
    response = authed_client.post(
        "/api/fetch-url",
        headers=csrf_headers,
        json={"url": "https://redirector.example.com/x"},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "url_not_allowed"}


def test_url_import_reports_an_oversized_image(
    authed_client, csrf_headers, stub_url_fetch
):
    stub_url_fetch["raise"] = ImageTooLarge()
    response = authed_client.post(
        "/api/fetch-url", headers=csrf_headers, json={"url": "https://x/a.png"}
    )
    assert response.status_code == 400
    assert response.get_json()["error"] == "image_too_large"


def test_url_import_reports_an_unsupported_content_type(
    authed_client, csrf_headers, stub_url_fetch
):
    stub_url_fetch["raise"] = UnsupportedContentType("text/html")
    response = authed_client.post(
        "/api/fetch-url", headers=csrf_headers, json={"url": "https://x/a.png"}
    )
    assert response.status_code == 400


def test_url_import_reports_an_upstream_failure_as_502(
    authed_client, csrf_headers, stub_url_fetch
):
    stub_url_fetch["raise"] = FetchFailed("dns failure")
    response = authed_client.post(
        "/api/fetch-url", headers=csrf_headers, json={"url": "https://x/a.png"}
    )
    assert response.status_code == 502


def test_url_import_honors_the_per_user_cooldown(
    authed_client, csrf_headers, monkeypatch
):
    monkeypatch.setattr(web_import, "check_url_import_cooldown", lambda pk: (False, 3))
    response = authed_client.post(
        "/api/fetch-url", headers=csrf_headers, json={"url": "https://x/a.png"}
    )
    assert response.status_code == 429


def test_a_malformed_json_body_is_handled_as_a_missing_field(
    authed_client, csrf_headers
):
    response = authed_client.post(
        "/api/fetch-url",
        headers={**csrf_headers, "Content-Type": "application/json"},
        data="{not json",
    )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Avatar removal
# ---------------------------------------------------------------------------


def test_removal_requires_authentication(client):
    assert client.post("/api/remove-avatar").status_code == 302


def test_removal_requires_a_csrf_token(authed_client):
    assert authed_client.post("/api/remove-avatar").status_code == 403


def test_removal_clears_the_attribute_and_the_session(
    authed_client, csrf_headers, monkeypatch
):
    removed = []
    monkeypatch.setattr(web_reset, "remove_avatar_url", lambda pk: removed.append(pk))
    with authed_client.session_transaction() as sess:
        sess["user"] = {**sess["user"], "avatar": "https://cdn/current.jpg"}

    response = authed_client.post("/api/remove-avatar", headers=csrf_headers)

    assert response.status_code == 200
    assert response.get_json() == {"success": True}
    assert removed == [42]
    with authed_client.session_transaction() as sess:
        assert sess["user"]["avatar"] == ""


def test_a_backend_failure_returns_500_and_keeps_the_session_avatar(
    authed_client, csrf_headers, monkeypatch
):
    monkeypatch.setattr(
        web_reset,
        "remove_avatar_url",
        lambda pk: (_ for _ in ()).throw(RuntimeError("Authentik down")),
    )
    with authed_client.session_transaction() as sess:
        sess["user"] = {**sess["user"], "avatar": "https://cdn/current.jpg"}

    response = authed_client.post("/api/remove-avatar", headers=csrf_headers)

    assert response.status_code == 500
    assert response.get_json() == {"error": "remove_failed"}
    with authed_client.session_transaction() as sess:
        assert sess["user"]["avatar"] == "https://cdn/current.jpg"


def test_dry_run_reports_the_removal_without_clearing_the_session(
    authed_client, csrf_headers, monkeypatch
):
    # The backend attribute was not really cleared, so the session must keep
    # reflecting the real state.
    monkeypatch.setattr(web_reset, "skip_backend_writes", True)
    monkeypatch.setattr(web_reset, "remove_avatar_url", lambda pk: None)
    with authed_client.session_transaction() as sess:
        sess["user"] = {**sess["user"], "avatar": "https://cdn/current.jpg"}

    response = authed_client.post("/api/remove-avatar", headers=csrf_headers)

    assert response.get_json() == {"success": True, "dry_run": True}
    with authed_client.session_transaction() as sess:
        assert sess["user"]["avatar"] == "https://cdn/current.jpg"
