from urllib.error import HTTPError

from beta_lag import probe


def test_missing_credentials_do_not_send_requests(monkeypatch):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("COINGLASS_API_KEY", raising=False)
    assert probe.alpaca_check()["error"] == "missing_credentials"
    assert probe.coinglass_current_check() == {"ok": False, "error": "missing_credentials"}


def test_coinglass_denial_is_sanitized_without_history_fallback(monkeypatch):
    monkeypatch.setenv("COINGLASS_API_KEY", "test-secret")
    calls = []

    def request(url, *args):
        calls.append(url)
        return {"ok": True, "payload": {"code": "403", "msg": "echoed test-secret"}}

    monkeypatch.setattr(probe, "request_json", request)
    result = probe.coinglass_current_check()
    assert not result["ok"]
    assert len(calls) == 1
    assert "history" not in calls[0]
    assert "test-secret" not in str(result)


def test_empty_market_data_is_not_reported_as_success(monkeypatch):
    monkeypatch.setenv("COINGLASS_API_KEY", "test-secret")
    monkeypatch.setattr(
        probe, "request_json", lambda *a, **kw: {"ok": True, "payload": {"code": "0", "data": []}}
    )
    assert not probe.coinglass_current_check()["ok"]


def test_http_error_never_exposes_provider_body_or_secret(monkeypatch):
    class FailingOpener:
        def open(self, *args, **kwargs):
            raise HTTPError("https://example.invalid", 403, "echoed test-secret", {}, None)

    monkeypatch.setattr(probe, "build_opener", lambda *args: FailingOpener())
    result = probe.request_json("https://example.invalid", {"secret": "test-secret"})
    assert result == {"ok": False, "error": "http_error", "http_status": 403}
