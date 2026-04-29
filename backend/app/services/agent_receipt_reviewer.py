from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping

RiskLevel = Literal["pass", "warn", "block"]
RecommendedAction = Literal["accept", "ask_user", "manual_review", "block_report"]

_AMOUNT_TOLERANCE = Decimal("0.01")
_LEGAL_SUPPLIER_SUFFIXES = {
    "as",
    "co",
    "corp",
    "corporation",
    "inc",
    "ltd",
    "limited",
    "llc",
    "plc",
    "sa",
}


@dataclass(frozen=True)
class AgentReceiptRead:
    merchant_name: str | None = None
    merchant_address: str | None = None
    receipt_date: date | None = None
    receipt_time: str | None = None
    total_amount: Decimal | None = None
    currency: str | None = None
    amount_text: str | None = None
    line_items: list[dict[str, Any]] = field(default_factory=list)
    tax_amount: Decimal | None = None
    payment_method: str | None = None
    receipt_category: str | None = None
    confidence: float | None = None
    raw_text_summary: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentReceiptRead":
        return cls(
            merchant_name=_clean_optional_string(payload.get("merchant_name")),
            merchant_address=_clean_optional_string(payload.get("merchant_address")),
            receipt_date=_coerce_date(payload.get("receipt_date")),
            receipt_time=_clean_optional_string(payload.get("receipt_time")),
            total_amount=_coerce_decimal(payload.get("total_amount")),
            currency=_normalize_currency(payload.get("currency")),
            amount_text=_clean_optional_string(payload.get("amount_text")),
            line_items=_coerce_line_items(payload.get("line_items")),
            tax_amount=_coerce_decimal(payload.get("tax_amount")),
            payment_method=_clean_optional_string(payload.get("payment_method")),
            receipt_category=_clean_optional_string(payload.get("receipt_category")),
            confidence=_coerce_float(payload.get("confidence")),
            raw_text_summary=_clean_optional_string(payload.get("raw_text_summary")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "merchant_name": self.merchant_name,
            "merchant_address": self.merchant_address,
            "receipt_date": self.receipt_date.isoformat() if self.receipt_date else None,
            "receipt_time": self.receipt_time,
            "total_amount": str(self.total_amount) if self.total_amount is not None else None,
            "currency": self.currency,
            "amount_text": self.amount_text,
            "line_items": self.line_items,
            "tax_amount": str(self.tax_amount) if self.tax_amount is not None else None,
            "payment_method": self.payment_method,
            "receipt_category": self.receipt_category,
            "confidence": self.confidence,
            "raw_text_summary": self.raw_text_summary,
        }


@dataclass(frozen=True)
class AgentReceiptComparison:
    amount_match: bool
    date_match: bool
    currency_match: bool
    supplier_match: bool
    risk_level: RiskLevel
    differences: list[str] = field(default_factory=list)
    recommended_action: RecommendedAction = "accept"
    suggested_user_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount_match": self.amount_match,
            "date_match": self.date_match,
            "currency_match": self.currency_match,
            "supplier_match": self.supplier_match,
            "risk_level": self.risk_level,
            "differences": list(self.differences),
            "recommended_action": self.recommended_action,
            "suggested_user_message": self.suggested_user_message,
        }


@dataclass(frozen=True)
class AgentReceiptReviewResult:
    canonical_fields: dict[str, Any]
    agent_read: AgentReceiptRead
    comparison: AgentReceiptComparison
    schema_version: str = "0a"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "canonical_fields": _jsonable(self.canonical_fields),
            "agent_read": self.agent_read.to_dict(),
            "comparison": self.comparison.to_dict(),
        }


