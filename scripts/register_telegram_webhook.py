"""Register or inspect the Telegram webhook for the expense bot.

The script prints sanitized JSON only. It never includes the bot token or
webhook secret in output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = REPO_ROOT.parent
WEBHOOK_PATH = "/telegram/webhook"


Urlopen = Callable[[urllib.request.Request, int], Any]


def _load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def _load_local_env() -> None:
    for env_path in (REPO_ROOT / ".env", REPO_ROOT / "backend" / ".env", WORKSPACE_ROOT / ".env"):
        _load_env_file(env_path)


def normalize_webhook_url(raw_url: str) -> str:
    url = raw_url.strip()
    if not url:
        raise ValueError("Webhook URL is required")
    if not url.startswith(("https://", "http://")):
        raise ValueError("Webhook URL must start with https:// or http://")
    parsed = urllib.parse.urlparse(url)
    if parsed.path in ("", "/"):
        return url.rstrip("/") + WEBHOOK_PATH
    return url.rstrip("/")


def _api_call(token: str, method: str, payload: dict[str, Any] | None = None, urlopen: Urlopen = urllib.request.urlopen) -> dict[str, Any]:
    data = urllib.parse.urlencode(payload or {}).encode("utf-8")
    request = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}", data=data)
    with urlopen(request, timeout=15) as response:
        value = json.loads(response.read().decode("utf-8"))
    return value if isinstance(value, dict) else {"ok": False, "description": "Unexpected Telegram response"}


def _sanitize_webhook_info(response: dict[str, Any]) -> dict[str, Any]:
    result = response.get("result") if isinstance(response.get("result"), dict) else {}
    return {
        "ok": bool(response.get("ok")),
        "url": result.get("url"),
        "pending_update_count": result.get("pending_update_count", 0),
        "last_error_date": result.get("last_error_date"),
        "last_error_message": result.get("last_error_message"),
    }


def get_webhook_status(token: str, urlopen: Urlopen = urllib.request.urlopen) -> dict[str, Any]:
    info = _api_call(token, "getWebhookInfo", urlopen=urlopen)
    return {
        "status": "status",
        "telegram": _sanitize_webhook_info(info),
    }


def register_webhook(
    token: str,
    webhook_url: str,
    secret_token: str | None = None,
    urlopen: Urlopen = urllib.request.urlopen,
) -> dict[str, Any]:
    normalized_url = normalize_webhook_url(webhook_url)
    payload: dict[str, Any] = {
        "url": normalized_url,
        "drop_pending_updates": "false",
    }
    if secret_token:
        payload["secret_token"] = secret_token
    set_response = _api_call(token, "setWebhook", payload=payload, urlopen=urlopen)
    status = get_webhook_status(token, urlopen=urlopen)["telegram"]
    return {
        "status": "configured" if set_response.get("ok") else "failed",
        "webhook_url": normalized_url,
        "secret_token_configured": bool(secret_token),
        "telegram": status,
        "telegram_set_ok": bool(set_response.get("ok")),
        "telegram_description": set_response.get("description"),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Register or inspect the Telegram webhook.")
    parser.add_argument("--url", help="Public base URL or full /telegram/webhook URL for this app.")
    parser.add_argument("--status", action="store_true", help="Only print sanitized Telegram webhook status.")
    return parser


def main(argv: list[str] | None = None) -> int:
    _load_local_env()
    args = _parser().parse_args(argv)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print(json.dumps({"status": "failed", "reason": "TELEGRAM_BOT_TOKEN missing"}))
        return 2
    try:
        if args.status:
            print(json.dumps(get_webhook_status(token), ensure_ascii=False))
            return 0
        webhook_url = args.url or os.getenv("TELEGRAM_WEBHOOK_URL")
        if not webhook_url:
            print(json.dumps({"status": "failed", "reason": "Webhook URL missing; pass --url or set TELEGRAM_WEBHOOK_URL"}))
            return 2
        result = register_webhook(
            token=token,
            webhook_url=webhook_url,
            secret_token=os.getenv("TELEGRAM_WEBHOOK_SECRET"),
        )
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] == "configured" else 1
    except Exception as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
