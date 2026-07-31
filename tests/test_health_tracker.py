import time

import pytest

from gateway.core.provider_health import HealthTracker


@pytest.fixture
def fake_clock(monkeypatch):
    current = [1000.0]

    def advance(seconds):
        current[0] += seconds

    monkeypatch.setattr(time, "monotonic", lambda: current[0])
    return advance


def test_exponential_backoff_capped_at_300s(fake_clock):
    tracker = HealthTracker()
    expected = [30, 60, 120, 240, 300]
    for index, seconds in enumerate(expected, start=1):
        tracker.record_failure("alpha")
        health = tracker.get("alpha")
        assert health.consecutive_failures == index
        assert health.cooldown_until - 1000.0 == seconds
        assert not tracker.is_available("alpha")


def test_success_resets_failures_and_cooldown(fake_clock):
    tracker = HealthTracker()
    tracker.record_failure("alpha")
    tracker.record_failure("alpha")
    tracker.record_success("alpha")
    health = tracker.get("alpha")
    assert health.consecutive_failures == 0
    assert health.cooldown_until is None
    assert tracker.is_available("alpha")


def test_cooldown_expiry_restores_availability(fake_clock):
    tracker = HealthTracker()
    tracker.record_failure("alpha")
    assert not tracker.is_available("alpha")
    fake_clock(30.5)
    assert tracker.is_available("alpha")


def test_disable_blocks_provider_forever(fake_clock):
    tracker = HealthTracker()
    tracker.record_failure("alpha")
    tracker.disable("alpha")
    fake_clock(10000)
    assert not tracker.is_available("alpha")


def test_providers_tracked_independently(fake_clock):
    tracker = HealthTracker()
    tracker.record_failure("alpha")
    assert tracker.is_available("beta")
    tracker.record_failure("beta")
    assert not tracker.is_available("beta")
