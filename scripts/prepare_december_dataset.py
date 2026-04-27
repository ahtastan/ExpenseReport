from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib import error, request


DEFAULT_BASE_URL = "https://app.dcexpense.com"
DEFAULT_STATEMENT_PATH = Path(
    r"C:\Users\CASPER\OneDrive - Enzymatic Deinking Technologies LLC"
    r"\Masaüstü\Expense\2025\11_11_Receipts\Statement.xlsx"
)


def _request_json(
    method: str,
    url: str,
    *,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    req = request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with request.urlopen(req, timeout=300) as response:
            payload = response.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: HTTP {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

    return json.loads(payload) if payload else {}


def _auth_header(basic_auth: str | None) -> dict[str, str]:
    if not basic_auth:
        return {}
    token = base64.b64encode(basic_auth.encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {token}"}


def post_file(
    url: str,
    file_path: Path,
    *,
    basic_auth: str | None = None,
    field_name: str = "file",
) -> dict[str, Any]:
    boundary = f"----expense-december-{uuid.uuid4().hex}"
    mime_type = (
        mimetypes.guess_type(file_path.name)[0]
        or "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    file_bytes = file_path.read_bytes()
    body = b"".join(
        [
            f"--{boundary}\r\n".encode("utf-8"),
            (
                f'Content-Disposition: form-data; name="{field_name}"; '
                f'filename="{file_path.name}"\r\n'
            ).encode("utf-8"),
            f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8"),
            file_bytes,
            f"\r\n--{boundary}--\r\n".encode("utf-8"),
        ]
    )
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
        **_auth_header(basic_auth),
    }
    return _request_json("POST", url, body=body, headers=headers)


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    basic_auth: str | None = None,
) -> dict[str, Any]:
    return _request_json(
        "POST",
        url,
        body=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **_auth_header(basic_auth)},
    )


def prepare_december_dataset(
    statement_path: Path,
    *,
    base_url: str,
    basic_auth: str | None = None,
) -> dict[str, Any]:
    if not statement_path.exists():
        raise FileNotFoundError(f"Statement XLSX not found: {statement_path}")

    base = base_url.rstrip("/")
    imported = post_file(
        f"{base}/statements/import-excel",
        statement_path,
        basic_auth=basic_auth,
    )
    statement_import_id = imported["id"]
    matching = post_json(
        f"{base}/matching/run",
        {"statement_import_id": statement_import_id, "auto_approve_high_confidence": True},
        basic_auth=basic_auth,
    )
    review = post_json(
        f"{base}/reviews/report/{statement_import_id}/build",
        {},
        basic_auth=basic_auth,
    )

    return {
        "statement_import_id": statement_import_id,
        "review_session_id": review["id"],
        "row_count": imported.get("row_count"),
        "period_start": imported.get("period_start"),
        "period_end": imported.get("period_end"),
        "matching": matching,
        "review_rows": len(review.get("rows", [])),
        "review_url": f"{base}/review?statement_import_id={statement_import_id}",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Upload the already-exported December 2025 Statement.xlsx to "
            "/statements/import-excel, run matching, and build the review session."
        )
    )
    parser.add_argument(
        "statement_xlsx",
        nargs="?",
        type=Path,
        default=DEFAULT_STATEMENT_PATH,
        help=f"Path to Statement.xlsx. Default: {DEFAULT_STATEMENT_PATH}",
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Production app base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--basic-auth",
        default=None,
        help="Optional Basic auth credentials in user:password form.",
    )
    args = parser.parse_args(argv)

    result = prepare_december_dataset(
        args.statement_xlsx,
        base_url=args.base_url,
        basic_auth=args.basic_auth,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
