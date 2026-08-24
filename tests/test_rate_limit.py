"""Tests for src/rate_limit.py - the sliding-window IP limiter and per-user cooldowns.

The production limiters keep their state in a ``multiprocessing.Manager`` server
process so it is shared across gunicorn workers.  Spawning one per test would be
slow and flaky, so the Manager is stubbed with an in-process equivalent
(``dict`` + ``threading.Lock``) that has the same proxy semantics the limiter
relies on.  The counting, eviction and whitelist logic under test is unchanged.
"""

import threading

import pytest
from flask import Flask

import src.rate_limit as rate_limit
from src.rate_limit import (
    COST_NORMAL,
    ENDPOINT_AVATARS,
    ENDPOINT_METADATA,
    _LimiterConfig,
    _RateLimiter,
    _RateLimitManager,
    _UserCooldown,
    check_gravatar_import_cooldown,
    check_upload_cooldown,
    check_url_import_cooldown,
    init_rate_limiting,
)


class StubManager:
    """In-process stand-in for multiprocessing.Manager()."""

    def dict(self):
        return {}

    def Lock(self):
        return threading.Lock()


@pytest.fixture
def stub_manager(monkeypatch):
    """Make every Manager() call inside rate_limit return the in-process stub."""
    monkeypatch.setattr(rate_limit.multiprocessing, "Manager", StubManager)
    # The fork-detach hook is meaningless without a real server process.
    monkeypatch.setattr(rate_limit, "_detach_manager_after_fork", lambda _m: None)


def make_limiter(max_points=10, window=60, eviction_interval=10) -> _RateLimiter:
    config = _LimiterConfig(
        name="test",
        max_points=max_points,
        window=window,
        eviction_interval=eviction_interval,
    )
    return _RateLimiter(config, {}, threading.Lock())


# ---------------------------------------------------------------------------
# Sliding-window counting
# ---------------------------------------------------------------------------


def test_requests_within_the_budget_are_allowed():
    limiter = make_limiter(max_points=3)
    assert [limiter.check("1.2.3.4", 1)[0] for _ in range(3)] == [True, True, True]


def test_the_request_that_would_exceed_the_budget_is_denied():
    limiter = make_limiter(max_points=3)
    for _ in range(3):
        limiter.check("1.2.3.4", 1)

    allowed, retry_after = limiter.check("1.2.3.4", 1)
    assert allowed is False
    assert retry_after > 0


def test_a_denied_request_is_not_recorded():
    # Otherwise a client hammering a blocked endpoint would extend its own ban.
    limiter = make_limiter(max_points=2)
    limiter.check("1.2.3.4", 2)
    limiter.check("1.2.3.4", 1)
    assert sum(cost for _ts, cost in limiter._entries["1.2.3.4"]) == 2


def test_a_single_request_costing_more_than_the_budget_is_denied():
    limiter = make_limiter(max_points=3)
    assert limiter.check("1.2.3.4", 99)[0] is False


def test_budgets_are_tracked_per_ip():
    limiter = make_limiter(max_points=1)
    assert limiter.check("1.1.1.1", 1)[0] is True
    assert limiter.check("2.2.2.2", 1)[0] is True
    assert limiter.check("1.1.1.1", 1)[0] is False


def test_retry_after_accounts_for_the_window_and_the_eviction_delay():
    limiter = make_limiter(max_points=1, window=60, eviction_interval=10)
    limiter.check("1.2.3.4", 1)
    _allowed, retry_after = limiter.check("1.2.3.4", 1)
    # The oldest entry must age out (60 s) and then eviction must run (10 s).
    assert 60 <= retry_after <= 72


def test_penalty_points_bypass_the_limit_check():
    # 404 penalties are applied after the fact and must always be recorded.
    limiter = make_limiter(max_points=2)
    limiter.check("1.2.3.4", 1)
    limiter.add_points("1.2.3.4", 4)
    assert limiter.check("1.2.3.4", 1)[0] is False


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


def test_eviction_prunes_expired_entries_and_unblocks_the_ip(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])

    limiter = make_limiter(max_points=1, window=60)
    limiter.check("1.2.3.4", 1)
    assert limiter.check("1.2.3.4", 1)[0] is False

    clock[0] += 61  # the entry is now older than the window
    pruned, evicted = limiter.evict()

    assert (pruned, evicted) == (1, 1)
    assert limiter.check("1.2.3.4", 1)[0] is True


