# System design: cross-asset crypto early warning

**Version:** 2.1

**Updated:** 2026-11-18

**Status:** Implemented research MVP; operational validation remains open.

This document replaces the original design draft and describes the current implementation. The [README](README.md) provides setup instructions, the [replay contract](docs/replay.md) defines exact event and calculation semantics, and the [operations guide](docs/operations.md) covers deployment. Terminology is explained in the [glossary](glossary.md).

## 1. Purpose and trading hypothesis

The system investigates whether sharp weakness in US technology equities can precede a delayed Bitcoin decline. QQQ and SOXX act as stress indicators. If either falls while BTC initially holds its level, the system watches derivatives positioning and requires a local BTC support break before recording a short signal.

The hypothesis is that a shared reduction in risk appetite may reach markets at different times. Elevated and rising open interest can indicate expanding derivatives exposure. A high account-long share can indicate a directional imbalance among accounts. A support break supplies price confirmation rather than assuming equity weakness must immediately spread to BTC.

These observations do not establish causality or prove a liquidation cascade. Every futures contract has both a long and a short side; rising OI alone does not identify new long exposure. Account counts do not reveal position sizes, entry prices, leverage, or liquidation levels. BTC resilience can persist. Combining aggregate CoinGlass OI with Binance positioning is an explicit research assumption because their market populations differ.

The objective is reproducible forward testing with simulated trades and stated costs. Profitability has not been established. MFE exceeding fees does not prove realizable profit: configured entry and exit rules must be evaluated after costs, accounting for data gaps and incomplete runs.

## 2. Implemented scope

- Forward-only collection of prices, OI, account positioning, and today's equity-session calendar.
- Normalized event journals, deterministic replay, and a four-state signal engine.
- A local paper broker for one short position at a time.
- DuckDB research records, periodic live checkpoints, status reporting, and sampled excursion analysis.
- Optional Slack notifications for startup, setup changes, confirmed signals, and paper entry/exit outcomes.
- System and user systemd service examples for a Linux NUC, both using `.venv`.
- Bounded raw/accepted price-cadence diagnostics and strategy filter/cancellation counters.
- An offline fixed-grid comparison of holding limits and warning stability.

No historical market series are downloaded and interruptions are not backfilled. Rolling windows contain observations collected during the current run. Replay reads locally recorded or synthetic events.

Exchange execution, testnet orders, wallets, Spot-CVD, liquidation feeds, funding costs, and automatic state restoration are not implemented. There is no external health-monitoring service or web dashboard.

## 3. Runtime and data flow

The application uses Python 3.12+, asyncio, aiohttp, Pydantic validation, and DuckDB. uv manages dependencies through `pyproject.toml` and `uv.lock`. The package under `src/beta_lag` exposes the installed `beta-lag` command. Development uses an editable installation in `.venv`; the NUC service invokes that environment's command directly.

`uv build` uses `uv_build` to create a wheel and source distribution. Publishing to PyPI or building a container is not required for deployment from a checkout. Ruff checks code and formatting; pytest checks behavior. Neither is a runtime dependency.

Provider tasks feed a bounded asynchronous queue. A single consumer processes accepted events through the strategy and paper broker, maintaining an ordered research record. Live clock events advance time every five seconds without inventing prices. Python scheduling and network delivery offer no hard real-time guarantees.

## 4. Providers and data meanings

| Source | Data and default cadence | Constraint |
| --- | --- | --- |
| Alpaca IEX WebSocket | QQQ/SOXX trades, sampled at most once per second per symbol | Qualifying regular-session trades only; this is not a consolidated market feed. |
| Hyperliquid mainnet WebSocket | BTC L2 best-bid/best-ask midpoint, sampled at most once per second | Reference price, not an executable fill; no orders are sent. |
| CoinGlass REST | Current BTC OI exchange-list snapshot, aggregated `All` row, every 30 seconds | Comparisons use `open_interest_quantity`; no provider observation timestamp is supplied. |
| Binance public REST | Latest BTCUSDT account ratio, `period=5m&limit=1`, polled every 30 seconds | `longAccount` measures Binance account share, not market-wide exposure or taker flow. |
| Alpaca calendar | Today's regular US session, every five minutes | Includes holidays and early closes, not unscheduled trading halts. |

Alpaca and CoinGlass require credentials. Public Hyperliquid and Binance data do not. Credentials come from the environment. Keep `.env` excluded from version control; `.env.example` contains empty placeholders.

The composite metrics series is `coinglass:BTC:All:quantity|binance:BTCUSDT:global-account-share:5m`. CoinGlass OI uses receipt time with `oi_time_basis="receipt"`; a successful poll cannot prove the underlying data is fresh. Binance keeps its original sample timestamp. Polling the same ratio again cannot refresh its age. A usable account sample may accompany multiple new OI snapshots, but each OI snapshot contributes at most one metrics event.

