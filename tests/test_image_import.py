"""Tests for src/image_import.py - the outbound fetch / SSRF boundary.

``/api/fetch-url`` lets an authenticated user make this server issue an HTTP
request to an address they choose.  Everything that keeps that from becoming an
internal-network proxy lives in this module: the scheme allow-list, the
per-redirect-hop private-IP check, the response size ceiling and the MIME
allow-list.  Each of those gets a test here.
"""

import hashlib
import socket

import pytest
import requests

import src.image_import as image_import
from src.image_import import (
    FetchFailed,
    GravatarNotFound,
    ImageTooLarge,
    UnsupportedContentType,
    build_gravatar_url,
    fetch_gravatar_image,
    fetch_remote_image,
    read_with_limit,
    resolves_to_private_ip,
    safe_fetch,
    validate_gravatar_email,
    validate_import_url,
)
from tests.helpers import FakeResponse, image_bytes


def addrinfo(*ips):
    """Build a getaddrinfo() return value for the given IP strings."""
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in ips]


def png_response(url="https://images.example.com/a.png", **overrides):
    """Build a successful image response with a valid Content-Type."""
    kwargs = {
        "content": image_bytes((100, 100), "PNG"),
        "headers": {"Content-Type": "image/png"},
        "url": url,
    }
    kwargs.update(overrides)
    return FakeResponse(**kwargs)


# ---------------------------------------------------------------------------
# Size limiting
# ---------------------------------------------------------------------------


def test_read_with_limit_returns_the_full_body_when_it_fits():
    response = FakeResponse(content=b"x" * 500)
    assert read_with_limit(response) == b"x" * 500


def test_read_with_limit_rejects_early_on_a_declared_content_length(monkeypatch):
    monkeypatch.setattr(image_import, "_MAX_FETCH_SIZE", 100)
    response = FakeResponse(content=b"x" * 10, headers={"Content-Length": "999999"})

    assert read_with_limit(response) is None
    # The body was never streamed - the header alone ended it.
    assert response.closed


def test_read_with_limit_ignores_a_non_numeric_content_length(monkeypatch):
    monkeypatch.setattr(image_import, "_MAX_FETCH_SIZE", 100)
    response = FakeResponse(content=b"x" * 10, headers={"Content-Length": "many"})
    assert read_with_limit(response) == b"x" * 10


def test_read_with_limit_aborts_a_lying_server_mid_stream(monkeypatch):
    # No (or an understated) Content-Length: the streaming guard is the backstop.
    monkeypatch.setattr(image_import, "_MAX_FETCH_SIZE", 100)
    response = FakeResponse(content=b"x" * 5000)

    assert read_with_limit(response) is None
    assert response.closed


# ---------------------------------------------------------------------------
# Private-IP resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",  # loopback
        "10.1.2.3",  # RFC1918
        "192.168.0.5",  # RFC1918
        "172.16.9.9",  # RFC1918
        "169.254.169.254",  # link-local (cloud metadata)
        "0.0.0.0",  # unspecified
        "100.64.0.1",  # carrier-grade NAT
    ],
)
def test_non_global_addresses_are_blocked(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: addrinfo(ip))
    assert resolves_to_private_ip("target.example.com") is True


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8"])
def test_globally_routable_addresses_are_allowed(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: addrinfo(ip))
    assert resolves_to_private_ip("example.com") is False


def test_a_hostname_with_any_private_answer_is_blocked(monkeypatch):
    # A split-horizon / multi-answer name must not slip through on one good IP.
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *a, **kw: addrinfo("93.184.216.34", "127.0.0.1")
    )
    assert resolves_to_private_ip("mixed.example.com") is True


def test_dns_failure_fails_closed(monkeypatch):
    def boom(*_a, **_kw):
        raise socket.gaierror("NXDOMAIN")

    monkeypatch.setattr(socket, "getaddrinfo", boom)
    assert resolves_to_private_ip("does-not-resolve.invalid") is True


def test_ipv6_scope_ids_are_stripped_before_parsing(monkeypatch):
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", 0, 0, 2))
        ],
    )
    # Link-local IPv6 is not global, so it must be blocked (and must not raise).
    assert resolves_to_private_ip("v6.example.com") is True


