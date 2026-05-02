from __future__ import annotations

import base64
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from app.json_utils import dumps
from app.models import ReceiptDocument

_DEFAULT_MODEL = "gpt-5.4"
_MAX_COMPLETION_TOKENS = 2048
_IMAGE_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


class LiveAgentReceiptProviderError(ValueError):
    """Safe operator-facing live provider error."""


class LiveAgentReceiptMalformedResponse(LiveAgentReceiptProviderError):
    """Raised when the model returns text that cannot become an agent read."""

    def __init__(
        self,
        raw_response_json: str,
        *,
        prompt_text: str,
        model_name: str,
        message: str,
    ) -> None:
        super().__init__(message)
        self.raw_response_json = raw_response_json
        self.prompt_text = prompt_text
        self.model_name = model_name


@dataclass(frozen=True)
class LiveAgentReceiptReviewResult:
    agent_payload: dict[str, Any]
    raw_response_json: str
    prompt_text: str
    model_name: str


def ensure_live_provider_configured() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        raise LiveAgentReceiptProviderError(
            "OPENAI_API_KEY is not configured; refusing live shadow receipt review."
        )


def live_agent_receipt_model_name() -> str:
    return (
        os.getenv("AGENT_RECEIPT_REVIEW_MODEL")
        or os.getenv("OCR_VISION_MODEL")
        or _DEFAULT_MODEL
    )


def call_live_model_with_image(
    *,
    receipt: ReceiptDocument,
    prompt_text: str,
    model_name: str | None = None,
) -> str:
    """Generic image+text OpenAI call shared by the second-read and
    F-AI-Stage1 inline-keyboard run kinds. Returns the raw text response.

    Callers build whatever prompt they need and pass the result; no
    prompt-shape knowledge is baked in here. The live-provider config
    check still applies.
    """
    ensure_live_provider_configured()
    return _call_openai_live_receipt_review(
        receipt=receipt,
        prompt_text=prompt_text,
        model_name=model_name or live_agent_receipt_model_name(),
    )


def call_live_agent_receipt_review(
    *,
    receipt: ReceiptDocument,
    canonical: Mapping[str, Any],
    statement_context: Mapping[str, Any] | None = None,
) -> LiveAgentReceiptReviewResult:
    """Call OpenAI for one operator-selected shadow receipt second-read.

    This provider is intentionally separate from OCR routing. It does not
    update ReceiptDocument, matching, reports, Telegram, or OCR model policy.
    """

    ensure_live_provider_configured()
    model_name = live_agent_receipt_model_name()
    prompt_text = build_live_agent_receipt_prompt(
        canonical=canonical,
        statement_context=statement_context,
    )
    raw_response = _call_openai_live_receipt_review(
        receipt=receipt,
        prompt_text=prompt_text,
        model_name=model_name,
    )
    try:
        agent_payload = agent_payload_from_live_response(raw_response)
    except LiveAgentReceiptMalformedResponse as exc:
        raise LiveAgentReceiptMalformedResponse(
            raw_response,
            prompt_text=prompt_text,
            model_name=model_name,
            message=str(exc),
        ) from exc
    return LiveAgentReceiptReviewResult(
        agent_payload=agent_payload,
        raw_response_json=raw_response,
        prompt_text=prompt_text,
        model_name=model_name,
    )


def build_live_agent_receipt_prompt(
    *,
    canonical: Mapping[str, Any],
    statement_context: Mapping[str, Any] | None,
) -> str:
    statement_json = dumps(dict(statement_context or {}), indent=2, sort_keys=True, default=str)
    canonical_json = dumps(dict(canonical), indent=2, sort_keys=True, default=str)
    return f"""You are an advisory shadow receipt second-reader.

Read only the attached receipt image. Return JSON only. Do not approve an
expense, do not match a receipt to a statement, do not generate reports, and
do not overwrite application data.

Canonical OCR fields are context only. Statement context is context only. If
the visible receipt conflicts with context, return what is visible on the
receipt and explain briefly in notes. If a value is not visible, return null.

Canonical OCR context:
{canonical_json}

Statement row context:
{statement_json}

Set business_context_needed to true only when the receipt is visibly for a
meal, restaurant, cafe, or customer entertainment expense where project or
attendee clarification is useful. Set it to false for fuel/gasoline/petrol,
market/grocery/supermarket, toll, parking, transport, travel, or unknown
receipt types. Use business_context_category as one of: meal, restaurant,
cafe, entertainment, fuel, market, grocery, toll, parking, transport, travel,
other, unknown.

Return exactly this JSON shape, with no markdown or prose outside JSON:
{{
  "date": "YYYY-MM-DD or null",
  "amount": "decimal string or null",
  "currency": "ISO code or null",
  "supplier": "string or null",
  "business_reason": "string or null",
  "attendees": "string or null",
  "business_context_needed": true or false,
  "business_context_category": "meal/restaurant/cafe/entertainment/fuel/market/grocery/toll/parking/transport/travel/other/unknown",
  "business_context_reason": "short reason",
  "notes": "short explanation"
}}
"""


