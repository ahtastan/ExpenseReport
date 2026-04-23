"""Model routing policy for OCR, matching, and report synthesis.

Policy (user-defined):
  - default OCR model      = OCR_MINI_MODEL  (cheap, high throughput)
  - escalation OCR model   = OCR_FULL_MODEL  (hard cases / final review)
  - report synthesis model = OCR_FULL_MODEL  (stronger reasoning)
  - chat + matching model  = OCR_MINI_MODEL  (routine orchestration)

Staged OCR pipeline (implemented in ``vision_extract``):
  1. caller runs deterministic parsing first (regex over caption/filename);
  2. if critical fields are still missing, caller invokes ``vision_extract``;
  3. ``vision_extract`` tries the mini model first;
  4. if the mini result is invalid or still missing critical fields, it
     escalates once to the full model.

The real model identifiers are env-driven so non-production environments
can point at fakes/stubs without code changes.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Defaults match the policy stated by the user.  Override per-env.
MINI_MODEL = os.getenv("OCR_MINI_MODEL", "gpt-5.4-mini")
FULL_MODEL = os.getenv("OCR_FULL_MODEL", "gpt-5.4")
CHAT_MODEL = os.getenv("CHAT_MODEL", MINI_MODEL)
SYNTHESIS_MODEL = os.getenv("SYNTHESIS_MODEL", FULL_MODEL)
MATCHING_MODEL = os.getenv("MATCHING_MODEL", MINI_MODEL)

CRITICAL_FIELDS = ("date", "supplier", "amount")

_VISION_PROMPT = (
    "You are an expense receipt parser. Extract the following fields from the "
    "receipt image and return ONLY a JSON object with exactly these keys:\n"
    "  date (ISO 8601 string YYYY-MM-DD or null),\n"
    "  supplier (string or null),\n"
    "  amount (number or null),\n"
    "  currency (3-letter ISO code string or null),\n"
    "  business_or_personal (\"Business\" or \"Personal\" or null).\n"
    "Return only the JSON object, no other text."
)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}


@dataclass(frozen=True)
class VisionResult:
    """Outcome of a staged vision call."""

    fields: dict[str, Any]
    model: str  # which tier actually produced the fields
    escalated: bool  # true if the full model was used after the mini attempt
    notes: list[str]


@dataclass(frozen=True)
class MatchDisambiguation:
    """Outcome of a matching-model disambiguation call."""

    transaction_id: int | None  # chosen candidate id, or None if model abstained
    confidence: str  # "high" | "medium" | "low" as judged by the model
    reasoning: str  # short natural-language rationale (for audit trail)
    model: str


def _count_missing(fields: dict[str, Any]) -> list[str]:
    return [key for key in CRITICAL_FIELDS if not fields.get(key)]


def _extract_json(text: str) -> dict[str, Any] | None:
    """Parse a JSON object from a model response, tolerating code fences."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```$", "", cleaned)
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _read_image_b64(path: Path) -> tuple[str, str] | None:
    if not path.exists() or path.suffix.lower() not in _IMAGE_EXTENSIONS:
        return None
    media = _MEDIA_TYPES.get(path.suffix.lower(), "image/jpeg")
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return media, data


def _call_openai(model: str, media_type: str, b64: str) -> dict[str, Any] | None:
    """Invoke the OpenAI chat-completions vision API for a single image.

    Returns ``None`` when the key is unset, the SDK is unavailable, or the
    response cannot be parsed as JSON. Callers handle fallback.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI  # deferred import — optional dependency
    except Exception:
        return None
    try:
        client = OpenAI(api_key=api_key)
        data_url = f"data:{media_type};base64,{b64}"
        response = client.chat.completions.create(
            model=model,
            max_tokens=256,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
        )
        content = response.choices[0].message.content or ""
        return _extract_json(content)
    except Exception as exc:  # pragma: no cover - depends on live API
        logger.warning("OpenAI vision call failed on %s: %s", model, exc)
        return None


# The concrete call is indirected through this module-level attribute so
# tests can monkey-patch a fake without reaching into the OpenAI SDK.
_vision_call = _call_openai


_MATCH_PROMPT = (
    "You are a receipt-to-bank-statement matcher. You will be given a single "
    "receipt and a list of candidate statement transactions. Pick the single "
    "best candidate, or abstain if none is plausible.\n\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    "  transaction_id (integer id from the candidate list, or null to abstain),\n"
    "  confidence (\"high\", \"medium\", or \"low\"),\n"
    "  reasoning (one short sentence explaining the pick).\n"
    "Do not invent a transaction_id that is not in the candidate list."
)


def _call_openai_text(model: str, prompt: str, payload: str) -> dict[str, Any] | None:
    """Invoke a text-only OpenAI chat completion and parse a JSON response."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI  # deferred import
    except Exception:
        return None
    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            max_tokens=256,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": payload},
            ],
        )
        content = response.choices[0].message.content or ""
        return _extract_json(content)
    except Exception as exc:  # pragma: no cover - depends on live API
        logger.warning("OpenAI text call failed on %s: %s", model, exc)
        return None


