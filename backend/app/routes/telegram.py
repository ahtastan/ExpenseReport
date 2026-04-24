from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlmodel import Session

from app.config import get_settings
from app.db import get_session
from app.schemas import TelegramWebhookResult
from app.services.telegram import handle_update

router = APIRouter()


@router.get("/status")
def telegram_status():
    settings = get_settings()
    return {
        "configured": bool(settings.telegram_bot_token),
        "webhook_secret_required": bool(settings.telegram_webhook_secret),
        "allowlist_enabled": bool(settings.allowed_telegram_user_ids),
    }


@router.post("/webhook", response_model=TelegramWebhookResult)
async def telegram_webhook(
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
    session: Session = Depends(get_session),
):
    settings = get_settings()
    if not settings.telegram_webhook_secret:
        raise HTTPException(status_code=503, detail="Telegram webhook is unavailable")
    if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid Telegram webhook secret")
    payload = await request.json()
    return handle_update(session, payload)
