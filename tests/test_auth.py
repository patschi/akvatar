"""Tests for src/auth.py - OIDC session building and the login gate."""

import pytest
from flask import Flask, session

import src.auth as auth
from src.auth import (
    build_provider_logout_url,
    build_user_initials,
    login_required,
    process_oidc_callback,
)
from src.config import OIDC_SCOPES, public_webui_url

USERINFO = {
    "sub": "oidc-subject-id",
    "preferred_username": "testuser",
    "name": "Test User",
    "email": "test.user@example.com",
    "locale": "de-DE",
}


@pytest.fixture
def stub_retrieve_user(monkeypatch):
    """Replace the Authentik lookup with a recorded stub."""
    calls = []

    def fake(username):
        calls.append(username)
        return {"pk": 42, "avatar": "https://cdn/current.jpg"}

    monkeypatch.setattr(auth, "retrieve_user", fake)
    return calls


# ---------------------------------------------------------------------------
# Initials
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ({"name": "Ada Lovelace", "username": "ada"}, "AL"),
        ({"name": "Jean Luc Picard", "username": "jlp"}, "JP"),  # first + last
        ({"name": "Prince", "username": "prince"}, "P"),  # single name -> username
        ({"name": "", "username": "solo"}, "S"),
        ({"name": "  ", "username": "spaces"}, "S"),
        ({"username": "nokey"}, "N"),
        ({}, "?"),
        ({"name": "", "username": ""}, "?"),
    ],
)
def test_initials_are_built_from_the_name_with_a_username_fallback(user, expected):
    assert build_user_initials(user) == expected


def test_initials_are_uppercased():
    assert build_user_initials({"name": "ada lovelace", "username": "a"}) == "AL"


# ---------------------------------------------------------------------------
# OIDC callback processing
# ---------------------------------------------------------------------------


def test_the_session_user_is_built_from_the_userinfo_and_the_authentik_lookup(
    stub_retrieve_user,
):
    user, id_token, locale = process_oidc_callback({"id_token": "jwt"}, USERINFO)

    assert user == {
        "pk": 42,
        "username": "testuser",
        "name": "Test User",
        "email": "test.user@example.com",
        "avatar": "https://cdn/current.jpg",
    }
    assert stub_retrieve_user == ["testuser"]
    assert locale == "de_DE"
    assert id_token == "jwt"


def test_the_configured_username_claim_is_used(monkeypatch, stub_retrieve_user):
    monkeypatch.setattr(auth, "oidc_username_claim", "email")
    process_oidc_callback({}, USERINFO)
    assert stub_retrieve_user == ["test.user@example.com"]


def test_the_subject_is_used_when_the_username_claim_is_absent(stub_retrieve_user):
    process_oidc_callback({}, {"sub": "oidc-subject-id"})
    assert stub_retrieve_user == ["oidc-subject-id"]


def test_optional_profile_fields_default_to_empty(stub_retrieve_user):
    user, _id_token, _locale = process_oidc_callback({}, {"sub": "s"})
    assert user["name"] == "" and user["email"] == ""


def test_the_id_token_is_not_stored_when_provider_logout_is_disabled(
    monkeypatch, stub_retrieve_user
):
    # Keeping the JWT would be dead weight in the cookie for no benefit.
    monkeypatch.setattr(auth, "oidc_end_provider_session", False)
    _user, id_token, _locale = process_oidc_callback({"id_token": "jwt"}, USERINFO)
    assert id_token is None


def test_an_authentik_lookup_failure_propagates(monkeypatch):
    def boom(_username):
        raise ValueError("user not found")

    monkeypatch.setattr(auth, "retrieve_user", boom)
    with pytest.raises(ValueError):
        process_oidc_callback({}, USERINFO)


def test_an_unsupported_locale_claim_falls_back_to_the_default(stub_retrieve_user):
    from src.config import DEFAULT_LOCALE

    _user, _id_token, locale = process_oidc_callback({}, {**USERINFO, "locale": "zz"})
    assert locale == DEFAULT_LOCALE


# ---------------------------------------------------------------------------
# RP-Initiated Logout URL
# ---------------------------------------------------------------------------


def stub_metadata(monkeypatch, metadata):
    """Make oauth.authentik.load_server_metadata() return *metadata* (or raise)."""

    class FakeClient:
        def load_server_metadata(self):
            if isinstance(metadata, Exception):
                raise metadata
            return metadata

    monkeypatch.setattr(auth.oauth, "authentik", FakeClient(), raising=False)


def test_the_logout_url_targets_the_provider_end_session_endpoint(monkeypatch):
    stub_metadata(
        monkeypatch, {"end_session_endpoint": "https://auth.example.com/end-session"}
    )
    url = build_provider_logout_url("jwt-token")

    assert url.startswith("https://auth.example.com/end-session?")
    assert "id_token_hint=jwt-token" in url
    assert "post_logout_redirect_uri=" in url
    assert public_webui_url.replace(":", "%3A").replace("/", "%2F") in url


def test_the_id_token_hint_is_omitted_when_no_token_was_stored(monkeypatch):
    stub_metadata(
        monkeypatch, {"end_session_endpoint": "https://auth.example.com/end-session"}
    )
    assert "id_token_hint" not in build_provider_logout_url(None)


def test_no_logout_url_when_provider_logout_is_disabled(monkeypatch):
    monkeypatch.setattr(auth, "oidc_end_provider_session", False)
    assert build_provider_logout_url("jwt") is None


def test_no_logout_url_when_the_provider_advertises_no_endpoint(monkeypatch):
    stub_metadata(monkeypatch, {})
    assert build_provider_logout_url("jwt") is None


def test_no_logout_url_when_metadata_cannot_be_loaded(monkeypatch, caplog):
    # A discovery outage must degrade to the local logged-out page, not a 500.
    stub_metadata(monkeypatch, RuntimeError("discovery unreachable"))
    with caplog.at_level("WARNING", logger="auth"):
        assert build_provider_logout_url("jwt") is None
    assert "falling back to local logout" in caplog.text


# ---------------------------------------------------------------------------
# login_required
# ---------------------------------------------------------------------------


@pytest.fixture
def guarded_app(flask_app):
    """A tiny app that reuses the real login route target for url_for()."""
    app = Flask(__name__)
    app.secret_key = "test-secret"

    @app.route("/login", endpoint="routes.login_page")
    def login_page():
        return "login page"

    @app.route("/private")
    @login_required
    def private():
        return f"hello {session['user']['username']}"

    return app


def test_an_anonymous_visitor_is_redirected_to_the_login_page(guarded_app):
    response = guarded_app.test_client().get("/private")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_an_authenticated_visitor_reaches_the_view(guarded_app):
    client = guarded_app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = {"username": "testuser"}
    assert client.get("/private").get_data(as_text=True) == "hello testuser"


def test_the_decorator_preserves_the_view_name(guarded_app):
    # functools.wraps keeps Flask's endpoint registration working.
    assert guarded_app.view_functions["private"].__name__ == "private"


# ---------------------------------------------------------------------------
# OAuth client registration
# ---------------------------------------------------------------------------


def test_the_registered_scopes_match_the_documented_set():
    assert OIDC_SCOPES == "openid profile email"


def test_the_oauth_client_is_registered_on_the_application(flask_app):
    # init_oauth() runs inside create_app(); the client must be reachable.
    assert auth.oauth.authentik is not None