def compare_agent_receipt_read(
    canonical_fields: Mapping[str, Any],
    agent_read: AgentReceiptRead | Mapping[str, Any],
    *,
    date_tolerance_days: int = 1,
) -> AgentReceiptReviewResult:
    read = agent_read if isinstance(agent_read, AgentReceiptRead) else AgentReceiptRead.from_dict(agent_read)
    canonical = dict(canonical_fields)
    differences: list[str] = []
    block_reasons: list[str] = []
    warn_reasons: list[str] = []

    amount_match = _compare_amount(canonical, read, differences, block_reasons)
    currency_match = _compare_currency(canonical, read, differences, block_reasons)
    date_match = _compare_date(canonical, read, date_tolerance_days, differences, warn_reasons)
    supplier_match = _compare_supplier(canonical, read, differences, warn_reasons)

    business_context_missing = _collect_business_context_differences(canonical, differences, warn_reasons)
    if block_reasons:
        risk_level: RiskLevel = "block"
        recommended_action: RecommendedAction = "block_report"
    elif warn_reasons:
        risk_level = "warn"
        recommended_action = "ask_user" if business_context_missing else "manual_review"
    else:
        risk_level = "pass"
        recommended_action = "accept"

    comparison = AgentReceiptComparison(
        amount_match=amount_match,
        date_match=date_match,
        currency_match=currency_match,
        supplier_match=supplier_match,
        risk_level=risk_level,
        differences=differences,
        recommended_action=recommended_action,
        suggested_user_message=_suggested_user_message(risk_level, recommended_action, differences),
    )
    return AgentReceiptReviewResult(canonical_fields=canonical, agent_read=read, comparison=comparison)


def build_agent_receipt_review_prompt(canonical_fields: Mapping[str, Any]) -> str:
    canonical_json = json.dumps(_jsonable(dict(canonical_fields)), indent=2, sort_keys=True)
    return f"""You are a shadow AI receipt reviewer for a non-production expense reporting prototype.

Independently read the full visible receipt and extract the full receipt context. Return only what is visible:
- merchant_name
- merchant_address
- receipt_date
- receipt_time
- total_amount
- currency
- amount_text
- line_items
- tax_amount
- payment_method
- receipt_category
- confidence
- raw_text_summary

Preserve raw visible evidence, especially the exact visible amount text in amount_text.
Do not guess, infer, or fill fields from memory. If a value is not visible, return null for that field.
Return strict JSON only, with no markdown.

Canonical OCR fields are provided only as context for the application pipeline. The model is not final authority.
The model must not approve, match, report, or overwrite canonical DB values.
Deterministic app code will compare the agent read against canonical OCR fields after this extraction step:

{canonical_json}

Strict JSON shape:
{{
  "agent_read": {{
    "merchant_name": null,
    "merchant_address": null,
    "receipt_date": null,
    "receipt_time": null,
    "total_amount": null,
    "currency": null,
    "amount_text": null,
    "line_items": [],
    "tax_amount": null,
    "payment_method": null,
    "receipt_category": null,
    "confidence": null,
    "raw_text_summary": null
  }}
}}
"""


def _compare_amount(
    canonical: Mapping[str, Any],
    read: AgentReceiptRead,
    differences: list[str],
    block_reasons: list[str],
) -> bool:
    canonical_amount = _coerce_decimal(canonical.get("amount"))
    if canonical_amount is None:
        differences.append("missing_canonical_amount")
        block_reasons.append("missing_canonical_amount")
        return False
    if read.total_amount is None:
        differences.append("missing_agent_amount")
        block_reasons.append("missing_agent_amount")
        return False
    if abs(canonical_amount - read.total_amount) <= _AMOUNT_TOLERANCE:
        return True
    differences.append("amount_mismatch")
    block_reasons.append("amount_mismatch")
    return False


def _compare_currency(
    canonical: Mapping[str, Any],
    read: AgentReceiptRead,
    differences: list[str],
    block_reasons: list[str],
) -> bool:
    canonical_currency = _normalize_currency(canonical.get("currency"))
    if not canonical_currency:
        differences.append("missing_canonical_currency")
        block_reasons.append("missing_canonical_currency")
        return False
    if not read.currency:
        differences.append("missing_agent_currency")
        block_reasons.append("missing_agent_currency")
        return False
    if canonical_currency == read.currency:
        return True
    differences.append("currency_mismatch")
    block_reasons.append("currency_mismatch")
    return False


