"""One transaction per replay; inputs and assumptions remain independently inspectable."""

import json
from datetime import datetime
from pathlib import Path
from uuid import uuid4

import duckdb

from beta_lag.config import Settings
from beta_lag.engine import Engine
from beta_lag.models import Event
from beta_lag.paper import PaperBroker


def json_value(value: object) -> str:
    return json.dumps(value, default=lambda v: v.isoformat() if isinstance(v, datetime) else str(v))


class Store:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = duckdb.connect(str(path))
        self.connection.execute("SET TimeZone='UTC'")
        self.connection.execute("SET memory_limit='256MB'")
        self.connection.execute("SET threads=2")
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS runs (
                run_id VARCHAR PRIMARY KEY, created_at TIMESTAMPTZ DEFAULT current_timestamp,
                strategy_version VARCHAR, config JSON, summary JSON
            );
            CREATE TABLE IF NOT EXISTS events (
                run_id VARCHAR, sequence BIGINT, timestamp TIMESTAMPTZ, kind VARCHAR, payload JSON,
                PRIMARY KEY(run_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS transitions (
                run_id VARCHAR, sequence BIGINT, timestamp TIMESTAMPTZ,
                previous VARCHAR, state VARCHAR, reason VARCHAR
            );
            CREATE TABLE IF NOT EXISTS signals (
                run_id VARCHAR, signal_id BIGINT, timestamp TIMESTAMPTZ, snapshot JSON
            );
            CREATE TABLE IF NOT EXISTS trades (
                run_id VARCHAR, signal_id BIGINT, entry_at TIMESTAMPTZ, exit_at TIMESTAMPTZ,
                entry_price DOUBLE, exit_price DOUBLE, exit_reason VARCHAR,
                net_return_ex_funding DOUBLE, data_gap BOOLEAN
            );
            CREATE TABLE IF NOT EXISTS excursions (
                run_id VARCHAR, signal_id BIGINT, horizon_minutes INTEGER,
                mfe DOUBLE, mae DOUBLE, status VARCHAR
            );
        """)
        self.run_id = str(uuid4())
        self.transaction_active = False

    def begin(self, config: Settings) -> None:
        self.connection.execute("BEGIN TRANSACTION")
        self.transaction_active = True
        self.connection.execute(
            "INSERT INTO runs(run_id, strategy_version, config) VALUES (?, ?, ?)",
            [self.run_id, config.strategy_version, config.model_dump_json()],
        )

    def append(self, sequence: int, event: Event) -> None:
        self.connection.execute(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            [self.run_id, sequence, event.timestamp, event.kind, event.model_dump_json()],
        )

    def finish(self, engine: Engine, broker: PaperBroker, count: int, status="completed") -> dict:
        for index, item in enumerate(engine.transitions):
            self.connection.execute(
                "INSERT INTO transitions VALUES (?, ?, ?, ?, ?, ?)",
                [self.run_id, index, item.timestamp, item.previous, item.state, item.reason],
            )
        for signal in engine.signals:
            self.connection.execute(
                "INSERT INTO signals VALUES (?, ?, ?, ?)",
                [self.run_id, signal.id, signal.timestamp, signal.model_dump_json()],
            )
        for trade in broker.trades:
            self.connection.execute(
                "INSERT INTO trades VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    self.run_id,
                    trade.signal_id,
                    trade.entry_at,
                    trade.exit_at,
                    trade.entry_price,
                    trade.exit_price,
                    trade.exit_reason,
                    trade.net_return_ex_funding,
                    trade.data_gap,
                ],
            )
            for evaluation in trade.excursions:
                self.connection.execute(
                    "INSERT INTO excursions VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        self.run_id,
                        trade.signal_id,
                        evaluation.horizon_minutes,
                        evaluation.mfe,
                        evaluation.mae,
                        evaluation.status,
                    ],
                )
        closed = [t for t in broker.trades if t.exit_at is not None]
        summary = {
            "status": status,
            "run_id": self.run_id,
            "events": count,
            "signals": len(engine.signals),
            "trades": len(broker.trades),
            "closed_trades": len(closed),
            "open_trades": len(broker.trades) - len(closed),
            "trades_with_data_gaps": sum(t.data_gap for t in broker.trades),
            "pending_signal": broker.pending.id if broker.pending else None,
            "skipped_signals": broker.skipped,
            "mean_net_return_ex_funding": (
                sum(t.net_return_ex_funding for t in closed) / len(closed) if closed else None
            ),
            "funding_included": False,
            "final_state": engine.state.value,
        }
        self.connection.execute(
            "UPDATE runs SET summary = ? WHERE run_id = ?", [json_value(summary), self.run_id]
        )
        self.connection.execute("COMMIT")
        self.transaction_active = False
        return summary

    def checkpoint(self, config, batch, engine, broker, count, status="running") -> dict:
        """Atomically commit new inputs and replace the current research snapshot."""
        try:
            exists = self.connection.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", [self.run_id]
            ).fetchone()
            if exists:
                self.connection.execute("BEGIN TRANSACTION")
                self.transaction_active = True
            else:
                self.begin(config)
            for sequence, event in batch:
                self.append(sequence, event)
            for table in ("transitions", "signals", "trades", "excursions"):
                self.connection.execute(f"DELETE FROM {table} WHERE run_id = ?", [self.run_id])
            result = self.finish(engine, broker, count, status)
            self.connection.execute("CHECKPOINT")
            return result
        except BaseException:
            self.rollback()
            raise

    def rollback(self) -> None:
        if self.transaction_active:
            self.connection.execute("ROLLBACK")
            self.transaction_active = False

    def close(self) -> None:
        self.connection.close()