# ---------------------------------------------------------------------------
# safe_fetch - scheme checks and per-hop redirect validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://x/", "ftp://x/f"])
def test_non_http_schemes_are_refused(url, allow_any_host):
    with pytest.raises(ValueError, match="non-HTTP"):
        safe_fetch(url)


def test_a_url_without_a_hostname_is_refused(allow_any_host):
    with pytest.raises(ValueError, match="no hostname"):
        safe_fetch("http:///just-a-path")


def test_a_private_target_is_refused_before_any_connection(import_session):
    # No handler is installed, so reaching the session at all would raise
    # AssertionError - proving the SSRF check runs before the request.
    with pytest.raises(ValueError, match="private/internal address blocked"):
        safe_fetch("http://127.0.0.1/admin")
    assert import_session.calls == []


def test_redirects_are_never_followed_by_the_http_client_itself(
    import_session, allow_any_host
):
    """Every request must opt out of automatic redirect following.

    The whole per-hop SSRF design depends on this: if ``requests`` follows
    redirects itself, only the original URL is ever checked and a
    server-controlled redirect to 127.0.0.1 walks straight through.  The
    per-hop tests below cannot see this on their own, because a stubbed session
    ignores the flag - so it is asserted directly.
    """
    responses = [
        FakeResponse(
            status_code=302,
            headers={"Location": "https://images.example.com/final.png"},
            url="https://start.example.com/a",
        ),
        png_response(),
    ]
    import_session.handler = lambda *a, **kw: responses.pop(0)

    safe_fetch("https://start.example.com/a")

    assert import_session.calls
    for call in import_session.calls:
        assert call.kwargs["allow_redirects"] is False, (
            f"{call.url} was fetched with automatic redirects enabled"
        )


def test_redirects_are_followed_and_the_final_response_is_returned(
    import_session, allow_any_host
):
    responses = [
        FakeResponse(
            status_code=302,
            headers={"Location": "https://images.example.com/final.png"},
            url="https://start.example.com/a",
        ),
        png_response(),
    ]
    import_session.handler = lambda *a, **kw: responses.pop(0)

    result = safe_fetch("https://start.example.com/a")
    assert result.status_code == 200
    assert [call.url for call in import_session.calls] == [
        "https://start.example.com/a",
        "https://images.example.com/final.png",
    ]


def test_a_relative_redirect_is_resolved_against_the_current_url(
    import_session, allow_any_host
):
    responses = [
        FakeResponse(
            status_code=301,
            headers={"Location": "/images/final.png"},
            url="https://start.example.com/a/b",
        ),
        png_response(),
    ]
    import_session.handler = lambda *a, **kw: responses.pop(0)

    safe_fetch("https://start.example.com/a/b")
    assert import_session.calls[1].url == "https://start.example.com/images/final.png"


def test_every_redirect_hop_is_re_checked_against_the_ssrf_filter(
    import_session, monkeypatch
):
    # The first host is public, the redirect target resolves to loopback: the
    # classic redirect-based SSRF bypass, which per-hop checking must stop.
    def resolver(hostname):
        return hostname == "internal.example.com"

    monkeypatch.setattr(image_import, "resolves_to_private_ip", resolver)
    import_session.handler = lambda *a, **kw: FakeResponse(
        status_code=302,
        headers={"Location": "http://internal.example.com/secrets"},
        url="https://public.example.com/",
    )

    with pytest.raises(ValueError, match="private/internal address blocked"):
        safe_fetch("https://public.example.com/")
    # Only the first hop was ever requested.
    assert len(import_session.calls) == 1


def test_a_redirect_to_a_non_http_scheme_is_refused(import_session, allow_any_host):
    import_session.handler = lambda *a, **kw: FakeResponse(
        status_code=302,
        headers={"Location": "file:///etc/shadow"},
        url="https://start.example.com/",
    )
    with pytest.raises(ValueError, match="non-HTTP"):
        safe_fetch("https://start.example.com/")