## 5. Time, validation, and session handling

Normalized events are `price`, `metrics`, `session`, and `clock`. `timestamp` represents availability time; `observed_at` represents observation time except for explicitly marked receipt-time OI. `long_share_observed_at` preserves the independent account-ratio timestamp. Session events carry the session date and open/close instants; absent boundaries indicate a closed day.

Timestamps must be timezone-aware. Invalid values, unknown fields, and future observations are rejected. Replay requires nondecreasing availability timestamps and strictly increasing observation timestamps per price series and metrics series. File order resolves availability-time ties. Live normalization drops stale, future, duplicate, and out-of-order observations rather than revising prior decisions.

Live mode always enables regular-session gating and records the effective settings. Session times use the Alpaca calendar and America/New_York timezone. Unknown or closed sessions block new setups. Calendar metadata remains valid for its own date during refresh failures, but cannot authorize a later date.

## 6. Warm-up and signal rules

Defaults are research assumptions in [config.example.toml](config.example.toml). Exact boundary and calculation behavior is specified in the replay contract.

A 15-minute return compares the latest price with the latest observation at or before the window boundary. Current and boundary observations each have a 90-second freshness tolerance. This avoids look-ahead but does not make different markets perfectly synchronized.

OI requires at least 20 prior observations within a rolling 24-hour window. At a successful 30-second cadence the 21st sample arrives after about ten minutes. Price warm-up still takes at least 15 minutes. Every restart begins warm-up again; each regular equity session requires its own return baseline, excluding pre-market prices.

| State | Transition condition | Behavior |
| --- | --- | --- |
| `IDLE` | Wait for usable data and cooldown expiry. | Maintain rolling observations. |
| `WARNING` | Either ETF's 15-minute return is strictly below −1%, while BTC's is strictly above −0.2%. | Record the triggering ETF; choose the more negative return if both qualify. |
| `ARMED` | New OI exceeds more than 90% of prior samples and the immediately preceding sample; account-long share is strictly above 55%. | Freeze the preceding 60-second sampled BTC support. |
| `CONFIRMED` | A subsequent BTC observation is strictly below support × (1 − 0.0005). | Save a signal and request a simulated entry. |

OI rank is `count(previous OI < current OI) / count(previous OI)`; the current sample is excluded. Samples are equally weighted, so cadence matters. Support uses the backward boundary observation within freshness tolerance, requires at least two observations, and rejects gaps above 90 seconds. The breakout price cannot redefine frozen support.

Warning and armed states share a five-minute deadline from warning. Missing usable prices or return history, recovery of the triggering ETF, or session closure invalidates the setup. Loss of BTC divergence invalidates a warning; after arming a BTC decline can confirm the signal. Armed OI expires after 300 seconds on its recorded clock, and account share after 600 seconds on its independent timestamp.

The event after confirmation returns the engine to idle. Every return to idle, including invalidations, starts a 15-minute cooldown. Session closure cancels pending paper entries; open positions continue processing BTC exits outside equity hours.

## 7. Paper execution and evaluation

Only one pending entry or open short is allowed. Additional signals are recorded with a broker skip reason. Entry uses the next fresh BTC observation observed strictly after signal availability and received within 90 seconds. No retrospective fill at the confirmation price is allowed.

Defaults are two basis points of adverse slippage and five basis points of fees per side. Slippage reduces sell entries and increases buy exits. Fees apply to both entry and exit notional. Exits use a 0.5% stop, 1% take-profit, and 30-minute maximum holding time. Stops and targets fill at the first qualifying observed price, including gaps, rather than ideal thresholds. Time exits also require a fresh price and can be delayed by outages.

Return is unleveraged and named `net_return_ex_funding`. There is no funding, leverage, collateral, liquidation, partial-fill, depth, or variable spread model. Fixed slippage does not establish real execution quality. Open trades are not forcibly closed when a recording ends.

MFE and MAE measure sampled BTC excursions from entry over 15, 30, and 60 minutes, continuing after exit. They exclude fees and are clipped at zero. Post-horizon prices cannot improve that horizon's extrema. Results distinguish `complete`, `data_gap`, and `incomplete`; execution gaps are also flagged per trade. Summary means describe closed trades, not portfolio performance or statistical significance.

## 8. Persistence and reproducibility

Each live invocation creates a separate run directory:

| File | Purpose |
| --- | --- |
| `manifest.json` | Source definitions, effective settings, and lifecycle metadata. |
| `events.jsonl` | Accepted normalized events for replay. |
| `observations.jsonl` | Selected provider snapshots and sanitized status changes. |
| `signals.duckdb` | Events and research results. |
| `status.json` | Atomically replaced operational snapshot. |
| `summary.json` | Final summary after shutdown. |

