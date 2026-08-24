"""Tests for src/web_auth.py - the OIDC login, callback and logout routes.

The handshake itself belongs to Authlib; what is tested here is what the
application does around it: session regeneration on login, CSRF-protected
logout, and the failure redirects that must never surface a stack trace.
"""

import pytest

import src.web_auth as web_auth

USERINFO = {
    "sub": "oidc-subject",
    "preferred_username": "testuser",
    "name": "Test User",
    "email": "test.user@example.com",
    "locale": "en-US",
}


@pytest.fixture
def oidc(monkeypatch):
    """Stub the Authlib client so /callback can be driven end to end."""
    state = {
        "token": {"id_token": "jwt-token", "userinfo": dict(USERINFO)},
        "token_error": None,
        "userinfo_endpoint": dict(USERINFO),
        "metadata": {
            "end_session_endpoint": "https://auth.test.example.com/end-session"
        },
    }

    class FakeClient:
        def authorize_redirect(self, redirect_uri):
            from flask import redirect

            return redirect(f"https://auth.test.example.com/authorize?r={redirect_uri}")

        def authorize_access_token(self):
            if state["token_error"]:
                raise state["token_error"]
            return state["token"]

        def userinfo(self):
            return state["userinfo_endpoint"]

        def load_server_metadata(self):
            return state["metadata"]

    monkeypatch.setattr(web_auth.oauth, "authentik", FakeClient(), raising=False)
    monkeypatch.setattr("src.auth.oauth.authentik", FakeClient(), raising=False)
    monkeypatch.setattr(
        web_auth,
        "process_oidc_callback",
        _real_callback_with_stubbed_lookup(monkeypatch),
    )
    return state


def _real_callback_with_stubbed_lookup(monkeypatch):
    """Return process_oidc_callback with the Authentik user lookup stubbed out."""
    import src.auth as auth

    monkeypatch.setattr(
        auth,
        "retrieve_user",
        lambda username: {"pk": 42, "avatar": "https://cdn/a.jpg"},
    )
    return auth.process_oidc_callback


# ---------------------------------------------------------------------------
# Login start
# ---------------------------------------------------------------------------


def test_login_start_redirects_to_the_provider(client, oidc):
    response = client.get("/login-start")
    assert response.status_code == 302
    assert response.headers["Location"].startswith(
        "https://auth.test.example.com/authorize"
    )


def test_the_redirect_uri_sent_to_the_provider_is_external(client, oidc):
    location = client.get("/login-start").headers["Location"]
    assert "http://localhost/callback" in location


# ---------------------------------------------------------------------------
# Callback
# ---------------------------------------------------------------------------


def test_a_successful_callback_signs_the_user_in(client, oidc):
    response = client.get("/callback")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/dashboard")
    with client.session_transaction() as sess:
        assert sess["user"]["pk"] == 42
        assert sess["user"]["username"] == "testuser"
        assert sess["locale"] == "en_US"


def test_the_session_is_regenerated_to_prevent_fixation(client, oidc):
    # Any pre-authentication state (including an attacker-planted value) must
    # be discarded when the user signs in.
    with client.session_transaction() as sess:
        sess["planted_by_attacker"] = "value"

    client.get("/callback")

    with client.session_transaction() as sess:
        assert "planted_by_attacker" not in sess


def test_the_id_token_is_stored_for_provider_logout(client, oidc):
    client.get("/callback")
    with client.session_transaction() as sess:
        assert sess["id_token"] == "jwt-token"


def test_the_userinfo_endpoint_is_called_when_the_token_omits_it(client, oidc):
    oidc["token"] = {"id_token": "jwt-token"}  # no inline userinfo
    client.get("/callback")
    with client.session_transaction() as sess:
        assert sess["user"]["username"] == "testuser"


def test_a_failed_token_exchange_redirects_with_an_error(client, oidc, caplog):
    oidc["token_error"] = RuntimeError("invalid_grant")

    with caplog.at_level("ERROR", logger="auth"):
        response = client.get("/callback")

    assert response.status_code == 302
    assert "error=oidc_failed" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "user" not in sess


def test_a_failed_user_lookup_redirects_with_an_error(client, oidc, monkeypatch):
    monkeypatch.setattr(
        web_auth,
        "process_oidc_callback",
        lambda token, userinfo: (_ for _ in ()).throw(ValueError("no such user")),
    )
    response = client.get("/callback")

    assert "error=pk_failed" in response.headers["Location"]
    with client.session_transaction() as sess:
        assert "user" not in sess


# ---------------------------------------------------------------------------
# Logout
# ---------------------------------------------------------------------------


def test_logout_rejects_a_get_request(client):
    # A GET logout could be triggered by <img src="/logout">.
    assert client.get("/logout").status_code == 405


def test_logout_without_a_csrf_token_redirects_to_login(authed_client, caplog):
    with caplog.at_level("WARNING", logger="auth"):
        response = authed_client.post("/logout")

    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")
    # The session must survive a rejected logout attempt.
    with authed_client.session_transaction() as sess:
        assert "user" in sess


def test_logout_clears_the_session(authed_client, csrf_headers, oidc):
    response = authed_client.post("/logout", headers=csrf_headers)

    assert response.status_code == 302
    with authed_client.session_transaction() as sess:
        assert "user" not in sess


def test_logout_redirects_to_the_provider_end_session_endpoint(
    authed_client, csrf_headers, oidc
):
    with authed_client.session_transaction() as sess:
        sess["id_token"] = "jwt-token"

    location = authed_client.post("/logout", headers=csrf_headers).headers["Location"]

    assert location.startswith("https://auth.test.example.com/end-session")
    assert "id_token_hint=jwt-token" in location


def test_logout_falls_back_to_the_local_page_without_a_provider_endpoint(
    authed_client, csrf_headers, oidc
):
    oidc["metadata"] = {}
    location = authed_client.post("/logout", headers=csrf_headers).headers["Location"]
    assert location.endswith("/logged-out")


def test_logout_accepts_the_token_from_a_form_field(authed_client, oidc):
    response = authed_client.post(
        "/logout", data={"csrf_token": authed_client.csrf_token}
    )
    assert response.status_code == 302
    with authed_client.session_transaction() as sess:
        assert "user" not in sess


def test_the_logged_out_page_renders_for_anonymous_visitors(client):
    response = client.get("/logged-out")
    assert response.status_code == 200
    assert response.mimetype == "text/html"
