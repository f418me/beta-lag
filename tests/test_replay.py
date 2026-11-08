import asyncio
import json

import pytest

from beta_lag.config import Settings, load_settings
from beta_lag.demo import demo_events
from beta_lag.replay import read_events, replay
from beta_lag.storage import Store


def test_replay_persists_inputs_signals_and_complete_evaluations(tmp_path):
    store = Store(tmp_path / "signals.duckdb")
    try:
        result = asyncio.run(replay(demo_events(), Settings(), store))
        assert result["signals"] == result["closed_trades"] == 1
        assert result["mean_net_return_ex_funding"] > 0
        assert (
            store.connection.execute("SELECT count(*) FROM events").fetchone()[0]
            == result["events"]
        )
        assert store.connection.execute("SELECT count(*) FROM signals").fetchone()[0] == 1
        assert (
            store.connection.execute(
                "SELECT count(*) FROM excursions WHERE status = 'complete'"
            ).fetchone()[0]
            == 3
        )
        config = json.loads(store.connection.execute("SELECT config FROM runs").fetchone()[0])
        assert config["strategy_version"] == "support-break-v1"
    finally:
        store.close()


def test_failed_replay_rolls_back_all_rows():
    store = Store(":memory:")
    try:
        events = demo_events()
        events.append(events[0])
        with pytest.raises(ExceptionGroup):
            asyncio.run(replay(events, Settings(), store))
        for table in ("runs", "events", "signals", "trades"):
            assert store.connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    finally:
        store.close()


def test_invalid_jsonl_identifies_line_and_rolls_back(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text(demo_events()[0].model_dump_json() + '\n{"kind":"unknown"}\n')
    store = Store(":memory:")
    try:
        with pytest.raises(ExceptionGroup) as caught:
            asyncio.run(replay(read_events(path), Settings(), store))
        assert f"{path}:2:" in str(caught.value.exceptions[0])
        assert store.connection.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
    finally:
        store.close()


def test_jsonl_round_trip_and_example_config(tmp_path):
    path = tmp_path / "events.jsonl"
    events = demo_events()
    path.write_text("\n".join(event.model_dump_json() for event in events))
    assert list(read_events(path)) == events
    from pathlib import Path

    assert load_settings(Path("config.example.toml")) == Settings()
