"""Tests for src/web_serve_avatar.py - avatar serving, content negotiation and
metadata access control.

These endpoints are the only ones reachable without a session (avatars are
public by design), so the validation in front of the filesystem - dimension
allow-list, format allow-list and traversal check - is what keeps them from
becoming an arbitrary-file-read.
"""

import json

import pytest
from werkzeug.exceptions import NotFound

import src.web_serve_avatar as serve
from src.config import img_formats, img_sizes
from src.imaging import AVATAR_ROOT, METADATA_ROOT, generate_filename, process_image
from tests.conftest import login
from tests.helpers import make_image


@pytest.fixture
def avatar_set():
    """A complete avatar set on disk owned by the canonical test user (pk=42)."""
    base = generate_filename()
    process_image(make_image((300, 300)), base)
    (METADATA_ROOT / f"{base}.meta.json").write_text(
        json.dumps({"filename": base, "user_pk": 42, "total_bytes": 1}),
        encoding="utf-8",
    )
    return base


# ---------------------------------------------------------------------------
# Direct file serving
# ---------------------------------------------------------------------------


def test_a_stored_avatar_is_served(client, avatar_set):
    response = client.get(f"/user-avatars/256x256/{avatar_set}.jpg")
    assert response.status_code == 200
    assert response.mimetype == "image/jpeg"
    assert response.get_data().startswith(b"\xff\xd8\xff")


def test_avatars_are_public(client, avatar_set):
    # No session: the URL is unguessable rather than access-controlled.
    assert client.get(f"/user-avatars/64x64/{avatar_set}.png").status_code == 200


def test_avatars_are_served_as_immutable(client, avatar_set):
    # Filenames carry ~740 bits of entropy and are never reused, so the content
    # at a URL can never change.
    response = client.get(f"/user-avatars/256x256/{avatar_set}.jpg")
    assert response.headers["Cache-Control"] == "public, max-age=31536000, immutable"


@pytest.mark.parametrize("size", img_sizes)
def test_every_configured_size_is_reachable(client, avatar_set, size):
    assert (
        client.get(f"/user-avatars/{size}x{size}/{avatar_set}.jpg").status_code == 200
    )


@pytest.mark.parametrize("ext", img_formats)
def test_every_configured_format_is_reachable(client, avatar_set, ext):
    assert client.get(f"/user-avatars/256x256/{avatar_set}.{ext}").status_code == 200


def test_a_missing_avatar_is_a_404(client):
    assert client.get("/user-avatars/256x256/does-not-exist.jpg").status_code == 404


@pytest.mark.parametrize(
    "dimensions",
    ["999x999", "256x128", "abcxdef", "256", "0x0", "123456x123456", "256X256"],
)
def test_invalid_or_unconfigured_dimensions_are_rejected(
    client, avatar_set, dimensions
):
    assert client.get(f"/user-avatars/{dimensions}/{avatar_set}.jpg").status_code == 404


def test_a_format_that_is_not_configured_is_rejected(client, avatar_set):
    # AVIF is not in the test config's images.formats.
    assert client.get(f"/user-avatars/256x256/{avatar_set}.avif").status_code == 404


def test_an_arbitrary_extension_is_rejected(client, avatar_set):
    (AVATAR_ROOT / "256x256" / f"{avatar_set}.yml").write_bytes(b"secret: value")
    assert client.get(f"/user-avatars/256x256/{avatar_set}.yml").status_code == 404


def test_an_uppercase_extension_does_not_reach_a_stored_file(client, avatar_set):
    # send_from_directory would treat ".JPG" as a different file name, so the
    # request must 404 rather than fall through to a case-insensitive match.
    assert client.get(f"/user-avatars/256x256/{avatar_set}.JPG").status_code == 404


