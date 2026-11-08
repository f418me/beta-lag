"""Synthetic scenario, deliberately containing one qualifying short setup."""

from datetime import UTC, datetime, timedelta

from beta_lag.models import Event, Metrics, Price


def demo_events() -> list[Event]:
    origin = datetime(2026, 1, 5, 15, 0, tzinfo=UTC)
    events: list[Event] = []
    for minute in range(-30, 0):
        stamp = origin + timedelta(minutes=minute)
        events.append(
            Metrics(
                timestamp=stamp,
                observed_at=stamp,
                open_interest=1000 + minute,
                long_share=0.5,
                series="synthetic:BTC:contracts:account-share",
            )
        )
    for second in range(0, 5101, 30):
        stamp = origin + timedelta(seconds=second)
        qqq = 100 if second < 900 else 98.5
        btc = 100.0
        if second >= 960:
            btc = max(97.0, 99.8 - (second - 960) / 600)
        for symbol, value in (("QQQ", qqq), ("SOXX", qqq), ("BTC", btc)):
            events.append(Price(timestamp=stamp, observed_at=stamp, symbol=symbol, price=value))
        if second == 930:
            events.append(
                Metrics(
                    timestamp=stamp,
                    observed_at=stamp,
                    open_interest=1200,
                    long_share=0.6,
                    series="synthetic:BTC:contracts:account-share",
                )
            )
    return events