def test_eviction_keeps_entries_still_inside_the_window(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])

    limiter = make_limiter(max_points=5, window=60)
    limiter.check("1.2.3.4", 1)
    clock[0] += 61
    limiter.check("1.2.3.4", 1)  # fresh entry

    pruned, evicted = limiter.evict()
    assert (pruned, evicted) == (1, 0)
    assert len(limiter._entries["1.2.3.4"]) == 1


def test_eviction_removes_empty_records():
    limiter = make_limiter()
    limiter._entries["1.2.3.4"] = []
    assert limiter.evict() == (0, 1)
    assert "1.2.3.4" not in limiter._entries


def test_eviction_is_a_no_op_when_there_is_nothing_to_prune():
    limiter = make_limiter()
    limiter.check("1.2.3.4", 1)
    assert limiter.evict() == (0, 0)


# ---------------------------------------------------------------------------
# Manager: whitelist and endpoint routing
# ---------------------------------------------------------------------------


def test_a_disabled_manager_allows_everything(stub_manager, monkeypatch):
    monkeypatch.setattr(rate_limit, "rate_limiting_enabled", False)
    manager = _RateLimitManager({})
    assert manager.check(ENDPOINT_AVATARS, "1.2.3.4", 1) == (True, 0)


def enabled_manager(monkeypatch, **cfg) -> _RateLimitManager:
    monkeypatch.setattr(rate_limit, "rate_limiting_enabled", True)
    base = {
        "avatars": {"points": 3, "window": 60},
        "metadata": {"points": 2, "window": 60},
        "points_cost_404": 5,
    }
    base.update(cfg)
    return _RateLimitManager(base)


def test_each_endpoint_type_gets_its_own_budget(stub_manager, monkeypatch):
    manager = enabled_manager(monkeypatch)
    for _ in range(3):
        assert manager.check(ENDPOINT_AVATARS, "1.2.3.4", 1)[0] is True
    assert manager.check(ENDPOINT_AVATARS, "1.2.3.4", 1)[0] is False
    # The metadata budget is untouched by avatar traffic.
    assert manager.check(ENDPOINT_METADATA, "1.2.3.4", 1)[0] is True


def test_an_endpoint_type_can_be_disabled_individually(stub_manager, monkeypatch):
    manager = enabled_manager(monkeypatch, metadata={"enabled": False})
    for _ in range(50):
        assert manager.check(ENDPOINT_METADATA, "1.2.3.4", 1) == (True, 0)


def test_an_unknown_endpoint_type_is_never_limited(stub_manager, monkeypatch):
    manager = enabled_manager(monkeypatch)
    assert manager.check("something-else", "1.2.3.4", 1) == (True, 0)


@pytest.mark.parametrize("ip", ["10.0.0.7", "192.168.1.1"])
def test_whitelisted_ranges_and_hosts_bypass_the_limit(stub_manager, monkeypatch, ip):
    manager = enabled_manager(monkeypatch, ip_whitelist=["10.0.0.0/8", "192.168.1.1"])
    for _ in range(20):
        assert manager.check(ENDPOINT_AVATARS, ip, 1) == (True, 0)


def test_non_whitelisted_traffic_is_still_limited(stub_manager, monkeypatch):
    manager = enabled_manager(monkeypatch, ip_whitelist=["10.0.0.0/8"])
    for _ in range(3):
        manager.check(ENDPOINT_AVATARS, "8.8.8.8", 1)
    assert manager.check(ENDPOINT_AVATARS, "8.8.8.8", 1)[0] is False


def test_an_invalid_whitelist_entry_is_ignored_with_a_warning(
    stub_manager, monkeypatch, caplog
):
    with caplog.at_level("WARNING", logger="ratelimit"):
        manager = enabled_manager(monkeypatch, ip_whitelist=["not-an-ip", "10.0.0.0/8"])
    assert "Ignoring invalid whitelist entry" in caplog.text
    assert manager.check(ENDPOINT_AVATARS, "10.1.1.1", 1) == (True, 0)


def test_an_unparseable_client_ip_is_treated_as_non_whitelisted(
    stub_manager, monkeypatch
):
    manager = enabled_manager(monkeypatch, ip_whitelist=["10.0.0.0/8"])
    assert manager._is_whitelisted("unix-socket") is False


def test_penalty_points_skip_whitelisted_clients(stub_manager, monkeypatch):
    manager = enabled_manager(monkeypatch, ip_whitelist=["10.0.0.0/8"])
    manager.add_points(ENDPOINT_AVATARS, "10.0.0.5", 100)
    assert manager.check(ENDPOINT_AVATARS, "10.0.0.5", 1) == (True, 0)


# ---------------------------------------------------------------------------
# Flask integration
# ---------------------------------------------------------------------------


