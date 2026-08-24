"""Tests for src/authentik.py - the Authentik Admin API client.

The two behaviors most worth pinning here are the read-modify-write merge in
``_patch_user`` (a naive PATCH would wipe every unrelated user attribute) and
the page-number-driven pagination loop (Authentik returns an integer ``next``,
not a URL, and a misbehaving server must not be able to loop forever).
"""

import pytest
import requests

import src.authentik as authentik
from src.authentik import (
    _iter_users,
    _normalize_user,
    _parse_json,
    _retry_request,
    get_user,
    list_active_user_pks,
    list_all_user_pks,
    list_users,
    remove_avatar_url,
    retrieve_user,
    revert_avatar_url,
    update_avatar_url,
)
from tests.helpers import FakeResponse

USERS_URL = "https://auth.test.example.com/api/v3/core/users/"


def user_page(results, next_page=0):
    """Build one page of Authentik's paginated user list response."""
    return FakeResponse(
        json_data={"results": results, "pagination": {"next": next_page}}
    )


def user_object(pk=42, **overrides):
    """Build a raw Authentik user object with sensible defaults."""
    user = {
        "pk": pk,
        "username": f"user{pk}",
        "name": f"User {pk}",
        "email": f"user{pk}@example.com",
        "is_active": True,
        "attributes": {},
    }
    user.update(overrides)
    return user


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def test_parse_json_returns_the_decoded_dict():
    assert _parse_json(FakeResponse(json_data={"pk": 1})) == {"pk": 1}


def test_parse_json_raises_a_clear_error_on_a_non_json_body():
    response = FakeResponse(content=b"<html>gateway error</html>", status_code=502)
    with pytest.raises(ValueError, match="non-JSON response"):
        _parse_json(response)


def test_parse_json_rejects_a_json_list_where_a_dict_is_expected():
    with pytest.raises(TypeError, match="unexpected JSON type"):
        _parse_json(FakeResponse(json_data=[1, 2, 3]))


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------


def test_retry_recovers_from_a_transient_connection_error(monkeypatch):
    monkeypatch.setattr(authentik.time, "sleep", lambda _s: None)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise requests.exceptions.ConnectionError("connection refused")
        return "recovered"

    assert _retry_request(flaky) == "recovered"
    assert len(attempts) == 3


def test_retry_gives_up_after_the_configured_maximum(monkeypatch):
    monkeypatch.setattr(authentik.time, "sleep", lambda _s: None)
    attempts = []

    def always_timing_out():
        attempts.append(1)
        raise requests.exceptions.Timeout("timed out")

    with pytest.raises(requests.exceptions.Timeout):
        _retry_request(always_timing_out)
    assert len(attempts) == authentik._RETRY_MAX


def test_http_errors_are_not_retried(monkeypatch):
    # A 4xx/5xx means the server processed the request - repeating it is wrong.
    monkeypatch.setattr(authentik.time, "sleep", lambda _s: None)
    attempts = []

    def server_error():
        attempts.append(1)
        raise requests.exceptions.HTTPError("500 Server Error")

    with pytest.raises(requests.exceptions.HTTPError):
        _retry_request(server_error)
    assert len(attempts) == 1


# ---------------------------------------------------------------------------
# retrieve_user
# ---------------------------------------------------------------------------


