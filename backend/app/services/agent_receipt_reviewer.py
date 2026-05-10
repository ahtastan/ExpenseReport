from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, time
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Mapping

RiskLevel = Literal["pass", "warn", "block"]
RecommendedAction = Literal["accept", "ask_user", "manual_review", "block_report"]

# F-AI-Stage1 sub-PR 2: prompt/parser for the inline-keyboard run_kind.
# Existing receipt-second-read constants stay unchanged; the new run_kind
# uses a separate prompt version so persisted runs can be filtered.
INLINE_KEYBOARD_PROMPT_VERSION = "agent_receipt_inline_keyboard_prompt_stage1_v1"
INLINE_KEYBOARD_SCHEMA_VERSION = "stage1"

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


# ─── F-AI-Stage1 sub-PR 2: inline-keyboard run_kind ─────────────────────────
#
# A second prompt-builder + parser, parallel to the receipt-second-read
# pipeline above. The inline-keyboard run_kind asks the model to propose a
# complete classification (business/personal + bucket + attendees + customer
# + business reason + a confidence score) given a per-user context window.
# It does NOT extract OCR fields — those still come from the deterministic
# pipeline plus the existing receipt-second-read run_kind.
#
# The model output is parsed into ``InlineKeyboardSuggestion`` and persisted
# into the new ``suggested_*`` columns on ``agent_receipt_read``.


@dataclass(frozen=True)
class InlineKeyboardSuggestion:
    """Parsed proposal from the inline-keyboard agent.

    All fields default to None so a partial response (per spec — model may
    omit ``customer`` for example) still produces a valid object. Unknown
    keys in the raw response are dropped silently.

    ``receipt_time`` is the printed time of the transaction (HH:MM,
    24-hour) when visible on the receipt — used by the post-validation
    bucket guard to anchor meal-bucket choices on time of day.
    """

    business_or_personal: str | None = None
    report_bucket: str | None = None
    attendees: list[str] | None = None
    customer: str | None = None
    business_reason: str | None = None
    confidence_overall: float | None = None
    receipt_time: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "business_or_personal": self.business_or_personal,
            "report_bucket": self.report_bucket,
            "attendees": list(self.attendees) if self.attendees is not None else None,
            "customer": self.customer,
            "business_reason": self.business_reason,
            "confidence_overall": self.confidence_overall,
            "receipt_time": self.receipt_time,
        }


def inline_keyboard_bucket_vocabulary() -> list[str]:
    """Return the report_bucket values the suggester is allowed to propose.

    Returns the full canonical EDT bucket taxonomy from
    ``app.category_vocab.all_buckets()``. The Edit menu's Tier 2 picker
    already exposes this same set, so AI suggestions and operator picks
    share one vocabulary across surfaces.

    Sub-PR 7 broadens this from the narrower ``merchant_buckets._RULES``
    set (which omitted Lunch / Dinner / Breakfast) so the inline-keyboard
    AI can suggest meal-time-anchored buckets. The deterministic
    supplier-name suggester at ``merchant_buckets.suggest_bucket`` keeps
    its narrower vocabulary — it never had Lunch/Dinner rules to begin
    with.
    """
    from app.category_vocab import all_buckets  # local import: avoid load-order cycle

    return sorted(set(all_buckets()))


