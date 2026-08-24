"""Tests for src/sec_csrf.py and src/sec_csp.py.

Both modules are defense-in-depth layers whose failure mode is silent: a CSRF
check that accepts an empty token, or a CSP header that loses its nonce, still
looks like a working application.  These tests pin the exact behavior.
"""

import json

import pytest
from flask import Flask, g, jsonify

import src.sec_csp as sec_csp
from src.config import public_avatar_url
from src.sec_csp import (
    CSP_HEADER_NAME,
    build_csp_header,
    build_report_to_header,
    generate_csp_nonce,
)
from src.sec_csrf import csrf_required, generate_csrf_token, validate_csrf_token


@pytest.fixture
def mini_app():
    """A bare Flask app with CSRF-protected endpoints, isolated from the real one."""
    app = Flask(__name__)
    app.secret_key = "test-secret-key-for-session-signing"

    @app.route("/token")
    def token():
        return generate_csrf_token()

    @app.route("/guarded", methods=["POST"])
    @csrf_required
    def guarded():
        return jsonify({"ok": True})

    @app.route("/manual", methods=["POST"])
    def manual():
        rejection = validate_csrf_token()
        if rejection:
            return rejection
        return jsonify({"ok": True})

    return app


# ---------------------------------------------------------------------------
# CSRF token lifecycle
# ---------------------------------------------------------------------------


def test_a_token_is_generated_once_and_reused_within_a_session(mini_app):
    client = mini_app.test_client()
    first = client.get("/token").get_data(as_text=True)
    second = client.get("/token").get_data(as_text=True)
    assert first == second
    assert len(first) == 64  # 32 random bytes, hex-encoded


def test_separate_sessions_get_separate_tokens(mini_app):
    a = mini_app.test_client().get("/token").get_data(as_text=True)
    b = mini_app.test_client().get("/token").get_data(as_text=True)
    assert a != b


# ---------------------------------------------------------------------------
# CSRF validation
# ---------------------------------------------------------------------------


def test_a_matching_header_token_is_accepted(mini_app):
    client = mini_app.test_client()
    token = client.get("/token").get_data(as_text=True)
    assert client.post("/guarded", headers={"X-CSRF-Token": token}).status_code == 200


def test_a_matching_form_field_is_accepted(mini_app):
    # Plain HTML form submissions (e.g. the logout button) send the token this way.
    client = mini_app.test_client()
    token = client.get("/token").get_data(as_text=True)
    assert client.post("/guarded", data={"csrf_token": token}).status_code == 200


def test_the_header_takes_precedence_over_the_form_field(mini_app):
    client = mini_app.test_client()
    token = client.get("/token").get_data(as_text=True)
    response = client.post(
        "/guarded", headers={"X-CSRF-Token": token}, data={"csrf_token": "wrong"}
    )
    assert response.status_code == 200


def test_a_missing_token_is_rejected(mini_app):
    client = mini_app.test_client()
    client.get("/token")
    response = client.post("/guarded")
    assert response.status_code == 403
    assert response.get_json() == {"error": "csrf_failed"}


def test_a_wrong_token_is_rejected(mini_app):
    client = mini_app.test_client()
    client.get("/token")
    assert (
        client.post("/guarded", headers={"X-CSRF-Token": "0" * 64}).status_code == 403
    )


def test_a_request_with_no_session_token_is_rejected(mini_app):
    # No /token call first: the session holds nothing to compare against.
    client = mini_app.test_client()
    assert client.post("/guarded", headers={"X-CSRF-Token": "x"}).status_code == 403


@pytest.mark.parametrize("value", ["", None])
def test_an_empty_session_token_never_counts_as_valid(mini_app, value):
    # A falsy expected value must not be treated as "validation passed".
    client = mini_app.test_client()
    with client.session_transaction() as session:
        session["csrf_token"] = value
    assert client.post("/guarded", headers={"X-CSRF-Token": ""}).status_code == 403


def test_another_users_token_is_rejected(mini_app):
    victim = mini_app.test_client()
    victim.get("/token")
    attacker_token = mini_app.test_client().get("/token").get_data(as_text=True)
    assert (
        victim.post("/guarded", headers={"X-CSRF-Token": attacker_token}).status_code
        == 403
    )


def test_the_decorator_and_the_manual_call_behave_identically(mini_app):
    client = mini_app.test_client()
    token = client.get("/token").get_data(as_text=True)
    assert client.post("/manual", headers={"X-CSRF-Token": token}).status_code == 200
    assert client.post("/manual").status_code == 403