# Indirect text calls the same way vision calls are indirected so tests can
# substitute a recorder without touching the OpenAI SDK.
_text_call = _call_openai_text


def match_disambiguate(
    receipt: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> MatchDisambiguation | None:
    """Ask the matching model to pick the best candidate transaction.

    ``receipt`` and each ``candidates`` entry should be a small dict of the
    fields relevant to matching (supplier, date, amount, currency, and a
    transaction id on each candidate). The function validates that the chosen
    ``transaction_id`` is actually among the candidates and returns ``None``
    for any invalid or unparseable response.
    """
    if not candidates:
        return None

    candidate_ids = {
        candidate.get("transaction_id")
        for candidate in candidates
        if isinstance(candidate.get("transaction_id"), int)
    }
    if not candidate_ids:
        return None

    payload = json.dumps(
        {"receipt": receipt, "candidates": candidates},
        ensure_ascii=False,
        sort_keys=True,
    )
    result = _text_call(MATCHING_MODEL, _MATCH_PROMPT, payload)
    if not isinstance(result, dict):
        return None

    raw_tx = result.get("transaction_id")
    chosen: int | None
    if raw_tx is None:
        chosen = None
    elif isinstance(raw_tx, int) and raw_tx in candidate_ids:
        chosen = raw_tx
    else:
        # Model hallucinated an id that was not offered; treat as abstain.
        return MatchDisambiguation(
            transaction_id=None,
            confidence="low",
            reasoning="model returned an id that was not in the candidate list",
            model=MATCHING_MODEL,
        )

    confidence = str(result.get("confidence") or "low").lower()
    if confidence not in {"high", "medium", "low"}:
        confidence = "low"
    reasoning = str(result.get("reasoning") or "")[:300]
    return MatchDisambiguation(
        transaction_id=chosen,
        confidence=confidence,
        reasoning=reasoning,
        model=MATCHING_MODEL,
    )


def vision_extract(storage_path: str) -> VisionResult | None:
    """Run the staged vision pipeline (mini → full) for one image.

    Returns ``None`` when the file is unsupported or no model responded.
    """
    encoded = _read_image_b64(Path(storage_path))
    if encoded is None:
        return None
    media_type, b64 = encoded

    notes: list[str] = []
    mini_fields = _vision_call(MINI_MODEL, media_type, b64)
    if mini_fields is not None:
        missing = _count_missing(mini_fields)
        if not missing:
            notes.append(f"Vision extraction succeeded on mini model ({MINI_MODEL}).")
            return VisionResult(fields=mini_fields, model=MINI_MODEL, escalated=False, notes=notes)
        notes.append(
            f"Mini model ({MINI_MODEL}) returned missing critical fields {missing}; escalating."
        )
    else:
        notes.append(f"Mini model ({MINI_MODEL}) unavailable or invalid; escalating.")

    full_fields = _vision_call(FULL_MODEL, media_type, b64)
    if full_fields is not None:
        notes.append(f"Vision extraction escalated to full model ({FULL_MODEL}).")
        return VisionResult(fields=full_fields, model=FULL_MODEL, escalated=True, notes=notes)

    # Both tiers failed but the mini attempt produced *something* — prefer
    # returning partial data over nothing so deterministic fields still merge.
    if mini_fields is not None:
        notes.append("Full model unavailable; returning partial mini-model fields.")
        return VisionResult(fields=mini_fields, model=MINI_MODEL, escalated=False, notes=notes)

    notes.append("All vision tiers failed.")
    return None