def build_rate_limited_app(monkeypatch, manager) -> Flask:
    """Build a tiny app wired to *manager* through init_rate_limiting()."""
    monkeypatch.setattr(rate_limit, "_manager", manager)
    app = Flask(__name__)

    @app.route("/user-avatars/<path:rest>")
    def avatar(rest):
        return ("missing", 404) if "missing" in rest else "image-bytes"

    @app.route("/other")
    def other():
        return "not rate limited"

    init_rate_limiting(app)
    return app


def test_rate_limited_paths_return_429_with_retry_after(stub_manager, monkeypatch):
    manager = enabled_manager(monkeypatch)
    client = build_rate_limited_app(monkeypatch, manager).test_client()

    for _ in range(3):
        assert client.get("/user-avatars/256x256/a.jpg").status_code == 200

    response = client.get("/user-avatars/256x256/a.jpg")
    assert response.status_code == 429
    assert response.headers["Retry-After"]
    assert response.get_json()["error"] == "Too Many Requests"


def test_paths_outside_the_avatar_namespace_are_never_limited(
    stub_manager, monkeypatch
):
    manager = enabled_manager(monkeypatch)
    client = build_rate_limited_app(monkeypatch, manager).test_client()
    for _ in range(20):
        assert client.get("/other").status_code == 200


def test_metadata_paths_are_charged_to_the_metadata_budget(stub_manager, monkeypatch):
    manager = enabled_manager(monkeypatch)
    client = build_rate_limited_app(monkeypatch, manager).test_client()

    for _ in range(2):
        assert client.get("/user-avatars/_metadata/a.meta.json").status_code == 200
    assert client.get("/user-avatars/_metadata/a.meta.json").status_code == 429
    # Avatar traffic (a separate budget) still flows.
    assert client.get("/user-avatars/256x256/a.jpg").status_code == 200


def test_a_404_costs_the_configured_penalty(stub_manager, monkeypatch):
    # points=10, 404 penalty=5 -> two misses exhaust the budget.
    manager = enabled_manager(
        monkeypatch, avatars={"points": 10, "window": 60}, points_cost_404=5
    )
    client = build_rate_limited_app(monkeypatch, manager).test_client()

    assert client.get("/user-avatars/256x256/missing.jpg").status_code == 404
    assert client.get("/user-avatars/256x256/missing.jpg").status_code == 404
    assert client.get("/user-avatars/256x256/a.jpg").status_code == 429


def test_a_successful_request_only_costs_the_normal_amount(stub_manager, monkeypatch):
    manager = enabled_manager(
        monkeypatch, avatars={"points": 10, "window": 60}, points_cost_404=5
    )
    client = build_rate_limited_app(monkeypatch, manager).test_client()
    for _ in range(10):
        assert client.get("/user-avatars/256x256/a.jpg").status_code == 200
    assert client.get("/user-avatars/256x256/a.jpg").status_code == 429
    assert COST_NORMAL == 1


def test_registration_is_skipped_entirely_when_disabled(stub_manager, monkeypatch):
    monkeypatch.setattr(rate_limit, "rate_limiting_enabled", False)
    manager = _RateLimitManager({})
    client = build_rate_limited_app(monkeypatch, manager).test_client()
    for _ in range(50):
        assert client.get("/user-avatars/256x256/a.jpg").status_code == 200


# ---------------------------------------------------------------------------
# Per-user cooldowns
# ---------------------------------------------------------------------------


def test_a_second_action_inside_the_window_is_denied(stub_manager, monkeypatch):
    cooldown = _UserCooldown("upload", 10)
    assert cooldown.check_and_record(42) == (True, 0)

    allowed, retry_after = cooldown.check_and_record(42)
    assert allowed is False
    assert 0 < retry_after <= 11


def test_the_cooldown_expires(stub_manager, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(rate_limit.time, "monotonic", lambda: clock[0])

    cooldown = _UserCooldown("upload", 10)
    cooldown.check_and_record(42)
    clock[0] += 11
    assert cooldown.check_and_record(42)[0] is True


def test_cooldowns_are_tracked_per_user(stub_manager, monkeypatch):
    cooldown = _UserCooldown("upload", 10)
    cooldown.check_and_record(1)
    assert cooldown.check_and_record(2)[0] is True


@pytest.mark.parametrize(
    "check",
    [check_upload_cooldown, check_gravatar_import_cooldown, check_url_import_cooldown],
)
def test_cooldown_checks_allow_everything_when_disabled(check):
    # rate_limiting.enabled is false in the test config, so every cooldown
    # singleton is None and each check must be a permissive no-op.
    for _ in range(10):
        assert check(42) == (True, 0)