@pytest.mark.parametrize(
    "attempt",
    [
        "/user-avatars/256x256/..%2f..%2fconfig%2fconfig.yml",
        "/user-avatars/256x256/%2e%2e%2f%2e%2e%2fconfig.yml",
        "/user-avatars/..%2f_metadata/a.meta.json",
        "/user-avatars/256x256/..%2f..%2fconfig.jpg",
    ],
)
def test_traversal_shaped_urls_never_reach_the_handler(client, attempt):
    # The <basename> URL converter does not match "/", so an encoded separator
    # turns the URL into extra path segments that match no rule at all.
    assert client.get(attempt).status_code == 404


def test_the_handler_refuses_a_basename_that_escapes_the_avatar_root(flask_app):
    """A basename that escapes the storage root must never serve a file.

    Called directly, bypassing routing: over HTTP the URL converter already
    rejects a basename containing separators, so this asserts the property the
    handler itself is responsible for.  Three layers enforce it (the converter,
    the explicit ``_check_path_traversal`` guard, and ``send_from_directory``'s
    own safe_join), which is why removing any single one of them still leaves
    this test green - it pins the outcome, not one particular implementation.
    """
    with flask_app.test_request_context("/"):
        with pytest.raises(NotFound):
            serve.serve_avatar_file("256x256", "../../config/config", "jpg")


def test_the_metadata_handler_refuses_a_filename_that_escapes_its_root(
    flask_app, avatar_set
):
    with flask_app.test_request_context("/") as ctx:
        ctx.session["user"] = {"pk": 42}
        with pytest.raises(NotFound):
            serve.serve_avatar_metadata("../../config/config.yml")


def test_the_traversal_helper_rejects_escapes():
    assert serve._check_path_traversal(AVATAR_ROOT, "256x256/a.jpg") is True
    assert serve._check_path_traversal(AVATAR_ROOT, "../../etc/passwd") is False


# ---------------------------------------------------------------------------
# Content negotiation
# ---------------------------------------------------------------------------


def test_an_extensionless_url_redirects_to_a_concrete_format(client, avatar_set):
    response = client.get(f"/user-avatars/256x256/{avatar_set}")
    assert response.status_code == 302
    assert response.headers["Location"].endswith(f"/{avatar_set}.webp")


def test_negotiation_prefers_webp_when_the_client_accepts_it(client, avatar_set):
    response = client.get(
        f"/user-avatars/256x256/{avatar_set}",
        headers={"Accept": "image/webp,image/jpeg,*/*"},
    )
    assert response.headers["Location"].endswith(".webp")


@pytest.mark.parametrize(
    ("accept", "expected_ext"),
    [("image/png", "png"), ("image/jpeg", "jpg")],
)
def test_negotiation_honors_an_accept_header_that_differs_from_the_fallback(
    client, avatar_set, accept, expected_ext
):
    # The no-Accept fallback is webp here, so asking for png or jpg is what
    # actually proves the Accept header is being read rather than ignored.
    response = client.get(
        f"/user-avatars/256x256/{avatar_set}", headers={"Accept": accept}
    )
    assert response.headers["Location"].endswith(f".{expected_ext}")


def test_negotiation_falls_back_when_the_preferred_format_is_not_configured(
    client, avatar_set
):
    # The client wants AVIF, but AVIF is not generated in this deployment.
    response = client.get(
        f"/user-avatars/256x256/{avatar_set}", headers={"Accept": "image/avif"}
    )
    assert response.headers["Location"].endswith(".webp")


def test_negotiation_answers_a_client_that_sends_no_accept_header(client, avatar_set):
    response = client.get(f"/user-avatars/256x256/{avatar_set}", headers={"Accept": ""})
    assert response.status_code == 302


def test_negotiation_marks_the_response_as_varying_on_accept(client, avatar_set):
    # Without this a CDN would cache one format for every client.
    response = client.get(f"/user-avatars/256x256/{avatar_set}")
    assert response.headers["Vary"] == "Accept"
    assert response.headers["Cache-Control"] == "no-store"


def test_negotiation_rejects_an_unconfigured_size(client, avatar_set):
    assert client.get(f"/user-avatars/999x999/{avatar_set}").status_code == 404


