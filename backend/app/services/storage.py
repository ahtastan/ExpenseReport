import shutil
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi import UploadFile

from app.config import get_settings


def storage_root() -> Path:
    root = get_settings().storage_root
    root.mkdir(parents=True, exist_ok=True)
    return root


def make_storage_path(kind: str, user_id: int | None, filename: str | None) -> Path:
    suffix = Path(filename or "").suffix
    safe_name = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_{uuid4().hex}{suffix}"
    user_segment = f"user_{user_id}" if user_id else "unassigned"
    path = storage_root() / user_segment / kind / safe_name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def save_upload_file(upload: UploadFile, kind: str, user_id: int | None = None) -> Path:
    path = make_storage_path(kind, user_id, upload.filename)
    with path.open("wb") as out:
        while chunk := await upload.read(1024 * 1024):
            out.write(chunk)
    return path


def save_bytes(content: bytes, kind: str, user_id: int | None, filename: str | None) -> Path:
    path = make_storage_path(kind, user_id, filename)
    path.write_bytes(content)
    return path


def copy_file_to_storage(src: Path, kind: str, user_id: int | None = None) -> Path:
    path = make_storage_path(kind, user_id, src.name)
    shutil.copy2(src, path)
    return path
