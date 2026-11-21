"""Bounded public WebSocket probe; no credentials, orders, files, or strategy run."""

import argparse
import asyncio
import json
from datetime import UTC, datetime

import aiohttp

from beta_lag.diagnostics import PriceCadence
from beta_lag.live import HYPERLIQUID_WS, hyperliquid_prices


async def probe(seconds: int) -> dict:
    cadence = PriceCadence()
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=seconds + 10)) as session:
        async with session.ws_connect(HYPERLIQUID_WS, heartbeat=20) as ws:
            await ws.send_json(
                {
                    "method": "subscribe",
                    "subscription": {
                        "type": "l2Book",
                        "coin": "BTC",
                    },
                }
            )
            try:
                async with asyncio.timeout(seconds):
                    async for message in ws:
                        if message.type != aiohttp.WSMsgType.TEXT:
                            continue
                        for price in hyperliquid_prices(json.loads(message.data)):
                            now = datetime.now(UTC)
                            cadence.observe("BTC", "raw_received", now, 90)
                            cadence.observe("BTC", "raw_observed", price["observed_at"], 90)
            except TimeoutError:
                pass
    return {"duration_seconds": seconds, "price_cadence": cadence.snapshot(datetime.now(UTC))}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=40, choices=range(1, 121), metavar="1..120")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(probe(args.seconds)), indent=2))