DuckDB tables are `runs`, `events`, `transitions`, `signals`, `trades`, and `excursions`, all associated with a run identifier. Settings and strategy version accompany results. Replay must use the original effective settings, including session gating.

Offline replay uses one transaction and rolls back on invalid input. Live mode commits periodically, by default every 30 seconds with clock scheduling granularity, and on graceful shutdown. Journals are flushed and synchronized before checkpoints; DuckDB is checkpointed after the transaction. Snapshots retain open trades and incomplete evaluations.

A crash can leave journal events newer than the last database commit. Committed checkpoints survive reopening, but there is no automatic journal-tail reconciliation or restoration of strategy state and positions. Restarting creates a new run; old open paper trades remain open in their original records. The schema has no migration mechanism yet.

## 9. Operations and failure handling

The initial deployment target is one collector process on an always-on Linux NUC using the supplied systemd service. Persistent data lives in a writable service directory separate from the checkout. The service requests graceful termination and restarts on process failure. Installation, permissions, reboot behavior, and resource use still need validation on the target machine.

The `status` command reads status files without opening the active database. Status includes provider states, freshness, warm-up, counts, checkpoint time, and open paper trades; it reports stale heartbeats. Structured status also goes to stderr.

An optional `SLACK_WEBHOOK_URL` enables live startup, warning, armed, confirmed, cancellation, and paper-trade outcome messages. Demo/replay remain offline. A separate bounded worker sends messages with timeouts, rate-limit handling, and bounded retries; the market-data consumer only enqueues messages. Notification delivery counts and sanitized errors appear in status and final summaries. The secret URL is never stored in research settings or artifacts. Delivery is best effort, with an in-memory queue, no transactional outbox, and a ten-second shutdown drain. A retry after an ambiguous response can duplicate a notification. Slack notifications do not authorize or execute exchange orders.

WebSocket disconnections trigger exponential reconnect backoff. Authentication or subscription rejection can stop an individual provider task while the process stays alive. A running service therefore does not prove collection health. Missing or stale inputs suppress strategy readiness. Provider-health and stale-heartbeat alerts, disk-space alarms, and retention remain operational work. Provider failure handling will be assessed during supervised real operation; additional simulated provider-failure tests are not required for the initial deployment.

Storage grows with event cadence and recording duration. The supplied synthetic estimator supports capacity planning, not a claim of measured monthly usage. Journals and DuckDB deliberately duplicate useful data. Backups and retention need separate capacity. Deployment and sizing details are maintained in the operations guide.

Price sampling is an upper storage-rate limit, not a guaranteed feed frequency.
Status diagnostics distinguish raw receipt times, provider timestamps and accepted
prices over a bounded interval history. A public, independent probe on 2026-11-18
observed approximately 12.4 seconds between BTC book updates. This reproduces the
journal's coarse resolution without strategy/database processing, but does not
identify server versus transport causation. No provider or price semantics were
changed. See [feed diagnostics](docs/operations.md#feed-and-strategy-diagnostics).

Offline comparisons use the session manifest and a fixed byte snapshot of its
journal, preserving causal next-observation fills and unfinished positions. They
combine 15/30/60-minute holds with original warnings, 15-second observed
persistence or an equity release threshold relaxed to 80% of the entry threshold.
These constructor options are not live configuration fields. The live defaults
remain unchanged. Results are exploratory in-sample measurements, not a validated
parameter selection. See [comparison rules](docs/replay.md#exploratory-parameter-comparison).

## 10. Verification and remaining acceptance criteria

Tests cover strategy timing, validation and replay, paper execution, provider parsing, sessions, status, and checkpoints, including abrupt termination after a committed checkpoint. Tests also cover bounded cadence telemetry, failed-filter reporting, warning persistence/recovery, hysteresis and unfinished offline trades. On 2026-11-18, all 84 tests and both Ruff checks passed. Synthetic demos verify pipeline behavior, not profitability. Short provider probes and live smoke tests do not replace observing a complete market session.

Before the initial code commit:

- Run pytest, Ruff linting and formatting checks, and the package build.
- Review staged files and exclude credentials, IDE settings, generated data, environments, caches, and build artifacts.
- Keep README, design, replay contract, example settings, and operations instructions consistent.

Before unattended operation:

- Run the existing tests, lint and formatting checks, package build, and demo/report flow under Linux.
- Validate service installation, writable paths, graceful shutdown, and reboot recovery on the NUC.
- Observe a complete regular US session, including warm-up, provider freshness, checkpoints, and measured resource growth.
- Establish provider-health monitoring, stale-heartbeat and disk-space alerts, plus a tested backup and retention procedure.
- Confirm operators understand restart isolation and the absence of position restoration.

Real-money execution requires a separate design and validation effort for order handling, reconciliation, risk limits, realistic costs, and independently assessed strategy performance. It is not enabled by completing this research deployment checklist.
