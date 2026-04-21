import os
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    app_name: str = "Expense Reporting App"
    database_url: str
    storage_root: Path
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    allowed_telegram_user_ids: set[int] = Field(default_factory=set)
    report_template_path: Path | None = None


def _default_storage_root() -> Path:
    return Path(__file__).resolve().parents[1] / "data"


def _default_report_template_path() -> Path | None:
    candidate = Path(__file__).resolve().parents[3] / "Expense Report Form_Blank.xlsx"
    return candidate if candidate.exists() else None


def _parse_user_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    ids: set[int] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        ids.add(int(item))
    return ids


@lru_cache
def get_settings() -> Settings:
    storage_root = Path(os.getenv("EXPENSE_STORAGE_ROOT") or _default_storage_root())
    database_url = os.getenv("DATABASE_URL") or f"sqlite:///{storage_root / 'expense_app.db'}"
    return Settings(
        database_url=database_url,
        storage_root=storage_root,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=os.getenv("TELEGRAM_WEBHOOK_SECRET"),
        allowed_telegram_user_ids=_parse_user_ids(os.getenv("ALLOWED_TELEGRAM_USER_IDS")),
        report_template_path=Path(os.environ["EXPENSE_REPORT_TEMPLATE_PATH"])
        if os.getenv("EXPENSE_REPORT_TEMPLATE_PATH")
        else _default_report_template_path(),
    )
