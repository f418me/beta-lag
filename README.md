# beta-lag

A cross-asset early-warning research system for studying whether equity-market stress can precede a delayed Bitcoin sell-off.

The first version is intended for forward testing with real market data and simulated trades. The trading hypothesis is unproven; the purpose of the project is to measure whether it provides a repeatable advantage after costs.

## Project status

The first runnable research core is implemented: normalized event replay, a deterministic state machine, single-position paper shorts, DuckDB persistence, and sampled MFE/MAE evaluation. A synthetic demo exercises the complete flow without credentials.

Forward-only live collection is implemented: Alpaca IEX trades, Hyperliquid BTC order-book mid-prices, current CoinGlass OI snapshots, and the latest Binance BTCUSDT account-long-share sample. It starts with new observations and never downloads historical data or backfills gaps. A separate read-only command checks provider access.

Live inputs now reach the complete paper-trading state machine after warm-up. CoinGlass aggregate OI is paired with Binance account positioning; these measure different market populations and their combination is a research assumption. OI freshness is based on local receipt time because CoinGlass does not provide an observation timestamp. There is no exchange order execution. The live strategy is gated by the regular US session, with holidays and early closes from Alpaca. Funding and realistic order-book fills remain unimplemented.

- [Glossary](glossary.md): plain-English explanations of market and engineering terminology, including Spot-CVD.
- [Example configuration](config.example.toml): configurable research assumptions.
- [Always-on operations](docs/operations.md): NUC service setup, checkpoints, status, storage estimates, and Fly.io costs.
- [Replay format and implemented rules](docs/replay.md): input contract, timing, and simulation details.
- [System design specification](software_design_document_cross_asset_crypto_warning_system.md): the current architecture, trading hypothesis, implemented behavior, limitations, and operational acceptance criteria.

