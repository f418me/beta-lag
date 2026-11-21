import asyncio
import json
from datetime import timedelta

import aiohttp
import pytest

from beta_lag.config import Settings
from beta_lag.demo import demo_events
from beta_lag.engine import Engine
from beta_lag.live import Collector
from beta_lag.models import Clock, State
from beta_lag.notifications import SlackNotifications, check_slack
from beta_lag.paper import PaperBroker
from beta_lag.runtime import process_event

WEBHOOK = "https://hooks.slack.com/services/TEST/CHANNEL/EXAMPLE"


class Response:
    def __init__(self, status=200, body="ok", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def text(self):
        return self.body


class Session:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def messages(notifier):
    result = []
    while not notifier.queue.empty():
        result.append(notifier.queue.get_nowait())
        notifier.queue.task_done()
    return result


def test_connection_check_posts_to_webhook_and_reports_delivery(monkeypatch):
    session = Session([Response()])
    monkeypatch.setattr("beta_lag.notifications.aiohttp.ClientSession", lambda **kw: session)
    result = asyncio.run(check_slack(WEBHOOK))
    assert result == {
        "enabled": True,
        "sent": 1,
        "failed": 0,
        "dropped": 0,
        "pending": 0,
        "last_error": None,
    }
    url, request = session.requests[0]
    assert url == WEBHOOK
    assert not request["allow_redirects"]
    assert "TEST" in request["json"]["text"]
    assert WEBHOOK not in json.dumps(result)


@pytest.mark.parametrize(
    "url", [None, "", "https://example.com/private", "http://hooks.slack.com/x"]
)
def test_missing_or_invalid_webhook_never_opens_a_session(url, monkeypatch):
    def unexpected(**kwargs):
        pytest.fail("Disabled Slack must not open an HTTP session")

    monkeypatch.setattr("beta_lag.notifications.aiohttp.ClientSession", unexpected)
    result = asyncio.run(check_slack(url))
    assert not result["enabled"]
    assert result["sent"] == result["pending"] == 0
    assert result["last_error"] == ("invalid_webhook_url" if url else None)


def test_rate_limits_and_transient_failures_retry_without_redirecting(monkeypatch):
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("beta_lag.notifications.asyncio.sleep", sleep)
    session = Session([Response(429, headers={"Retry-After": "30"}), Response(503), Response()])
    notifier = SlackNotifications(WEBHOOK)
    asyncio.run(notifier._deliver(session, "test"))
    assert notifier.sent == 1 and notifier.failed == 0
    assert len(session.requests) == 3
    assert sleeps[1] >= 29
    assert sleeps[2] >= 1


@pytest.mark.parametrize("mode", ["denied", "network_error", "unexpected_body"])
def test_delivery_failure_is_bounded_and_never_logs_secrets(mode, monkeypatch, caplog):
    async def sleep(delay):
        pass

    monkeypatch.setattr("beta_lag.notifications.asyncio.sleep", sleep)
    responses = {
        "denied": [Response(403, WEBHOOK)],
        "network_error": [aiohttp.ClientConnectionError(WEBHOOK) for _ in range(3)],
        "unexpected_body": [Response(200, WEBHOOK)],
    }
    notifier = SlackNotifications(WEBHOOK)
    session = Session(responses[mode])
    asyncio.run(notifier._deliver(session, "test"))
    assert notifier.failed == 1 and notifier.sent == 0
    assert len(session.requests) == (3 if mode == "network_error" else 1)
    assert WEBHOOK not in caplog.text + json.dumps(notifier.snapshot())


def test_signal_notifications_precede_fill_and_are_not_repeated():
    engine, broker = Engine(Settings()), PaperBroker(Settings())
    notifier = SlackNotifications(WEBHOOK)
    delivered = []
    for event in demo_events():
        process_event(engine, broker, event)
        notifier.observe(engine, broker)
        current = messages(notifier)
        if any("CONFIRMED" in text for text in current):
            assert not broker.trades
            assert broker.pending is not None
        delivered.extend(current)
        notifier.observe(engine, broker)
        assert messages(notifier) == []
    titles = [text.splitlines()[0] for text in delivered]
    assert len(titles) == 5
    assert "WARNING" in titles[0]
    assert "ARMED" in titles[1]
    assert "CONFIRMED" in titles[2]
    assert "Paper short opened" in titles[3]
    assert "Paper short closed" in titles[4]
    assert "excluding funding" in delivered[-1]


def test_invalidated_setup_has_a_cancellation_notification():
    engine, broker = Engine(Settings()), PaperBroker(Settings())
    notifier = SlackNotifications(WEBHOOK)
    for event in demo_events():
        process_event(engine, broker, event)
        notifier.observe(engine, broker)
        messages(notifier)
        if engine.state == State.ARMED:
            break
    event = Clock(timestamp=engine.now + timedelta(seconds=301))
    process_event(engine, broker, event)
    notifier.observe(engine, broker)
    assert "Setup cancelled" in messages(notifier)[0]
    assert not broker.trades


def test_skipped_entry_is_reported_without_claiming_a_fill():
    engine, broker = Engine(Settings()), PaperBroker(Settings())
    notifier = SlackNotifications(WEBHOOK)
    for event in demo_events():
        process_event(engine, broker, event)
        notifier.observe(engine, broker)
        messages(notifier)
        if broker.pending:
            broker.submit(broker.pending)
            break
    notifier.observe(engine, broker)
    result = messages(notifier)
    assert len(result) == 1
    assert "Paper entry skipped" in result[0] and "position_already_open" in result[0]
    assert not broker.trades


def test_full_queue_drops_notifications_without_blocking():
    notifier = SlackNotifications(WEBHOOK)
    for index in range(101):
        notifier.publish("test", str(index))
    assert notifier.queue.qsize() == 100
    assert notifier.snapshot()["dropped"] == 1
    assert notifier.snapshot()["pending"] == 100


def test_slow_slack_does_not_block_capture_or_shutdown(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    started = asyncio.Event()

    async def stalled_delivery(self, session, text):
        started.set()
        await asyncio.Event().wait()

    original_close = SlackNotifications.close

    async def close(self):
        await original_close(self, timeout=0.01)

    monkeypatch.setattr(SlackNotifications, "_deliver", stalled_delivery)
    monkeypatch.setattr(SlackNotifications, "close", close)

    async def scenario():
        collector = Collector(Settings())
        consumer = asyncio.create_task(collector.consume(tmp_path))
        await asyncio.wait_for(started.wait(), timeout=3)
        await collector.emit("clock")
        await asyncio.wait_for(collector.queue.join(), timeout=3)
        assert collector.counters["events"] == 1
        await collector.queue.put({"kind": "stop"})
        await asyncio.wait_for(consumer, timeout=3)
        assert collector.notifications.snapshot()["dropped"] == 1
        assert collector.notifications.snapshot()["pending"] == 0
        assert collector.notifications._worker.done()

    asyncio.run(scenario())
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["notifications"]["last_error"] == "shutdown_timeout"
    assert status["status"] == "completed"


def test_live_start_sends_once_after_initial_checkpoint(tmp_path, monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", WEBHOOK)
    delivered = []

    async def delivery(self, session, text):
        assert (tmp_path / "signals.duckdb").exists()
        delivered.append(text)
        self.sent += 1

    monkeypatch.setattr(SlackNotifications, "_deliver", delivery)

    async def scenario():
        collector = Collector(Settings())
        await collector.emit("clock")
        await collector.queue.put({"kind": "stop"})
        await collector.consume(tmp_path)

    asyncio.run(scenario())
    assert len(delivered) == 1
    assert "Collector started" in delivered[0] and "Warm-up pending" in delivered[0]
    status = json.loads((tmp_path / "status.json").read_text())
    assert status["notifications"]["sent"] == 1
