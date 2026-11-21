"""Optional Slack notifications; delivery never waits in the market-data consumer."""

import asyncio
import logging
import math
import re
from datetime import UTC, datetime

import aiohttp

from beta_lag.engine import Engine
from beta_lag.models import State
from beta_lag.paper import PaperBroker, Trade

LOGGER = logging.getLogger(__name__)
WEBHOOK = re.compile(
    r"https://hooks\.(?:slack\.com|slack-gov\.com)/services/"
    r"[A-Za-z0-9_-]+/[A-Za-z0-9_-]+/[A-Za-z0-9_-]+"
)


class SlackNotifications:
    def __init__(self, webhook_url: str | None):
        value = (webhook_url or "").strip()
        self._url = value if WEBHOOK.fullmatch(value) else None
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        self._worker: asyncio.Task | None = None
        self._closed = False
        self._queued = self.sent = self.failed = self.dropped = 0
        self._next_send = 0.0
        self.last_error = "invalid_webhook_url" if value and not self._url else None
        self.run_name = "connection-check"
        self._transitions = self._signals = self._trades = self._skipped = 0
        self._open_trade: Trade | None = None

    def snapshot(self) -> dict:
        return {
            "enabled": self._url is not None,
            "sent": self.sent,
            "failed": self.failed,
            "dropped": self.dropped,
            "pending": self._queued - self.sent - self.failed - self.dropped,
            "last_error": self.last_error,
        }

    async def start(self, run_name: str) -> None:
        self.run_name = run_name
        if self._url and self._worker is None and not self._closed:
            self._worker = asyncio.create_task(self._run(), name="slack-notifications")
        elif self.last_error:
            LOGGER.warning("Slack notifications disabled: %s", self.last_error)

    def publish(self, title: str, details: str, timestamp: datetime | None = None) -> None:
        if not self._url or self._closed:
            return
        stamp = (timestamp or datetime.now(UTC)).astimezone(UTC).isoformat()
        text = f"[beta-lag | PAPER] {title}\nRun: {self.run_name}\nTime: {stamp}\n{details}"
        # Prevent Slack mentions/link markup in configurable labels.
        text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        try:
            self.queue.put_nowait(text)
            self._queued += 1
        except asyncio.QueueFull:
            self.dropped += 1
            self._queued += 1
            self.last_error = "queue_full"
            LOGGER.warning("Slack notification dropped: queue_full")

    def observe(self, engine: Engine, broker: PaperBroker) -> None:
        """Enqueue only newly recorded changes, after the shared strategy/broker step."""
        if not self._url:
            return
        for transition in engine.transitions[self._transitions :]:
            if transition.state == State.WARNING:
                self.publish(
                    "WARNING — equity stress with BTC divergence",
                    f"{engine.trigger}: {engine.price_return(engine.trigger):+.2%}; "
                    f"BTC: {engine.price_return('BTC'):+.2%}.\n"
                    "Waiting for derivatives filters; no entry yet.",
                    transition.timestamp,
                )
            elif transition.state == State.ARMED:
                metrics = engine.armed_metrics
                self.publish(
                    "ARMED — derivatives filters passed",
                    f"OI rank: {engine.percentile:.1%}; long-account share: "
                    f"{metrics.long_share:.1%}.\nFrozen BTC support: {engine.support:,.2f}; "
                    f"confirmation below "
                    f"{engine.support * (1 - engine.config.support_break_fraction):,.2f}.\n"
                    "Waiting for a support break; no entry yet.",
                    transition.timestamp,
                )
            elif transition.state == State.IDLE and transition.previous in (
                State.WARNING,
                State.ARMED,
            ):
                self.publish(
                    "Setup cancelled", f"Reason: {transition.reason}.", transition.timestamp
                )
        for signal in engine.signals[self._signals :]:
            entry = (
                "Paper entry pending on the next fresh BTC observation."
                if broker.pending and broker.pending.id == signal.id
                else "No new paper entry: another position or entry is already active."
            )
            self.publish(
                f"CONFIRMED — BTC short signal #{signal.id}",
                f"{signal.trigger_symbol}: {signal.equity_return:+.2%}; "
                f"BTC: {signal.btc_return:+.2%}.\n"
                f"BTC reference: {signal.reference_price:,.2f}; "
                f"frozen support: {signal.support:,.2f}.\n"
                f"OI rank: {signal.oi_percentile:.1%}; "
                f"long-account share: {signal.metrics.long_share:.1%}.\n{entry}",
                signal.timestamp,
            )
        if self._open_trade and self._open_trade.exit_at is not None:
            trade = self._open_trade
            self.publish(
                f"Paper short closed — signal #{trade.signal_id}",
                f"Entry: {trade.entry_price:,.2f}; exit: {trade.exit_price:,.2f}.\n"
                f"Reason: {trade.exit_reason}; return after fees/slippage, excluding funding: "
                f"{trade.net_return_ex_funding:+.3%}. Data gap: {trade.data_gap}.",
                trade.exit_at,
            )
            self._open_trade = None
        for trade in broker.trades[self._trades :]:
            self.publish(
                f"Paper short opened — signal #{trade.signal_id}",
                f"Simulated entry: {trade.entry_price:,.2f}.\n"
                f"Stop: {trade.entry_price * (1 + engine.config.stop_loss_fraction):,.2f}; "
                f"target: {trade.entry_price * (1 - engine.config.take_profit_fraction):,.2f}; "
                f"maximum holding time: {engine.config.max_holding_seconds / 60:g} minutes.\n"
                "Local simulation; no exchange order was submitted.",
                trade.entry_at,
            )
            self._open_trade = trade
        for skipped in broker.skipped[self._skipped :]:
            self.publish(
                f"Paper entry skipped — signal #{skipped['signal_id']}",
                f"Reason: {skipped['reason']}.",
                engine.now,
            )
        self._transitions, self._signals = len(engine.transitions), len(engine.signals)
        self._trades, self._skipped = len(broker.trades), len(broker.skipped)

    async def _deliver(self, session: aiohttp.ClientSession, text: str) -> None:
        loop = asyncio.get_running_loop()
        for attempt in range(3):
            await asyncio.sleep(max(0, self._next_send - loop.time()))
            self._next_send = loop.time() + 1
            try:
                async with session.post(
                    self._url,
                    json={"text": text, "unfurl_links": False, "unfurl_media": False},
                    allow_redirects=False,
                ) as response:
                    if response.status == 200:
                        if (await response.text()).strip() == "ok":
                            self.sent += 1
                            self.last_error = None
                            return
                        error = "unexpected_response"
                        break
                    error = f"http_{response.status}"
                    if response.status == 429:
                        try:
                            delay = float(response.headers.get("Retry-After", "30"))
                            if not math.isfinite(delay) or delay < 0:
                                raise ValueError
                        except ValueError:
                            delay = 30
                        self._next_send = loop.time() + max(1, delay)
                    elif response.status >= 500:
                        self._next_send = loop.time() + 2**attempt
                    else:
                        break
            except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
                # Exception strings and response bodies can contain the secret webhook URL.
                error = "request_error"
                self._next_send = loop.time() + 2**attempt
        self.failed += 1
        self.last_error = error
        LOGGER.warning("Slack notification delivery failed: %s", error)

    async def _run(self) -> None:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            while True:
                text = await self.queue.get()
                try:
                    await self._deliver(session, text)
                finally:
                    self.queue.task_done()

    async def close(self, timeout: float = 10) -> None:
        if self._closed:
            return
        self._closed = True
        if self._worker is None:
            return
        try:
            await asyncio.wait_for(self.queue.join(), timeout=timeout)
        except TimeoutError:
            self.last_error = "shutdown_timeout"
            LOGGER.warning("Slack notifications remaining at shutdown were dropped")
        finally:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self.dropped += self.snapshot()["pending"]
            while not self.queue.empty():
                self.queue.get_nowait()
                self.queue.task_done()


async def check_slack(webhook_url: str | None) -> dict:
    notifier = SlackNotifications(webhook_url)
    await notifier.start("connection-check")
    notifier.publish("TEST — Slack connection check", "No strategy run or exchange order started.")
    await notifier.close()
    return notifier.snapshot()
