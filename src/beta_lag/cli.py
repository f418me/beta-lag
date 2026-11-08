"""Command-line entry points for local research; no credentials required."""

import argparse
import asyncio
import json
from pathlib import Path

import duckdb

from beta_lag.config import load_settings
from beta_lag.demo import demo_events
from beta_lag.replay import read_events, replay
from beta_lag.storage import Store


def error_details(error: BaseException) -> str:
    if isinstance(error, BaseExceptionGroup):
        return "\n".join(error_details(item) for item in error.exceptions)
    return str(error)


def main() -> None:
    parser = argparse.ArgumentParser(description="Cross-asset signal research and paper trading")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("demo", "replay"):
        command = commands.add_parser(name)
        command.add_argument("--db", type=Path, default=Path("data/signals.duckdb"))
        command.add_argument("--config", type=Path)
        if name == "replay":
            command.add_argument("input", type=Path, help="Normalized events in JSONL format")
        else:
            command.add_argument("--export", type=Path, help="Also write demo inputs as JSONL")
    report = commands.add_parser("report")
    report.add_argument("--db", type=Path, default=Path("data/signals.duckdb"))
    probe = commands.add_parser(
        "check-providers", help="Read-only API access and market data check"
    )
    probe.add_argument("--output", type=Path)
    live = commands.add_parser("live", help="Collect new market data only; no historical downloads")
    live.add_argument("--seconds", type=int, default=60)
    live.add_argument("--output", type=Path, default=Path("data/live"))
    live.add_argument("--config", type=Path)
    status = commands.add_parser(
        "status", help="Read the latest live heartbeat without locking DuckDB"
    )
    status.add_argument("--output", type=Path, default=Path("data/live"))
    args = parser.parse_args()
    try:
        if args.command == "status":
            from beta_lag.status import read_status

            print(json.dumps(read_status(args.output), indent=2))
            return
        if args.command == "live":
            from beta_lag.live import collect_live

            result = asyncio.run(
                collect_live(args.seconds, args.output, load_settings(args.config))
            )
            print(json.dumps(result, indent=2))
            return
        if args.command == "check-providers":
            from beta_lag.probe import check_providers

            result = check_providers()
            serialized = json.dumps(result, indent=2, allow_nan=False)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(serialized + "\n")
            print(serialized)
            if not result["all_providers_accessible"]:
                parser.exit(1)
            return
        if args.command == "report":
            with duckdb.connect(str(args.db), read_only=True) as connection:
                rows = connection.execute("SELECT summary FROM runs ORDER BY created_at").fetchall()
            print(json.dumps([json.loads(row[0]) for row in rows], indent=2))
            return
        config = load_settings(args.config)
        events = demo_events() if args.command == "demo" else read_events(args.input)
        if args.command == "demo" and args.export:
            args.export.parent.mkdir(parents=True, exist_ok=True)
            with args.export.open("x") as file:
                for event in events:
                    file.write(event.model_dump_json() + "\n")
        store = Store(args.db)
        try:
            summary = asyncio.run(replay(events, config, store))
        finally:
            store.close()
        print(json.dumps(summary, indent=2))
    except (ValueError, OSError, duckdb.Error, ExceptionGroup) as error:
        parser.exit(1, f"Error: {error_details(error)}\n")


if __name__ == "__main__":
    main()
