"""Exploratory, offline-only comparisons against a bounded event-journal snapshot."""

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from beta_lag.config import Settings
from beta_lag.diagnostics import strategy_diagnostics
from beta_lag.engine import Engine
from beta_lag.models import EVENT_ADAPTER
from beta_lag.paper import PaperBroker
from beta_lag.runtime import process_event


def variants(config: Settings):
    """Fixed grid, selected before evaluating results; no automatic parameter selection."""
    for minutes in (15, 30, 60):
        for mode in ("baseline", "persist_15s", "release_minus_0_8pct"):
            settings = Settings.model_validate(
                {
                    **config.model_dump(),
                    "max_holding_seconds": minutes * 60,
                }
            )
            options = {}
            if mode == "persist_15s":
                options["warning_min_seconds"] = 15
            elif mode == "release_minus_0_8pct":
                # Use an explicit 20% relaxation of the original negative entry threshold.
                options["equity_release_threshold"] = config.equity_threshold * 0.8
            yield {
                "name": f"{mode}_{minutes}m",
                "mode": mode,
                "holding_minutes": minutes,
                "options": options,
                "engine": Engine(settings, **options),
                "broker": PaperBroker(settings),
            }


def compare(session: Path) -> dict:
    manifest = json.loads((session / "manifest.json").read_text())
    config = Settings.model_validate(manifest["config"])
    journal = session / "events.jsonl"
    limit = journal.stat().st_size
    scenarios = list(variants(config))
    count = 0
    last = None
    consumed = 0
    with journal.open("rb") as source:
        while consumed < limit:
            line = source.readline(limit - consumed)
            consumed += len(line)
            if not line:
                break
            if not line.endswith(b"\n"):
                break  # Ignore only an incomplete final line in the captured byte boundary.
            event = EVENT_ADAPTER.validate_json(line)
            for scenario in scenarios:
                process_event(scenario["engine"], scenario["broker"], event)
            count += 1
            last = event.timestamp.isoformat()
    results = []
    for scenario in scenarios:
        engine, broker = scenario["engine"], scenario["broker"]
        closed = [t for t in broker.trades if t.exit_at is not None]
        returns = [t.net_return_ex_funding for t in closed]
        results.append(
            {
                "variant": scenario["name"],
                "holding_minutes": scenario["holding_minutes"],
                "experimental_options": scenario["options"],
                "diagnostics": strategy_diagnostics(engine),
                "closed_trades": len(closed),
                "open_trades": len(broker.trades) - len(closed),
                "pending_signal": broker.pending.id if broker.pending else None,
                "wins": sum(r > 0 for r in returns),
                "mean_net_return_ex_funding": sum(returns) / len(returns) if returns else None,
                "exit_reasons": dict(Counter(t.exit_reason for t in closed)),
                "skipped_signals": broker.skipped,
                "trades": broker.snapshot(),
            }
        )
    return {
        "created_at": datetime.now(UTC).isoformat(),
        "source_session": str(session.resolve()),
        "snapshot_bytes": limit,
        "events": count,
        "last_event_at": last,
        "effective_baseline_config": config.model_dump(),
        "interpretation": [
            "Exploratory in-sample comparison; not an out-of-sample validation.",
            "No live configuration changes, notifications, provider calls, or exchange orders.",
            "Same observed prices and costs for every variant; no interpolated fills.",
            "Open positions and pending entries remain unfinished at snapshot end.",
            "Signals can differ by warning mode; compare signal counts as well as returns.",
            "Persistence requires observed stress and divergence at every intervening event.",
            "Longer holds may suppress subsequent entries because only one position is allowed.",
            "Do not select parameters from this sample and call the same sample validation.",
        ],
        "variants": results,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; choose a new path")
    result = compare(args.session)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as output:
        json.dump(result, output, indent=2, default=str, allow_nan=False)
        output.write("\n")
    for row in result["variants"]:
        print(
            json.dumps(
                {
                    k: row[k]
                    for k in (
                        "variant",
                        "closed_trades",
                        "open_trades",
                        "wins",
                        "mean_net_return_ex_funding",
                        "exit_reasons",
                    )
                }
            )
        )
    print(f"Report: {args.output}")


if __name__ == "__main__":
    main()
