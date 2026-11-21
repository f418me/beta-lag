"""Bounded, observational telemetry; never changes strategy decisions."""

from collections import Counter, deque
from datetime import datetime
from math import ceil
from statistics import median


def interval_summary(values) -> dict:
    ordered = sorted(values)
    return {
        "interval_samples": len(ordered),
        "median_seconds": round(median(ordered), 3) if ordered else None,
        "p95_seconds": round(ordered[max(0, ceil(len(ordered) * 0.95) - 1)], 3)
        if ordered
        else None,
        "max_seconds": round(max(ordered), 3) if ordered else None,
    }


class PriceCadence:
    """Recent 512 positive intervals plus lifetime counts per symbol/stage.

    Raw timestamps are captured before queueing. Accepted timestamps are provider
    observation times. Thus upstream cadence and local sampling stay distinguishable.
    """

    def __init__(self):
        self.series: dict[tuple[str, str], dict] = {}

    def observe(self, symbol: str, stage: str, timestamp: datetime, gap_limit: float) -> None:
        key = symbol, stage
        series = self.series.setdefault(
            key,
            {
                "count": 0,
                "last": None,
                "intervals": deque(maxlen=512),
                "gaps": 0,
            },
        )
        series["count"] += 1
        if series["last"] is None:
            series["last"] = timestamp
        elif timestamp > series["last"]:
            interval = (timestamp - series["last"]).total_seconds()
            series["intervals"].append(interval)
            series["gaps"] += interval > gap_limit
            series["last"] = timestamp

    def snapshot(self, now: datetime) -> dict:
        result = {}
        for (symbol, stage), series in self.series.items():
            result.setdefault(symbol, {})[stage] = {
                "observations": series["count"],
                "gaps_over_price_max_age": series["gaps"],
                "last_age_seconds": round(max(0, (now - series["last"]).total_seconds()), 3),
                **interval_summary(series["intervals"]),
            }
        return result


def strategy_diagnostics(engine) -> dict:
    cancellations = Counter(
        t.reason
        for t in engine.transitions
        if t.previous in ("WARNING", "ARMED") and t.state == "IDLE"
    )
    last_warning = next(
        (t.timestamp for t in reversed(engine.transitions) if t.state == "WARNING"), None
    )
    waiting = None
    if engine.state == "WARNING":
        check = engine.last_filter_check
        waiting = (
            "waiting_for_next_metrics"
            if check is None or datetime.fromisoformat(check["timestamp"]) < last_warning
            else "derivatives_filters_not_passed"
        )
    elif engine.state == "ARMED":
        waiting = "waiting_for_btc_support_break"
    return {
        "warnings": sum(t.state == "WARNING" for t in engine.transitions),
        "armed": sum(t.state == "ARMED" for t in engine.transitions),
        "confirmed": len(engine.signals),
        "cancellations": dict(cancellations),
        "metrics_checks_during_warning": engine.filter_checks,
        "filter_blockers": dict(engine.filter_blockers),
        "last_filter_check": engine.last_filter_check,
        "setup_waiting_reason": waiting,
    }
