# Replay contract and research rules

## Scope

The replay command is an offline research pipeline. It accepts normalized price observations and derivatives metrics through a bounded asynchronous queue. A single consumer makes deterministic decisions; DuckDB writes run sequentially in a worker thread. The replay input reader is local-file based. The separate `live` command collects new observations and writes replay-compatible event journals; see the [live collection guide](../README.md#collect-live-data-from-now-on).

One replay is one database transaction. Invalid input aborts the run and rolls back its rows. A failed replay does not leave a partial result that could be mistaken for a successful experiment. Live collection uses separate periodic transactions, preserving previous checkpoints after an error. Automatic position/state restoration after restart remains future work.

## Input format

Input is JSONL: one JSON event per line. Blank lines are ignored. Unknown fields, non-finite numbers, invalid prices, and timestamps without a timezone are rejected.

- `timestamp`: when this information became available to the strategy.
- `observed_at`: when the provider observed the price or metric. Must not be later than `timestamp`.
- Events must be in nondecreasing `timestamp` order. File order resolves ties.
- Observations must be strictly increasing per symbol and for the metrics series. Duplicate observations and late corrections are rejected rather than silently changing history. Future live adapters must normalize provider messages accordingly.
- Timestamps may include an explicit offset; comparisons use absolute instants and DuckDB stores timezone-aware timestamps.

Price example:

```json
{"kind":"price","timestamp":"2026-01-05T15:00:00Z","observed_at":"2026-01-05T15:00:00Z","symbol":"BTC","price":95000.0}
```

Supported symbols are `BTC`, `QQQ`, and `SOXX`. These are price observations, not OHLC bars: candle highs and lows must not be passed off as a known intrabar tick sequence.

Metrics example:

```json
{"kind":"metrics","timestamp":"2026-01-05T15:00:01Z","observed_at":"2026-01-05T15:00:00Z","open_interest":120000.0,"long_share":0.60,"series":"provider:BTC:contracts:venue-set:account-share"}
```

`series` must identify the provider, instrument, OI units, venue set, and positioning definition. All metrics in one run must use the same series identifier. `long_share` is a fraction between 0 and 1, not a long/short quotient or a percentage from 0 to 100. The live adapter uses CoinGlass aggregate `open_interest_quantity` and Binance BTCUSDT `longAccount`. These refer to different exchange populations and are recorded as one explicitly named composite series.

Metrics optionally include `long_share_observed_at` (the independent Binance publication timestamp) and `oi_time_basis` (`provider` by default, or `receipt` for CoinGlass snapshots without a source timestamp). `observed_at` is the OI receipt time for the live composite series. Legacy/demo events without the optional account timestamp retain their single-clock behavior. No provider observation time is invented.

A clock event advances time without supplying market information:

```json
{"kind":"clock","timestamp":"2026-01-05T15:01:00Z"}
```

Clock events let replays exercise timeouts during quiet periods. They never synthesize prices or fills. Without new events, the offline engine does not advance wall-clock time.

## Signal rules

Defaults come from `config.example.toml`; all percentage returns are fractions, so `-0.01` means −1%.

1. A 15-minute return compares the latest observed price with the latest earlier observation at or before the window boundary. The current price and boundary observation each have a 90-second freshness tolerance. Different markets may therefore have slightly different effective observation times; these are not perfectly synchronized exchange returns.
2. Either QQQ or SOXX falling strictly more than 1%, while BTC falls less than 0.2%, starts `WARNING`. If both ETFs qualify, the more negative return selects the triggering ETF. The other ETF is not required to be present.
3. A newly received metrics event can arm the setup. Its OI must exceed more than 90% of prior OI observations in the trailing 24 hours, with at least 20 earlier samples. The current sample is excluded from its own baseline. OI must also exceed the immediately preceding sample, and long share must be strictly above 55%.
4. OI percentile means an empirical rank: `count(previous OI < current OI) / count(previous OI)`. It does not interpolate a percentile threshold. Samples are equally weighted, so sampling cadence matters. This is not proof of representative historical coverage.
5. On arming, freeze the sampled BTC low over the preceding 60 seconds. The window starts with the latest observation at or before its boundary, subject to the freshness tolerance. At least two observations and no intervening gap above the tolerance are required. This is a sampled support level, not an exchange candle low.
6. A subsequent BTC observation strictly below `support × (1 − 0.0005)` confirms the signal. The support is not recomputed using the breakout price.
7. Warning and armed states share a five-minute deadline measured from the warning. Missing current data, missing return history, or recovery of the triggering ETF invalidate the setup. Loss of BTC divergence invalidates a warning; after arming, a BTC decline is expected and can confirm the signal. Armed metrics expire after five minutes on their OI clock. If an independent account timestamp is supplied, it must also remain within `long_share_max_age_seconds` (default ten minutes).
8. The event after confirmation returns the engine to `IDLE`. A 15-minute cooldown starts on every return to idle, including invalidations. No additional setup starts during that cooldown.

Live mode records `session` events from today's Alpaca calendar and enables `require_market_session=true`. A session event carries `session_date`, `opens_at`, and `closes_at`; null boundaries mean a closed day. The engine blocks new setups outside `[open, close)`, invalidates active setups at close, and refuses pre-market return baselines. Pending paper entries are canceled at close; existing paper positions continue to exit on BTC prices. Replay must use the original run's effective settings to preserve this behavior. Demo/legacy replays keep the default `require_market_session=false`. Spot-CVD is not implemented.

## Paper fills and exits

The broker permits one open short or pending entry at a time. Signals generated while a position is open are recorded but skipped by the broker.

- Entry uses the next fresh BTC observation whose observation time is strictly after signal availability. It must arrive within 90 seconds of the signal. There is no fill at the confirmation price.
- The entry sell price is reduced by configured slippage. The exit buy price is increased by configured slippage.
- Defaults are a 0.5% stop, a 1% take-profit, and a 30-minute maximum holding time, measured from the slipped entry price or entry availability time as appropriate.
- A stop or target exits at the first observed qualifying price, not the ideal threshold. A time exit also needs a fresh BTC price. Gaps can therefore delay an exit beyond the nominal holding period.
- Fees apply to both entry and exit notional. Reported return is unleveraged, normalized to entry notional, and named `net_return_ex_funding` because funding is not included.
- No collateral model, leverage, liquidation engine, partial fills, bid/ask spread model, or exchange order execution is implemented. Slippage is a fixed assumption, not an order-book simulation.

For one unit of BTC:

```text
entry_fill = next_price × (1 − slippage_bps / 10,000)
exit_fill = exit_observation × (1 + slippage_bps / 10,000)
fees = fee_bps / 10,000 × (entry_fill + exit_fill)
net_return_ex_funding = (entry_fill − exit_fill − fees) / entry_fill
```

## Excursion evaluation and reporting

MFE and MAE use observed BTC prices relative to the simulated entry fill over 15, 30, and 60 minutes after entry. They continue after a simulated exit to study the original opportunity. They are clipped to a minimum of zero and exclude fees. They describe sampled moves, not guaranteed market extremes between observations.

An observation after a horizon never contributes its price to that horizon's extrema. It can establish that the horizon has elapsed. Gaps above the freshness tolerance mark an evaluation `data_gap`; otherwise an elapsed horizon is `complete`. Horizons not reached before end-of-input remain `incomplete`.

Execution gaps flag the affected trade independently. Run summaries include `trades_with_data_gaps`; the headline arithmetic mean includes all closed trades and is descriptive only. It is not a portfolio return or a statistical significance test. Open trades have no realized return, and no end-of-file liquidation is invented.

Database tables:

| Table | Contents |
| --- | --- |
| `runs` | Run identifier, strategy version, full settings, and summary. |
| `events` | Ordered, normalized input payloads and availability timestamps. |
| `transitions` | State changes and their reasons. |
| `signals` | Confirmation-time inputs, frozen support, and derivatives snapshot. |
| `trades` | Paper entry/exit prices, costs through return, exit reason, and gap flag. |
| `excursions` | Per-trade MFE/MAE for each horizon and completion status. |

Every table includes `run_id`. `uv run beta-lag report` prints stored run summaries as JSON; detailed SQL analysis can use the tables above. This schema has no migration mechanism yet.
