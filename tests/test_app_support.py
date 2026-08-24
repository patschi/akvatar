"""Tests for src/app_middleware.py, src/app_static.py and src/app_monitor.py.

Small supporting pieces, but each one has a failure mode that is easy to miss:
double-prefixed URLs behind a reverse proxy, a stale ETag served from the
in-memory cache, or a template loader that eats meaningful whitespace.
"""

import pytest
from flask import Flask
from jinja2 import DictLoader, Environment

from src.app_middleware import MinifyingTemplateLoader, PrefixMiddleware
from src.app_monitor import _get_rss_mb
from src.app_static import serve_static_file, static_cache

# ---------------------------------------------------------------------------
# PrefixMiddleware
# ---------------------------------------------------------------------------


def capture_environ(prefix, **environ_overrides):
    """Run PrefixMiddleware over a request and return the environ it produced."""
    seen = {}

    def inner_app(environ, start_response):
        seen.update(environ)
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"ok"]

    environ = {"PATH_INFO": "/dashboard", "REQUEST_METHOD": "GET"}
    environ.update(environ_overrides)
    PrefixMiddleware(inner_app, prefix)(environ, lambda *_a: None)
    return seen


def test_the_prefix_is_applied_when_the_proxy_did_not_set_one():
    environ = capture_environ("/avatar-update", PATH_INFO="/avatar-update/dashboard")
    assert environ["SCRIPT_NAME"] == "/avatar-update"
    assert environ["PATH_INFO"] == "/dashboard"


def test_a_path_outside_the_prefix_keeps_its_path_info():
    environ = capture_environ("/avatar-update", PATH_INFO="/healthz")
    assert environ["SCRIPT_NAME"] == "/avatar-update"
    assert environ["PATH_INFO"] == "/healthz"


def test_the_middleware_defers_to_a_proxy_supplied_script_name():
    # Applying the prefix twice would produce /avatar-update/avatar-update/...
    # and break the OIDC redirect_uri.
    environ = capture_environ(
        "/avatar-update",
        SCRIPT_NAME="/avatar-update",
        PATH_INFO="/dashboard",
    )
    assert environ["SCRIPT_NAME"] == "/avatar-update"
    assert environ["PATH_INFO"] == "/dashboard"


def test_url_generation_under_a_prefix_includes_it():
    app = Flask(__name__)

    @app.route("/callback")
    def callback():
        from flask import url_for

        return url_for("callback")

    app.wsgi_app = PrefixMiddleware(app.wsgi_app, "/avatar-update")
    body = app.test_client().get("/avatar-update/callback").get_data(as_text=True)
    assert body == "/avatar-update/callback"


# ---------------------------------------------------------------------------
# MinifyingTemplateLoader
# ---------------------------------------------------------------------------


def render(source: str, **context) -> str:
    """Render *source* through the minifying loader."""
    env = Environment(loader=MinifyingTemplateLoader(DictLoader({"t.html": source})))
    return env.get_template("t.html").render(**context)


def test_html_comments_are_stripped():
    assert render("<p>a</p><!-- secret note -->\n<p>b</p>") == "<p>a</p>\n<p>b</p>"


def test_multiline_html_comments_are_stripped():
    assert "TODO" not in render("<p>a</p>\n<!--\n  TODO: fix\n-->\n<p>b</p>")


def test_runs_of_blank_lines_collapse_to_one():
    assert render("a\n\n\n\n\nb") == "a\n\nb"


def test_a_single_blank_line_is_preserved():
    assert render("a\n\nb") == "a\n\nb"


def test_whitespace_only_lines_are_treated_as_blank():
    assert render("a\n   \n\t\n   \nb") == "a\n\nb"


def test_jinja_expressions_still_render():
    # Minifying happens on the template *source*, so Jinja must still compile it.
    assert render("<p>{{ name }}</p>", name="Ada") == "<p>Ada</p>"


def test_a_comment_inside_a_jinja_block_is_still_stripped():
    output = render("{% if show %}<!-- note -->kept{% endif %}", show=True)
    assert output == "kept"


# ---------------------------------------------------------------------------
# In-memory static cache
# ---------------------------------------------------------------------------


def test_the_cache_contains_the_shipped_assets():
    assert "robots.txt" in static_cache
    assert "css/style.css" in static_cache
    assert "js/vendor/cropper.min.js" in static_cache


def test_cached_entries_carry_data_a_mimetype_and_an_etag():
    data, mime, etag = static_cache["css/style.css"]
    assert data
    assert mime == "text/css"
    assert len(etag) == 16  # truncated sha256


def test_etags_are_content_derived_and_differ_between_files():
    css_etag = static_cache["css/style.css"][2]
    js_etag = static_cache["js/dashboard-main.js"][2]
    assert css_etag != js_etag


def test_a_cached_file_is_served_with_caching_headers(client):
    response = client.get("/static/robots.txt")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "public, max-age=3600"
    assert response.headers["ETag"].startswith('"')


def test_a_matching_if_none_match_returns_304(client):
    first = client.get("/static/robots.txt")
    second = client.get(
        "/static/robots.txt", headers={"If-None-Match": first.headers["ETag"]}
    )
    assert second.status_code == 304
    assert second.get_data() == b""


def test_a_stale_if_none_match_returns_the_body(client):
    response = client.get("/static/robots.txt", headers={"If-None-Match": '"stale"'})
    assert response.status_code == 200


def test_an_unknown_static_file_is_a_404(client):
    assert client.get("/static/does-not-exist.js").status_code == 404


def test_serving_an_unknown_file_aborts(flask_app):
    from werkzeug.exceptions import NotFound

    with flask_app.test_request_context("/static/nope"):
        with pytest.raises(NotFound):
            serve_static_file("nope")


def test_javascript_assets_get_a_javascript_mimetype(client):
    response = client.get("/static/js/dashboard-main.js")
    assert response.mimetype in ("text/javascript", "application/javascript")


# ---------------------------------------------------------------------------
# Memory monitor
# ---------------------------------------------------------------------------


def test_rss_reading_returns_a_number_or_none():
    # /proc/self/status exists on Linux only; the helper must not raise elsewhere.
    rss = _get_rss_mb()
    assert rss is None or rss > 0