def test_a_redirect_without_a_location_header_is_refused(
    import_session, allow_any_host
):
    import_session.handler = lambda *a, **kw: FakeResponse(status_code=302)
    with pytest.raises(ValueError, match="missing Location"):
        safe_fetch("https://start.example.com/")


def test_a_redirect_loop_is_bounded(import_session, allow_any_host):
    import_session.handler = lambda *a, **kw: FakeResponse(
        status_code=302,
        headers={"Location": "https://loop.example.com/next"},
        url="https://loop.example.com/",
    )
    with pytest.raises(ValueError, match="Too many redirects"):
        safe_fetch("https://loop.example.com/")
    assert len(import_session.calls) == image_import.MAX_REDIRECTS + 1


def test_ssrf_checks_are_skipped_when_the_operator_disables_them(
    import_session, monkeypatch
):
    monkeypatch.setattr(image_import, "RESTRICT_PRIVATE_IPS", False)
    import_session.handler = lambda *a, **kw: png_response()
    # A loopback target now goes through - the documented opt-out.
    assert safe_fetch("http://127.0.0.1/avatar.png").status_code == 200


# ---------------------------------------------------------------------------
# Content-Type allow-list
# ---------------------------------------------------------------------------


def test_content_type_parameters_are_ignored(import_session, allow_any_host):
    import_session.handler = lambda *a, **kw: png_response(
        headers={"Content-Type": "IMAGE/PNG; charset=binary"}
    )
    _data, content_type = fetch_remote_image("https://images.example.com/a.png")
    assert content_type == "image/png"


@pytest.mark.parametrize(
    "content_type",
    ["text/html", "image/svg+xml", "application/octet-stream", ""],
)
def test_disallowed_content_types_are_rejected(
    import_session, allow_any_host, content_type
):
    import_session.handler = lambda *a, **kw: png_response(
        headers={"Content-Type": content_type}
    )
    with pytest.raises(UnsupportedContentType):
        fetch_remote_image("https://images.example.com/a")


def test_an_oversized_remote_image_is_rejected(
    import_session, allow_any_host, monkeypatch
):
    monkeypatch.setattr(image_import, "_MAX_FETCH_SIZE", 64)
    import_session.handler = lambda *a, **kw: png_response()
    with pytest.raises(ImageTooLarge):
        fetch_remote_image("https://images.example.com/a.png")


def test_a_network_error_is_wrapped_in_fetch_failed(import_session, allow_any_host):
    import_session.handler = lambda *a, **kw: requests.exceptions.ConnectTimeout("nope")
    with pytest.raises(FetchFailed):
        fetch_remote_image("https://images.example.com/a.png")


def test_an_http_error_status_is_wrapped_in_fetch_failed(
    import_session, allow_any_host
):
    import_session.handler = lambda *a, **kw: png_response(status_code=500)
    with pytest.raises(FetchFailed):
        fetch_remote_image("https://images.example.com/a.png")


def test_a_successful_remote_fetch_returns_bytes_and_type(
    import_session, allow_any_host
):
    payload = image_bytes((80, 80), "PNG")
    import_session.handler = lambda *a, **kw: png_response(content=payload)
    assert fetch_remote_image("https://images.example.com/a.png") == (
        payload,
        "image/png",
    )


# ---------------------------------------------------------------------------
# Gravatar
# ---------------------------------------------------------------------------


def test_gravatar_url_hashes_the_normalized_email():
    url, digest = build_gravatar_url("  User@Example.COM ", size=512)
    expected = hashlib.md5(b"user@example.com", usedforsecurity=False).hexdigest()
    assert digest == expected
    assert url == f"https://www.gravatar.com/avatar/{expected}?s=512&d=404"


def test_gravatar_url_requests_a_404_instead_of_a_default_image():
    # d=404 is what lets the sync distinguish "no Gravatar" from "some image".
    url, _digest = build_gravatar_url("a@b.com")
    assert url.endswith("&d=404")