def _compare_date(
    canonical: Mapping[str, Any],
    read: AgentReceiptRead,
    date_tolerance_days: int,
    differences: list[str],
    warn_reasons: list[str],
) -> bool:
    canonical_date = _coerce_date(canonical.get("date"))
    if canonical_date is None:
        differences.append("missing_canonical_date")
        warn_reasons.append("missing_canonical_date")
        return False
    if read.receipt_date is None:
        differences.append("missing_agent_date")
        warn_reasons.append("missing_agent_date")
        return False
    if abs((canonical_date - read.receipt_date).days) <= max(date_tolerance_days, 0):
        return True
    differences.append("date_mismatch")
    warn_reasons.append("date_mismatch")
    return False


def _compare_supplier(
    canonical: Mapping[str, Any],
    read: AgentReceiptRead,
    differences: list[str],
    warn_reasons: list[str],
) -> bool:
    canonical_supplier = _clean_optional_string(canonical.get("supplier"))
    if not canonical_supplier:
        differences.append("missing_canonical_supplier")
        warn_reasons.append("missing_canonical_supplier")
        return False
    if not read.merchant_name:
        differences.append("missing_agent_supplier")
        warn_reasons.append("missing_agent_supplier")
        return False
    if _supplier_soft_match(canonical_supplier, read.merchant_name):
        return True
    differences.append("supplier_mismatch")
    warn_reasons.append("supplier_mismatch")
    return False


def _collect_business_context_differences(
    canonical: Mapping[str, Any],
    differences: list[str],
    warn_reasons: list[str],
) -> bool:
    if str(canonical.get("business_or_personal") or "").strip().lower() != "business":
        return False

    missing = False
    if not _clean_optional_string(canonical.get("business_reason")):
        differences.append("missing_business_reason")
        warn_reasons.append("missing_business_reason")
        missing = True
    if not _has_attendees(canonical.get("attendees")):
        differences.append("missing_attendees")
        warn_reasons.append("missing_attendees")
        missing = True
    return missing


def _suggested_user_message(
    risk_level: RiskLevel,
    recommended_action: RecommendedAction,
    differences: list[str],
) -> str | None:
    if recommended_action == "accept":
        return None
    if recommended_action == "ask_user" and (
        "missing_business_reason" in differences or "missing_attendees" in differences
    ):
        return "Please add the business reason and attendee names for this business receipt before review continues."
    if risk_level == "block":
        return "The shadow reviewer found a critical amount or currency issue. Please send this receipt to manual review."
    return "The shadow reviewer found non-blocking receipt differences. Please check the receipt details manually."


def _supplier_soft_match(left: str, right: str) -> bool:
    left_norm = _normalize_supplier(left)
    right_norm = _normalize_supplier(right)
    if not left_norm or not right_norm:
        return False
    if left_norm == right_norm:
        return True
    if left_norm in right_norm or right_norm in left_norm:
        return True

    left_tokens = set(left_norm.split())
    right_tokens = set(right_norm.split())
    if not left_tokens or not right_tokens:
        return False
    shared = left_tokens & right_tokens
    return len(shared) / min(len(left_tokens), len(right_tokens)) >= 0.67


def _normalize_supplier(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    tokens = re.findall(r"[a-z0-9]+", ascii_value.lower())
    filtered = [token for token in tokens if token not in _LEGAL_SUPPLIER_SUFFIXES]
    return " ".join(filtered)


def _coerce_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None


def _coerce_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _normalize_currency(value: Any) -> str | None:
    text = _clean_optional_string(value)
    return text.upper() if text else None


def _clean_optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_line_items(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _coerce_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _has_attendees(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, list):
        return any(_clean_optional_string(item) for item in value)
    return bool(_clean_optional_string(value))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    return value