def build_inline_keyboard_review_prompt(
    canonical: Mapping[str, Any],
    context_window: Mapping[str, Any] | None,
    *,
    statement_context: Mapping[str, Any] | None = None,
) -> str:
    """Build the AI prompt for ``run_kind='receipt_inline_keyboard'``.

    Asks the model to propose a complete classification (B/P, bucket,
    attendees, customer, business_reason, confidence_overall) using the
    receipt image plus the per-user context window.
    """
    canonical_json = json.dumps(_jsonable(dict(canonical)), indent=2, sort_keys=True)
    context_json = json.dumps(
        _jsonable(dict(context_window or {})),
        indent=2,
        sort_keys=True,
    )
    statement_json = json.dumps(
        _jsonable(dict(statement_context or {})),
        indent=2,
        sort_keys=True,
    )
    bucket_vocab_json = json.dumps(inline_keyboard_bucket_vocabulary())
    return f"""You are a shadow AI receipt classifier for a private, non-production expense reporting prototype.

Read the attached receipt image. Propose a complete classification using the
per-user context window below to ground your answer (employees the user works
with, the user's recent classified receipts, and recent attendees on those
receipts).

Do not approve, match, report, or overwrite application data. Output is
advisory; deterministic application code decides what to persist.

If a classification is genuinely ambiguous, you may return null for the
optional ``customer`` field; ``business_or_personal``, ``report_bucket``,
``attendees``, ``business_reason``, and ``confidence_overall`` should
always be populated. ``receipt_time`` should be the printed transaction
time when visible on the receipt (HH:MM 24-hour) and null otherwise.

Allowed values:
- ``business_or_personal``: "Business" or "Personal"
- ``report_bucket``: one of {bucket_vocab_json}
- ``attendees``: list of strings (use [] for solo / not-applicable)
- ``customer``: string or null
- ``business_reason``: short string
- ``confidence_overall``: float in [0, 1]
- ``receipt_time``: string "HH:MM" 24-hour, or null

Bucket-selection guidance for meal/entertainment receipts. These are
prior anchors, not hard rules — supplier name, line items, and group
size still matter. Apply them when the supplier signal alone is weak:

- Amount bands (TRY): under 500 TL typically Meals/Snacks (coffee,
  bakery, single-person snack); 500–2000 TL typically Lunch; 2000–5000
  TL typically Dinner; over 5000 TL typically Dinner or Entertainment
  (large group / customer dinner).
- Amount bands (USD/EUR/GBP/CAD): under $15 → Snacks; $15–50 → Lunch;
  over $50 → Dinner.
- Time-of-day bias when the receipt prints a time:
    * 11:30–14:30 → Lunch bias.
    * 18:00–22:30 → Dinner bias.
    * Outside meal hours with a small amount → Snacks/Coffee.
- A "snacks-grade" amount (under 500 TL / $15) at any hour stays
  Meals/Snacks; do not bump to Lunch/Dinner from time alone.

When supplier evidence (named restaurant / customer-facing dinner venue)
contradicts these anchors, prefer supplier evidence and explain in
``business_reason``.

CONTEXT (last N days, this user only):
{context_json}

CANONICAL OCR fields (context only — the pipeline already has these):
{canonical_json}

STATEMENT row context (context only):
{statement_json}

Propose a complete classification. Output JSON with keys: business_or_personal,
report_bucket, attendees (list of strings), customer (string or null),
business_reason (string), confidence_overall (float 0–1),
receipt_time (string "HH:MM" or null).

Strict JSON shape, no markdown or prose outside JSON:
{{
  "business_or_personal": null,
  "report_bucket": null,
  "attendees": [],
  "customer": null,
  "business_reason": null,
  "confidence_overall": null,
  "receipt_time": null
}}
"""


