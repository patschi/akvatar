"""Tests for the application-wide hardening applied in app.py.

These are headers and gates that every response depends on, so a regression
here is invisible in normal use and only shows up as a downgraded security
posture in production.
"""

import pytest

from src.config import (
    max_upload_size_mb,
    trusted_hosts,
    web_session_lifetime_seconds,
)

# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/login", "/healthz", "/static/robots.txt"])
def test_nosniff_is_set_on_every_response(client, path):
    assert client.get(path).headers["X-Content-Type-Options"] == "nosniff"


@pytest.mark.parametrize("path", ["/login", "/healthz", "/static/robots.txt"])
def test_referrer_policy_is_set_on_every_response(client, path):
    assert (
        client.get(path).headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
    )


@pytest.mark.parametrize("path", ["/login", "/healthz"])
def test_permissions_policy_disables_unused_browser_apis(client, path):
    policy = client.get(path).headers["Permissions-Policy"]
    assert "microphone=()" in policy
    assert "geolocation=()" in policy
    assert "payment=()" in policy


def test_the_camera_api_is_allowed_because_webcam_import_is_enabled(client):
    # getUserMedia() is hard-blocked by the browser without this, regardless of
    # what the user consents to.
    from src.config import webcam_enabled

    policy = client.get("/login").headers["Permissions-Policy"]
    assert ("camera=(self)" if webcam_enabled else "camera=()") in policy


def test_html_responses_deny_framing(client):
    assert client.get("/login").headers["X-Frame-Options"] == "DENY"


def test_html_responses_carry_a_content_security_policy(client):
    policy = client.get("/login").headers["Content-Security-Policy"]
    assert policy.startswith("default-src 'none'")
    assert "'nonce-" in policy


def test_each_request_gets_a_fresh_csp_nonce(client):
    first = client.get("/login").headers["Content-Security-Policy"]
    second = client.get("/login").headers["Content-Security-Policy"]
    assert first != second


def test_non_html_responses_omit_the_html_only_headers(client):
    response = client.get("/healthz")
    assert "X-Frame-Options" not in response.headers
    assert "Content-Security-Policy" not in response.headers


def test_no_hsts_is_sent_without_tls(client):
    # Setting HSTS on a plain-HTTP deployment would lock users out.
    from src.config import tls_configured

    assert tls_configured is False
    assert "Strict-Transport-Security" not in client.get("/login").headers


def test_responses_carry_no_report_to_header_without_a_report_uri(client):
    # The Reporting API group is only meaningful alongside a report-uri.
    assert "Report-To" not in client.get("/login").headers


# ---------------------------------------------------------------------------
# HTTP method gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["TRACE", "CONNECT", "PROPFIND", "LOCK"])
def test_methods_outside_the_route_map_are_rejected(client, method):
    assert client.open("/login", method=method).status_code == 405


def test_methods_the_app_actually_uses_are_allowed(client):
    assert client.get("/healthz").status_code == 200
    assert client.head("/healthz").status_code == 200
    assert client.options("/healthz").status_code == 200


def test_a_wrong_method_on_a_known_route_is_still_a_405(client):
    # GET is in the global allow-list, but /logout only accepts POST.
    assert client.get("/logout").status_code == 405


def test_delete_is_rejected_because_no_route_uses_it(client):
    assert client.delete("/login").status_code == 405


@pytest.mark.parametrize("method", ["TRACE", "PROPFIND", "DELETE"])
def test_a_disallowed_method_is_rejected_before_routing(client, method):
    """The global gate must fire ahead of URL matching.

    On a routed path Flask would answer 405 by itself, so that case cannot tell
    whether the gate exists.  On a path that matches no rule the difference is
    visible: the gate answers 405, plain routing would answer 404.
    """
    assert client.open("/no-such-path", method=method).status_code == 405
    # Sanity check that the path really is unrouted.
    assert client.get("/no-such-path").status_code == 404


# ---------------------------------------------------------------------------
# Trusted hosts
# ---------------------------------------------------------------------------


def test_a_configured_host_is_accepted(client):
    response = client.get("/healthz", headers={"Host": "avatar.test.example.com"})
    assert response.status_code == 200


