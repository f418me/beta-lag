"""Synthetic storage calibration, not a market-performance benchmark.

Run with: uv run python scripts/estimate_storage.py
"""

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from beta_lag.config import Settings
from beta_lag.models import Clock, Metrics, Price
from beta_lag.storage import Store


def sample(folder: Path, symbols: tuple[str, ...]) -> dict:
    path = folder / ("-".join(symbols) + ".duckdb")
    store = Store(path)
    store.connection.execute("CHECKPOINT")
    base = path.stat().st_size
    origin = datetime(2026, 11, 9, 15, tzinfo=UTC)
    rows = []
    jsonl_bytes = 0
    for second in range(3600):
        stamp = origin + timedelta(seconds=second, microseconds=123456)
        events = [
            Price(
                timestamp=stamp,
                observed_at=stamp - timedelta(milliseconds=35),
                symbol=symbol,
                price=76000 + (second % 137) * 0.13,
            )
            for symbol in symbols
        ]
        if second % 5 == 0:
            events.append(Clock(timestamp=stamp))
        if second % 30 == 0:
            events.append(
                Metrics(
                    timestamp=stamp,
                    observed_at=stamp,
                    open_interest=685000 + second,
                    long_share=0.6,
                    oi_time_basis="receipt",
                    long_share_observed_at=stamp - timedelta(seconds=second % 300),
                    series="coinglass:BTC:All:quantity|binance:BTCUSDT:global-account-share:5m",
                )
            )
        for event in events:
            payload = event.model_dump_json()
            jsonl_bytes += len(payload.encode()) + 1
            rows.append(("synthetic", len(rows), stamp, event.kind, payload))
    store.connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", rows)
    store.connection.execute("CHECKPOINT")
    store.close()
    return {
        "events_per_hour": len(rows),
        "jsonl_bytes_per_hour": jsonl_bytes,
        "database_growth_bytes_per_hour": path.stat().st_size - base,
    }


def main():
    with tempfile.TemporaryDirectory() as temporary:
        folder = Path(temporary)
        crypto = sample(folder, ("BTC",))
        all_assets = sample(folder, ("BTC", "QQQ", "SOXX"))
    month = {}
    for key in ("jsonl_bytes_per_hour", "database_growth_bytes_per_hour"):
        # 30 calendar days; 22 full 6.5-hour equity sessions. One stored price/second/symbol.
        month[key.replace("per_hour", "per_month")] = (
            crypto[key] * 24 * 30 + (all_assets[key] - crypto[key]) * 6.5 * 22
        )
    result = {
        "kind": "synthetic_capacity_estimate_not_observed_daily_usage",
        "assumptions": {
            "calendar_days": 30,
            "equity_sessions": 22,
            "equity_session_hours": 6.5,
            "price_sample_seconds": 1,
            "notes": "Excludes observation journal, status logs, snapshots, backups and failures.",
        },
        "crypto_hour": crypto,
        "all_assets_hour": all_assets,
        "monthly": month,
        "base_monthly_gb_decimal": sum(month.values()) / 1e9,
        "config": Settings().model_dump(),
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
