"""Never deliver real Slack messages from the test suite."""

import pytest


@pytest.fixture(autouse=True)
def disable_slack_webhook(monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
