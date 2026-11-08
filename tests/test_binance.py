from datetime import UTC, datetime, timedelta

import pytest

from beta_lag.binance import account_snapshot
from beta_lag.config import Settings
from beta_lag.demo import demo_events
from beta_lag.engine import Engine
from beta_lag.live import Collector
from beta_lag.models import Metrics

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


def oi(time=NOW, value=100):
    return {
        "kind": "open_interest",
        "received_at": time,
        "open_interest_quantity": value,
        "provider_observed_at": None,
    }


def accounts(time=NOW, share=0.6):
    return {"kind": "account_share", "received_at": NOW, "observed_at": time, "long_share": share}


def test_account_parser_uses_share_not_ratio_and_preserves_timestamp():
    sample = account_snapshot(
        [
            {
                "symbol": "BTCUSDT",
                "longAccount": "0.6",
                "shortAccount": "0.4",
                "longShortRatio": "1.5",
                "timestamp": NOW.timestamp() * 1000,
            }
        ]
    )
    assert sample["long_share"] == 0.6
    assert sample["observed_at"] == NOW


@pytest.mark.parametrize("long,short", [(float("nan"), 0.4), (1.1, -0.1), (0.7, 0.7)])
def test_account_parser_rejects_invalid_shares(long, short):
    with pytest.raises(ValueError):
        account_snapshot(
            [
                {
                    "symbol": "BTCUSDT",
                    "longAccount": long,
                    "shortAccount": short,
                    "timestamp": NOW.timestamp() * 1000,
                }
            ]
        )


def test_pairing_preserves_independent_clocks_and_receipt_time_basis():
    collector = Collector(Settings())
    assert collector.normalize(accounts(NOW - timedelta(seconds=400)), NOW) is None
    metric = collector.normalize(oi(), NOW)
    assert metric.oi_time_basis == "receipt"
    assert metric.observed_at == NOW
    assert metric.long_share_observed_at == NOW - timedelta(seconds=400)
    assert metric.open_interest == 100
    assert Metrics.model_validate_json(metric.model_dump_json()) == metric


def test_duplicate_ratio_does_not_duplicate_oi_or_refresh_source_timestamp():
    collector = Collector(Settings())
    collector.normalize(oi(), NOW)
    assert collector.normalize(accounts(), NOW) is not None
    assert collector.normalize(accounts(), NOW + timedelta(seconds=30)) is None
    assert collector.current_accounts["observed_at"] == NOW
    later = NOW + timedelta(seconds=30)
    metric = collector.normalize(oi(later, 110), later)
    assert metric.long_share_observed_at == NOW
    assert collector.normalize(accounts(later), later) is None


def test_stale_future_or_older_ratios_do_not_enable_metrics():
    collector = Collector(Settings())
    collector.normalize(oi(), NOW)
    assert collector.normalize(accounts(NOW - timedelta(seconds=601)), NOW) is None
    assert collector.normalize(accounts(NOW + timedelta(seconds=1)), NOW) is None
    assert collector.normalize(accounts(), NOW) is not None
    assert collector.normalize(accounts(NOW - timedelta(seconds=1)), NOW) is None


def test_failed_oi_poll_cannot_be_hidden_by_new_ratio():
    collector = Collector(Settings())
    collector.normalize(oi(), NOW)
    assert collector.normalize(accounts(), NOW) is not None
    later = NOW + timedelta(seconds=301)
    assert collector.normalize(accounts(later), later) is None


def test_engine_expires_account_timestamp_separately_from_new_oi():
    collector = Collector(Settings())
    collector.normalize(accounts(NOW - timedelta(seconds=590)), NOW)
    metric = collector.normalize(oi(), NOW)
    engine = Engine(Settings())
    engine.now = NOW
    assert engine.metrics_fresh(metric)
    engine.now = NOW + timedelta(seconds=11)
    assert not engine.metrics_fresh(metric)


def test_combined_live_inputs_drive_full_signal_chain():
    collector = Collector(Settings())
    for event in demo_events():
        now = event.timestamp
        if isinstance(event, Metrics):
            rows = [accounts(now, event.long_share), oi(now, event.open_interest)]
        else:
            rows = [event.model_dump()]
        for row in rows:
            normalized = collector.normalize(row, now)
            if normalized:
                collector.engine.process(normalized)
    assert len(collector.engine.signals) == 1
    signal = collector.engine.signals[0]
    assert signal.metrics.oi_time_basis == "receipt"
    assert signal.metrics.long_share_observed_at is not None
    assert "binance:BTCUSDT" in signal.metrics.series
