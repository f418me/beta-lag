"""Identical decision and paper-fill ordering for live and replay."""

from beta_lag.engine import Engine
from beta_lag.models import Event, Price
from beta_lag.paper import PaperBroker


def process_event(
    engine: Engine, broker: PaperBroker, event: Event, *other_brokers: PaperBroker
) -> None:
    signal = engine.process(event)
    session_open = engine.session_open()
    for book in (broker, *other_brokers):
        if not session_open and book.pending:
            book.skipped.append({"signal_id": book.pending.id, "reason": "market_session_closed"})
            book.pending = None
        if isinstance(event, Price):
            # Existing positions can exit after the equity session closes.
            book.on_price(event)
        if signal:
            book.submit(signal)