Latest engineering review (2026-11-18): feed/strategy diagnostics and offline
parameter comparisons are implemented; live trading rules remain unchanged.
The independent BTC probe observed roughly 12.4-second updates. The baseline has
only two completed paper trades, so no profitability conclusion or parameter
change is justified. See the [dated findings](docs/replay.md#findings-from-the-2026-11-18-snapshot).

## Quick start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run from the repository root:

```sh
uv sync --locked
uv run beta-lag demo
uv run beta-lag report
```

The demo writes a new run to `data/signals.duckdb`. Its synthetic prices deliberately produce one signal and one completed paper trade; this verifies the pipeline, not profitability. Running it again appends another independently identified run.

To export the demo inputs and replay them with explicit settings:

```sh
uv run beta-lag demo --export data/demo.jsonl
uv run beta-lag replay data/demo.jsonl --config config.example.toml
```

Export refuses to overwrite an existing file. Each database run stores the full configuration, normalized inputs, state transitions, signal snapshots, trades, and excursion evaluations. Open trades and unfinished evaluation horizons remain open or incomplete at end-of-input; the replay does not invent closing prices.

Development checks:

```sh
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

Dependencies are managed through `pyproject.toml` and `uv.lock`. Use `uv add` or `uv add --dev` to change them.

### Python package and build

Ruff is a development tool: `ruff check` checks for code issues and import ordering; `ruff format --check` checks formatting without changing files. Use `uv run ruff format .` to apply formatting. Pytest runs the behavior tests. These tools are not needed by the running collector.

The app is a Python package with a `beta-lag` command registered in `pyproject.toml`. `uv sync --locked` installs it into the project's `.venv` in editable mode, so local source changes are available without rebuilding a release artifact. `uv run beta-lag ...` runs that installed command. The NUC service runs `.venv/bin/beta-lag` directly from the checkout, after dependencies have been installed with uv.

To check release packaging:

```sh
uv build
```

The `uv_build` backend creates an installable wheel (`.whl`) and a source archive (`.tar.gz`) in the `dist/` directory. The build verifies packaging; it does not replace tests, publish to PyPI, or create a Docker image or standalone executable. Building these artifacts is optional for the documented checkout-based NUC deployment. Python 3.12 or newer and the runtime dependencies are still required.

## Local API credentials

Use [.env.example](.env.example) as the template for a local `.env`. Set `APCA_API_KEY_ID` and `APCA_API_SECRET_KEY` to your Alpaca paper-account credentials, and `COINGLASS_API_KEY` to your CoinGlass API key. Hyperliquid and Binance public market data require no credentials. No Binance account or API key is needed. Existing local credentials are not overwritten by any command.

Keep `.env` out of version control; only the empty example belongs there. The current demo and replay do not read credentials or require API access. To load the local credentials and perform a bounded read-only provider check:

```sh
uv run --env-file .env beta-lag check-providers --output data/provider-check.json
```

This checks Alpaca IEX latest trades for QQQ/SOXX, the Hyperliquid mainnet BTC order book, the current CoinGlass OI exchange-list snapshot, and the latest Binance account-long-share sample. It does not download historical series. The output flags stale ETF observations and reports the Binance sample age independently from provider access. It contains selected market data and status codes, never credentials or raw provider error messages. The optional output file is replaced on each invocation.

The connection check is not a streaming strategy run. In particular, successful API access does not mean all inputs required for trade confirmation are available.

## Slack notifications

Set `SLACK_WEBHOOK_URL` in the local `.env` to an [Incoming Webhook](https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks/) for your chosen Slack channel. The URL is a secret; keep it out of Git, TOML settings, and chat messages. An empty value disables notifications. The webhook selects the destination channel; no bot token or additional Python dependency is required.

Send one explicit test message without starting market-data collection:

```sh
uv run --env-file .env beta-lag check-slack
```

Live runs send these English-language messages, labeled `PAPER` and identified by run and UTC time:

- **Collector started:** after the initial database checkpoint. This does not claim provider access or warm-up is complete.
- **WARNING / ARMED:** a new equity-divergence setup and a passed derivatives filter, respectively. Neither is an entry.
- **CONFIRMED:** a BTC support-break signal, including the triggering ETF return, BTC reference/support, OI rank, and long-account share.
- **Paper short opened / closed:** the actual simulated fill or exit, with prices, exit reason, and return after fees/slippage but excluding funding.
- **Setup cancelled / paper entry skipped:** the setup expired or was invalidated, or the broker could not open the simulated position.

Demo and replay never send notifications. Signal rules and simulated execution are unchanged. No exchange orders are submitted.

Delivery uses a separate bounded queue, five-second HTTP timeouts, and up to three attempts for network errors, HTTP 5xx responses, or rate limits. Requests are spaced by at least one second and respect Slack's `Retry-After` header. Slack delivery never blocks market-event processing. Shutdown allows up to ten seconds to finish queued messages; remaining messages are dropped. Status and final summaries expose notification `sent`, `failed`, `dropped`, `pending`, and sanitized `last_error` fields, without the webhook URL or raw response bodies.

Notifications are best effort: the queue is in memory, and signals can be announced before the next periodic database checkpoint. Crashes can lose pending messages, and retries after an ambiguous network failure can duplicate a message. Run and signal identifiers help identify duplicates. This is not an external heartbeat monitor or a provider-outage alert service.

## Collect live data from now on

Run a one-minute smoke test or a longer collection session:

```sh
uv run --env-file .env beta-lag live --seconds 60
uv run --env-file .env beta-lag live --seconds 3600 --config config.example.toml
```

Duration defaults to 60 seconds and accepts 1–86,400 seconds, or `--seconds 0` for continuous operation. The command runs in the foreground and stops after that duration; Ctrl+C requests a graceful stop. It does not install a background service. Each invocation creates a separate directory under `data/live/`; `--output` changes the parent directory.

| Source | Live behavior |
| --- | --- |
| Alpaca | Subscribe to QQQ/SOXX IEX trades; accept regular-session trades without special sale conditions and sample each symbol at most once per second. |
| Hyperliquid | Subscribe to the mainnet BTC L2 book and sample the best-bid/best-ask midpoint at most once per second. |
| CoinGlass | Poll the current BTC OI exchange-list snapshot every 30 seconds and select the aggregated `All` row. |
| Binance | Poll the public BTCUSDT global account ratio every 30 seconds with `period=5m&limit=1`, requesting only the latest published sample. |
| Alpaca calendar | Fetch today's US session every five minutes, including holidays and early closes. |

The [CoinGlass OI endpoint](https://docs.coinglass.com/reference/oi-exchange-list) is documented as available across plans, including Hobbyist. It provides a current snapshot rather than historical candles. Its documented payload has no observation timestamp: we record local receipt time and leave provider observation time unknown. The combined metrics explicitly carry `oi_time_basis="receipt"`; a successful request cannot prove the underlying provider data is fresh. We use `open_interest_quantity` for the OI comparison and retain USD OI in the observation journal.

The [Binance account-ratio endpoint](https://developers.binance.com/docs/derivatives/usds-margined-futures/market-data/rest-api/Long-Short-Ratio) supplies `longAccount` as a fraction, so `0.60` means 60%. It covers Binance BTCUSDT accounts, not all exchanges or position sizes. The collector requests one newest five-minute sample without a historical range or pagination. Polling more often does not change its original timestamp.

Each OI snapshot contributes at most one combined metrics event, once a usable Binance sample is available. Repeated Binance timestamps are ignored; the same valid account sample may accompany several genuinely new OI polls. OI receipt age is limited to 300 seconds. Binance sample age is independently limited to 600 seconds by `long_share_max_age_seconds`, allowing a five-minute publication interval plus delay. This is a configurable research tolerance, not a freshness guarantee. Stale, future, or out-of-order account samples cannot refresh the cached value. Missing or expired inputs prevent new arming; an armed snapshot also expires when either age limit is exceeded.

The collector reconnects dropped WebSockets with exponential backoff, rejects stale/future/duplicate/out-of-order price observations, and advances the state machine with five-second clock events. Authentication or subscription rejection is recorded without exposing raw provider messages. No historical requests or gap filling occur after reconnects.

**Warm-up starts at zero on every run.** The 15-minute price return needs at least 15 minutes of newly collected observations with sufficiently recent boundary prices. OI percentile logic needs at least 20 earlier combined OI observations collected during the run. At a successful 30-second cadence, the 21st sample arrives after roughly ten minutes. The price warm-up still takes at least 15 minutes. Longer outages or rejected inputs extend these periods. There is no imported history or automatic restoration of a prior session. Each new regular session also requires a price return baseline from that session; pre-market observations cannot satisfy warm-up. At weekends or when qualifying IEX trades are absent, BTC collection can continue but the equity-dependent strategy cannot become ready.

Each session contains:

- `manifest.json`: settings, source definitions, OI timestamp assumptions, start/end times, and completion status.
- `events.jsonl`: accepted normalized price, combined metrics, and clock events, written incrementally.
- `observations.jsonl`: current OI snapshots, received Binance account samples, and sanitized provider status changes, written incrementally.
- `signals.duckdb`: accepted events and research snapshots, committed every 30 seconds and on graceful shutdown.
- `status.json`: atomically updated heartbeat with data ages, waiting reasons, session eligibility, and the last checkpoint time.
- `summary.json`: counts, provider status, per-symbol return readiness, OI warm-up progress, derivatives freshness, and research results. `full_strategy_ready` means the required data and warm-up are available, not that an entry signal exists.

The state machine processes accepted events during capture. Checkpoints flush and fsync the journals and commit new inputs and current research results atomically into DuckDB; shutdown commits the final interval. Raw selected OI/account observations remain in their separate journal, while combined metrics are part of the replayable events. Confirmed signals create local paper entries on the next usable BTC update; no orders leave the process.

To inspect a finished session, replace `SESSION` with the directory printed by the command:

```sh
uv run beta-lag report --db data/live/SESSION/signals.duckdb
```

During collection, inspect the latest heartbeat without opening the writer database:

```sh
uv run beta-lag status
```

The heartbeat includes observed raw/accepted price cadence and strategy filter
blockers. See [feed diagnostics](docs/operations.md#feed-and-strategy-diagnostics).
For an exploratory comparison of 15/30/60-minute holds and warning stability,
without changing the live strategy:

```sh
uv run python -m beta_lag.research data/live/SESSION --output data/research/comparison.json
```

This fixed-grid, in-sample report is not validation or automatic parameter tuning.
See [comparison semantics and limits](docs/replay.md#exploratory-parameter-comparison).

New setups and pending entries are blocked outside the regular US session or when its calendar is unknown. Existing paper positions still exit using BTC prices. Live mode always records `require_market_session=true`. Checkpoints preserve previous commits after an error; the latest uncommitted interval may need recovery from the journal. Restarting creates a fresh research run and does not automatically resume old paper positions. See [operations and recovery](docs/operations.md) for NUC setup, backup limits, and capacity estimates.

## The idea

The hypothesis is that stress in traditional financial markets can sometimes reach crypto with a delay. A rapid decline in technology equities may indicate deteriorating risk appetite, while BTC initially remains stable. We want to investigate whether some of these divergences precede a BTC sell-off.

That divergence alone is insufficient. BTC may be genuinely resilient, and the relationship between equities and crypto can change. The proposed strategy therefore looks for three conditions in sequence:

1. **External stress:** QQQ or SOXX falls sharply over a short interval.
2. **Potential fragility:** BTC has barely fallen, while selected derivatives metrics suggest unusually elevated exposure and long-biased positioning in the measured population.
3. **Actual price weakness:** BTC breaks a previously defined local support level. Spot-CVD is a possible additional confirmation once a suitable spot-data source and precise rule are defined.

The system studies whether this combination identifies a vulnerable market before a larger decline, rather than assuming every equity sell-off will drag BTC lower.

## Why a short trade could make sense

Leveraged longs depend on sufficient collateral. If BTC begins to fall, some traders may close positions, stop-loss orders may execute, and positions with insufficient margin may be liquidated. These sales can consume available bids and push the price down further, potentially forcing more positions to close.

This feedback loop is the proposed mechanism behind the strategy. The initial equity shock supplies context; derivatives data supplies a possible fragility filter; the support break supplies evidence that weakness has started to appear in BTC itself.

We wait for confirmation because an apparently fragile market can remain stable or rally. Even after confirmation, selling pressure may be absorbed and the trade may fail. Waiting also has a cost: the entry may be worse, and some moves may be missed. The forward test must measure that tradeoff.

Open interest alone does not establish long crowding: every derivative contract has a long and a short side. Long/short metrics must be interpreted according to their exact account or position definitions. None of these measurements directly reveals all traders' stops or liquidation levels.

## Proposed signal sequence

The thresholds below come from the initial specification. They are starting hypotheses, not optimized or validated parameters.

| State | Meaning | Proposed behavior |
| --- | --- | --- |
| `IDLE` | No active setup | Collect market data and maintain rolling 15-minute returns. |
| `WARNING` | Equity stress with BTC divergence | Detect an ETF return below −1.0% while BTC is above −0.2%, then request derivatives metrics. |
| `ARMED` | Fragility filter passes | Check a precisely defined OI condition and a long-share threshold, initially suggested as above 55%. Wait for confirmation. |
| `CONFIRMED` | BTC weakness appears | Save the evidence and create a simulated short under predefined entry and exit rules. |

Timeouts, invalidation rules, data-freshness checks, and a cooldown must govern the return to `IDLE`. A setup must not remain armed indefinitely or generate repeated trades from one episode.

The first implementation uses either ETF, a trailing 24-hour OI reference with at least 20 earlier samples, both elevated and rising OI, and a long share strictly above 55%. Support is the sampled BTC low over the preceding minute, frozen when the setup arms; confirmation requires a further 0.05% break. These defaults remain research assumptions. Provider adapters must identify consistent OI units and a specific positioning metric. Signals use information available at decision time, including input availability timestamps. See the [exact rules](docs/replay.md).

## Data and implementation approach

The proposed first version uses Python 3.12, `asyncio`, and DuckDB:

- **Alpaca:** equity data for QQQ and SOXX. The free IEX feed covers one exchange, so data quality and coverage must be considered when interpreting short-window moves. [Alpaca market data FAQ](https://docs.alpaca.markets/us/v1.1/docs/market-data-faq)
- **Hyperliquid mainnet:** real BTC market observations for research. Local paper fills are the initial execution model; testnet is reserved for later order-integration checks. [WebSocket endpoints](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket)
- **CoinGlass:** current aggregated OI snapshots, combined with Binance BTCUSDT account positioning. Percentile comparisons require a rolling buffer of observations collected after startup; there are no historical API downloads. [API overview](https://docs.coinglass.com/reference/endpoint-overview)
- **DuckDB:** persist market observations, state transitions, signal snapshots, simulated trades, and evaluation results so decisions can be inspected and replayed.

The first confirmation rule should be a support break. Spot-CVD requires spot trade data and is not interchangeable with CVD from a BTC perpetual market. The initial prototype does not require a dashboard.

## How we will evaluate the idea

For each confirmed signal, record the information available at the decision time and track subsequent BTC price behavior over 15, 30, and 60 minutes. Calculate MFE and MAE, but evaluate tradability using explicit entry, exit, stop, and maximum-holding-time rules.

MFE is the best subsequent price opportunity within a window. It is not an achievable exit by default, so an MFE above fees does not prove profitability. Net results must include entry and exit fees, slippage, applicable funding, and a defensible fill model.

The evaluation should answer:

- Does the complete signal perform better than simpler baselines, such as equity divergence without the derivatives filter?
- What are the average net result, loss distribution, drawdown, and uncertainty around the estimates?
- Do results persist across separate episodes and market conditions, rather than depending on a few overlapping signals?
- Does confirmation improve outcomes enough to justify entering later?
- How sensitive are results to thresholds, delays, stale data, and execution assumptions?

The original two-to-four-week period is an initial observation window, not a guaranteed sufficient sample. If qualifying events are rare, research must continue longer. Rule changes must be versioned so performance is not judged solely on the same observations used to tune the strategy.

## Implementation milestones

1. **Implemented — reproducible core:** configuration, typed events, state transitions, synthetic scenarios, recorded-event replay, and persistent storage.
2. **Partly implemented — live observation:** price streams, current OI and Binance account-share polling, timestamp checks, reconnect handling, forward-only warm-up, and incremental journals are available. Regular-session gating, atomic periodic checkpoints, continuous mode, and heartbeat status are implemented. Automatic position/state restoration after a restart remains future work.
3. **Partly implemented — paper evaluation:** simulated entries and exits, fee/slippage assumptions, sampled MFE/MAE, and run summaries are available. Funding, realistic fills, statistical comparisons, and baseline reports remain to be added.

A future execution service or Rust migration should follow measured needs. Neither a language change nor a short period of promising results establishes a trading edge or justifies live deployment by itself.
