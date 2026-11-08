from datetime import timedelta

import pytest
from pydantic import ValidationError

from beta_lag.config import Settings
from beta_lag.demo import demo_events
from beta_lag.engine import Engine
from beta_lag.models import Clock, Metrics, Price, State


def until_armed(config=None):
    engine = Engine(config or Settings())
    for event in demo_events():
        engine.process(event)
        if engine.state == State.ARMED:
            return engine
    raise AssertionError("Scenario did not arm")


def test_demo_has_one_causal_signal_and_expected_states():
    engine = Engine(Settings())
    for event in demo_events():
        engine.process(event)
    assert len(engine.signals) == 1
    assert [t.state for t in engine.transitions] == [
        State.WARNING,
        State.ARMED,
        State.CONFIRMED,
        State.IDLE,
    ]
    signal = engine.signals[0]
    assert signal.support == 100
    assert signal.reference_price == 99.8
    assert signal.metrics.timestamp < signal.timestamp
    assert signal.oi_percentile == 1


def test_no_signal_without_oi_history():
    engine = Engine(Settings())
    for event in demo_events():
        if isinstance(event, Metrics) and event.long_share == 0.5:
            continue
        engine.process(event)
    assert not engine.signals
    assert State.ARMED not in [t.state for t in engine.transitions]


def test_oi_must_be_rising_and_long_biased():
    for changes in ({"open_interest": 900}, {"long_share": 0.55}):
        engine = Engine(Settings())
        for event in demo_events():
            if isinstance(event, Metrics) and event.long_share == 0.6:
                event = event.model_copy(update=changes)
            engine.process(event)
        assert not engine.signals


def test_delayed_metrics_do_not_arm():
    engine = Engine(Settings(metrics_max_age_seconds=60))
    for event in demo_events():
        if isinstance(event, Metrics) and event.long_share == 0.6:
            event = event.model_copy(
                update={"observed_at": event.timestamp - timedelta(seconds=61)}
            )
        engine.process(event)
    assert not engine.signals


def test_timeout_and_cooldown():
    engine = until_armed()
    expiry = engine.deadline
    engine.process(Clock(timestamp=expiry))
    assert engine.state == State.IDLE
    assert engine.transitions[-1].reason == "setup_expired"
    assert engine.cooldown_until > expiry


def test_missing_market_data_invalidates_armed_setup():
    engine = until_armed()
    engine.process(Clock(timestamp=engine.now + timedelta(seconds=91)))
    assert engine.state == State.IDLE
    assert engine.transitions[-1].reason == "stale_market_data"


def test_duplicate_and_reversed_events_are_rejected():
    engine = Engine(Settings())
    events = demo_events()
    engine.process(events[1])
    with pytest.raises(ValueError, match="Duplicate"):
        engine.process(events[1])
    with pytest.raises(ValueError, match="availability"):
        engine.process(events[0])


def test_cannot_mix_derivative_series():
    engine = Engine(Settings())
    events = demo_events()
    engine.process(events[0])
    with pytest.raises(ValueError, match="Cannot mix"):
        engine.process(events[1].model_copy(update={"series": "different-units"}))


@pytest.mark.parametrize("price", [0, -1, float("nan"), float("inf")])
def test_invalid_price_rejected(price):
    timestamp = demo_events()[0].timestamp
    with pytest.raises(ValidationError):
        Price(timestamp=timestamp, observed_at=timestamp, symbol="BTC", price=price)


def test_future_observations_and_naive_timestamps_rejected():
    timestamp = demo_events()[0].timestamp
    with pytest.raises(ValidationError):
        Price(
            timestamp=timestamp, observed_at=timestamp + timedelta(seconds=1), symbol="BTC", price=1
        )
    with pytest.raises(ValidationError):
        Clock(timestamp=timestamp.replace(tzinfo=None))


def test_unknown_config_keys_rejected():
    with pytest.raises(ValidationError):
        Settings(typo_threshold=0.5)


def test_support_accepts_irregular_ticks_without_exact_boundary_match():
    engine = until_armed()
    original = engine.find_support()
    latest = engine.prices["BTC"][-1]
    engine.prices["BTC"][-1] = latest.model_copy(
        update={"observed_at": latest.observed_at + timedelta(seconds=1)}
    )
    engine.now += timedelta(seconds=1)
    assert engine.find_support() == original


def test_missing_return_baseline_invalidates_instead_of_crashing():
    engine = until_armed()
    cutoff = engine.now - timedelta(seconds=800)
    while engine.prices["BTC"][0].observed_at < cutoff:
        engine.prices["BTC"].popleft()
    timestamp = engine.now + timedelta(seconds=1)
    engine.process(Price(timestamp=timestamp, observed_at=timestamp, symbol="BTC", price=99))
    assert engine.state == State.IDLE
    assert engine.transitions[-1].reason == "missing_btc_return_history"
    assert not engine.signals


def test_prefix_has_no_signal_before_confirmation():
    engine = Engine(Settings())
    for event in demo_events():
        if isinstance(event, Price) and event.symbol == "BTC" and event.price < 100:
            break
        engine.process(event)
    assert not engine.signals
    assert engine.state == State.ARMED
