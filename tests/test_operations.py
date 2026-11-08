import asyncio
import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta

import duckdb
import pytest

from beta_lag.config import Settings
from beta_lag.demo import demo_events
from beta_lag.engine import Engine
from beta_lag.live import Collector
from beta_lag.market_session import parse_calendar
from beta_lag.models import Clock, MarketSession, Price, State
from beta_lag.paper import PaperBroker
from beta_lag.runtime import process_event
from beta_lag.status import read_status, write_json
from beta_lag.storage import Store


@pytest.mark.parametrize("day,opening", [("2026-03-06", 14), ("2026-03-09", 13)])
def test_calendar_handles_us_daylight_saving(day, opening):
    stamp = datetime.fromisoformat(day).replace(tzinfo=UTC)
    session = parse_calendar(
        [{"date": day, "open": "09:30", "close": "16:00"}], stamp.date(), stamp
    )
    assert session.opens_at.hour == opening
    assert session.opens_at.minute == 30


def test_calendar_holiday_and_early_close():
    holiday = datetime(2026, 11, 26, 12, tzinfo=UTC)
    session = parse_calendar([], holiday.date(), holiday)
    assert session.opens_at is None
    engine = Engine(Settings(require_market_session=True))
    engine.process(session)
    assert not engine.session_open()
    early = parse_calendar(
        [{"date": "2026-11-27", "open": "09:30", "close": "13:00"}],
        date(2026, 11, 27),
        holiday + timedelta(days=1),
    )
    engine.process(early)
    engine.process(Clock(timestamp=datetime(2026, 11, 27, 17, 59, tzinfo=UTC)))
    assert engine.session_open()
    engine.state = State.ARMED
    engine.process(Clock(timestamp=datetime(2026, 11, 27, 18, tzinfo=UTC)))
    assert engine.state == State.IDLE
    assert not engine.session_open()


def test_calendar_unavailable_blocks_signals_and_premarket_does_not_warm_up():
    engine = Engine(Settings(require_market_session=True))
    for event in demo_events():
        engine.process(event)
    assert not engine.signals
    now = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
    engine = Engine(Settings(require_market_session=True))
    engine.process(
        MarketSession(
            timestamp=now - timedelta(minutes=20),
            session_date=now.date(),
            opens_at=now,
            closes_at=now + timedelta(hours=6, minutes=30),
        )
    )
    engine.process(
        Price(
            timestamp=now - timedelta(minutes=15),
            observed_at=now - timedelta(minutes=15),
            symbol="BTC",
            price=100,
        )
    )
    engine.process(Price(timestamp=now, observed_at=now, symbol="BTC", price=100))
    assert engine.price_return("BTC") is None


def test_pending_entry_cancelled_at_close_but_open_trade_still_exits():
    from test_paper import signal, tick

    item = signal()
    broker = PaperBroker(Settings(slippage_bps_per_side=0))
    engine = Engine(Settings(require_market_session=True))
    broker.submit(item)
    process_event(engine, broker, tick(item, 1, 100))
    assert broker.pending is None
    assert not broker.trades
    assert broker.skipped[-1]["reason"] == "market_session_closed"
    broker.submit(item)
    broker.on_price(tick(item, 2, 100))
    process_event(engine, broker, tick(item, 3, 105))
    assert broker.trades[0].exit_reason == "stop_loss"


def test_checkpoints_survive_reopen_and_do_not_duplicate_rows(tmp_path):
    db = tmp_path / "test.duckdb"
    store = Store(db)
    engine, broker = Engine(Settings()), PaperBroker(Settings())
    events = demo_events()
    batch = []
    for sequence, event in enumerate(events):
        process_event(engine, broker, event)
        batch.append((sequence, event))
        if sequence == 100:
            store.checkpoint(Settings(), batch, engine, broker, sequence + 1)
            batch.clear()
    store.checkpoint(Settings(), batch, engine, broker, len(events))
    store.checkpoint(Settings(), [], engine, broker, len(events), "completed")
    store.close()
    reopened = Store(db)
    try:
        assert reopened.connection.execute("SELECT count(*) FROM events").fetchone()[0] == len(
            events
        )
        assert reopened.connection.execute("SELECT count(*) FROM signals").fetchone()[0] == 1
        assert reopened.connection.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
        assert reopened.connection.execute("SELECT count(*) FROM excursions").fetchone()[0] == 3
    finally:
        reopened.close()


def test_failed_checkpoint_preserves_previous_commit(tmp_path):
    store = Store(tmp_path / "check.duckdb")
    events = demo_events()
    engine, broker = Engine(Settings()), PaperBroker(Settings())
    process_event(engine, broker, events[0])
    store.checkpoint(Settings(), [(0, events[0])], engine, broker, 1)
    with pytest.raises(duckdb.ConstraintException):
        store.checkpoint(Settings(), [(0, events[0])], engine, broker, 2)
    assert store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    store.close()


def test_status_detects_a_dead_heartbeat_without_claiming_process_is_alive(tmp_path):
    folder = tmp_path / "session"
    folder.mkdir()
    write_json(
        folder / "status.json",
        {"updated_at": (datetime.now(UTC) - timedelta(minutes=5)).isoformat(), "status": "running"},
    )
    assert read_status(tmp_path)["heartbeat_stale"]
    assert not (folder / "status.json.tmp").exists()


def test_live_checkpoint_written_before_capture_stops(tmp_path):
    collector = Collector(Settings(checkpoint_seconds=1, status_seconds=1))

    async def scenario():
        task = asyncio.create_task(collector.consume(tmp_path))
        await collector.emit("clock")
        await asyncio.sleep(1.05)
        await collector.emit("clock")
        await collector.queue.join()
        status = json.loads((tmp_path / "status.json").read_text())
        assert status["status"] == "running"
        assert status["last_checkpoint_at"] is not None
        assert collector.research["events"] == 2
        await collector.queue.put({"kind": "stop"})
        await task

    asyncio.run(scenario())


def test_committed_checkpoint_survives_abrupt_process_exit(tmp_path):
    path = tmp_path / "crash.duckdb"
    program = """
import os, sys
from beta_lag.config import Settings
from beta_lag.demo import demo_events
from beta_lag.engine import Engine
from beta_lag.paper import PaperBroker
from beta_lag.runtime import process_event
from beta_lag.storage import Store
store = Store(sys.argv[1])
engine, broker = Engine(Settings()), PaperBroker(Settings())
event = demo_events()[0]
process_event(engine, broker, event)
store.checkpoint(Settings(), [(0, event)], engine, broker, 1)
os._exit(17)
"""
    result = subprocess.run([sys.executable, "-c", program, str(path)], check=False)
    assert result.returncode == 17
    store = Store(path)
    try:
        assert store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        summary = json.loads(store.connection.execute("SELECT summary FROM runs").fetchone()[0])
        assert summary["status"] == "running"
    finally:
        store.close()


def test_full_signal_chain_is_allowed_during_a_valid_session():
    engine = Engine(Settings(require_market_session=True))
    events = demo_events()
    day = events[0].timestamp.date()
    calendar = parse_calendar(
        [{"date": day.isoformat(), "open": "09:30", "close": "16:00"}], day, events[0].timestamp
    )
    engine.process(calendar)
    for event in events:
        engine.process(event)
    assert len(engine.signals) == 1