def test_an_unknown_host_header_is_rejected(client):
    # Blocks host-header poisoning of generated URLs (e.g. password-reset style
    # links or the OIDC redirect_uri).
    assert (
        client.get("/healthz", headers={"Host": "evil.example.com"}).status_code == 400
    )


def test_the_trusted_host_list_comes_from_config():
    assert trusted_hosts is not None
    assert "avatar.test.example.com" in trusted_hosts


# ---------------------------------------------------------------------------
# Session cookie hardening
# ---------------------------------------------------------------------------


def test_the_session_cookie_is_httponly_and_samesite_lax(flask_app):
    # Lax (not Strict) so the OIDC redirect back to /callback still carries it.
    assert flask_app.config["SESSION_COOKIE_HTTPONLY"] is True
    assert flask_app.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_the_session_has_an_absolute_lifetime(flask_app):
    # Without an explicit lifetime Flask issues a browser-session cookie that
    # never expires server-side.
    assert (
        flask_app.permanent_session_lifetime.total_seconds()
        == web_session_lifetime_seconds
    )


def test_the_cookie_name_follows_the_secure_flag(flask_app):
    # The __Host- prefix is only valid on a Secure cookie; the test config
    # forces Secure off, so the plain name must be used.
    assert flask_app.config["SESSION_COOKIE_SECURE"] is False
    assert flask_app.config["SESSION_COOKIE_NAME"] == "akvatar_session"


def test_the_session_cookie_is_set_with_the_hardening_flags(flask_app, user):
    # Start from a session that has no CSRF token yet: rendering the dashboard
    # generates one and writes it back, so a Set-Cookie is guaranteed rather
    # than incidental (an assertion guarded by "if cookies:" would pass
    # vacuously on a response that never touched the session).
    test_client = flask_app.test_client()
    with test_client.session_transaction() as sess:
        sess["user"] = dict(user)

    cookies = test_client.get("/dashboard").headers.getlist("Set-Cookie")

    assert cookies, "expected the session cookie to be re-issued"
    for cookie in cookies:
        attributes = {part.strip() for part in cookie.split(";")[1:]}
        assert "HttpOnly" in attributes
        assert "SameSite=Lax" in attributes
        # Secure is forced off in the test config, so the cookie must not claim it.
        assert "Secure" not in attributes


# ---------------------------------------------------------------------------
# Upload size limit
# ---------------------------------------------------------------------------


def test_the_body_size_limit_matches_the_configured_maximum(flask_app):
    assert flask_app.config["MAX_CONTENT_LENGTH"] == max_upload_size_mb * 1024 * 1024


# ---------------------------------------------------------------------------
# Debug posture
# ---------------------------------------------------------------------------


def test_the_app_is_not_running_in_debug_mode(flask_app):
    # Flask's debugger would expose an interactive console on any traceback.
    assert flask_app.debug is False
    assert flask_app.config["TEMPLATES_AUTO_RELOAD"] is False


def test_no_flask_default_static_route_is_registered(flask_app):
    # Static files come from the in-memory cache, not from Flask's sender.
    static_rules = [
        rule for rule in flask_app.url_map.iter_rules() if rule.endpoint == "static"
    ]
    assert len(static_rules) == 1
    assert flask_app.static_folder is None


# ---------------------------------------------------------------------------
# Reverse proxy support
# ---------------------------------------------------------------------------


def test_the_client_ip_is_taken_from_x_forwarded_for():
    """The real client IP must survive the reverse proxy hop.

    Rate limiting and every security log line key off ``remote_addr``; without
    ProxyFix every request would appear to come from the proxy itself.  A fresh
    application instance is built here because a probe route cannot be added to
    the session-wide app after it has served its first request.
    """
    from app import create_app

    application = create_app()
    seen = {}

    @application.route("/__probe_remote_addr")
    def _probe():
        from flask import request

        seen["addr"] = request.remote_addr
        return "ok"

    application.test_client().get(
        "/__probe_remote_addr", headers={"X-Forwarded-For": "203.0.113.9"}
    )
    assert seen["addr"] == "203.0.113.9"


def test_generated_urls_honor_the_forwarded_prefix(client):
    # A reverse proxy mounting the app under a subfolder sets X-Forwarded-Prefix.
    response = client.get(
        "/", headers={"X-Forwarded-Prefix": "/avatar-update", "Host": "localhost"}
    )
    assert response.headers["Location"].endswith("/avatar-update/login")
