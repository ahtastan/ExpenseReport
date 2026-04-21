from fastapi import APIRouter

from app.config import get_settings

router = APIRouter()

@router.get('/health')
def health():
    settings = get_settings()
    return {
        "ok": True,
        "storage_root": str(settings.storage_root),
        "telegram_configured": bool(settings.telegram_bot_token),
    }