def test_retrieve_user_returns_pk_and_the_custom_avatar_attribute(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page(
        [user_object(7, attributes={"avatar": "https://cdn/x.jpg"})]
    )

    assert retrieve_user("user7") == {"pk": 7, "avatar": "https://cdn/x.jpg"}
    assert authentik_session.calls[0].params == {"username": "user7"}


def test_retrieve_user_returns_an_empty_avatar_when_the_attribute_is_unset(
    authentik_session,
):
    authentik_session.handler = lambda *a, **kw: user_page([user_object(7)])
    assert retrieve_user("user7")["avatar"] == ""


def test_retrieve_user_raises_when_no_user_matches(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page([])
    with pytest.raises(ValueError, match="not found in Authentik"):
        retrieve_user("ghost")


def test_retrieve_user_uses_the_first_match_and_warns_on_duplicates(
    authentik_session, caplog
):
    authentik_session.handler = lambda *a, **kw: user_page(
        [user_object(1), user_object(2)]
    )
    with caplog.at_level("WARNING", logger="authentik"):
        assert retrieve_user("dup")["pk"] == 1
    assert "username_claim" in caplog.text


def test_retrieve_user_rejects_a_non_integer_pk(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page([user_object("abc")])
    with pytest.raises(TypeError, match="non-integer PK"):
        retrieve_user("weird")


def test_retrieve_user_tolerates_a_non_dict_attributes_field(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page(
        [user_object(3, attributes=None)]
    )
    assert retrieve_user("user3") == {"pk": 3, "avatar": ""}


# ---------------------------------------------------------------------------
# get_user
# ---------------------------------------------------------------------------


def test_get_user_fetches_a_single_user_by_pk(authentik_session):
    authentik_session.handler = lambda *a, **kw: FakeResponse(json_data=user_object(9))
    assert get_user(9)["pk"] == 9
    assert authentik_session.calls[0].url == f"{USERS_URL}9/"


# ---------------------------------------------------------------------------
# _patch_user merge semantics (via update_avatar_url)
# ---------------------------------------------------------------------------


def test_update_avatar_url_merges_attributes_without_dropping_siblings(
    authentik_session,
):
    current = user_object(
        42,
        attributes={
            "avatar": "https://cdn/old.jpg",
            "avatar_id": "old-id",
            "ldap_uniq": "S-1-5-21",
            "unrelated": "keep-me",
        },
    )
    sent = {}

    def handler(method, url, **kwargs):
        if method == "GET":
            return FakeResponse(json_data=current)
        sent["payload"] = kwargs["json"]
        merged = {**current, **kwargs["json"]}
        return FakeResponse(json_data=merged)

    authentik_session.handler = handler

    attrs, old_url, old_id = update_avatar_url(42, "https://cdn/new.jpg", "new-id")

    # Sibling attributes survive the PATCH - the whole point of the read-merge.
    assert sent["payload"]["attributes"] == {
        "avatar": "https://cdn/new.jpg",
        "avatar_id": "new-id",
        "ldap_uniq": "S-1-5-21",
        "unrelated": "keep-me",
    }
    assert (old_url, old_id) == ("https://cdn/old.jpg", "old-id")
    assert attrs["ldap_uniq"] == "S-1-5-21"


def test_update_avatar_url_reports_no_previous_values_for_a_first_upload(
    authentik_session,
):
    current = user_object(42)

    def handler(method, url, **kwargs):
        if method == "GET":
            return FakeResponse(json_data=current)
        return FakeResponse(json_data={**current, **kwargs["json"]})

    authentik_session.handler = handler
    _attrs, old_url, old_id = update_avatar_url(42, "https://cdn/new.jpg", "new-id")
    assert old_url is None and old_id is None


def test_update_avatar_url_rejects_a_non_dict_attributes_field(authentik_session):
    authentik_session.handler = lambda *a, **kw: FakeResponse(
        json_data=user_object(42, attributes=["not", "a", "dict"])
    )
    with pytest.raises(TypeError, match="unexpected attributes type"):
        update_avatar_url(42, "https://cdn/new.jpg", "new-id")


def test_update_avatar_url_warns_when_the_api_echoes_back_a_different_value(
    authentik_session, caplog
):
    current = user_object(42)

    def handler(method, url, **kwargs):
        if method == "GET":
            return FakeResponse(json_data=current)
        # Server silently stored something else.
        return FakeResponse(
            json_data={"attributes": {"avatar": "https://cdn/other.jpg"}}
        )

    authentik_session.handler = handler
    with caplog.at_level("WARNING", logger="authentik"):
        update_avatar_url(42, "https://cdn/new.jpg", "new-id")
    assert "instead of expected" in caplog.text


def test_dry_run_skips_the_patch_but_still_performs_the_get(
    authentik_session, monkeypatch
):
    monkeypatch.setattr(authentik, "skip_backend_writes", True)
    current = user_object(42, attributes={"avatar": "https://cdn/old.jpg"})
    authentik_session.handler = lambda *a, **kw: FakeResponse(json_data=current)

    attrs, old_url, _old_id = update_avatar_url(42, "https://cdn/new.jpg", "new-id")

    assert authentik_session.methods == ["GET"]
    assert old_url == "https://cdn/old.jpg"
    assert attrs == current["attributes"]


def test_remove_avatar_url_nulls_both_attributes(authentik_session):
    current = user_object(42, attributes={"avatar": "x", "avatar_id": "y", "keep": 1})
    sent = {}

    def handler(method, url, **kwargs):
        if method == "GET":
            return FakeResponse(json_data=current)
        sent["payload"] = kwargs["json"]
        return FakeResponse(json_data=current)

    authentik_session.handler = handler
    remove_avatar_url(42)

    assert sent["payload"]["attributes"]["avatar"] is None
    assert sent["payload"]["attributes"]["avatar_id"] is None
    # Nulling must not take unrelated attributes with it.
    assert sent["payload"]["attributes"]["keep"] == 1


def test_revert_avatar_url_restores_the_previous_values(authentik_session):
    current = user_object(42, attributes={"avatar": "new", "avatar_id": "new-id"})
    sent = {}

    def handler(method, url, **kwargs):
        if method == "GET":
            return FakeResponse(json_data=current)
        sent["payload"] = kwargs["json"]
        return FakeResponse(json_data=current)

    authentik_session.handler = handler
    revert_avatar_url(42, "old-url", "old-id")

    assert sent["payload"]["attributes"] == {
        "avatar": "old-url",
        "avatar_id": "old-id",
    }


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


def bounded_handler(response_for_page, max_pages=10):
    """Answer paginated requests, but refuse to serve more than *max_pages*.

    Pagination bugs manifest as an unbounded loop that accumulates results until
    the process runs out of memory - which in CI means a timed-out job rather
    than a test failure.  Capping the fake server turns that into an immediate,
    readable failure instead.
    """

    def handler(method, url, **kwargs):
        page = kwargs["params"]["page"]
        if len(handler.pages_served) >= max_pages:
            raise AssertionError(
                f"pagination did not terminate: {max_pages} pages requested "
                f"(pages seen: {handler.pages_served})"
            )
        handler.pages_served.append(page)
        return response_for_page(page)

    handler.pages_served = []
    return handler


def test_iter_users_follows_the_integer_next_page_number(authentik_session):
    pages = {
        1: user_page([user_object(1), user_object(2)], next_page=2),
        2: user_page([user_object(3)], next_page=0),
    }
    authentik_session.handler = bounded_handler(lambda page: pages[page])

    assert [u["pk"] for u in _iter_users()] == [1, 2, 3]


def test_iter_users_stops_when_next_does_not_advance(authentik_session):
    # A server that keeps answering "next: 1" must not spin forever.  The fake
    # server stops after a handful of pages so a regression fails here instead
    # of exhausting memory.
    authentik_session.handler = bounded_handler(
        lambda _page: user_page([user_object(1)], next_page=1)
    )

    assert [u["pk"] for u in _iter_users()] == [1]


def test_iter_users_stops_when_next_moves_backwards(authentik_session):
    # A server that answers with a lower page number must terminate too - the
    # guard is "does not advance", not merely "not equal".
    authentik_session.handler = bounded_handler(
        lambda page: user_page([user_object(page)], next_page=max(1, page - 1))
    )

    assert [u["pk"] for u in _iter_users()] == [1]


def test_iter_users_stops_when_next_is_not_an_integer(authentik_session):
    authentik_session.handler = lambda *a, **kw: FakeResponse(
        json_data={"results": [user_object(1)], "pagination": {"next": "2"}}
    )
    assert len(list(_iter_users())) == 1


def test_iter_users_raises_when_results_is_not_a_list(authentik_session):
    authentik_session.handler = lambda *a, **kw: FakeResponse(
        json_data={"results": {"pk": 1}, "pagination": {"next": 0}}
    )
    with pytest.raises(TypeError, match="non-list results"):
        list(_iter_users())


def test_active_only_adds_the_is_active_filter(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page([user_object(1)])
    list(_iter_users(active_only=True))
    assert authentik_session.calls[0].params["is_active"] == "true"


def test_all_user_pks_does_not_filter_on_active(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page(
        [user_object(1), user_object(2)]
    )
    assert list_all_user_pks() == {1, 2}
    assert "is_active" not in authentik_session.calls[0].params


def test_active_user_pks_filters_on_active(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page([user_object(1)])
    assert list_active_user_pks() == {1}
    assert authentik_session.calls[0].params["is_active"] == "true"


def test_user_pk_collection_skips_entries_without_an_integer_pk(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page(
        [user_object(1), user_object(None), {"username": "no-pk-at-all"}]
    )
    assert list_all_user_pks() == {1}


# ---------------------------------------------------------------------------
# Normalization for the Gravatar sync
# ---------------------------------------------------------------------------


def test_normalize_user_lowercases_and_trims_the_email():
    # Gravatar keys images on the lowercase, trimmed address.
    normalized = _normalize_user(user_object(5, email="  MiXeD@Example.COM "))
    assert normalized["email"] == "mixed@example.com"


def test_normalize_user_returns_none_without_an_integer_pk():
    assert _normalize_user({"username": "x"}) is None
    assert _normalize_user(user_object("nope")) is None


def test_normalize_user_replaces_null_fields_with_empty_defaults():
    normalized = _normalize_user(
        {"pk": 5, "username": None, "name": None, "email": None, "attributes": None}
    )
    assert normalized == {
        "pk": 5,
        "username": "",
        "name": "",
        "email": "",
        "is_active": True,
        "attributes": {},
    }


def test_list_users_drops_unusable_records(authentik_session):
    authentik_session.handler = lambda *a, **kw: user_page(
        [user_object(1), {"username": "broken"}, user_object(2)]
    )
    assert [u["pk"] for u in list_users()] == [1, 2]
