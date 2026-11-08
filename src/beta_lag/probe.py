"""Bounded, read-only provider checks. Never log credentials or response bodies."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from beta_lag.binance import BINANCE_RATIO, account_snapshot
from beta_lag.config import Settings


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward authentication headers to a redirected host.
        return None


def request_json(url: str, headers: dict | None = None, body: dict | None = None) -> dict:
    request = Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "beta-lag/0.1", **(headers or {})},
        data=json.dumps(body).encode() if body is not None else None,
    )
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            payload = json.load(response)
        return {"ok": True, "payload": payload}
    except HTTPError as error:
        return {"ok": False, "error": "http_error", "http_status": error.code}
    except (URLError, TimeoutError, OSError):
        return {"ok": False, "error": "network_error"}
    except (ValueError, UnicodeError):
        return {"ok": False, "error": "invalid_json"}


def alpaca_check() -> dict:
    key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
    if not key or not secret:
        return {"ok": False, "error": "missing_credentials"}
    result = request_json(
        "https://data.alpaca.markets/v2/stocks/trades/latest?symbols=QQQ,SOXX&feed=iex",
        {"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
    )
    if not result["ok"]:
        return result
    try:
        trades = result["payload"]["trades"]
        observations = {}
        for symbol in ("QQQ", "SOXX"):
            trade = trades[symbol]
            stamp = datetime.fromisoformat(trade["t"].replace("Z", "+00:00"))
            age = (datetime.now(UTC) - stamp).total_seconds()
            observations[symbol] = {
                "price": float(trade["p"]),
                "observed_at": stamp.isoformat(),
                "age_seconds": round(age),
                "fresh_for_strategy": 0 <= age <= 90,
            }
        return {"ok": True, "feed": "iex", "observations": observations}
    except (KeyError, TypeError, ValueError):
        return {"ok": False, "error": "unexpected_market_data"}


def hyperliquid_check() -> dict:
    result = request_json(
        "https://api.hyperliquid.xyz/info", body={"type": "l2Book", "coin": "BTC"}
    )
    if not result["ok"]:
        return result
    try:
        book = result["payload"]
        bid, ask = float(book["levels"][0][0]["px"]), float(book["levels"][1][0]["px"])
        stamp = datetime.fromtimestamp(book["time"] / 1000, UTC)
        return {
            "ok": True,
            "network": "mainnet",
            "best_bid": bid,
            "best_ask": ask,
            "mid_price": (bid + ask) / 2,
            "observed_at": stamp.isoformat(),
        }
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return {"ok": False, "error": "unexpected_market_data"}


def coinglass_current_check() -> dict:
    from beta_lag.live import COINGLASS_OI, oi_snapshot

    key = os.getenv("COINGLASS_API_KEY")
    if not key:
        return {"ok": False, "error": "missing_credentials"}
    result = request_json(COINGLASS_OI + "?symbol=BTC", {"CG-API-KEY": key})
    if not result["ok"]:
        return result
    try:
        snapshot = oi_snapshot(result["payload"])
    except (ValueError, KeyError, TypeError, StopIteration, AttributeError):
        return {"ok": False, "error": "current_snapshot_unavailable"}
    return {"ok": True, "snapshot": snapshot, "received_at": datetime.now(UTC).isoformat()}


def binance_check() -> dict:
    result = request_json(BINANCE_RATIO + "?symbol=BTCUSDT&period=5m&limit=1")
    if not result["ok"]:
        return result
    try:
        snapshot = account_snapshot(result["payload"])
        age = (datetime.now(UTC) - snapshot["observed_at"]).total_seconds()
        snapshot["observed_at"] = snapshot["observed_at"].isoformat()
    except (ValueError, KeyError, TypeError, OverflowError, OSError):
        return {"ok": False, "error": "invalid_account_snapshot"}
    return {
        "ok": True,
        "snapshot": snapshot,
        "age_seconds": round(age),
        "fresh_for_strategy": 0 <= age <= Settings().long_share_max_age_seconds,
    }


def check_providers() -> dict:
    jobs = {
        "alpaca": alpaca_check,
        "hyperliquid": hyperliquid_check,
        "coinglass_open_interest": coinglass_current_check,
        "binance_account_share": binance_check,
    }
    with ThreadPoolExecutor(max_workers=4) as executor:
        pending = {name: executor.submit(job) for name, job in jobs.items()}
        results = {name: future.result() for name, future in pending.items()}
    return {
        "checked_at": datetime.now(UTC).isoformat(),
        "mode": "read_only_provider_check",
        "all_providers_accessible": all(item["ok"] for item in results.values()),
        "providers": results,
        "historical_requests": False,
        "orders_submitted": 0,
        "note": "Connectivity and access check only; not a strategy run or edge validation.",
    }
