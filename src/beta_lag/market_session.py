"""Today's exchange calendar; no price history or holiday guessing."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo

from beta_lag.models import MarketSession

NEW_YORK = ZoneInfo("America/New_York")
CALENDAR_URL = "https://paper-api.alpaca.markets/v2/calendar"


def parse_calendar(payload: list, requested: date, received: datetime) -> MarketSession:
    if not isinstance(payload, list) or len(payload) > 1:
        raise ValueError("Unexpected calendar response")
    if not payload:
        return MarketSession(timestamp=received, session_date=requested)
    row = payload[0]
    if date.fromisoformat(row["date"]) != requested:
        raise ValueError("Calendar date does not match request")
    bounds = {}
    for source, target in (("open", "opens_at"), ("close", "closes_at")):
        local = datetime.combine(requested, time.fromisoformat(row[source]), NEW_YORK)
        bounds[target] = local.astimezone(UTC)
    return MarketSession(timestamp=received, session_date=requested, **bounds)