def test_gravatar_fetch_returns_bytes_type_and_a_hash_based_filename(
    import_session, allow_any_host
):
    payload = image_bytes((100, 100), "JPEG")
    import_session.handler = lambda *a, **kw: FakeResponse(
        content=payload, headers={"Content-Type": "image/jpeg"}
    )

    data, content_type, filename = fetch_gravatar_image("user@example.com")

    _url, digest = build_gravatar_url("user@example.com")
    assert (data, content_type) == (payload, "image/jpeg")
    assert filename == f"{digest}.jpg"


def test_gravatar_404_raises_gravatar_not_found(import_session, allow_any_host):
    import_session.handler = lambda *a, **kw: FakeResponse(status_code=404)
    with pytest.raises(GravatarNotFound):
        fetch_gravatar_image("nobody@example.com")


def test_gravatar_network_failure_raises_fetch_failed(import_session, allow_any_host):
    import_session.handler = lambda *a, **kw: requests.exceptions.ConnectionError("x")
    with pytest.raises(FetchFailed):
        fetch_gravatar_image("user@example.com")


def test_gravatar_ssrf_rejection_is_reported_as_a_fetch_failure(
    import_session, monkeypatch
):
    # Route validation failures to FetchFailed rather than letting a raw
    # ValueError bubble into a 500 from the route handler.
    monkeypatch.setattr(image_import, "resolves_to_private_ip", lambda _h: True)
    with pytest.raises(FetchFailed):
        fetch_gravatar_image("user@example.com")


def test_gravatar_gif_response_keeps_its_extension(import_session, allow_any_host):
    import_session.handler = lambda *a, **kw: FakeResponse(
        content=b"GIF89a" + b"\x00" * 32, headers={"Content-Type": "image/gif"}
    )
    _data, _ct, filename = fetch_gravatar_image("user@example.com")
    assert filename.endswith(".gif")


# ---------------------------------------------------------------------------
# Input validation helpers
# ---------------------------------------------------------------------------


def test_gravatar_email_must_match_the_session_email():
    # Otherwise the endpoint becomes an "does this address have a Gravatar" oracle.
    assert validate_gravatar_email("me@x.com", "me@x.com", "u") is None
    assert validate_gravatar_email("other@x.com", "me@x.com", "u") == "email_mismatch"


def test_gravatar_lookup_is_refused_when_the_session_has_no_email(monkeypatch):
    monkeypatch.setattr(image_import, "GRAVATAR_RESTRICT_EMAIL", True)
    assert validate_gravatar_email("me@x.com", "", "u") == "email_mismatch"


def test_gravatar_session_email_is_compared_case_insensitively():
    assert validate_gravatar_email("me@x.com", "  Me@X.com ", "u") is None


def test_gravatar_any_email_is_allowed_when_the_restriction_is_off(monkeypatch):
    monkeypatch.setattr(image_import, "GRAVATAR_RESTRICT_EMAIL", False)
    assert validate_gravatar_email("someone@else.com", "", "u") is None


@pytest.mark.parametrize("url", ["ftp://x/a.png", "javascript:alert(1)", "/relative"])
def test_import_url_scheme_is_restricted_to_http(url, allow_any_host):
    assert validate_import_url(url) is not None


def test_import_url_requires_a_hostname(allow_any_host):
    assert validate_import_url("http:///nohost") is not None


def test_import_url_rejects_a_private_target(monkeypatch):
    monkeypatch.setattr(image_import, "resolves_to_private_ip", lambda _h: True)
    assert validate_import_url("http://internal/a.png") == "url_not_allowed"


def test_import_url_accepts_a_public_https_target(allow_any_host):
    assert validate_import_url("https://images.example.com/a.png") is None


# ---------------------------------------------------------------------------
# Session hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("domain", ["example.com", "images.example.com", ""])
def test_the_shared_fetch_session_refuses_all_cookies(domain):
    # The fetch session is shared across every user in the worker process, so a
    # Set-Cookie from one user's target must never be replayed on another
    # user's later fetch to the same host.  An empty allow-list makes the jar
    # reject every domain outright.
    policy = image_import._NO_COOKIES_POLICY
    assert policy.allowed_domains() == ()
    assert policy.is_not_allowed(domain) is True
