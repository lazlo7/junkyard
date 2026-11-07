from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from fastapi.testclient import TestClient
import pytest

from junkyard.app import create_app
from junkyard.rate_limits import RateLimited, TokenBuckets
from conftest import CLI_PASSWORD


def test_bucket_burst_refill_and_retry_after(clock):
    limiter = TokenBuckets(1, 10, 100, clock=clock)
    for _ in range(10):
        limiter.take("client")
    with pytest.raises(RateLimited) as error:
        limiter.take("client")
    assert error.value.retry_after == 1
    clock.advance(0.5)
    with pytest.raises(RateLimited):
        limiter.take("client")
    clock.advance(0.5)
    limiter.take("client")


def test_concurrent_checks_cannot_exceed_burst(clock):
    limiter = TokenBuckets(1, 10, 100, clock=clock)

    def take(_):
        try:
            limiter.take("client")
            return True
        except RateLimited:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(take, range(40))) == 10


def test_bounded_state_does_not_evict_blocked_addresses(clock):
    limiter = TokenBuckets(1, 1, 2, clock=clock)
    limiter.take("first")
    limiter.take("second")
    with pytest.raises(RateLimited):
        limiter.take("third")
    with pytest.raises(RateLimited):
        limiter.take("first")
    assert len(limiter._buckets) == 2
    clock.advance(60)
    limiter.take("third")
    assert len(limiter._buckets) == 1


def test_authentication_reservations_refund_success_but_not_failures(clock):
    limiter = TokenBuckets(5 / 60, 5, 1, clock=clock)
    tickets = [limiter.take("client", reserve=True) for _ in range(5)]
    with pytest.raises(RateLimited) as error:
        limiter.take("client", reserve=True)
    assert error.value.retry_after == 12
    limiter.settle(tickets.pop(), refund=True)
    ticket = limiter.take("client", reserve=True)
    limiter.settle(ticket, refund=False)
    for ticket in tickets:
        limiter.settle(ticket, refund=False)
    clock.advance(12)
    ticket = limiter.take("client", reserve=True)
    limiter.settle(ticket, refund=True)
    assert limiter._buckets["client"].pending == 0


def test_pending_authentication_is_not_evicted(clock):
    limiter = TokenBuckets(1, 1, 1, clock=clock)
    ticket = limiter.take("client", reserve=True)
    clock.advance(100)
    with pytest.raises(RateLimited):
        limiter.take("other")
    limiter.settle(ticket, refund=True)
    limiter.take("other")


def application(settings, clock):
    app = create_app(settings)
    app.state.limits.anonymous._clock = clock
    app.state.limits.authentication._clock = clock
    return app


def test_unknown_paths_head_and_get_share_anonymous_budget(settings, database, clock):
    app = application(replace(settings, anonymous_burst=2), clock)
    with TestClient(app) as client:
        assert client.get("/first").status_code == 404
        assert client.head("/second").status_code == 404
        blocked = client.get("/third")
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "1"
        assert blocked.headers["cache-control"] == "no-store"
        assert client.head("/third").content == b""
        clock.advance(1)
        assert client.get("/first").status_code == 404


def test_only_verified_cli_authentication_bypasses_read_throttle(settings, database, clock):
    app = application(replace(settings, anonymous_burst=1), clock)
    with TestClient(app) as client:
        assert client.get("/unknown").status_code == 404
        assert client.get("/unknown").status_code == 429
        assert client.get("/unknown", headers={"Authorization": "Basic invalid"}).status_code == 401
        for _ in range(7):
            assert client.get("/unknown", auth=("admin", CLI_PASSWORD)).status_code == 404
        assert client.get("/unknown").status_code == 429


def test_failed_auth_is_limited_before_hashing(settings, database, clock, monkeypatch):
    app = application(settings, clock)
    calls = []

    def verify(username, password):
        calls.append(username)
        return False

    monkeypatch.setattr(app.state.cli_authenticator, "verify", verify)
    with TestClient(app) as client:
        for _ in range(5):
            assert client.get("/auth/check", auth=("admin", "wrong")).status_code == 401
        blocked = client.get("/auth/check", auth=("admin", "wrong"))
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "12"
        assert len(calls) == 5
        clock.advance(12)
        assert client.get("/auth/check", auth=("admin", "wrong")).status_code == 401
        assert len(calls) == 6


def test_forwarded_headers_from_untrusted_peer_cannot_reset_limits(settings, database, clock):
    app = application(replace(settings, anonymous_burst=1), clock)
    with TestClient(app, client=("192.0.2.1", 5000)) as client:
        assert client.get("/unknown", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 404
        assert client.get("/unknown", headers={"X-Forwarded-For": "198.51.100.2", "Forwarded": "for=198.51.100.3"}).status_code == 429


def test_trusted_proxy_requires_one_overwritten_client_ip(settings, database, clock):
    app = application(replace(settings, anonymous_burst=1, trusted_proxies=("127.0.0.1",)), clock)
    with TestClient(app, client=("127.0.0.1", 5000)) as client:
        assert client.get("/unknown").status_code == 400
        for invalid in ("", "not-an-ip", "198.51.100.1, 198.51.100.2", "fe80::1%eth0"):
            assert client.get("/unknown", headers={"X-Forwarded-For": invalid}).status_code == 400
        assert client.get("/unknown", headers=[("X-Forwarded-For", "198.51.100.1"), ("X-Forwarded-For", "198.51.100.2")]).status_code == 400
        assert client.get("/unknown", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 404
        assert client.get("/unknown", headers={"X-Forwarded-For": "::ffff:198.51.100.1"}).status_code == 429
        assert client.get("/unknown", headers={"X-Forwarded-For": "2001:db8::1"}).status_code == 404


def test_proxy_cannot_override_authentication_peer_budget_with_forwarded_chain(settings, database, clock):
    app = application(replace(settings, auth_failures_per_minute=1), clock)
    with TestClient(app, client=("192.0.2.1", 5000)) as client:
        assert client.get("/auth/check", headers={"X-Forwarded-For": "198.51.100.1"}).status_code == 401
        response = client.get("/auth/check", headers={"X-Forwarded-For": "198.51.100.2"})
        assert response.status_code == 429
        assert response.headers["retry-after"] == "60"