def test_following_the_redirect_serves_the_image(client, avatar_set):
    response = client.get(
        f"/user-avatars/256x256/{avatar_set}",
        headers={"Accept": "image/webp"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert response.mimetype == "image/webp"


# ---------------------------------------------------------------------------
# Metadata access control (owner_only in the test config)
# ---------------------------------------------------------------------------


def test_the_owner_can_read_their_metadata(authed_client, avatar_set):
    response = authed_client.get(f"/user-avatars/_metadata/{avatar_set}.meta.json")
    assert response.status_code == 200
    assert response.get_json()["user_pk"] == 42
    assert response.headers["Cache-Control"] == "no-store"


def test_another_user_gets_404_not_403(flask_app, avatar_set):
    # 404 for both "missing" and "not yours" so the two are indistinguishable.
    other = flask_app.test_client()
    login(other, {"pk": 99, "username": "other", "name": "", "email": ""})
    assert (
        other.get(f"/user-avatars/_metadata/{avatar_set}.meta.json").status_code == 404
    )


def test_an_anonymous_visitor_is_redirected_to_login(client, avatar_set):
    response = client.get(f"/user-avatars/_metadata/{avatar_set}.meta.json")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_a_missing_metadata_file_is_a_404(authed_client):
    assert (
        authed_client.get("/user-avatars/_metadata/nope.meta.json").status_code == 404
    )


def test_metadata_traversal_shaped_urls_never_reach_the_handler(authed_client):
    assert (
        authed_client.get("/user-avatars/_metadata/..%2f..%2fconfig.yml").status_code
        == 404
    )


def test_authed_user_mode_lets_any_signed_in_user_read(
    flask_app, avatar_set, monkeypatch
):
    monkeypatch.setattr(serve, "_METADATA_ACCESS_MODE", "authed_user")
    other = flask_app.test_client()
    login(other, {"pk": 99, "username": "other", "name": "", "email": ""})
    assert (
        other.get(f"/user-avatars/_metadata/{avatar_set}.meta.json").status_code == 200
    )


def test_public_mode_serves_metadata_without_a_session(client, avatar_set, monkeypatch):
    monkeypatch.setattr(serve, "_METADATA_ACCESS_MODE", "public")
    response = client.get(f"/user-avatars/_metadata/{avatar_set}.meta.json")
    assert response.status_code == 200
    assert response.mimetype == "application/json"


# ---------------------------------------------------------------------------
# Health-probe metadata endpoint
# ---------------------------------------------------------------------------


def test_the_check_probe_returns_a_static_payload(authed_client):
    response = authed_client.get("/user-avatars/_metadata/CHECK.meta.json")
    assert response.status_code == 200
    assert response.get_json() == {"status": "ok"}
    assert response.headers["Cache-Control"] == "no-store"


def test_the_check_probe_still_requires_authentication(client):
    # Otherwise it would let anyone confirm the endpoint is reachable.
    response = client.get("/user-avatars/_metadata/CHECK.meta.json")
    assert response.status_code == 302


def test_the_check_probe_shadows_a_real_file_of_the_same_name(authed_client):
    (METADATA_ROOT / "CHECK.meta.json").write_text(
        json.dumps({"filename": "CHECK", "user_pk": 1234, "secret": "leaked"}),
        encoding="utf-8",
    )
    response = authed_client.get("/user-avatars/_metadata/CHECK.meta.json")
    assert response.get_json() == {"status": "ok"}


def test_the_check_probe_is_reachable_without_a_session_in_public_mode(
    client, monkeypatch
):
    monkeypatch.setattr(serve, "_METADATA_ACCESS_MODE", "public")
    assert client.get("/user-avatars/_metadata/CHECK.meta.json").status_code == 200


# ---------------------------------------------------------------------------
# Format negotiation helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dimensions", "expected"),
    [("256x256", True), ("256x128", False), ("12x12", False), ("x", False)],
)
def test_dimension_validation(dimensions, expected):
    assert serve._validate_dimensions(dimensions) is expected
