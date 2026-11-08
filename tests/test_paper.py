from datetime import timedelta

import pytest
from test_engine import until_armed

from beta_lag.config import Settings
from beta_lag.models import Price
from beta_lag.paper import PaperBroker


def signal():
    engine = until_armed()
    timestamp = engine.now + timedelta(seconds=30)
    return engine.process(
        Price(timestamp=timestamp, observed_at=timestamp, symbol="BTC", price=99.8)
    )


def tick(signal, seconds, price):
    stamp = signal.timestamp + timedelta(seconds=seconds)
    return Price(timestamp=stamp, observed_at=stamp, symbol="BTC", price=price)


def test_entry_requires_next_observation_and_costs_both_sides():
    item = signal()
    broker = PaperBroker(Settings(fee_bps_per_side=10, slippage_bps_per_side=10))
    broker.submit(item)
    broker.on_price(tick(item, 0, 100))
    assert not broker.trades
    broker.on_price(tick(item, 1, 100))
    assert broker.trades[0].entry_price == pytest.approx(99.9)
    broker.on_price(tick(item, 2, 98))
    trade = broker.trades[0]
    assert trade.exit_price == pytest.approx(98.098)
    assert trade.exit_reason == "take_profit"
    expected = (99.9 - 98.098 - 0.001 * (99.9 + 98.098)) / 99.9
    assert trade.net_return_ex_funding == pytest.approx(expected)


def test_stop_uses_observed_gap_price_not_ideal_stop_price():
    item = signal()
    broker = PaperBroker(Settings(slippage_bps_per_side=0))
    broker.submit(item)
    broker.on_price(tick(item, 1, 100))
    broker.on_price(tick(item, 2, 105))
    assert broker.trades[0].exit_price == 105
    assert broker.trades[0].exit_reason == "stop_loss"


def test_time_exit_and_incomplete_horizons():
    item = signal()
    broker = PaperBroker(Settings(max_holding_seconds=30, slippage_bps_per_side=0))
    broker.submit(item)
    broker.on_price(tick(item, 1, 100))
    broker.on_price(tick(item, 31, 100))
    assert broker.trades[0].exit_reason == "max_holding_time"
    assert all(e.status == "incomplete" for e in broker.trades[0].excursions)


def test_post_horizon_price_not_included_and_gap_flagged():
    item = signal()
    broker = PaperBroker(Settings(slippage_bps_per_side=0))
    broker.submit(item)
    broker.on_price(tick(item, 1, 100))
    broker.on_price(tick(item, 60, 99))
    broker.on_price(tick(item, 902, 50))
    evaluation = broker.trades[0].excursions[0]
    assert evaluation.mfe == pytest.approx(0.01)
    assert evaluation.status == "data_gap"


def test_complete_excursions_continue_after_trade_exit():
    item = signal()
    broker = PaperBroker(Settings(slippage_bps_per_side=0))
    broker.submit(item)
    broker.on_price(tick(item, 1, 100))
    for seconds in range(31, 3602, 30):
        broker.on_price(tick(item, seconds, 98))
    assert broker.trades[0].exit_reason == "take_profit"
    assert all(e.status == "complete" for e in broker.trades[0].excursions)
    assert all(e.mfe == pytest.approx(0.02) for e in broker.trades[0].excursions)


def test_expired_entry_does_not_fill():
    item = signal()
    broker = PaperBroker(Settings())
    broker.submit(item)
    broker.on_price(tick(item, 91, 100))
    assert not broker.trades
    assert broker.skipped[0]["reason"] == "entry_expired"


def test_single_position_limit():
    item = signal()
    broker = PaperBroker(Settings())
    broker.submit(item)
    broker.on_price(tick(item, 1, 100))
    broker.submit(item.model_copy(update={"id": 2}))
    assert broker.pending is None
    assert broker.skipped[0]["reason"] == "position_already_open"
