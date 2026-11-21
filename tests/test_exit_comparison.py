import asyncio
import json
from datetime import timedelta

import duckdb
import pytest

from beta_lag.config import Settings
from beta_lag.demo import demo_events
from beta_lag.live import Collector
from beta_lag.models import Metrics, Price


def test_live_exit_comparison_tracks_overlapping_signals_independently(tmp_path, monkeypatch):
    # Drive the normal consumer with a normalized journal, up to its first support break.
    collector = Collector(Settings())
    events = []
    for event in demo_events():
        events.append(event)
        if isinstance(event, Price) and event.price == 99.8 and event.symbol == "BTC":
            break
    start = events[-1].timestamp
    original_metrics = next(e for e in reversed(events) if isinstance(e, Metrics))
    for seconds in range(30, 3661, 30):
        now = start + timedelta(seconds=seconds)
        btc = 100 if seconds < 1890 else 99.8
        if seconds >= 1950:
            btc = 98.8
        for symbol, value in (("QQQ", 100 if seconds < 1860 else 98), ("BTC", btc)):
            events.append(Price(timestamp=now, observed_at=now, symbol=symbol, price=value))
        if seconds == 1860:
            events.append(
                original_metrics.model_copy(
                    update={
                        "timestamp": now,
                        "observed_at": now,
                        "open_interest": 1500,
                    }
                )
            )
    monkeypatch.setattr(collector, "normalize", lambda row, now: row["event"])

    async def run():
        for event in events:
            await collector.queue.put({"kind": "normalized", "event": event})
        await collector.queue.put({"kind": "stop"})
        await collector.consume(tmp_path)

    asyncio.run(run())
    status = json.loads((tmp_path / "status.json").read_text())["exit_comparison"]
    with duckdb.connect(str(tmp_path / "signals.duckdb"), read_only=True) as db:
        report = json.loads(db.execute("SELECT summary FROM runs").fetchone()[0])
        assert report["signals"] == 2
        assert status == report["exit_comparison"]
        books = {
            name: (json.loads(trades), json.loads(skips))
            for name, trades, skips in db.execute(
                "SELECT variant, trade_details, skipped_signals FROM paper_exit_snapshots"
            ).fetchall()
        }

    control, one, two = (
        books[name][0] for name in ("control", "target_1pct_60m", "target_2pct_60m")
    )
    assert [t["signal_id"] for t in control] == [1, 2]
    assert control[0]["exit_reason"] == "max_holding_time"
    assert control[0]["entry_at"] == one[0]["entry_at"] == two[0]["entry_at"]
    assert control[0]["entry_price"] == one[0]["entry_price"] == two[0]["entry_price"]
    assert one[0]["exit_reason"] == "take_profit"
    assert two[0]["exit_reason"] == "max_holding_time"
    assert control[0]["exit_at"] < one[0]["exit_at"] < two[0]["exit_at"]
    assert books["control"][1] == []
    for name in ("target_1pct_60m", "target_2pct_60m"):
        trades, skipped = books[name]
        assert len(trades) == 1
        assert skipped == [{"signal_id": 2, "reason": "position_already_open"}]
        assert status[name]["mean_net_return_ex_funding"] == pytest.approx(
            trades[0]["net_return_ex_funding"]
        )
