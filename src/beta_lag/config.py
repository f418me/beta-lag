"""Versioned research assumptions, deliberately independent of API providers."""

import tomllib
from pathlib import Path

from pydantic import Field

from beta_lag.models import Model


class Settings(Model):
    strategy_version: str = "support-break-v1"
    require_market_session: bool = False
    live_price_sample_seconds: float = Field(default=1.0, ge=0.1)
    checkpoint_seconds: int = Field(default=30, ge=1, le=300)
    status_seconds: int = Field(default=30, ge=1, le=300)
    return_window_seconds: int = Field(default=900, gt=0)
    price_max_age_seconds: int = Field(default=90, gt=0)
    equity_threshold: float = Field(default=-0.01, gt=-1, lt=0)
    btc_threshold: float = Field(default=-0.002, gt=-1, le=0)
    support_window_seconds: int = Field(default=60, gt=0)
    support_break_fraction: float = Field(default=0.0005, ge=0, lt=1)
    setup_timeout_seconds: int = Field(default=300, gt=0)
    cooldown_seconds: int = Field(default=900, gt=0)
    metrics_max_age_seconds: int = Field(default=300, gt=0)
    long_share_max_age_seconds: int = Field(default=600, gt=0)
    oi_history_seconds: int = Field(default=86400, gt=0)
    oi_min_samples: int = Field(default=20, ge=2)
    oi_percentile: float = Field(default=0.90, gt=0, lt=1)
    long_share_threshold: float = Field(default=0.55, ge=0, lt=1)
    fee_bps_per_side: float = Field(default=5, ge=0, lt=10000)
    slippage_bps_per_side: float = Field(default=2, ge=0, lt=10000)
    stop_loss_fraction: float = Field(default=0.005, gt=0, lt=1)
    take_profit_fraction: float = Field(default=0.01, gt=0, lt=1)
    max_holding_seconds: int = Field(default=1800, gt=0)


def load_settings(path: Path | None) -> Settings:
    if path is None:
        return Settings()
    with path.open("rb") as file:
        return Settings.model_validate(tomllib.load(file))
