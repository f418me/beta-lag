"""Public BTCUSDT account positioning; request only the newest published sample."""

import math
from datetime import UTC, datetime

BINANCE_RATIO = "https://fapi.binance.com/futures/data/globalLongShortAccountRatio"
BINANCE_PARAMS = {"symbol": "BTCUSDT", "period": "5m", "limit": 1}


def account_snapshot(payload: list) -> dict:
    if not isinstance(payload, list) or len(payload) != 1:
        raise ValueError("Expected one Binance account-ratio sample")
    row = payload[0]
    if row["symbol"] != "BTCUSDT":
        raise ValueError("Unexpected Binance symbol")
    long_share, short_share = float(row["longAccount"]), float(row["shortAccount"])
    if not all(math.isfinite(value) and 0 <= value <= 1 for value in (long_share, short_share)):
        raise ValueError("Invalid account shares")
    if abs(long_share + short_share - 1) > 0.002:
        raise ValueError("Account shares must sum to one")
    return {
        "long_share": long_share,
        "observed_at": datetime.fromtimestamp(int(row["timestamp"]) / 1000, UTC),
        "source": "binance:BTCUSDT:global-account-share:5m",
    }
