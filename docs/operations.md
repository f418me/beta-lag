# Always-on operation and capacity

## Run and inspect

```sh
uv sync --locked
uv run --env-file .env beta-lag live --seconds 0
```

`--seconds 0` runs until interrupted. A positive duration still performs a bounded run. Ctrl+C drains accepted events and commits the final snapshot. No exchange orders are sent. Each process start creates a new research run and performs a new warm-up; it does not resume old paper positions or import older market data.

In another terminal:

```sh
uv run beta-lag status
```

Use `--output /path/to/live-root` to inspect another directory, or point directly to a session directory. This reads `status.json`, not DuckDB, so it works while the collector holds the database writer lock. It reports market-session eligibility, price ages, OI/account ages, warm-up progress, the latest state transition, and waiting reasons. A stale heartbeat is flagged; a last-known `running` status is not proof that a process is alive.

Status is atomically replaced every 30 seconds and at shutdown; concise JSON status records also go to stderr for service logging. `status_seconds` is configurable. A five-second clock drives quiet-period updates, so checkpoint/status intervals have up to approximately five seconds of scheduling granularity, plus processing time.

Optional Slack notifications use `SLACK_WEBHOOK_URL` from the environment. The service reads it from the same `/opt/beta-lag/.env` as the provider credentials; restart the service after changing it. See the [Slack setup and message list](../README.md#slack-notifications). A startup notification confirms collector initialization, not provider readiness. `status.json` includes delivery counters and sanitized errors. Slack failures do not stop collection. Delivery drains for at most ten seconds during graceful shutdown; undelivered messages are not restored after a restart. An external monitor is still needed to detect a dead process or stale heartbeat.

## Regular-session gate

Live mode always enables `require_market_session`, regardless of the example TOML value used for offline demos. The effective settings are recorded in both the manifest and database.

Every five minutes, the collector requests only today's [Alpaca US market calendar](https://docs.alpaca.markets/us/v1.1/reference/getcalendar-1) using the existing paper-account credentials. Dates and open/close times are interpreted in `America/New_York`, then converted to UTC. Holidays return no session; early closes use the supplied close time. This is calendar metadata, not a historical market-data download.

Unknown calendars and dates without a regular session block new setups. The open boundary is inclusive and the close boundary exclusive. Pending paper entries are canceled at session close, while already-open paper positions continue to use BTC prices for their configured exits. Pre-market prices cannot supply the return-window baseline: each regular session needs its own price warm-up.

The last successfully obtained calendar remains usable for that trading date if a subsequent refresh fails. It cannot authorize a later date. There is no separate exchange-halt feed; scheduled sessions alone cannot identify every unscheduled market halt.

## Persistence and interruption

Every 30 seconds, the consumer flushes and fsyncs both journals, then commits new events and the current research snapshot in one DuckDB transaction and performs a database checkpoint. `checkpoint_seconds` is configurable. Status includes `last_checkpoint_at`.

Committed inputs, signals, transitions, open/closed paper trades, and excursion results survive process termination. A failed transaction rolls back only that checkpoint; earlier commits remain. The latest uncommitted interval can be lost from the database after a crash, and the journals may contain a newer recoverable tail. Storage or OS failures can still prevent a successful write.

Graceful stops commit the final interval. A restart creates a new run: previous open paper trades remain recorded as open, not silently closed or resumed. Replaying a recovered journal requires the effective settings from its manifest, including `require_market_session=true`; session events are included in the journal. Use a separate recovery database, and remove only an incomplete final JSONL line if an interrupted write left one. Do not pass the generic example settings if they differ from the original run.

For a simple consistent backup, stop the service, copy the entire data directory to another disk/host, then restart. Do not assume copying an actively changing DuckDB file alone produces a consistent backup. There is no automatic retention or deletion policy: monitor free disk space and archive completed runs deliberately.

## Linux NUC service example

The repository includes [a systemd unit](../deploy/beta-lag.service), but does not install or activate it. It assumes a Linux host with systemd, Python 3.12, repository files at `/opt/beta-lag`, and a dedicated `beta-lag` service user. Adapt paths for your host.

After copying the project to `/opt/beta-lag`, create its environment with uv and a system Python accessible to the service user:

```sh
cd /opt/beta-lag
uv sync --locked --no-dev --python /usr/bin/python3.12
```

Avoid an interpreter installed under a personal or root home directory: the unit intentionally restricts home-directory access. Place credentials in `/opt/beta-lag/.env`, owned by root with mode `0600`; systemd reads `EnvironmentFile` before dropping privileges. Do not put secrets into the unit itself. The service runs the uv-created virtual environment directly, so it needs no package downloads at startup.

Example installation commands, run on the target host after reviewing its paths:

```sh
sudo useradd --system --user-group --no-create-home beta-lag
sudo install -m 0644 deploy/beta-lag.service /etc/systemd/system/beta-lag.service
sudo systemctl daemon-reload
sudo systemctl enable --now beta-lag
sudo journalctl -u beta-lag -f
```

Skip user creation if the account already exists. `StateDirectory` creates `/var/lib/beta-lag` with service-user ownership. Repository files and the virtual environment must be readable by that user. The unit uses SIGINT for graceful shutdown and restarts failed processes after 15 seconds. It does not restart intentionally stopped services.

Inspect the service data with:

```sh
sudo -u beta-lag /opt/beta-lag/.venv/bin/beta-lag status --output /var/lib/beta-lag
du -sh /var/lib/beta-lag
df -h /var/lib/beta-lag
```

Use a service-log retention limit appropriate for the host. Keep system time synchronized; future observations are rejected and backward clock jumps stop the run.

## Existing NUC user service

The local checkout also supplies `deploy/beta-lag-user.service`, using
`/home/fre/beta-lag/.venv/bin/beta-lag` and writing under `data/live`.
This is a user service, separate from the `/opt` system-service example above:

```sh
systemctl --user status beta-lag-user.service
journalctl --user -u beta-lag-user.service -n 50 --no-pager
```

After changing the installed unit, run `systemctl --user daemon-reload`.
After code/environment changes, a controlled
`systemctl --user restart beta-lag-user.service` loads them. Check `beta-lag status`
first: restarting does not restore open paper trades and resets warm-up.
Use one collector per Alpaca credential set/feed: concurrent local/manual
collectors can exhaust the provider's connection allowance. A process being
`active` alone does not confirm provider health; check the heartbeat as well.

The diagnostics release was activated on 2026-11-18 at 18:17 Swiss time after
checking there were no active setups or open trades. All providers connected in
the new run. Existing journals remain available for historical research.

## Data volume

Default collection retains at most one price per second for each symbol. BTC is collected around the clock; QQQ/SOXX only during the regular session. Full L2 order-book messages are consumed from the network but are not written to disk. Their midpoint is stored. Sampling can miss intrasecond extrema and is explicitly a research-resolution choice; `live_price_sample_seconds` controls it.

The included synthetic calibration can be reproduced with:

```sh
uv run python scripts/estimate_storage.py
```

On 2026-11-07, with one stored price per second, 30 calendar days, and 22 full 6.5-hour equity sessions, the measured format/database extrapolation was:

| Component | Estimated monthly size |
| --- | --- |
| Normalized event JSONL | 0.55 GB |
| DuckDB event storage | 0.41 GB |
| Subtotal | 0.96 GB |

This is a synthetic capacity estimate, not an observed month. It uses short sample databases, so allocation and compression can differ in a long run. It excludes observation journals, calendar events, service logs, research snapshots, backups, retries, and database maintenance overhead. Budget **1–3 GB/month (roughly 30–100 MB/day)** for local data and logs at the default sampling rate, plus separate backup capacity. A 20 GB data allocation is a practical initial reserve for a multi-week experiment; check actual growth after a full trading day. There is no hard storage cap, and changing the sampling rate changes the estimate.

Wire traffic is larger than stored data because the order-book stream is decoded before sampling. Short weekend tests do not measure weekday equity activity or long-run memory use.

## Fly.io option

Fly.io can host this as a single always-on worker with a persistent volume. No public web server, managed Postgres database, or dedicated ingress IPv4 address is needed for the collector. One writer must own a given DuckDB file. Do not enable automatic stopping of the worker just because it receives no HTTP requests.

For planning, start with one shared CPU and **1 GB RAM**, then measure actual peak usage. DuckDB is configured with a 256 MB internal memory limit and two threads, but that limit is not a cap on total Python/process memory. A 512 MB machine might work; it has not been validated with a full-day workload.

Published pricing checked on 2026-11-07:

| Item | Approximate price per 30-day month |
| --- | --- |
| Frankfurt, shared-cpu-1x, 1 GB RAM | USD 7.73 |
| 20 GB volume at USD 0.15/GB | USD 3.00 |
| Compute + provisioned volume | USD 10.73 |

Allow roughly **USD 12–15/month** as a planning budget before taxes and market-data subscriptions, with actual snapshot storage and outbound traffic determining the extras. Inbound internet transfer is free; European internet egress is USD 0.02/GB. These are estimates, not a quote or a spending cap. [Fly resource pricing](https://docs.fly.io/about/pricing)

Use a persistent volume mounted at the chosen `--output` path. The Machine root filesystem is ephemeral. Volumes are local to a Machine/region and are not automatically replicated; maintain an independent backup. Snapshot storage can add cost. [Fly volume documentation](https://docs.fly.io/volumes/overview)

Test all provider endpoints from the chosen hosting region before moving the collector: public Binance access can depend on the source IP/region. No Fly account, app, paid resource, or deployment has been created here. For an existing always-on NUC, local hosting is the simplest first option; Fly is reasonable if independent hosting and remote operations are more valuable than the additional monthly cost.

## Feed and strategy diagnostics

New live runs include `diagnostics` in each `status.json` heartbeat:

- `price_cadence` distinguishes `raw_received` (local receipt before queueing),
  `raw_observed` (provider timestamps) and `accepted` (normalized stored prices).
  Each series has lifetime observation/gap counts and median/p95/max over its
  latest 512 positive intervals. Duplicate/backward timestamps do not refresh
  the interval baseline. Gap counts use `price_max_age_seconds`.
- `configured_minimum_price_interval_seconds` is a sampling floor, not a promise
  of one provider observation per second. `queue_size` helps detect backpressure.
- `strategy` records warning/arming/confirmation totals, cancellation reasons,
  checks and blockers at metrics events during WARNING, the last check and current
  setup wait reason. A check can fail several filters; counts are checks, not
  distinct setups. Support is only evaluated after freshness and OI warm-up pass.
  The last check is historical; its timestamp may precede the current setup.

For a bounded, public Hyperliquid test independent of the collector:

```sh
uv run python scripts/probe_btc_feed.py --seconds 40
```

It opens only a BTC order-book WebSocket, never Alpaca, and sends no notifications
or orders. It prints timing statistics without saving credentials or payloads.
A test on 2026-11-18 received four BTC book messages in 40 seconds with about
12.4 seconds between observations/receipts, consistent with the stored journal.
This isolates that observation from strategy/database processing but does not
identify whether the remote server or transport causes the low rate. Hyperliquid
[documents](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)
its book as a snapshot feed pushed on eligible blocks at least 0.5 seconds apart;
that is not a guaranteed delivery interval. No source or price semantics have
been changed. Do not manufacture faster prices by repeating old observations.

New diagnostics require a process restart. Restarts still start a fresh warm-up
and do not resume open paper trades. Historical comparisons can use old journals
without restarting the collector.
