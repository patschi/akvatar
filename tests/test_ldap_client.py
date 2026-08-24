"""Tests for src/ldap_client.py - directory writes and server failover.

Nothing here opens a real socket: ``ldap3.Connection`` is replaced with a stub
so the tests can drive the failover loop, the LDAP-injection escaping and the
modify result handling deterministically.
"""

import pytest

import src.ldap_client as ldap_client
from src.ldap_client import (
    _apply_modifications,
    _describe_value,
    _find_user_dn,
    get_photos_config,
    is_enabled,
    update_photos,
)


class FakeEntry:
    def __init__(self, dn: str) -> None:
        self.entry_dn = dn


class FakeConnection:
    """Stands in for ldap3.Connection - records searches and modifications."""

    def __init__(self, entries=None, modify_result=None) -> None:
        self.entries = entries if entries is not None else [FakeEntry("CN=Test,DC=x")]
        self.result = modify_result or {"result": 0, "description": "success"}
        self.searches: list[dict] = []
        self.modifications: list[tuple] = []
        self.unbound = False
        self.search_returns = True

    def search(self, search_base, search_filter, attributes):
        self.searches.append(
            {"base": search_base, "filter": search_filter, "attributes": attributes}
        )
        return self.search_returns

    def modify(self, dn, changes):
        self.modifications.append((dn, changes))

    def unbind(self):
        self.unbound = True


@pytest.fixture
def fake_connect(monkeypatch):
    """Install a stub _connect() and return the connection it hands out."""
    connection = FakeConnection()
    monkeypatch.setattr(ldap_client, "_connect", lambda: connection)
    return connection


# ---------------------------------------------------------------------------
# Configuration surface
# ---------------------------------------------------------------------------


def test_enabled_flag_and_photo_config_are_exposed_from_config():
    assert is_enabled() is True
    assert [photo["attribute"] for photo in get_photos_config()] == [
        "thumbnailPhoto",
        "photoURL",
    ]


def test_server_objects_derive_ssl_and_port_from_each_url():
    # "ldaps://host" implies SSL on the default port; "ldap://host:389" does not.
    secure, plain = ldap_client._servers
    assert (secure.ssl, secure.port) == (True, 636)
    assert (plain.ssl, plain.port) == (False, 389)


# ---------------------------------------------------------------------------
# Logging helper
# ---------------------------------------------------------------------------


def test_binary_values_are_described_by_length_not_content():
    # Photo bytes must never be dumped into the log.
    assert _describe_value(b"\x00" * 1234) == "1234 bytes"
    assert _describe_value("https://cdn/x.jpg") == "'https://cdn/x.jpg'"


# ---------------------------------------------------------------------------
# User lookup
# ---------------------------------------------------------------------------


def test_the_search_filter_substitutes_the_unique_identifier(fake_connect):
    assert _find_user_dn(fake_connect, "S-1-5-21-99") == "CN=Test,DC=x"
    assert fake_connect.searches[0]["filter"] == "(objectSid=S-1-5-21-99)"


def test_filter_metacharacters_are_escaped(fake_connect):
    # Without escaping, ")(uid=*" would rewrite the filter into a wildcard match.
    _find_user_dn(fake_connect, "abc)(uid=*")
    used_filter = fake_connect.searches[0]["filter"]
    assert "*" not in used_filter.replace("(objectSid=", "").replace(")", "")
    assert "\\2a" in used_filter or "\\2A" in used_filter


def test_an_unmatched_search_raises(fake_connect):
    fake_connect.entries = []
    with pytest.raises(ValueError, match="LDAP user not found"):
        _find_user_dn(fake_connect, "missing")


def test_a_matched_entry_with_an_empty_dn_raises(fake_connect):
    fake_connect.entries = [FakeEntry("")]
    with pytest.raises(ValueError, match="DN is empty"):
        _find_user_dn(fake_connect, "weird")


def test_a_pathologically_long_identifier_is_refused_before_searching(fake_connect):
    with pytest.raises(ValueError, match="unreasonably long"):
        _find_user_dn(fake_connect, "x" * 513)
    assert fake_connect.searches == []


# ---------------------------------------------------------------------------
# Modification
# ---------------------------------------------------------------------------


def test_all_attributes_are_written_in_a_single_modify(fake_connect):
    updates = [
        {"attribute": "thumbnailPhoto", "value": b"\x01\x02"},
        {"attribute": "photoURL", "value": "https://cdn/x.webp"},
    ]
    _apply_modifications(fake_connect, "CN=Test,DC=x", updates)

    assert len(fake_connect.modifications) == 1
    dn, changes = fake_connect.modifications[0]
    assert dn == "CN=Test,DC=x"
    assert set(changes) == {"thumbnailPhoto", "photoURL"}
    # MODIFY_REPLACE semantics: the value replaces whatever was there.
    import ldap3

    assert changes["thumbnailPhoto"] == [(ldap3.MODIFY_REPLACE, [b"\x01\x02"])]
    assert changes["photoURL"] == [(ldap3.MODIFY_REPLACE, ["https://cdn/x.webp"])]