def test_a_rejected_request_never_reaches_the_view(mini_app):
    reached = []

    @mini_app.route("/tracked", methods=["POST"])
    @csrf_required
    def tracked():
        reached.append(True)
        return "ok"

    client = mini_app.test_client()
    client.get("/token")
    client.post("/tracked")
    assert reached == []


# ---------------------------------------------------------------------------
# CSP nonce
# ---------------------------------------------------------------------------


def test_the_nonce_is_stable_within_one_request(flask_app):
    with flask_app.test_request_context("/"):
        assert generate_csp_nonce() == generate_csp_nonce()


def test_the_nonce_differs_between_requests(flask_app):
    with flask_app.test_request_context("/"):
        first = generate_csp_nonce()
    with flask_app.test_request_context("/"):
        second = generate_csp_nonce()
    assert first != second


def test_the_nonce_is_stored_on_the_request_context(flask_app):
    # The after_request hook and the template must read the same value.
    with flask_app.test_request_context("/"):
        nonce = generate_csp_nonce()
        assert g.csp_nonce == nonce


def test_the_nonce_carries_enough_entropy(flask_app):
    with flask_app.test_request_context("/"):
        # 16 random bytes, base64url-encoded without padding.
        assert len(generate_csp_nonce()) >= 22


# ---------------------------------------------------------------------------
# CSP header construction
# ---------------------------------------------------------------------------


def test_the_policy_embeds_the_request_nonce():
    assert "'nonce-abc123'" in build_csp_header("abc123")


def test_the_policy_denies_everything_by_default():
    assert build_csp_header("n").startswith("default-src 'none'")


@pytest.mark.parametrize(
    "directive",
    [
        "script-src 'self'",
        "style-src 'self'",
        "font-src 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "form-action 'self'",
    ],
)
def test_the_policy_contains_the_expected_directives(directive):
    assert directive in build_csp_header("n")


def test_img_src_allows_the_separate_avatar_origin():
    # Avatars are served from cdn.test.example.com; without this they would be
    # blocked by default-src 'none'.
    policy = build_csp_header("n")
    assert "https://cdn.test.example.com" in policy
    assert public_avatar_url.startswith("https://cdn.test.example.com")


def test_img_src_allows_the_cropper_preview_schemes():
    # Cropper.js renders previews from data: and blob: URLs.
    policy = build_csp_header("n")
    img_src = next(d for d in policy.split("; ") if d.startswith("img-src"))
    assert "data:" in img_src and "blob:" in img_src


def test_connect_src_allows_blob_for_the_cropped_upload():
    policy = build_csp_header("n")
    connect_src = next(d for d in policy.split("; ") if d.startswith("connect-src"))
    assert "'self'" in connect_src and "blob:" in connect_src


def test_media_src_follows_the_webcam_feature_flag():
    from src.config import webcam_enabled

    policy = build_csp_header("n")
    expected = "media-src 'self'" if webcam_enabled else "media-src 'none'"
    assert expected in policy


def test_the_enforcing_header_name_is_used_by_default():
    assert CSP_HEADER_NAME == "Content-Security-Policy"


def test_the_header_is_omitted_when_csp_is_disabled(monkeypatch):
    monkeypatch.setattr(sec_csp, "_CSP_ENABLED", False)
    assert build_csp_header("n") is None


def test_reporting_directives_are_appended_when_a_report_uri_is_configured(
    monkeypatch,
):
    monkeypatch.setattr(sec_csp, "_CSP_REPORT_URI", "https://csp.example.com/report")
    policy = build_csp_header("n")
    # Level 2 (report-uri) and Level 3 (report-to) are emitted in parallel.
    assert "report-uri https://csp.example.com/report" in policy
    assert "report-to csp-endpoint" in policy


def test_no_report_to_header_without_a_configured_report_uri():
    assert build_report_to_header() is None


def test_the_report_to_header_names_the_csp_endpoint_group(monkeypatch):
    monkeypatch.setattr(
        sec_csp,
        "_REPORT_TO_HEADER",
        json.dumps(
            {
                "group": "csp-endpoint",
                "max_age": 86400,
                "endpoints": [{"url": "https://csp.example.com/report"}],
            },
            separators=(",", ":"),
        ),
    )
    parsed = json.loads(build_report_to_header())
    assert parsed["group"] == "csp-endpoint"
    assert parsed["endpoints"][0]["url"] == "https://csp.example.com/report"