def agent_payload_from_live_response(raw_response: str) -> dict[str, Any]:
    parsed = _extract_json_object(raw_response)
    if "agent_read" in parsed and isinstance(parsed["agent_read"], dict):
        parsed = parsed["agent_read"]

    date_value = parsed.get("date") if "date" in parsed else parsed.get("receipt_date")
    amount_value = parsed.get("amount") if "amount" in parsed else parsed.get("total_amount")
    currency_value = parsed.get("currency")
    supplier_value = parsed.get("supplier") if "supplier" in parsed else parsed.get("merchant_name")
    notes_value = parsed.get("notes") if "notes" in parsed else parsed.get("raw_text_summary")

    return {
        "merchant_name": _optional_string(supplier_value),
        "merchant_address": None,
        "receipt_date": _optional_string(date_value),
        "receipt_time": None,
        "total_amount": _optional_string(amount_value),
        "currency": _optional_string(currency_value),
        "amount_text": _optional_string(amount_value),
        "line_items": [],
        "tax_amount": None,
        "payment_method": None,
        "receipt_category": None,
        "confidence": None,
        "raw_text_summary": _optional_string(notes_value),
        "business_context_needed": _optional_bool(parsed.get("business_context_needed")),
        "business_context_category": _optional_string(parsed.get("business_context_category")),
        "business_context_reason": _optional_string(parsed.get("business_context_reason")),
    }


def _call_openai_live_receipt_review(
    *,
    receipt: ReceiptDocument,
    prompt_text: str,
    model_name: str,
) -> str:
    image = _receipt_image_content(receipt)
    try:
        from openai import OpenAI  # deferred optional dependency
    except Exception as exc:  # pragma: no cover - depends on environment
        raise LiveAgentReceiptProviderError("openai package is not available") from exc

    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt_text}]
    content.append(
        {
            "type": "image_url",
            "image_url": {"url": f"data:{image[0]};base64,{image[1]}"},
        }
    )
    try:
        response = client.chat.completions.create(
            model=model_name,
            max_completion_tokens=_MAX_COMPLETION_TOKENS,
            messages=[{"role": "user", "content": content}],
        )
    except Exception as exc:  # pragma: no cover - depends on live API
        raise LiveAgentReceiptProviderError(f"OpenAI live shadow review failed: {exc}") from exc
    return response.choices[0].message.content or ""


def _receipt_image_content(receipt: ReceiptDocument) -> tuple[str, str]:
    if not receipt.storage_path:
        raise LiveAgentReceiptProviderError("receipt has no storage_path for live image review")
    path = Path(receipt.storage_path)
    if not path.exists() or not path.is_file():
        raise LiveAgentReceiptProviderError("receipt storage file is not readable for live image review")
    media_type = _IMAGE_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise LiveAgentReceiptProviderError(
            f"unsupported receipt image extension for live review: {path.suffix}"
        )
    return media_type, base64.b64encode(path.read_bytes()).decode("ascii")


def _extract_json_object(raw_response: str) -> dict[str, Any]:
    text = (raw_response or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise LiveAgentReceiptMalformedResponse(
                raw_response,
                prompt_text="",
                model_name="",
                message=f"model response was not valid JSON: {exc}",
            ) from exc
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as inner_exc:
            raise LiveAgentReceiptMalformedResponse(
                raw_response,
                prompt_text="",
                model_name="",
                message=f"model response was not valid JSON: {inner_exc}",
            ) from inner_exc
    if not isinstance(parsed, dict):
        raise LiveAgentReceiptMalformedResponse(
            raw_response,
            prompt_text="",
            model_name="",
            message="model response JSON must be an object",
        )
    return parsed


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None
