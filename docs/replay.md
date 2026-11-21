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

## Exploratory parameter comparison

Run a fixed nine-variant comparison against one live session without touching its
writer database, sending notifications, or modifying the live settings:

```sh
uv run python -m beta_lag.research data/live/SESSION --output data/research/comparison.json
```

The original effective configuration comes from that session's manifest. The
comparison reads only the journal bytes present at startup and ignores an
incomplete last line. Other malformed events are errors. Output is created
exclusively and refuses overwrite. Each variant preserves fees, slippage,
session gating, stop/target rules and next-observation fills.

The grid combines 15/30/60-minute holding limits with three warning modes:

- Original rules.
- Require 15 seconds of continuously observed equity stress and BTC divergence
  before WARNING. Missing/stale data, closed sessions or broken conditions reset
  the candidate. Gaps between observations are not proof of continuous prices.
- Enter below the original equity threshold, but cancel only after recovery to
  80% of that negative threshold (defaults: enter below -1%, recover to -0.8%).
  All other cancellation conditions remain active.

These experimental options are passed only to the offline engine. They are not
live TOML settings. The original 30-minute variant is the control. Compare signal
counts, open/pending trades, data gaps and exit reasons as well as mean net return.
Longer positions can suppress later entries. MFE/MAE continue after a trade closes;
they are not achievable trade profits or an alternative fill simulation.

This is exploratory analysis on the same collected sample, with no automatic
winner selection. Freeze any proposed change before evaluating new, unseen data;
do not use this report as out-of-sample evidence or infer reliability from a
handful of trades.

## Findings from the 2026-11-18 snapshot

Source: run `20261110T051315Z-b995dd71`, 296,901 events through
2026-11-18 17:15:37 UTC (18:15:37 Swiss time). Full local artifact:
`data/research/comparison-20261118.json` (generated locally; research data are not versioned).
The live strategy retains its original settings.

| Warning mode | Holding limit | Closed trades | Winners | Mean net return per trade |
| --- | --- | --- | --- | --- |
| Original | 15 min | 2 | 1 | -0.005% |
| 15-second persistence | 15 min | 1 | 0 | -0.367% |
| Release at -0.8% | 15 min | 3 | 2 | +0.318% |
| Original | 30 min | 2 | 1 | -0.051% |
| 15-second persistence | 30 min | 1 | 0 | -0.646% |
| Release at -0.8% | 30 min | 3 | 2 | +0.288% |
| Original | 60 min | 2 | 1 | +0.139% |
| 15-second persistence | 60 min | 1 | 0 | -0.646% |
| Release at -0.8% | 60 min | 3 | 2 | +0.414% |

Returns include the configured fee/slippage assumptions and exclude funding.
These are arithmetic per-trade means, not portfolio returns. Every variant had
zero open trades at this snapshot. One to three trades per variant are far too
few to establish an advantage, and all variants use the same observed sample.

The original 30-minute control reproduces the actual two paper trades:

- 2026-11-10: stop-loss, -0.646% net.
- 2026-11-18: time exit, +0.544% net.

The original rules produced 12 warnings, three armings and two confirmations.
Ten cancellations were due to equity stress recovery (seven) or BTC divergence
ending before arming (three). Across 13 metrics checks during WARNING, OI rank
failed ten times and rising OI failed four times. Counts overlap; they are not
counts of distinct cancelled setups. The long-share filter never failed at
those checks.

Observed persistence reduced warnings but excluded the later winning trade,
retaining the first loss. The relaxed equity release threshold admitted an extra
winning trade on November 16 in this sample. A 60-minute hold let the November 18
trade hit its target, but did not rescue the earlier stopped trade. These are
hypotheses to test on future data, not reasons to deploy the best-looking row.
No live holding time, stop, target, warning threshold or data source was changed.
