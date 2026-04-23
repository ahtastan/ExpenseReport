"""Tests for Telegram webhook registration helper."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import register_telegram_webhook  # noqa: E402


class _FakeResponse:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class _FakeUrlopen:
    def __init__(self):
        self.calls: list[tuple[str, dict[str, list[str]]]] = []

    def __call__(self, request, data=None, timeout: int | None = None):
        assert data is None
        assert timeout == 15
        data = parse_qs((request.data or b"").decode("utf-8"))
        self.calls.append((request.full_url, data))
        if request.full_url.endswith("/setWebhook"):
            return _FakeResponse({"ok": True, "result": True, "description": "Webhook was set"})
        if request.full_url.endswith("/getWebhookInfo"):
            return _FakeResponse(
                {
                    "ok": True,
                    "result": {
                        "url": "https://example.ngrok-free.app/telegram/webhook",
                        "pending_update_count": 0,
                    },
                }
            )
        raise AssertionError(f"Unexpected Telegram method URL: {request.full_url}")


def main() -> None:
    fake_urlopen = _FakeUrlopen()
    token = "123456:secret-token-value"
    secret = "webhook-secret"
    result = register_telegram_webhook.register_webhook(
        token=token,
        webhook_url="https://example.ngrok-free.app",
        secret_token=secret,
        urlopen=fake_urlopen,
    )

    assert result["status"] == "configured"
    assert result["webhook_url"] == "https://example.ngrok-free.app/telegram/webhook"
    assert result["secret_token_configured"] is True
    assert result["telegram"]["ok"] is True
    assert result["telegram"]["url"] == "https://example.ngrok-free.app/telegram/webhook"
    serialized = json.dumps(result)
    assert token not in serialized
    assert secret not in serialized

    set_call = fake_urlopen.calls[0]
    assert set_call[0] == f"https://api.telegram.org/bot{token}/setWebhook"
    assert set_call[1]["url"] == ["https://example.ngrok-free.app/telegram/webhook"]
    assert set_call[1]["secret_token"] == [secret]
    assert fake_urlopen.calls[1][0] == f"https://api.telegram.org/bot{token}/getWebhookInfo"

    status = register_telegram_webhook.get_webhook_status(token=token, urlopen=fake_urlopen)
    assert status["status"] == "status"
    assert token not in json.dumps(status)
    print("telegram_webhook_registration_tests=passed")


if __name__ == "__main__":
    main()
