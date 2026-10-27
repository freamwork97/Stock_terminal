"""Alert edge detection, cooldown, configuration and history semantics."""
from decimal import Decimal

import pytest

from stock_terminal.alerts import AlertTracker


@pytest.fixture
def clock(monkeypatch):
    value = [0.0]
    monkeypatch.setattr("stock_terminal.alerts.monotonic", lambda: value[0])
    return value


def evaluate(tracker, price, symbol="AAPL", upper="110", lower="90"):
    return tracker.evaluate(symbol, Decimal(price), "USD",
                            Decimal(upper) if upper else None, Decimal(lower) if lower else None)


@pytest.mark.parametrize("price,direction", [("110", "upper"), ("90", "lower")])
def test_first_hit_once_and_reentry_after_cooldown(clock, price, direction):
    tracker = AlertTracker()
    event = evaluate(tracker, price)
    assert event.direction == direction
    assert event.threshold == Decimal(price)
    for seconds in (3, 60, 600):
        clock[0] = seconds
        assert evaluate(tracker, price) is None
    assert evaluate(tracker, "100") is None
    assert evaluate(tracker, price) is not None
    assert len(tracker.history) == 2


def test_suppressed_crossing_is_not_delivered_late(clock):
    tracker = AlertTracker()
    assert evaluate(tracker, "111") is not None
    clock[0] = 10
    assert evaluate(tracker, "100") is None
    assert evaluate(tracker, "111") is None
    clock[0] = 60
    assert evaluate(tracker, "111") is None
    assert evaluate(tracker, "100") is None
    assert evaluate(tracker, "110") is not None


def test_sides_and_symbols_have_independent_cooldowns(clock):
    tracker = AlertTracker()
    assert evaluate(tracker, "110") is not None
    assert evaluate(tracker, "90").direction == "lower"
    assert evaluate(tracker, "110", symbol="MSFT") is not None
    assert evaluate(tracker, "110") is None


def test_threshold_change_clear_and_reset_rearm(clock):
    tracker = AlertTracker()
    assert evaluate(tracker, "110") is not None
    assert evaluate(tracker, "110", upper="109") is not None
    assert evaluate(tracker, "110", upper=None, lower=None) is None
    assert evaluate(tracker, "110") is not None
    tracker.reset("AAPL")
    assert evaluate(tracker, "110") is not None
    assert len(tracker.history) == 4


def test_history_is_bounded_newest_first_and_records_values(clock):
    tracker = AlertTracker()
    for i in range(105):
        evaluate(tracker, "110", symbol=str(i))
    assert len(tracker.history) == 100
    assert tracker.history[0].symbol == "104"
    assert tracker.history[-1].symbol == "5"
    assert tracker.history[0].currency == "USD"
    assert tracker.history[0].occurred_at.tzinfo is not None


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-1", "0"])
def test_invalid_inputs_do_not_fire_or_mutate_state(clock, value):
    tracker = AlertTracker()
    assert evaluate(tracker, value) is None
    assert evaluate(tracker, "110", upper=value) is None
    assert evaluate(tracker, "110", upper="90", lower="110") is None
    assert not tracker.history
    assert evaluate(tracker, "110") is not None


def test_disabled_symbol_does_not_create_history_or_consume_cooldown(clock):
    tracker = AlertTracker()
    args = ("AAPL", Decimal("110"), "USD", Decimal("110"), None)
    assert tracker.evaluate(*args, enabled=False) is None
    assert not tracker.history
    assert not tracker._states
    assert tracker.evaluate(*args, enabled=True) is not None
    assert tracker.evaluate(*args, enabled=True) is None
    assert tracker.evaluate(*args, enabled=False) is None
    assert tracker.evaluate(*args, enabled=True) is not None
