"""Forward-only collection. No historical requests, backfill, or exchange orders."""

import asyncio
import json
import math
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import aiohttp

from beta_lag.binance import BINANCE_PARAMS, BINANCE_RATIO, account_snapshot
from beta_lag.config import Settings
from beta_lag.engine import Engine
from beta_lag.market_session import CALENDAR_URL, NEW_YORK, parse_calendar
from beta_lag.models import Clock, MarketSession, Metrics, Price
from beta_lag.paper import PaperBroker
from beta_lag.replay import write_call
from beta_lag.runtime import process_event
from beta_lag.status import write_json
from beta_lag.storage import Store

ALPACA_WS = "wss://stream.data.alpaca.markets/v2/iex"
HYPERLIQUID_WS = "wss://api.hyperliquid.xyz/ws"
COINGLASS_OI = "https://open-api-v4.coinglass.com/api/futures/open-interest/exchange-list"


def utc_now() -> datetime:
    return datetime.now(UTC)


def positive(value) -> float:
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ValueError("Expected a positive finite number")
    return result


def alpaca_prices(message: list) -> list[dict]:
    result = []
    for row in message:
        if row.get("T") != "t" or row.get("S") not in ("QQQ", "SOXX"):
            continue
        # Use unqualified regular trades; ignore odd lots and special sale conditions.
        if set(row.get("c", [])) - {"@"}:
            continue
        result.append(
            {
                "symbol": row["S"],
                "price": positive(row["p"]),
                "observed_at": datetime.fromisoformat(row["t"].replace("Z", "+00:00")),
            }
        )
    return result


def hyperliquid_prices(message: dict) -> list[dict]:
    if message.get("channel") != "l2Book":
        return []
    data = message["data"]
    if data["coin"] != "BTC":
        return []
    bid = positive(data["levels"][0][0]["px"])
    ask = positive(data["levels"][1][0]["px"])
    if bid > ask:
        raise ValueError("Crossed order book")
    return [
        {
            "symbol": "BTC",
            "price": (bid + ask) / 2,
            "observed_at": datetime.fromtimestamp(data["time"] / 1000, UTC),
        }
    ]


def oi_snapshot(payload: dict) -> dict:
    if str(payload.get("code")) != "0":
        # No raw provider message: it could echo a credential.
        raise ValueError("CoinGlass rejected current OI request")
    row = next(row for row in payload["data"] if row.get("exchange") == "All")
    return {
        "open_interest_usd": positive(row["open_interest_usd"]),
        "open_interest_quantity": positive(row["open_interest_quantity"]),
        "provider_observed_at": None,
        "source": "coinglass:BTC:exchange-list:All",
    }


