import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from beta_lag.config import Settings
from beta_lag.live import Collector, alpaca_prices, collect_live, hyperliquid_prices, oi_snapshot
from beta_lag.models import Price

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_binance_network(monkeypatch):
    async def poll(self, session):
        await asyncio.sleep(100)

    monkeypatch.setattr(Collector, "poll_accounts", poll)
    monkeypatch.setattr(Collector, "poll_calendar", poll)


def test_alpaca_ignores_special_conditions_and_unsubscribed_symbols():
    row = {"T": "t", "S": "QQQ", "p": 100, "t": NOW.isoformat(), "c": ["@"]}
    assert alpaca_prices([row])[0]["price"] == 100
    assert not alpaca_prices([{**row, "c": ["I"]}, {**row, "S": "AAPL"}])


def test_hyperliquid_uses_timestamped_mid_and_rejects_crossed_book():
    row = {
        "channel": "l2Book",
        "data": {
            "coin": "BTC",
            "time": NOW.timestamp() * 1000,
            "levels": [[{"px": "99"}], [{"px": "101"}]],
        },
    }
    price = hyperliquid_prices(row)[0]
    assert price["price"] == 100
    assert price["observed_at"] == NOW
    row["data"]["levels"][0][0]["px"] = "102"
    with pytest.raises(ValueError, match="Crossed"):
        hyperliquid_prices(row)


def test_oi_snapshot_never_invents_provider_timestamp_or_long_share():
    row = {
        "code": "0",
        "data": [
            {"exchange": "Other", "open_interest_usd": 5, "open_interest_quantity": 1},
            {"exchange": "All", "open_interest_usd": 100, "open_interest_quantity": 2},
        ],
    }
    result = oi_snapshot(row)
    assert result["open_interest_usd"] == 100
    assert result["provider_observed_at"] is None
    assert "long_share" not in result


def test_normalizer_drops_stale_future_duplicate_and_samples_btc():
    collector = Collector(Settings())
    row = {"kind": "price", "symbol": "BTC", "price": 100, "observed_at": NOW}
    assert collector.normalize(row, NOW - timedelta(seconds=1)) is None
    assert collector.normalize(row, NOW + timedelta(seconds=91)) is None
    assert isinstance(collector.normalize(row, NOW), Price)
    assert collector.normalize(row, NOW) is None
    assert (
        collector.normalize(
            {**row, "observed_at": NOW + timedelta(milliseconds=100)}, NOW + timedelta(seconds=1)
        )
        is None
    )
    assert (
        collector.normalize(
            {**row, "observed_at": NOW + timedelta(seconds=1)}, NOW + timedelta(seconds=1)
        )
        is not None
    )


def test_live_capture_stops_persists_and_never_arms_without_metrics(tmp_path, monkeypatch):
    async def stream(self, session, provider):
        await self.status(provider, "subscribed")
        now = datetime.now(UTC)
        symbol = "BTC" if provider == "hyperliquid" else "QQQ"
        await self.emit("price", provider=provider, observed_at=now, symbol=symbol, price=100)
        await asyncio.sleep(100)

    async def poll(self, session):
        await self.emit(
            "open_interest", provider="coinglass", open_interest_usd=100, provider_observed_at=None
        )
        await self.status("coinglass", "snapshot_received")
        await asyncio.sleep(100)

    monkeypatch.setattr(Collector, "stream", stream)
    monkeypatch.setattr(Collector, "poll_oi", poll)
    result = asyncio.run(collect_live(1, tmp_path, Settings()))
    assert result["research"]["signals"] == 0
    assert result["counts"]["prices_BTC"] == 1
    assert result["counts"]["open_interest_snapshots"] == 1
    assert not result["readiness"]["full_strategy_ready"]
    assert not result["readiness"]["price_returns_available"]["BTC"]
    folder = next(tmp_path.iterdir())
    assert (folder / "signals.duckdb").exists()
    events = [json.loads(line) for line in (folder / "events.jsonl").read_text().splitlines()]
    assert not any(event["kind"] == "metrics" for event in events)
    assert json.loads((folder / "manifest.json").read_text())["status"] == "completed"


def test_live_duration_validation_does_not_create_output(tmp_path):
    with pytest.raises(ValueError):
        asyncio.run(collect_live(-1, tmp_path / "unused", Settings()))
    assert not (tmp_path / "unused").exists()


def test_live_consumer_failure_cleans_up_producers(tmp_path, monkeypatch):
    cancelled = []

    async def stream(self, session, provider):
        try:
            await asyncio.sleep(100)
        finally:
            cancelled.append(provider)

    async def poll(self, session):
        await asyncio.sleep(100)

    async def consume(self, folder):
        await asyncio.sleep(0.01)
        raise ValueError("test disk failure")

    monkeypatch.setattr(Collector, "stream", stream)
    monkeypatch.setattr(Collector, "poll_oi", poll)
    monkeypatch.setattr(Collector, "consume", consume)
    with pytest.raises(ValueError, match="test disk failure"):
        asyncio.run(collect_live(10, tmp_path, Settings()))
    assert sorted(cancelled) == ["alpaca", "hyperliquid"]
    folder = next(tmp_path.iterdir())
    assert json.loads((folder / "manifest.json").read_text())["status"] == "failed"


def test_cancelled_collection_finishes_journal_and_database(tmp_path, monkeypatch):
    async def stream(self, session, provider):
        await asyncio.sleep(100)

    async def poll(self, session):
        await asyncio.sleep(100)

    async def scenario():
        task = asyncio.create_task(collect_live(60, tmp_path, Settings()))
        await asyncio.sleep(0.05)
        task.cancel()
        return await task

    monkeypatch.setattr(Collector, "stream", stream)
    monkeypatch.setattr(Collector, "poll_oi", poll)
    result = asyncio.run(scenario())
    assert result["interrupted"]
    folder = next(tmp_path.iterdir())
    assert (folder / "signals.duckdb").exists()
    assert json.loads((folder / "manifest.json").read_text())["status"] == "interrupted"
