"""Deterministic state machine; inputs are processed in availability order."""

from collections import Counter, deque
from datetime import datetime, timedelta

from beta_lag.config import Settings
from beta_lag.market_session import NEW_YORK
from beta_lag.models import Event, MarketSession, Metrics, Price, Signal, State, Transition


class Engine:
    def __init__(
        self,
        settings: Settings,
        *,
        warning_min_seconds: int = 0,
        equity_release_threshold: float | None = None,
    ):
        # Experimental knobs are used by offline research only; live uses these defaults.
        if warning_min_seconds < 0:
            raise ValueError("warning_min_seconds must be nonnegative")
        release = (
            settings.equity_threshold
            if equity_release_threshold is None
            else equity_release_threshold
        )
        if not settings.equity_threshold <= release < 0:
            raise ValueError(
                "Release threshold must be at least the entry threshold and below zero"
            )
        self.warning_min_seconds = warning_min_seconds
        self.equity_release_threshold = release
        self.candidate_since: dict[str, datetime] = {}
        self.filter_checks = 0
        self.filter_blockers: Counter = Counter()
        self.last_filter_check: dict | None = None
        self.config = settings
        self.state = State.IDLE
        self.prices: dict[str, deque[Price]] = {s: deque() for s in ("BTC", "QQQ", "SOXX")}
        self.metrics: deque[Metrics] = deque()
        self.last_observed: dict[str, datetime] = {}
        self.transitions: list[Transition] = []
        self.signals: list[Signal] = []
        self.now: datetime | None = None
        self.deadline: datetime | None = None
        self.cooldown_until: datetime | None = None
        self.trigger: str | None = None
        self.support: float | None = None
        self.armed_metrics: Metrics | None = None
        self.percentile = 0.0
        self.session: MarketSession | None = None

    def session_open(self) -> bool:
        if not self.config.require_market_session:
            return True
        session = self.session
        return bool(
            self.now
            and session
            and session.opens_at
            and self.now.astimezone(NEW_YORK).date() == session.session_date
            and session.opens_at <= self.now < session.closes_at
        )

    def move(self, state: State, reason: str) -> None:
        assert self.now is not None
        self.transitions.append(
            Transition(timestamp=self.now, previous=self.state, state=state, reason=reason)
        )
        self.state = state
        if state == State.IDLE:
            self.trigger = self.support = self.armed_metrics = None
            self.deadline = None
            self.cooldown_until = self.now + timedelta(seconds=self.config.cooldown_seconds)

    def latest(self, symbol: str) -> Price | None:
        values = self.prices[symbol]
        if not values or self.now is None:
            return None
        if (self.now - values[-1].observed_at).total_seconds() > self.config.price_max_age_seconds:
            return None
        return values[-1]

    def price_return(self, symbol: str) -> float | None:
        current = self.latest(symbol)
        if current is None:
            return None
        target = current.observed_at - timedelta(seconds=self.config.return_window_seconds)
        for value in reversed(self.prices[symbol]):
            if value.observed_at <= target:
                if self.config.require_market_session:
                    if not self.session_open() or value.observed_at < self.session.opens_at:
                        return None
                if (target - value.observed_at).total_seconds() > self.config.price_max_age_seconds:
                    return None
                return current.price / value.price - 1
        return None

    def find_support(self) -> float | None:
        current = self.latest("BTC")
        if current is None:
            return None
        cutoff = current.observed_at - timedelta(seconds=self.config.support_window_seconds)
        baseline = next((p for p in reversed(self.prices["BTC"]) if p.observed_at <= cutoff), None)
        if baseline is None:
            return None
        if (cutoff - baseline.observed_at).total_seconds() > self.config.price_max_age_seconds:
            return None
        values = [p for p in self.prices["BTC"] if p.observed_at >= baseline.observed_at]
        if len(values) < 2 or any(
            (right.observed_at - left.observed_at).total_seconds()
            > self.config.price_max_age_seconds
            for left, right in zip(values, values[1:], strict=False)
        ):
            return None
        return min(p.price for p in values)

    def metrics_fresh(self, metrics: Metrics) -> bool:
        assert self.now is not None
        if (self.now - metrics.observed_at).total_seconds() > self.config.metrics_max_age_seconds:
            return False
        return (
            metrics.long_share_observed_at is None
            or (self.now - metrics.long_share_observed_at).total_seconds()
            <= self.config.long_share_max_age_seconds
        )

    def process(self, event: Event) -> Signal | None:
        if self.now is not None and event.timestamp < self.now:
            raise ValueError("Events must be ordered by availability timestamp")
        if isinstance(event, (Price, Metrics)):
            key = event.symbol if isinstance(event, Price) else "metrics"
            if key in self.last_observed and event.observed_at <= self.last_observed[key]:
                raise ValueError(f"Duplicate or out-of-order observation for {key}")
            if (
                isinstance(event, Metrics)
                and self.metrics
                and event.series != self.metrics[-1].series
            ):
                raise ValueError("Cannot mix OI units, venues, or positioning series in one run")
            self.last_observed[key] = event.observed_at
        self.now = event.timestamp
        if isinstance(event, MarketSession):
            self.session = event
        if isinstance(event, Price):
            values = self.prices[event.symbol]
            values.append(event)
            retention = max(self.config.return_window_seconds, self.config.support_window_seconds)
            cutoff = self.now - timedelta(seconds=retention + self.config.price_max_age_seconds * 2)
            while values and values[0].observed_at < cutoff:
                values.popleft()
        if isinstance(event, Metrics):
            cutoff = self.now - timedelta(seconds=self.config.oi_history_seconds)
            while self.metrics and self.metrics[0].observed_at < cutoff:
                self.metrics.popleft()
            previous = list(self.metrics)
            self.metrics.append(event)
        else:
            previous = []

        if self.state == State.CONFIRMED:
            self.move(State.IDLE, "confirmation_recorded")
        if not self.session_open():
            self.candidate_since.clear()
            if self.state in (State.WARNING, State.ARMED):
                self.move(State.IDLE, "market_closed_or_calendar_unavailable")
            return None
        if self.state in (State.WARNING, State.ARMED):
            if self.deadline is not None and self.now >= self.deadline:
                self.move(State.IDLE, "setup_expired")
                return None
            if not self.latest("BTC") or not self.latest(self.trigger):
                self.move(State.IDLE, "stale_market_data")
                return None
            equity_return = self.price_return(self.trigger)
            if equity_return is None or equity_return >= self.equity_release_threshold:
                self.move(State.IDLE, "equity_stress_resolved")
                return None
            if self.price_return("BTC") is None:
                self.move(State.IDLE, "missing_btc_return_history")
                return None
            if self.state == State.WARNING:
                btc_return = self.price_return("BTC")
                if btc_return is None or btc_return <= self.config.btc_threshold:
                    self.move(State.IDLE, "divergence_resolved_before_arming")
                    return None

        if self.state == State.IDLE:
            if self.cooldown_until and self.now < self.cooldown_until:
                self.candidate_since.clear()
                return None
            btc_return = self.price_return("BTC")
            if btc_return is None or btc_return <= self.config.btc_threshold:
                self.candidate_since.clear()
                return None
            candidates = [(s, self.price_return(s)) for s in ("QQQ", "SOXX")]
            candidates = [(s, r) for s, r in candidates if r is not None]
            stressed = {
                symbol for symbol, value in candidates if value < self.config.equity_threshold
            }
            self.candidate_since = {
                symbol: self.candidate_since.get(symbol, self.now) for symbol in stressed
            }
            if candidates:
                symbol, value = min(candidates, key=lambda item: item[1])
                if (
                    value < self.config.equity_threshold
                    and (self.now - self.candidate_since[symbol]).total_seconds()
                    >= self.warning_min_seconds
                ):
                    self.trigger = symbol
                    self.deadline = self.now + timedelta(seconds=self.config.setup_timeout_seconds)
                    self.move(State.WARNING, "equity_shock_and_btc_divergence")

        if self.state == State.WARNING and isinstance(event, Metrics):
            fresh = self.metrics_fresh(event)
            warm = len(previous) >= self.config.oi_min_samples
            rank = (
                sum(p.open_interest < event.open_interest for p in previous) / len(previous)
                if previous
                else None
            )
            support = self.find_support() if fresh and warm else None
            checks = {
                "stale_derivatives": fresh,
                "oi_warmup": warm,
                "oi_rank_not_high": rank is not None and rank > self.config.oi_percentile,
                "oi_not_rising": bool(previous)
                and event.open_interest > previous[-1].open_interest,
                "long_share_not_high": event.long_share > self.config.long_share_threshold,
                "support_unavailable": not (fresh and warm) or support is not None,
            }
            blockers = [name for name, passed in checks.items() if not passed]
            self.filter_checks += 1
            self.filter_blockers.update(blockers)
            self.last_filter_check = {
                "timestamp": self.now.isoformat(),
                "blockers": blockers,
                "oi_rank": rank,
                "long_share": event.long_share,
            }
            if not blockers:
                self.armed_metrics = event
                self.percentile = rank
                self.support = support
                self.move(State.ARMED, "elevated_rising_oi_and_long_bias")

        if self.state == State.ARMED:
            assert self.armed_metrics is not None and self.support is not None
            if not self.metrics_fresh(self.armed_metrics):
                self.move(State.IDLE, "stale_armed_metrics")
                return None
            if isinstance(event, Price) and event.symbol == "BTC":
                if event.price < self.support * (1 - self.config.support_break_fraction):
                    signal = Signal(
                        id=len(self.signals) + 1,
                        timestamp=self.now,
                        trigger_symbol=self.trigger,
                        equity_return=self.price_return(self.trigger),
                        btc_return=self.price_return("BTC"),
                        support=self.support,
                        reference_price=event.price,
                        metrics=self.armed_metrics,
                        oi_percentile=self.percentile,
                    )
                    self.signals.append(signal)
                    self.move(State.CONFIRMED, "frozen_support_broken")
                    return signal
        return None