def parse_inline_keyboard_response(raw_text: str) -> InlineKeyboardSuggestion | None:
    """Parse a raw model response into an ``InlineKeyboardSuggestion``.

    Returns ``None`` for malformed input (invalid JSON, non-object, empty
    text, etc.). Partial responses are accepted: any of the six fields
    may be missing → that field stays ``None``. Unknown keys in the raw
    response are silently ignored.
    """
    text = (raw_text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    return InlineKeyboardSuggestion(
        business_or_personal=_clean_optional_string(parsed.get("business_or_personal")),
        report_bucket=_clean_optional_string(parsed.get("report_bucket")),
        attendees=_coerce_attendee_list(parsed.get("attendees")),
        customer=_clean_optional_string(parsed.get("customer")),
        business_reason=_clean_optional_string(parsed.get("business_reason")),
        confidence_overall=_coerce_unit_float(parsed.get("confidence_overall")),
        receipt_time=_clean_optional_string(parsed.get("receipt_time")),
    )


def _coerce_attendee_list(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        # Tolerate "Hakan, Burak" — split on the same separators the
        # context builder uses, so test fixtures and model output align.
        cleaned = [part.strip() for part in re.split(r"[,;+]", value)]
        return [item for item in cleaned if item] or []
    if isinstance(value, list):
        return [
            item.strip()
            for item in value
            if isinstance(item, str) and item.strip()
        ]
    return None


def _coerce_unit_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    # Per spec: confidence_overall is in [0, 1]. Out-of-range values are
    # clamped rather than dropped — the model occasionally returns 0–100.
    if result > 1.0 and result <= 100.0:
        result = result / 100.0
    if result < 0.0:
        return 0.0
    if result > 1.0:
        return 1.0
    return result


# ─── F-AI-Stage1 sub-PR 7: deterministic bucket post-validation guard ──────
#
# Defense-in-depth: even with the price+time anchoring added to the inline-
# keyboard prompt, the AI sometimes returns a snack-grade bucket on a
# dinner-grade receipt. This pure-Python guard runs after the parse and
# bumps a small set of structurally-counter-intuitive (bucket, amount, time)
# combinations into the right meal slot. Bumps stay inside the existing EDT
# meal-bucket vocabulary (Meals/Snacks, Breakfast, Lunch, Dinner) — no new
# value is invented.
#
# The guard is a strict superset improvement: it can only move a meal-
# family bucket sideways/up. Non-meal buckets (Taxi, Hotel, Fuel, etc.) and
# already-Dinner / already-Entertainment suggestions are returned unchanged.

_logger = logging.getLogger(__name__)

# Local-currency amount bands in their own currency unit. The TRY band is
# anchored to recent prod data (snacks 50–500 TL, lunches 500–2000 TL,
# dinners 2000+ TL); the USD band is anchored to EDT Code of Conduct
# per-head meal caps (~$30 lunch / $60 dinner) plus typical mid-market
# city pricing. EUR/GBP/CAD reuse the USD band as a coarse approximation
# until per-currency tuning is justified.
_TRY_LUNCH_FLOOR = Decimal("500")
_TRY_DINNER_FLOOR = Decimal("2000")
_USD_LUNCH_FLOOR = Decimal("15")
_USD_DINNER_FLOOR = Decimal("50")

_USD_LIKE_CURRENCIES: frozenset[str] = frozenset({"USD", "EUR", "GBP", "CAD"})

# Meal-hour windows. Keep these narrow so a 10:30 coffee or 16:00 dessert
# doesn't get nudged out of Snacks.
_LUNCH_HOUR_START = time(11, 30)
_LUNCH_HOUR_END = time(14, 30)
_DINNER_HOUR_START = time(18, 0)
_DINNER_HOUR_END = time(22, 30)

# Bucket families the guard touches. Anything outside this set is returned
# unchanged so the guard can never accidentally bucket a hotel as Dinner.
_MEAL_BUCKETS_BUMPABLE: frozenset[str] = frozenset({
    "Meals/Snacks",
    "Breakfast",
    "Lunch",
})


def apply_bucket_post_validation(
    suggestion: InlineKeyboardSuggestion,
    *,
    local_amount: Decimal | None,
    local_currency: str | None,
) -> tuple[InlineKeyboardSuggestion, str | None]:
    """Return ``(maybe_bumped_suggestion, reason_or_none)``.

    The guard fires only on meal-family buckets (Meals/Snacks, Breakfast,
    Lunch). Dinner / Entertainment / non-meal buckets are returned
    unchanged. When neither amount nor time gives a usable signal, the
    suggestion is returned unchanged.

    The returned ``reason`` is a short underscore-tag string suitable for
    logging or telemetry (e.g. ``"snacks_dinner_grade_amount"``); ``None``
    means "no bump fired".
    """
    if not isinstance(suggestion, InlineKeyboardSuggestion):
        return suggestion, None
    bucket = suggestion.report_bucket
    if bucket not in _MEAL_BUCKETS_BUMPABLE:
        return suggestion, None

    grade = _amount_grade(local_amount, local_currency)
    time_of_day = _parse_hhmm(suggestion.receipt_time)

    target = bucket
    reasons: list[str] = []

    # Amount-driven bump on snack/breakfast buckets: the receipt's amount
    # alone is sufficient to disqualify "Meals/Snacks".
    if bucket in {"Meals/Snacks", "Breakfast"}:
        if grade == "dinner":
            target = "Dinner"
            reasons.append("amount_dinner_grade")
        elif grade == "lunch":
            target = "Lunch"
            reasons.append("amount_lunch_grade")

    # Time-driven bump: a Lunch bucket past dinner-hour is structurally
    # wrong.
    if bucket == "Lunch" and time_of_day is not None and time_of_day >= _DINNER_HOUR_START:
        target = "Dinner"
        reasons.append("time_dinner_hour")

    # Time-driven bump on Snacks: dinner-hour with anything but a coffee-
    # grade amount → Dinner.
    if (
        bucket == "Meals/Snacks"
        and time_of_day is not None
        and time_of_day >= _DINNER_HOUR_START
        and grade in {"lunch", "dinner"}
    ):
        target = "Dinner"
        if "time_dinner_hour" not in reasons:
            reasons.append("time_dinner_hour")

    # Time-vs-amount disagreement on Lunch bucket: lunch-hour matches the
    # bucket; do nothing. Outside both lunch and dinner windows, amount
    # alone decides (already handled above).
    if (
        bucket in {"Meals/Snacks", "Breakfast"}
        and target == "Lunch"
        and time_of_day is not None
        and time_of_day >= _DINNER_HOUR_START
    ):
        # We bumped to Lunch on amount, but the receipt prints a dinner
        # hour — escalate to Dinner.
        target = "Dinner"
        if "time_dinner_hour" not in reasons:
            reasons.append("time_dinner_hour")

    if target == bucket:
        return suggestion, None

    reason = "+".join(reasons) if reasons else "post_validation_bump"
    bumped = replace(suggestion, report_bucket=target)
    _logger.info(
        "bucket_post_validation: bumped %r → %r (amount=%s %s, time=%s, reason=%s)",
        bucket,
        target,
        local_amount,
        local_currency,
        suggestion.receipt_time,
        reason,
    )
    return bumped, reason


def _amount_grade(
    local_amount: Decimal | None, local_currency: str | None
) -> str | None:
    """Classify the receipt's local-currency amount into one of
    ``snack`` / ``lunch`` / ``dinner``. Returns ``None`` when the amount
    or currency is missing, or the currency is one we don't have a
    calibrated band for."""
    if local_amount is None:
        return None
    if not local_currency:
        return None
    cur = local_currency.upper()
    try:
        amt = Decimal(local_amount)
    except (InvalidOperation, ValueError, TypeError):
        return None
    if amt < 0:
        return None
    if cur == "TRY":
        if amt > _TRY_DINNER_FLOOR:
            return "dinner"
        if amt > _TRY_LUNCH_FLOOR:
            return "lunch"
        return "snack"
    if cur in _USD_LIKE_CURRENCIES:
        if amt > _USD_DINNER_FLOOR:
            return "dinner"
        if amt > _USD_LUNCH_FLOOR:
            return "lunch"
        return "snack"
    return None


_HHMM_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?$")
_HHMM_COMPACT = re.compile(r"^(\d{2})(\d{2})$")


def _parse_hhmm(value: str | None) -> time | None:
    """Accept ``HH:MM`` / ``H:MM`` / ``HH:MM:SS`` / ``HHMM``. Returns
    ``None`` for any input that doesn't pin to a valid 24-hour clock
    time."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned:
        return None
    m = _HHMM_PATTERN.match(cleaned)
    if m is None:
        m = _HHMM_COMPACT.match(cleaned)
    if m is None:
        return None
    try:
        hh = int(m.group(1))
        mm = int(m.group(2))
    except (TypeError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return time(hh, mm)