def test_a_rejected_modify_raises_with_the_server_diagnostics(fake_connect):
    fake_connect.result = {
        "result": 50,
        "description": "insufficientAccessRights",
        "message": "denied",
    }
    with pytest.raises(RuntimeError, match="insufficientAccessRights"):
        _apply_modifications(
            fake_connect, "CN=Test,DC=x", [{"attribute": "a", "value": b"x"}]
        )


# ---------------------------------------------------------------------------
# update_photos - the public entry point
# ---------------------------------------------------------------------------


def test_update_photos_searches_then_modifies_then_unbinds(fake_connect):
    update_photos("S-1-5-21", [{"attribute": "thumbnailPhoto", "value": b"\x01"}])

    assert fake_connect.searches
    assert fake_connect.modifications
    assert fake_connect.unbound is True


def test_update_photos_is_a_no_op_when_ldap_is_disabled(monkeypatch):
    monkeypatch.setattr(ldap_client, "_enabled", False)
    monkeypatch.setattr(
        ldap_client, "_connect", lambda: pytest.fail("must not connect when disabled")
    )
    update_photos("S-1-5-21", [{"attribute": "a", "value": b"x"}])


def test_update_photos_is_a_no_op_with_no_updates(monkeypatch):
    monkeypatch.setattr(
        ldap_client, "_connect", lambda: pytest.fail("must not connect with no updates")
    )
    update_photos("S-1-5-21", [])


def test_malformed_updates_are_rejected_before_any_network_call(monkeypatch):
    monkeypatch.setattr(
        ldap_client, "_connect", lambda: pytest.fail("must not connect on bad input")
    )
    with pytest.raises(ValueError, match="missing required key"):
        update_photos("S-1-5-21", [{"attribute": "a"}])


def test_dry_run_logs_the_intent_without_connecting(monkeypatch, caplog):
    monkeypatch.setattr(ldap_client, "skip_backend_writes", True)
    monkeypatch.setattr(
        ldap_client, "_connect", lambda: pytest.fail("must not connect in dry-run")
    )
    with caplog.at_level("INFO", logger="ldap"):
        update_photos("S-1-5-21", [{"attribute": "thumbnailPhoto", "value": b"\x01"}])
    assert "[DRY-RUN] Would update LDAP thumbnailPhoto" in caplog.text


def test_a_transient_connection_failure_is_retried(monkeypatch):
    attempts = []
    connection = FakeConnection()

    def flaky_connect():
        attempts.append(1)
        if len(attempts) == 1:
            raise ConnectionError("network unreachable")
        return connection

    monkeypatch.setattr(ldap_client, "_connect", flaky_connect)
    monkeypatch.setattr(ldap_client.time, "sleep", lambda _s: None)

    update_photos("S-1-5-21", [{"attribute": "a", "value": b"x"}])

    assert len(attempts) == 2
    assert connection.modifications


def test_a_persistent_connection_failure_propagates(monkeypatch):
    def always_failing():
        raise ConnectionError("all servers down")

    monkeypatch.setattr(ldap_client, "_connect", always_failing)
    monkeypatch.setattr(ldap_client.time, "sleep", lambda _s: None)

    with pytest.raises(ConnectionError, match="all servers down"):
        update_photos("S-1-5-21", [{"attribute": "a", "value": b"x"}])


def test_a_user_not_found_error_still_unbinds(monkeypatch):
    connection = FakeConnection(entries=[])
    monkeypatch.setattr(ldap_client, "_connect", lambda: connection)

    with pytest.raises(ValueError):
        update_photos("missing", [{"attribute": "a", "value": b"x"}])
    assert connection.unbound is True


# ---------------------------------------------------------------------------
# Server failover
# ---------------------------------------------------------------------------


def test_connect_falls_back_to_the_next_server(monkeypatch):
    import ldap3

    attempts = []

    class FlakyConnection:
        def __init__(self, server, **kwargs):
            attempts.append(server)
            if len(attempts) == 1:
                raise ldap3.core.exceptions.LDAPException("TLS handshake failed")

    monkeypatch.setattr(ldap3, "Connection", FlakyConnection)
    connection = ldap_client._connect()

    assert len(attempts) == 2
    assert isinstance(connection, FlakyConnection)


def test_connect_raises_when_every_server_fails(monkeypatch):
    import ldap3

    class AlwaysFailing:
        def __init__(self, server, **kwargs):
            raise ldap3.core.exceptions.LDAPException("refused")

    monkeypatch.setattr(ldap3, "Connection", AlwaysFailing)
    with pytest.raises(ConnectionError, match="All LDAP servers failed"):
        ldap_client._connect()