class Collector:
    def __init__(self, config: Settings):
        self.config = config
        self.queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=4096)
        self.counters: Counter = Counter()
        self.provider_status: dict[str, str] = {}
        self.last_observed: dict[str, datetime] = {}
        self.engine = Engine(config)
        self.broker = PaperBroker(config)
        self.current_oi: dict | None = None
        self.current_accounts: dict | None = None
        self.last_metrics_oi: datetime | None = None
        self.research: dict | None = None
        self.checkpoint_at: str | None = None

    def combined_metrics(self, now: datetime) -> Metrics | None:
        if self.current_oi is None or self.current_accounts is None:
            return None
        oi_time = self.current_oi["received_at"]
        account_time = self.current_accounts["observed_at"]
        if not 0 <= (now - oi_time).total_seconds() <= self.config.metrics_max_age_seconds:
            return None
        if not 0 <= (now - account_time).total_seconds() <= self.config.long_share_max_age_seconds:
            return None
        if self.last_metrics_oi is not None and oi_time <= self.last_metrics_oi:
            return None
        self.last_metrics_oi = oi_time
        return Metrics(
            timestamp=now,
            observed_at=oi_time,
            open_interest=self.current_oi["open_interest_quantity"],
            long_share=self.current_accounts["long_share"],
            long_share_observed_at=account_time,
            oi_time_basis="receipt",
            series="coinglass:BTC:All:quantity|binance:BTCUSDT:global-account-share:5m",
        )

    def readiness(self) -> dict:
        now = self.engine.now or utc_now()
        returns = {s: self.engine.price_return(s) is not None for s in ("BTC", "QQQ", "SOXX")}
        metrics = self.engine.metrics[-1] if self.engine.metrics else None
        fresh = metrics is not None and self.engine.metrics_fresh(metrics)
        samples = sum(
            0 <= (now - m.observed_at).total_seconds() <= self.config.oi_history_seconds
            for m in self.engine.metrics
        )
        warm = samples >= self.config.oi_min_samples + 1
        session_open = self.engine.session_open()
        reasons = []
        if not session_open:
            reasons.append("market_closed_or_calendar_unavailable")
        if not returns["BTC"] or not (returns["QQQ"] or returns["SOXX"]):
            reasons.append("price_warmup_or_stale_prices")
        if not fresh:
            reasons.append("missing_or_stale_derivatives")
        if not warm:
            reasons.append("oi_warmup")
        if not reasons:
            reasons.append("waiting_for_signal_conditions")
        return {
            "market_session_open": session_open,
            "waiting_reasons": reasons,
            "price_age_seconds": {
                symbol: round((now - values[-1].observed_at).total_seconds(), 1) if values else None
                for symbol, values in self.engine.prices.items()
            },
            "oi_receipt_age_seconds": (
                round((now - metrics.observed_at).total_seconds(), 1) if metrics else None
            ),
            "account_sample_age_seconds": (
                round((now - self.current_accounts["observed_at"]).total_seconds(), 1)
                if self.current_accounts
                else None
            ),
            "return_window_seconds": self.config.return_window_seconds,
            "price_returns_available": returns,
            "complete_derivatives_metrics": fresh,
            "oi_samples": samples,
            "oi_warmup_complete": warm,
            "full_strategy_ready": fresh
            and session_open
            and warm
            and returns["BTC"]
            and (returns["QQQ"] or returns["SOXX"]),
        }

    def status_snapshot(self, state="running") -> dict:
        return {
            "updated_at": utc_now().isoformat(),
            "status": state,
            "status_seconds": self.config.status_seconds,
            "state": self.engine.state.value,
            "providers": dict(self.provider_status),
            "counts": dict(self.counters),
            "readiness": self.readiness(),
            "last_checkpoint_at": self.checkpoint_at,
            "last_transition": (
                self.engine.transitions[-1].model_dump(mode="json")
                if self.engine.transitions
                else None
            ),
            "session": self.engine.session.model_dump(mode="json") if self.engine.session else None,
            "open_paper_trades": sum(t.exit_at is None for t in self.broker.trades),
        }

    async def poll_calendar(self, session: aiohttp.ClientSession) -> None:
        key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
        if not key or not secret:
            await self.status("calendar", "missing_credentials")
            return
        while True:
            requested = utc_now().astimezone(NEW_YORK).date()
            try:
                async with session.get(
                    CALENDAR_URL,
                    params={"start": requested.isoformat(), "end": requested.isoformat()},
                    headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        await self.status("calendar", f"http_{response.status}")
                    else:
                        event = parse_calendar(await response.json(), requested, utc_now())
                        await self.emit("session", event=event)
                        await self.status("calendar", "received")
            except (aiohttp.ClientError, ValueError, KeyError, TypeError, OSError, TimeoutError):
                await self.status("calendar", "request_error")
            await asyncio.sleep(300)

    async def emit(self, kind: str, **values) -> None:
        await self.queue.put({"kind": kind, "received_at": utc_now(), **values})

    async def status(self, provider: str, status: str) -> None:
        await self.emit("status", provider=provider, status=status)

    async def stream(self, session: aiohttp.ClientSession, provider: str) -> None:
        alpaca = provider == "alpaca"
        key, secret = os.getenv("APCA_API_KEY_ID"), os.getenv("APCA_API_SECRET_KEY")
        if alpaca and (not key or not secret):
            await self.status(provider, "missing_credentials")
            return
        delay = 1
        while True:
            try:
                async with session.ws_connect(
                    ALPACA_WS if alpaca else HYPERLIQUID_WS,
                    heartbeat=20,
                    timeout=aiohttp.ClientWSTimeout(ws_close=3),
                ) as ws:
                    # Credentials go only in the authenticated provider's WebSocket message.
                    if alpaca:
                        await ws.send_json({"action": "auth", "key": key, "secret": secret})
                        async with asyncio.timeout(10):
                            while True:
                                reply = await ws.receive_json()
                                if any(row.get("T") == "error" for row in reply):
                                    await self.status(provider, "authentication_rejected")
                                    return
                                if any(row.get("msg") == "authenticated" for row in reply):
                                    break
                        await ws.send_json({"action": "subscribe", "trades": ["QQQ", "SOXX"]})
                    else:
                        await ws.send_json(
                            {
                                "method": "subscribe",
                                "subscription": {
                                    "type": "l2Book",
                                    "coin": "BTC",
                                },
                            }
                        )
                    await self.status(provider, "connected")
                    async for msg in ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            if msg.type == aiohttp.WSMsgType.ERROR:
                                break
                            continue
                        try:
                            payload = json.loads(msg.data)
                            if alpaca and any(row.get("T") == "error" for row in payload):
                                await self.status(provider, "subscription_rejected")
                                return
                            if alpaca and any(row.get("T") == "subscription" for row in payload):
                                await self.status(provider, "subscribed")
                            if not alpaca and payload.get("channel") == "subscriptionResponse":
                                await self.status(provider, "subscribed")
                            prices = (
                                alpaca_prices(payload) if alpaca else hyperliquid_prices(payload)
                            )
                            for price in prices:
                                await self.emit("price", provider=provider, **price)
                            if prices:
                                delay = 1
                        except (
                            ValueError,
                            KeyError,
                            IndexError,
                            TypeError,
                            AttributeError,
                            OverflowError,
                        ):
                            await self.status(provider, "invalid_message")
                await self.status(provider, "disconnected")
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError, TypeError):
                await self.status(provider, "connection_error")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def poll_oi(self, session: aiohttp.ClientSession) -> None:
        key = os.getenv("COINGLASS_API_KEY")
        if not key:
            await self.status("coinglass", "missing_credentials")
            return
        while True:
            try:
                async with session.get(
                    COINGLASS_OI,
                    params={"symbol": "BTC"},
                    headers={"CG-API-KEY": key},
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        await self.status("coinglass", f"http_{response.status}")
                        if response.status in (401, 403):
                            return
                    else:
                        payload = await response.json()
                        try:
                            snapshot = oi_snapshot(payload)
                        except (ValueError, KeyError, TypeError, StopIteration):
                            await self.status("coinglass", "snapshot_unavailable")
                        else:
                            await self.emit("open_interest", provider="coinglass", **snapshot)
                            await self.status("coinglass", "snapshot_received")
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                await self.status("coinglass", "request_error")
            await asyncio.sleep(30)

    async def clock(self) -> None:
        while True:
            await self.emit("clock")
            await asyncio.sleep(5)

    async def poll_accounts(self, session: aiohttp.ClientSession) -> None:
        while True:
            try:
                async with session.get(
                    BINANCE_RATIO,
                    params=BINANCE_PARAMS,
                    allow_redirects=False,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as response:
                    if response.status != 200:
                        await self.status("binance", f"http_{response.status}")
                        if response.status in (401, 403, 451):
                            return
                    else:
                        try:
                            snapshot = account_snapshot(await response.json())
                        except (ValueError, KeyError, TypeError, OverflowError, OSError):
                            await self.status("binance", "invalid_account_snapshot")
                        else:
                            await self.emit("account_share", provider="binance", **snapshot)
                            await self.status("binance", "snapshot_received")
            except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
                await self.status("binance", "request_error")
            await asyncio.sleep(30)

    def normalize(self, row: dict, now: datetime) -> Price | Clock | Metrics | MarketSession | None:
        if row["kind"] == "session":
            return row["event"].model_copy(update={"timestamp": now})
        if row["kind"] == "open_interest":
            self.current_oi = row
            return self.combined_metrics(now)
        if row["kind"] == "account_share":
            observed = row["observed_at"]
            age = (now - observed).total_seconds()
            if not 0 <= age <= self.config.long_share_max_age_seconds:
                self.counters["rejected_account_stale_or_future"] += 1
                return None
            if self.current_accounts and observed <= self.current_accounts["observed_at"]:
                self.counters["rejected_account_duplicate_or_late"] += 1
                return None
            self.current_accounts = row
            return self.combined_metrics(now)
        if row["kind"] == "clock":
            return Clock(timestamp=now)
        if row["kind"] != "price":
            return None
        symbol, observed = row["symbol"], row["observed_at"]
        age = (now - observed).total_seconds()
        if age < 0 or age > self.config.price_max_age_seconds:
            self.counters["rejected_stale_or_future"] += 1
            return None
        if symbol in self.last_observed and observed <= self.last_observed[symbol]:
            self.counters["rejected_duplicate_or_late"] += 1
            return None
        if self.config.require_market_session and symbol in ("QQQ", "SOXX"):
            session = self.engine.session
            if (
                not session
                or not session.opens_at
                or not (session.opens_at <= observed <= now < session.closes_at)
            ):
                self.counters["rejected_outside_session"] += 1
                return None
        if symbol in self.last_observed:
            if (
                observed - self.last_observed[symbol]
            ).total_seconds() < self.config.live_price_sample_seconds:
                self.counters[f"sampled_out_{symbol.lower()}"] += 1
                return None
        event = Price(timestamp=now, observed_at=observed, symbol=symbol, price=row["price"])
        self.last_observed[symbol] = observed
        return event

    async def consume(self, folder: Path) -> None:
        store = Store(folder / "signals.duckdb")
        batch = []
        last_checkpoint = last_status = time.monotonic()
        try:
            with (
                (folder / "events.jsonl").open("x", buffering=1) as events,
                (folder / "observations.jsonl").open("x", buffering=1) as observations,
            ):

                def persist(state="running"):
                    for file in (events, observations):
                        file.flush()
                        os.fsync(file.fileno())
                    self.research = store.checkpoint(
                        self.config,
                        batch,
                        self.engine,
                        self.broker,
                        self.counters["events"],
                        state,
                    )
                    self.checkpoint_at = utc_now().isoformat()

                await write_call(persist)
                await write_call(write_json, folder / "status.json", self.status_snapshot())
                while True:
                    row = await self.queue.get()
                    try:
                        if row["kind"] == "stop":
                            final_state = row.get("status", "completed")
                            await write_call(persist, final_state)
                            batch.clear()
                            await write_call(
                                write_json,
                                folder / "status.json",
                                self.status_snapshot(final_state),
                            )
                            return
                        now = utc_now()
                        if self.engine.now and now < self.engine.now:
                            raise ValueError("Local clock moved backwards; capture stopped")
                        event = self.normalize(row, now)
                        if row["kind"] == "status":
                            self.provider_status[row["provider"]] = row["status"]
                            self.counters[f"{row['provider']}:{row['status']}"] += 1
                        if row["kind"] == "open_interest":
                            self.counters["open_interest_snapshots"] += 1
                        if row["kind"] == "account_share":
                            self.counters["account_share_snapshots"] += 1
                        if row["kind"] in ("status", "open_interest", "account_share"):
                            observations.write(json.dumps(row, default=str, allow_nan=False) + "\n")
                        if event is not None:
                            events.write(event.model_dump_json() + "\n")
                            process_event(self.engine, self.broker, event)
                            batch.append((self.counters["events"], event))
                            if isinstance(event, Price):
                                self.counters[f"prices_{event.symbol}"] += 1
                            if isinstance(event, Metrics):
                                self.counters["combined_metrics"] += 1
                            self.counters["events"] += 1
                        tick = time.monotonic()
                        if tick - last_checkpoint >= self.config.checkpoint_seconds:
                            await write_call(persist)
                            batch.clear()
                            last_checkpoint = time.monotonic()
                        if tick - last_status >= self.config.status_seconds:
                            snapshot = self.status_snapshot()
                            await write_call(write_json, folder / "status.json", snapshot)
                            print(
                                json.dumps(snapshot, separators=(",", ":")),
                                file=sys.stderr,
                                flush=True,
                            )
                            last_status = time.monotonic()
                    finally:
                        self.queue.task_done()
        finally:
            store.close()


async def collect_live(seconds: int, output: Path, config: Settings) -> dict:
    if not 0 <= seconds <= 86400:
        raise ValueError("seconds must be between 0 (continuous) and 86400")
    config = config.model_copy(update={"require_market_session": True})
    folder = output / f"{utc_now():%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    folder.mkdir(parents=True, exist_ok=False)
    manifest = {
        "started_at": utc_now().isoformat(),
        "seconds_requested": seconds,
        "mode": "forward_only_collection",
        "historical_requests": False,
        "config": config.model_dump(),
        "status": "collecting",
        "price_sources": {
            "QQQ": "alpaca:iex:regular-trade:sampled",
            "SOXX": "alpaca:iex:regular-trade:sampled",
            "BTC": "hyperliquid:mainnet:book-mid:sampled",
        },
        "derivatives_source": "coinglass:BTC:All:quantity|binance:BTCUSDT:global-account-share:5m",
        "oi_timestamp_basis": "local_receipt_provider_timestamp_unavailable",
    }
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    collector = Collector(config)
    interrupted = False
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, connect=15)
        ) as session:
            consumer = asyncio.create_task(collector.consume(folder))
            producers = [
                asyncio.create_task(collector.stream(session, "alpaca")),
                asyncio.create_task(collector.stream(session, "hyperliquid")),
                asyncio.create_task(collector.poll_oi(session)),
                asyncio.create_task(collector.poll_accounts(session)),
                asyncio.create_task(collector.clock()),
                asyncio.create_task(collector.poll_calendar(session)),
            ]
            timer = asyncio.create_task(
                asyncio.sleep(seconds) if seconds else asyncio.Event().wait()
            )
            try:
                active = {consumer, timer, *producers}
                while not timer.done():
                    done, active = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                    if consumer in done:
                        raise RuntimeError("Collector consumer stopped unexpectedly")
            except asyncio.CancelledError:
                interrupted = True
                raise
            finally:
                for task in [*producers, timer]:
                    task.cancel()
                await asyncio.gather(*producers, timer, return_exceptions=True)
                if not consumer.done():
                    await collector.emit("clock")
                    await collector.queue.put(
                        {"kind": "stop", "status": "interrupted" if interrupted else "completed"}
                    )
                await consumer
    except asyncio.CancelledError:
        interrupted = True
    except BaseException:
        manifest["status"] = "failed"
        (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        try:
            write_json(folder / "status.json", collector.status_snapshot("failed"))
        except OSError:
            pass
        raise
    research = collector.research
    result = {
        "directory": str(folder.resolve()),
        "interrupted": interrupted,
        "providers": collector.provider_status,
        "counts": dict(collector.counters),
        "readiness": collector.readiness(),
        "research": research,
        "orders_submitted": 0,
    }
    (folder / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    manifest["status"] = "interrupted" if interrupted else "completed"
    manifest["ended_at"] = utc_now().isoformat()
    (folder / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return result
