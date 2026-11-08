"""Single-position short simulator using observed prices, not hindsight fills."""

from dataclasses import asdict, dataclass, field
from datetime import datetime

from beta_lag.config import Settings
from beta_lag.models import Price, Signal


@dataclass
class Excursion:
    horizon_minutes: int
    mfe: float = 0.0
    mae: float = 0.0
    status: str = "incomplete"
    gap_detected: bool = False


@dataclass
class Trade:
    signal_id: int
    entry_at: datetime
    entry_price: float
    last_price_at: datetime
    exit_at: datetime | None = None
    exit_price: float | None = None
    exit_reason: str | None = None
    net_return_ex_funding: float | None = None
    data_gap: bool = False
    excursions: list[Excursion] = field(
        default_factory=lambda: [Excursion(minutes) for minutes in (15, 30, 60)]
    )


class PaperBroker:
    def __init__(self, config: Settings):
        self.config = config
        self.pending: Signal | None = None
        self.trades: list[Trade] = []
        self.skipped: list[dict] = []

    def submit(self, signal: Signal) -> None:
        if self.pending or any(t.exit_at is None for t in self.trades):
            self.skipped.append({"signal_id": signal.id, "reason": "position_already_open"})
        else:
            self.pending = signal

    def on_price(self, event: Price) -> None:
        if event.symbol != "BTC":
            return
        if (
            event.timestamp - event.observed_at
        ).total_seconds() > self.config.price_max_age_seconds:
            return
        slip = self.config.slippage_bps_per_side / 10000
        for trade in self.trades:
            elapsed = (event.observed_at - trade.entry_at).total_seconds()
            if elapsed <= 0:
                continue
            gap = (event.observed_at - trade.last_price_at).total_seconds()
            if gap > self.config.price_max_age_seconds and trade.exit_at is None:
                trade.data_gap = True
            for evaluation in trade.excursions:
                if evaluation.status != "incomplete":
                    continue
                if gap > self.config.price_max_age_seconds:
                    evaluation.gap_detected = True
                horizon = evaluation.horizon_minutes * 60
                if elapsed <= horizon:
                    evaluation.mfe = max(evaluation.mfe, 1 - event.price / trade.entry_price)
                    evaluation.mae = max(evaluation.mae, event.price / trade.entry_price - 1)
                if elapsed >= horizon:
                    evaluation.status = "data_gap" if evaluation.gap_detected else "complete"
            trade.last_price_at = event.observed_at
            if trade.exit_at is not None:
                continue
            reason = None
            if event.price >= trade.entry_price * (1 + self.config.stop_loss_fraction):
                reason = "stop_loss"
            elif event.price <= trade.entry_price * (1 - self.config.take_profit_fraction):
                reason = "take_profit"
            elif (
                event.timestamp - trade.entry_at
            ).total_seconds() >= self.config.max_holding_seconds:
                reason = "max_holding_time"
            if reason:
                trade.exit_at = event.timestamp
                trade.exit_price = event.price * (1 + slip)
                trade.exit_reason = reason
                fee = self.config.fee_bps_per_side / 10000
                trade.net_return_ex_funding = (
                    (trade.entry_price - trade.exit_price)
                    - fee * (trade.entry_price + trade.exit_price)
                ) / trade.entry_price

        if self.pending:
            age = (event.timestamp - self.pending.timestamp).total_seconds()
            if age > self.config.price_max_age_seconds:
                self.skipped.append({"signal_id": self.pending.id, "reason": "entry_expired"})
                self.pending = None
            elif event.observed_at > self.pending.timestamp:
                self.trades.append(
                    Trade(
                        signal_id=self.pending.id,
                        entry_at=event.timestamp,
                        entry_price=event.price * (1 - slip),
                        last_price_at=event.observed_at,
                    )
                )
                self.pending = None

    def snapshot(self) -> list[dict]:
        return [asdict(trade) for trade in self.trades]
