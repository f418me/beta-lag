"""Identical decision and paper-fill ordering for live and replay."""

from beta_lag.engine import Engine
from beta_lag.models import Event, Price
from beta_lag.paper import PaperBroker


def process_event(engine: Engine, broker: PaperBroker, event: Event) -> None:
    signal = engine.process(event)
    if not engine.session_open() and broker.pending:
        broker.skipped.append({"signal_id": broker.pending.id, "reason": "market_session_closed"})
        broker.pending = None
    if isinstance(event, Price):
        # Existing paper positions still exit on BTC prices after the equity session closes.
        broker.on_price(event)
    if signal:
        broker.submit(signal)
