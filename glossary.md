# Glossary

Plain-English explanations of the market and engineering terms used in this project. Examples illustrate definitions; they are not trading recommendations or validated strategy settings.

## Order flow and market structure

### Spot market

A market where the asset itself is traded, such as buying BTC for USD. This differs from trading a derivative contract whose value tracks BTC.

### CVD — Cumulative Volume Delta

A running total of buyer-initiated trading volume minus seller-initiated trading volume, measured from a chosen starting point. An aggressive buyer accepts an existing sell offer; an aggressive seller accepts an existing buy offer. Every trade has both a buyer and a seller: the distinction is which side initiates the trade against available liquidity.

```text
Volume delta = aggressive buy volume − aggressive sell volume
CVD = running sum of volume delta
```

For example, 12 BTC of aggressive buys and 20 BTC of aggressive sells produce a delta of −8 BTC. If CVD was +30 BTC beforehand, it becomes +22 BTC: it is falling, but still positive. A falling CVD and a CVD below zero are different conditions. [CVD definition](https://bookmap.com/knowledgebase/docs/KB-Indicators-CVD)

### Spot-CVD

CVD calculated from spot trades, rather than futures or perpetual trades. Falling Spot-CVD indicates that aggressive selling outweighs aggressive buying over the observed interval. In our hypothesis, this could help confirm selling pressure while BTC initially appears resilient.

It does not measure money leaving the entire crypto market or guarantee a price decline. Its meaning depends on the exchanges, trading pairs, units, and accumulation period used. Perpetual-market trades cannot substitute for spot trades when calculating Spot-CVD. CoinGlass provides a spot CVD API; data-source selection remains an implementation decision. [CoinGlass spot CVD documentation](https://docs.coinglass.com/reference/spot-aggregated-cvd-history)

### Order book / L2

The outstanding buy bids and sell offers on an exchange. Level 2 (L2) data shows available quantities at multiple price levels. Orders can be changed or canceled; displayed liquidity is not executed trading volume.

### Liquidity

The ability to buy or sell without moving the price substantially. Thin liquidity means a relatively small order can consume several price levels.

### Maker / Taker

A maker adds an order that rests in the order book. A taker executes against an existing order, consuming liquidity. Fees can differ between the two.

### Slippage

The difference between the expected execution price and the actual fill price. For example, a simulated short entered at 100 may actually sell at 99.9, producing a worse entry.

## Positions and derivatives

### Long / Short

A long position benefits from a rising price. A short position benefits from a falling price. A short loses money when the price rises.

### Derivative / Perpetual

A derivative is a contract linked to an underlying asset. A perpetual is a derivative without a fixed expiry date; funding payments help keep its price aligned with the underlying market.

### Leverage / Margin

Leverage means controlling a position larger than the collateral committed to it. Margin is the collateral supporting the position. Leverage amplifies gains and losses relative to that collateral.

### OI — Open Interest

The outstanding derivative contracts that have not been closed. Each contract has a long and a short side. Rising OI alone therefore does not prove that traders are predominantly opening longs. USD-denominated OI can also change because prices change, so units matter.

### Long/Short Ratio / Long Share

A comparison of long and short positioning within a specified population. Some metrics count accounts; others measure positions. The exact provider definition matters.

A 55% long share is not the same number as a long/short ratio of 0.55. If long and short accounts form two exclusive groups, a 55% long share corresponds to a ratio of `55 / 45 ≈ 1.22`. It does not mean that 55% of all outstanding contract exposure is long.

### Liquidation / Liquidation Cascade

Liquidation is a forced position reduction or closure when collateral no longer meets an exchange's requirements. Liquidating longs creates selling pressure. A cascade can occur when that selling pushes prices lower, triggering further liquidations. It is a possible feedback loop, not an inevitable result of high OI.

### Stop-Loss

An instruction or strategy rule intended to exit a losing position after a threshold is reached. The execution price can be worse than the threshold during rapid moves.

### Funding

Periodic payments between long and short perpetual holders. Depending on the funding rate, a position may pay or receive funding. These payments affect the net result. [Hyperliquid funding documentation](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/funding)

## Signals and strategy

### TradFi / Cross-Asset / Spillover

TradFi means traditional finance, including stock markets. Cross-asset analysis compares different markets. Spillover describes a shock in one market potentially influencing another, such as equity weakness preceding BTC weakness.

### ETF / QQQ / SOXX

An exchange-traded fund (ETF) holds a portfolio and trades on an exchange. QQQ provides Nasdaq-100 exposure; SOXX provides semiconductor-stock exposure. The specification uses them as indicators of stress in technology-related equities.

### Return / Rolling Window

A return is a relative price change: `current price / earlier price − 1`. A rolling 15-minute return compares the current price with the price 15 minutes earlier, continuously updating the window.

### Candle / OHLC

A candle summarizes prices during an interval: open, high, low, and close (OHLC). A 1-minute candle covers one minute; a 5-minute candle covers five. An unfinished candle can still change.

### Divergence

Markets moving differently over the same interval. Here, equities fall sharply while BTC remains relatively stable. This may reflect delayed selling, but it could also reflect genuine strength.

### Support / Support Break

A price level used as a reference for recent buying interest. A support break occurs when price moves below that level under a defined rule. Our implementation still needs a precise lookback, threshold, and confirmation rule using only information available at the time.

### Percentile

A value's position within a reference distribution. OI above the historical 90th percentile is unusually high relative to that selected history. The lookback and minimum sample size must be specified; future observations must be excluded.

### Trigger / False Positive / Cooldown

A trigger is an event that activates a rule. A false positive is a signal that does not lead to the outcome being tested. A cooldown prevents repeated signals from the same episode for a defined period.

### Edge

A repeatable statistical advantage after realistic costs. A plausible explanation or a few successful trades does not establish an edge.

## Testing and evaluation

### Forward Test / Paper Trading / Backtest / Replay

A forward test evaluates rules on newly arriving data. Paper trading simulates orders and positions without committing real capital. A backtest evaluates historical data. Replay feeds recorded events through the engine to reproduce and inspect its behavior.

### Mainnet / Testnet

Mainnet is the production network with real assets and trading activity. Testnet is a separate testing environment. We intend to use mainnet market data for research and local paper fills for initial evaluation; testnet can later check order integration. [Hyperliquid network endpoints](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket)

### MFE — Maximum Favorable Excursion

The largest favorable price move after entry within a specified window. For a short entered at 100, a subsequent low of 97 gives a price-based MFE of 3%, before costs and leverage effects. It is a retrospective opportunity measure, not realized profit.

### MAE — Maximum Adverse Excursion

The largest unfavorable price move after entry within a specified window. For a short entered at 100, a subsequent high of 102 gives a price-based MAE of 2%. MAE and MFE alone do not show which extreme occurred first.

### P&L / Round-Trip Costs

P&L means profit and loss. Net P&L accounts for relevant costs. Round-trip trading costs include both entry and exit fees and slippage, plus applicable funding. Exit rules determine which part of a price move can actually be captured.

### Look-Ahead Bias

Using information that would not have been available when a decision was made. Examples include defining support using future candles or matching a signal to a later price observation.

## Software architecture

| Term | Meaning in this project |
| --- | --- |
| MVP | Minimum viable product: the smallest useful implementation that can test the hypothesis. |
| Event-driven | Processing incoming market or timer events as they arrive. |
| State machine | Explicit rules for moving between `IDLE`, `WARNING`, `ARMED`, and `CONFIRMED`. |
| Snapshot | A saved record of signal inputs, timestamps, and decisions at a particular moment. |
| API | A structured interface used to request data or interact with a service. |
| REST / Polling | Requesting data over HTTP; polling repeats those requests. |
| WebSocket / WSS | A persistent connection for streamed updates; WSS uses encryption. |
| `asyncio` / `aiohttp` | Python tools for concurrent asynchronous work and HTTP communication. |
| Queue | A buffer that passes events between producers and consumers. |
| DuckDB / OLAP | An analytical database and the style of analysis it supports across many records. |
| ASOF join | Matching a timestamped row to a nearby row in another series. Signal reconstruction must use backward-looking matches with a freshness limit to avoid future data and stale prices. |
| Latency | Delay between an event, its receipt, processing, and any resulting action. |
| Rust / Tokio | A compiled language and its asynchronous runtime; possible future tools if measurements justify a separate execution service. |
