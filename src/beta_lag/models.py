"""Validated normalized inputs. All times are timezone-aware availability times."""

from datetime import date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EventBase(Model):
    timestamp: AwareDatetime


class Price(EventBase):
    kind: Literal["price"] = "price"
    observed_at: AwareDatetime
    symbol: Literal["BTC", "QQQ", "SOXX"]
    price: float = Field(gt=0)

    @model_validator(mode="after")
    def check_time(self):
        if self.observed_at > self.timestamp:
            raise ValueError("observed_at cannot be later than timestamp (availability time)")
        return self


class Metrics(EventBase):
    kind: Literal["metrics"] = "metrics"
    observed_at: AwareDatetime
    open_interest: float = Field(gt=0)
    long_share: float = Field(ge=0, le=1)
    series: str = Field(min_length=1)
    oi_time_basis: Literal["provider", "receipt"] = "provider"
    long_share_observed_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_time(self):
        if self.observed_at > self.timestamp:
            raise ValueError("observed_at cannot be later than timestamp")
        if self.long_share_observed_at and self.long_share_observed_at > self.timestamp:
            raise ValueError("long_share_observed_at cannot be later than timestamp")
        return self


class Clock(EventBase):
    kind: Literal["clock"] = "clock"


class MarketSession(EventBase):
    kind: Literal["session"] = "session"
    session_date: date
    opens_at: AwareDatetime | None = None
    closes_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def check_bounds(self):
        if (self.opens_at is None) != (self.closes_at is None):
            raise ValueError("Both session boundaries must be supplied or absent")
        if self.opens_at is not None and self.closes_at <= self.opens_at:
            raise ValueError("Session close must follow open")
        return self


Event = Annotated[Price | Metrics | Clock | MarketSession, Field(discriminator="kind")]
EVENT_ADAPTER = TypeAdapter(Event)


class State(StrEnum):
    IDLE = "IDLE"
    WARNING = "WARNING"
    ARMED = "ARMED"
    CONFIRMED = "CONFIRMED"


class Transition(Model):
    timestamp: datetime
    previous: State
    state: State
    reason: str


class Signal(Model):
    id: int
    timestamp: datetime
    trigger_symbol: str
    equity_return: float
    btc_return: float
    support: float
    reference_price: float
    metrics: Metrics
    oi_percentile: float
