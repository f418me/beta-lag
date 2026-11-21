from datetime import timedelta

from beta_lag.config import Settings
from beta_lag.demo import demo_events
from beta_lag.diagnostics import PriceCadence, strategy_diagnostics
from beta_lag.engine import Engine
from beta_lag.live import Collector
from beta_lag.models import Clock, Metrics, Price, State
from beta_lag.research import compare


def test_cadence_distinguishes_raw_from_sampling_and_bounds_history():
    cadence = PriceCadence()
    origin = demo_events()[0].timestamp
    for second in range(1100):
        stamp = origin + timedelta(seconds=second)
        cadence.observe("BTC", "raw_received", stamp, 90)
        if second % 12 == 0:
            cadence.observe("BTC", "accepted", stamp, 90)
    # Repeated/out-of-order provider observations cannot manufacture negative intervals.
    cadence.observe("BTC", "accepted", origin, 90)
    cadence.observe("BTC", "accepted", origin + timedelta(seconds=1300), 90)
    data = cadence.snapshot(origin + timedelta(seconds=1300))["BTC"]
    assert data["raw_received"]["interval_samples"] == 512
    assert data["raw_received"]["median_seconds"] == 1
    assert data["accepted"]["median_seconds"] == 12
    assert data["accepted"]["gaps_over_price_max_age"] == 1


def test_filter_diagnostics_do_not_change_default_signals():
    engine = Engine(Settings())
    for event in demo_events():
        engine.process(event)
    assert len(engine.signals) == 1
    report = strategy_diagnostics(engine)
    assert report["warnings"] == report["armed"] == report["confirmed"] == 1
    assert report["last_filter_check"]["blockers"] == []
    assert not report["filter_blockers"]


def test_failed_filter_and_waiting_for_new_metrics_are_visible():
    engine = Engine(Settings())
    for event in demo_events():
        if isinstance(event, Metrics) and event.long_share == 0.6:
            assert (
                strategy_diagnostics(engine)["setup_waiting_reason"] == "waiting_for_next_metrics"
            )
            engine.process(event.model_copy(update={"long_share": 0.55}))
            break
        engine.process(event)
    report = strategy_diagnostics(engine)
    assert report["filter_blockers"] == {"long_share_not_high": 1}
    assert report["setup_waiting_reason"] == "derivatives_filters_not_passed"
    assert engine.state == State.WARNING
    collector = Collector(Settings())
    collector.engine = engine
    assert collector.status_snapshot()["diagnostics"]["strategy"] == report


def test_persistence_suppresses_brief_stress_and_resets_on_recovery():
    engine = Engine(Settings(return_window_seconds=60), warning_min_seconds=15)
    origin = demo_events()[0].timestamp
    for second in (0, 60):
        for symbol in ("BTC", "SOXX", "QQQ"):
            stamp = origin + timedelta(seconds=second)
            engine.process(
                Price(
                    timestamp=stamp,
                    observed_at=stamp,
                    symbol=symbol,
                    price=98.5 if second == 60 and symbol == "QQQ" else 100,
                )
            )
    assert engine.state == State.IDLE
    now = engine.now
    engine.process(Clock(timestamp=now + timedelta(seconds=14)))
    assert engine.state == State.IDLE
    engine.process(
        Price(
            timestamp=now + timedelta(seconds=14),
            observed_at=now + timedelta(seconds=14),
            symbol="QQQ",
            price=100,
        )
    )
    assert not engine.candidate_since
    engine.process(
        Price(
            timestamp=now + timedelta(seconds=15),
            observed_at=now + timedelta(seconds=15),
            symbol="QQQ",
            price=98.5,
        )
    )
    engine.process(Clock(timestamp=now + timedelta(seconds=29)))
    assert engine.state == State.IDLE
    engine.process(Clock(timestamp=now + timedelta(seconds=30)))
    assert engine.state == State.WARNING


def test_hysteresis_requires_full_recovery_before_cancel():
    engines = [Engine(Settings()), Engine(Settings(), equity_release_threshold=-0.008)]
    for engine in engines:
        for event in demo_events():
            engine.process(event)
            if engine.state == State.WARNING:
                break
        stamp = engine.now + timedelta(seconds=1)
        engine.process(Price(timestamp=stamp, observed_at=stamp, symbol="QQQ", price=99.1))
    assert engines[0].state == State.IDLE
    assert engines[1].state == State.WARNING
    stamp = engines[1].now + timedelta(seconds=1)
    engines[1].process(Price(timestamp=stamp, observed_at=stamp, symbol="QQQ", price=99.3))
    assert engines[1].state == State.IDLE


def test_offline_report_uses_manifest_and_leaves_open_trades_unfinished(tmp_path):
    config = Settings()
    (tmp_path / "manifest.json").write_text('{"config":' + config.model_dump_json() + "}")
    events = demo_events()
    # End just after entry: no artificial close at end of input.
    selected = []
    for event in events:
        selected.append(event)
        if isinstance(event, Price) and event.symbol == "BTC" and event.price < 99.8:
            break
    journal = tmp_path / "events.jsonl"
    text = "".join(e.model_dump_json() + "\n" for e in selected)
    journal.write_text(text + '{"kind":')
    result = compare(tmp_path)
    assert len(result["variants"]) == 9
    baseline = next(r for r in result["variants"] if r["variant"] == "baseline_30m")
    assert baseline["open_trades"] == 1
    assert baseline["closed_trades"] == 0
    assert baseline["mean_net_return_ex_funding"] is None
    assert result["effective_baseline_config"] == config.model_dump()
    assert journal.read_text() == text + '{"kind":'
