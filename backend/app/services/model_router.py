"""Model routing policy for OCR, matching, and report synthesis.

Policy (post-F1.3 rollback, 2026-04):
  - vision OCR model       = OCR_VISION_MODEL (single tier, ``gpt-5.4`` full)
  - report synthesis model = OCR_VISION_MODEL (unified)
  - chat + matching model  = OCR_VISION_MODEL (unified)

F1 moved to ``gpt-5.5`` for OCR, which proved too slow and too
conservative on the live receipt corpus. F1.3 rolls the receipt-OCR
path back to the previous working ``gpt-5.4`` full-vision snapshot —
the strongest model that has produced acceptable quality in
production. The ``gpt-5.4-mini`` tier from the original mini→full
architecture is intentionally NOT restored: leadership requires
near-perfect first-pass OCR and mini's accuracy is insufficient.
``MINI_MODEL`` and ``FULL_MODEL`` Python constants are kept as
aliases (both default to ``VISION_MODEL``) so existing callers and
env-var overrides keep working — but with both pointing at the same
full model, "mini" is effectively disabled for OCR.

OCR pipeline (implemented in ``vision_extract``):
  1. caller runs deterministic parsing first (regex over caption/filename);
  2. if critical fields are still missing, caller invokes ``vision_extract``;
  3. ``vision_extract`` runs the standard prompt against ``VISION_MODEL``
     and reads date / amount / supplier / receipt_type normally;
  4. before clarification questions are created, missing fields get focused
     retries against the same ``VISION_MODEL``: supplier-only for missing or
     unreadable supplier, date-only for missing date, and amount/currency-only
     for missing amount or currency. Each retry fills only the missing field(s)
     it owns; clean first-pass values are preserved verbatim.

The real model identifiers are env-driven so non-production environments
can point at fakes/stubs without code changes.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.json_utils import DecimalEncoder

logger = logging.getLogger(__name__)

# Single-tier vision model. F1.3 reverts the OCR path to the previous
# working ``gpt-5.4`` full snapshot after gpt-5.5 proved too slow and
# too conservative on live receipts. ``gpt-5.4-mini`` is deliberately
# NOT used here — leadership requires near-perfect first-pass OCR and
# mini's quality is below that bar.
VISION_MODEL = os.getenv("OCR_VISION_MODEL", "gpt-5.4")

# Backwards-compat aliases — both default to the unified VISION_MODEL but
# can be overridden independently via env for A/B testing. With both
# defaulting to the same full snapshot, the "mini tier" is effectively
# disabled for receipt OCR until an env override re-enables it.
MINI_MODEL = os.getenv("OCR_MINI_MODEL", VISION_MODEL)
FULL_MODEL = os.getenv("OCR_FULL_MODEL", VISION_MODEL)
CHAT_MODEL = os.getenv("CHAT_MODEL", VISION_MODEL)
SYNTHESIS_MODEL = os.getenv("SYNTHESIS_MODEL", VISION_MODEL)
MATCHING_MODEL = os.getenv("MATCHING_MODEL", VISION_MODEL)

# Lower bound for the OpenAI completion-token budget. Originally raised
# to 2048 in F1.1 to accommodate gpt-5.5's reasoning overhead; gpt-5.4
# has no such overhead and would be fine at the original 256. Kept high
# because it costs nothing at non-reasoning models (max, not target),
# leaves headroom if a future env override re-enables a reasoning model,
# and keeps the F1.1 regression guard meaningful.
_MAX_COMPLETION_TOKENS = 2048

CRITICAL_FIELDS = ("date", "supplier", "amount")

UNREADABLE_MERCHANT_SENTINEL = "UNREADABLE_MERCHANT"

_VISION_PROMPT = (
    "You are an expense receipt parser. Extract the following fields from the "
    "receipt image and return ONLY a JSON object with exactly these keys:\n"
    "  date (ISO 8601 string YYYY-MM-DD or null),\n"
    "  supplier (string or null),\n"
    "  amount (number or null),\n"
    "  currency (3-letter ISO code string or null),\n"
    "  business_or_personal (\"Business\" or \"Personal\" or null),\n"
    "  receipt_type (one of \"itemized\", \"payment_receipt\", \"invoice\", "
    "\"confirmation\", \"unknown\").\n"
    "MERCHANT NAME RULES (read carefully — F1 hardening):\n"
    "  Output the merchant name EXACTLY as printed at the top of the receipt "
    "(the masthead/header line, not the address block, not the VAT/tax ID "
    "line, not a slogan, not a payment-processor name like \"BKM EXPRESS\" or "
    "\"ISBANK POS\").\n"
    "  If the merchant line is unclear, ambiguous, partially obscured, or you "
    "find yourself reading from address text, neighborhood/district names, or "
    "context rather than a header, output the literal string "
    f"\"{UNREADABLE_MERCHANT_SENTINEL}\" for supplier — DO NOT guess and DO "
    "NOT infer. It is better to abstain than to invent.\n"
    "  Do NOT compose merchant names from address fragments (e.g. street "
    "names, neighborhood names, building names) or from text in the line-item "
    "list. The supplier must come from the printed receipt header.\n"
    "For Turkish receipts, the date may be labeled TARIH and may appear as "
    "DD/MM/YYYY or DD.MM.YYYY; convert it to YYYY-MM-DD.\n"
    "Payment slips may label the transaction date as ISLEM with a value like "
    "DD/MM/YYYY - HH:MM; use that as the receipt date.\n"
    "Classify receipt_type using this rubric:\n"
    "  itemized         — individual line items with prices are visible (a "
    "restaurant bill showing each dish; a hotel folio showing nightly rate "
    "and per-charge breakdown).\n"
    "  payment_receipt  — only the total amount paid is shown; no line-item "
    "breakdown (a POS terminal slip, credit-card machine receipt).\n"
    "  invoice          — formal invoice/fatura with tax ID numbers. Turkish "
    "commercial receipts that carry both a tax ID and itemized lines go here.\n"
    "  confirmation     — reservation/booking without proof of payment (an "
    "airline reservation printout, a hotel booking confirmation before "
    "check-in).\n"
    "  unknown          — cannot determine from the image.\n"
    "If unsure, default to \"unknown\" rather than guessing.\n"
    "Return only the JSON object, no other text."
)

# Stricter retry variant — used ONLY when the first pass returned the
# UNREADABLE_MERCHANT sentinel for supplier. The retry is scoped to
# merchant/supplier ambiguity: it asks the model to re-read the masthead
# more carefully and ignore everything else. Date and amount from the
# first pass are preserved by the caller, so this prompt deliberately
# does NOT re-instruct the model on amount or date selection — those
# fields stay valid even when supplier was unreadable.
_VISION_PROMPT_STRICT = (
    "Look at this receipt image one more time. The first extraction pass "
    "could not read the merchant name with confidence and abstained with "
    "a sentinel. Date and amount have already been captured — your only "
    "task here is to re-read the merchant masthead.\n"
    "\n"
    "MERCHANT NAME RULES — obey without exception:\n"
    "  Output the merchant name EXACTLY as printed on the masthead (the "
    "topmost line of the receipt header).\n"
    "  DO NOT compose merchant names from any of the following — these "
    "are all NOT the merchant:\n"
    "    - address fragments (street names, neighborhood/district names, "
    "building names, city names, postal codes)\n"
    "    - VAT IDs / vergi numarası lines\n"
    "    - MERSIS numbers\n"
    "    - tax-office labels (e.g. \"VERGİ DAİRESİ\")\n"
    "    - payment processor names (\"BKM\", \"ISBANK POS\", \"ZIRAAT POS\")\n"
    "    - line items inside the bill body\n"
    "    - cashier/operator names (e.g. \"KASIYER: ...\")\n"
    "    - slogans, taglines, or descriptive subtitles below the masthead\n"
    "  DO NOT infer merchant from category context (e.g. seeing fuel-pump "
    "line items does NOT mean the merchant is \"Generic Petrol Station\").\n"
    f"  If you still cannot read the masthead with confidence, output "
    f"\"{UNREADABLE_MERCHANT_SENTINEL}\" — DO NOT guess. We would rather "
    f"see the sentinel than a hallucinated name.\n"
    "\n"
    "Return ONLY a JSON object with exactly one key:\n"
    "  supplier (string — the merchant name, or the sentinel above).\n"
    "Do not include date, amount, or any other fields. No prose, no code "
    "fences, no explanation."
)

_VISION_PROMPT_DATE_ONLY = (
    "Look at this receipt image one more time. Date is the only missing "
    "field from the first extraction pass. Your only task is to find the "
    "printed receipt date.\n"
    "\n"
    "DATE RULES:\n"
    "  Search for the merchant receipt date, not the card transaction date "
    "unless this is a POS/payment slip where that printed transaction date is "
    "the receipt date.\n"
    "  For Turkish POS receipts, look near labels such as TARİH, TARIH, "
    "SAAT, FİŞ NO, FIS NO, and İŞLEM/ISLEM in the top or middle header area.\n"
    "  Turkish/common date formats may appear as DD-MM-YYYY, DD/MM/YYYY, "
    "or DD.MM.YYYY; convert them to YYYY-MM-DD.\n"
    "  Ignore due dates, statement dates, card expiry dates, tax-office "
    "registration dates, and any unrelated dates.\n"
    "  Do not infer a date from the current date, upload date, Telegram "
    "timestamp, or surrounding chat context.\n"
    "  Do not return impossible years or implausibly old years; if the year "
    "is unreadable, return null rather than guessing.\n"
    "  If no receipt/transaction/payment date is readable, return null.\n"
    "\n"
    "Return ONLY a JSON object with exactly one key:\n"
    "  date (string YYYY-MM-DD, or null).\n"
    "Do not include supplier, amount, currency, or any other fields. No "
    "prose, no code fences, no explanation."
)

_VISION_PROMPT_AMOUNT_ONLY = (
    "Look at this receipt image one more time. Amount and/or currency is "
    "the only missing field from the first extraction pass. Your only task "
    "is to find the final paid total and its currency.\n"
    "\n"
    "AMOUNT RULES:\n"
    "  Return the final grand total / paid amount, not subtotals, taxes, "
    "change, card balances, installments, tips-only lines, or per-item "
    "prices.\n"
    "  For Turkish receipts, totals may be labeled TOPLAM, GENEL TOPLAM, "
    "TUTAR, ODENEN, or ISLEM TUTARI. TRY, TL, and the Turkish lira symbol "
    "all map to TRY.\n"
    "  Preserve decimals as a number. If only currency is readable, return "
    "amount null and the currency. If only amount is readable, return amount "
    "and currency null.\n"
    "\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    "  amount (number, or null),\n"
    "  currency (3-letter ISO code string such as TRY, USD, EUR, or null).\n"
    "Do not include date, supplier, or any other fields. No prose, no code "
    "fences, no explanation."
)

_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_PDF_EXTENSIONS = {".pdf"}

_MEDIA_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_PDF_RASTER_DPI = 180
_PDF_MAX_PAGES = 10


@dataclass(frozen=True)
class VisionResult:
    """Outcome of a single-tier vision call with optional focused retries."""

    fields: dict[str, Any]
    model: str  # the model that produced the fields (single-tier post-F1.3)
    escalated: bool  # true when a focused retry contributed to the result
    notes: list[str]


@dataclass(frozen=True)
class MatchDisambiguation:
    """Outcome of a matching-model disambiguation call."""

    transaction_id: int | None  # chosen candidate id, or None if model abstained
    confidence: str  # "high" | "medium" | "low" as judged by the model
    reasoning: str  # short natural-language rationale (for audit trail)
    model: str
    # EDT-template bucket+category suggestion. Populated only when the model
    # returned a value from the closed set in EDT_BUCKETS / EDT_CATEGORIES;
    # unknown values are dropped to None during validation in
    # ``match_disambiguate``. NULL on abstention.
    suggested_bucket: str | None = None
    suggested_category: str | None = None


@dataclass(frozen=True)
class MatchClassification:
    """Outcome of a classify-only LLM call for an already-paired match.

    Distinct from MatchDisambiguation: that picks one of N candidates;
    this asks "given THIS receipt and THIS transaction, what EDT bucket
    fits?" — no candidate-picking semantics, no confidence-on-pick field.
    Used by run_matching on every approved match where disambiguation did
    not already produce a bucket (i.e. all deterministic-path matches).
    """

    bucket: str | None  # one of EDT_BUCKETS, or None if model abstained / dropped
    category: str | None  # one of EDT_CATEGORIES, or None if model abstained / dropped
    reasoning: str  # one short sentence (for audit trail)
    model: str


def _count_missing(fields: dict[str, Any]) -> list[str]:
    """Return the list of CRITICAL_FIELDS that are missing/null/sentinel.

    A supplier value of ``UNREADABLE_MERCHANT_SENTINEL`` counts as missing —
    that's the explicit abstention signal we instruct the mini model to emit
    when the masthead is unreadable, and it must trigger escalation to the
    full model the same way a null supplier does.
    """
    missing: list[str] = []
    for key in CRITICAL_FIELDS:
        value = fields.get(key)
        if not value:
            missing.append(key)
        elif key == "supplier" and _is_unreadable_merchant_sentinel(value):
            missing.append(key)
    return missing


def _is_unreadable_merchant_sentinel(value: Any) -> bool:
    return isinstance(value, str) and value.strip().upper() == UNREADABLE_MERCHANT_SENTINEL


def _supplier_needs_merchant_retry(value: Any) -> bool:
    """True when the first-pass supplier value should trigger the
    merchant-only retry.

    The retry exists to recover the merchant masthead when the first
    pass couldn't read it. Three first-pass shapes count as "couldn't
    read it":
      - the explicit ``UNREADABLE_MERCHANT`` abstention sentinel;
      - a literal ``None`` (model emitted no value);
      - an empty / whitespace-only string.

    The retry is merchant-only and preserves first-pass date / amount /
    currency / receipt_type, so it cannot blank good fields — making
    it safe to fire on the broader "supplier missing" signal, not just
    the explicit sentinel.
    """
    if _is_unreadable_merchant_sentinel(value):
        return True
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _date_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _amount_missing(value: Any) -> bool:
    return value is None


def _currency_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and not value.strip():
        return True
    return False


def _normalize_unreadable_supplier(fields: dict[str, Any]) -> dict[str, Any]:
    """If supplier is the abstention sentinel, surface it as ``None`` to
    downstream callers — they should not see the literal sentinel string in
    the supplier field.
    """
    if _is_unreadable_merchant_sentinel(fields.get("supplier")):
        normalized = dict(fields)
        normalized["supplier"] = None
        return normalized
    return fields


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


def _read_pdf_pages_b64(
    path: str,
    dpi: int = _PDF_RASTER_DPI,
    max_pages: int = _PDF_MAX_PAGES,
) -> list[str] | None:
    """Render each page of a PDF to PNG, return list of base64-encoded images.

    Returns ``None`` if the file isn't a PDF, doesn't exist, is empty, or
    cannot be opened. Caps at ``max_pages`` to protect against pathologically
    large files; pages past the cap are skipped with a warning.
    """
    pdf_path = Path(path)
    if not pdf_path.exists() or pdf_path.suffix.lower() not in _PDF_EXTENSIONS:
        return None
    try:
        import pypdfium2 as pdfium  # deferred import
    except Exception as exc:
        logger.warning("pypdfium2 unavailable: %s", exc)
        return None

    scale = dpi / 72.0
    pages_b64: list[str] = []
    try:
        document = pdfium.PdfDocument(str(pdf_path))
    except Exception as exc:
        logger.warning("Failed to open PDF %s: %s", pdf_path, exc)
        return None
    try:
        page_count = len(document)
        if page_count <= 0:
            return None
        if page_count > max_pages:
            logger.warning(
                "PDF %s has %d pages; rasterizing only the first %d.",
                pdf_path,
                page_count,
                max_pages,
            )
        render_count = min(page_count, max_pages)
        for index in range(render_count):
            page = document[index]
            try:
                bitmap = page.render(scale=scale)
                pil_image = bitmap.to_pil()
                buffer = io.BytesIO()
                pil_image.save(buffer, format="PNG")
                pages_b64.append(base64.standard_b64encode(buffer.getvalue()).decode())
            finally:
                try:
                    page.close()
                except Exception:
                    pass
    except Exception as exc:
        logger.warning("Failed to rasterize PDF %s: %s", pdf_path, exc)
        return None
    finally:
        try:
            document.close()
        except Exception:
            pass

    return pages_b64 or None


def _call_openai(
    model: str,
    images: list[tuple[str, str]],
    prompt: str = _VISION_PROMPT,
) -> dict[str, Any] | None:
    """Invoke the OpenAI chat-completions vision API for one or more images.

    ``images`` is a list of ``(media_type, base64_payload)`` tuples. All
    images are sent in a single user message as separate ``image_url``
    content blocks so the model sees them together (preserves cross-page
    context for multi-page PDFs).

    ``prompt`` defaults to the standard ``_VISION_PROMPT``; focused retry
    paths pass narrower prompts for supplier, date, or amount/currency.

    Returns ``None`` when the key is unset, the SDK is unavailable, the
    images list is empty, or the response cannot be parsed as JSON.
    """
    if not images:
        return None
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None
    try:
        from openai import OpenAI  # deferred import — optional dependency
    except Exception:
        return None
    try:
        client = OpenAI(api_key=api_key)
        content: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for media_type, b64 in images:
            data_url = f"data:{media_type};base64,{b64}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=_MAX_COMPLETION_TOKENS,
            messages=[{"role": "user", "content": content}],
        )
        content_text = response.choices[0].message.content or ""
        return _extract_json(content_text)
    except Exception as exc:  # pragma: no cover - depends on live API
        logger.warning("OpenAI vision call failed on %s: %s", model, exc)
        return None


# The concrete call is indirected through this module-level attribute so
# tests can monkey-patch a fake without reaching into the OpenAI SDK.
_vision_call = _call_openai


# Closed-set EDT template buckets + categories. Mirrors CATEGORY_GROUPS in
# frontend/review-table.html — kept in sync via
# tests/test_match_prompt_buckets_match_category_map.py (drift detector).
# When EDT changes its template, BOTH this list AND the frontend map must
# be updated together.
EDT_BUCKETS: tuple[str, ...] = (
    # Hotel & Travel
    "Hotel/Lodging/Laundry", "Auto Rental", "Auto Gasoline",
    "Taxi/Parking/Tolls/Uber", "Other Travel Related",
    # Meals & Entertainment
    "Meals/Snacks", "Breakfast", "Lunch", "Dinner", "Entertainment",
    # Air Travel
    "Airfare/Bus/Ferry/Other",
    # Other
    "Membership/Subscription Fees", "Customer Gifts", "Telephone/Internet",
    "Postage/Shipping", "Admin Supplies", "Lab Supplies",
    "Field Service Supplies", "Assets", "Other",
)
EDT_CATEGORIES: tuple[str, ...] = (
    "Hotel & Travel", "Meals & Entertainment",
    "Air Travel", "Personal Car", "Other",
)

# Bumped from implicit v1 (no version) to v2 — v2 is the first prompt that
# returns suggested_bucket + suggested_category. Future bumps (e.g. v3)
# should accompany any change that materially alters the model's expected
# output shape or its decision rules. M1 Day 3b PR-1 will start emitting
# this version into FieldProvenanceEvent.metadata_json on LLM_MATCH events.
MATCH_PROMPT_VERSION = "v2"

_MATCH_PROMPT = (
    "You are a receipt-to-bank-statement matcher. You will be given a single "
    "receipt and a list of candidate statement transactions. Pick the single "
    "best candidate, or abstain if none is plausible.\n\n"
    "You must also classify the receipt under EDT's expense template.\n"
    "Allowed buckets (pick exactly one or null): "
    + ", ".join(repr(b) for b in EDT_BUCKETS) + ".\n"
    "Allowed categories: " + ", ".join(repr(c) for c in EDT_CATEGORIES) + ".\n"
    "Categories map to buckets per the obvious topical grouping: "
    "'Hotel & Travel' covers all lodging + ground-transport + gasoline; "
    "'Air Travel' is just airfare; "
    "'Meals & Entertainment' is the meal+entertainment cluster; "
    "'Other' is admin/supplies/membership/telephone/internet; "
    "'Personal Car' has no buckets and is rarely used.\n\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    "  transaction_id (integer id from the candidate list, or null to abstain),\n"
    "  confidence (\"high\", \"medium\", or \"low\"),\n"
    "  reasoning (one short sentence explaining the pick),\n"
    "  suggested_bucket (one of the allowed buckets above, or null if uncertain),\n"
    "  suggested_category (one of the allowed categories above, or null).\n"
    "Do not invent a transaction_id that is not in the candidate list."
    " Do not invent a bucket or category that is not in the allowed list."
)

_SYNTHESIS_PROMPT = (
    "You are an internal expense report summarizer. Given structured report "
    "package data, write a concise Markdown summary for a finance reviewer. "
    "Cover trip purpose, totals by bucket, and flagged anomalies. "
    "Return ONLY a JSON object with exactly one key: summary_md."
)


# Bumped to v2: prompt now treats receipt.business_reason as the PRIMARY
# signal for classification. v1 leaned heavily on supplier name, which
# misleads when the supplier is generic (a gas-station mini-mart that's
# actually a snack stop; a market entry that's a customer coffee meeting;
# a restaurant chain row from a hotel folio). The operator's typed
# business_reason captures intent in a way the supplier name does not.
CLASSIFY_PROMPT_VERSION = "v2"

_CLASSIFY_PROMPT = (
    "You are an EDT expense-template classifier. You will be given a single "
    "receipt that has already been paired with a single statement transaction. "
    "Pick the EDT template bucket + category that best fits this expense. "
    "Be concise; do not second-guess the pairing.\n\n"
    "PRIMARY SIGNAL: receipt.business_reason captures the operator's intent "
    "for this expense (e.g. 'customer meeting in Sakarya', 'fuel for company "
    "travel', 'team lunch'). Treat it as the strongest cue when present and "
    "non-empty. The supplier name alone often misleads — a gas station may "
    "be a coffee/snack stop, a market may be a customer-meeting venue, and "
    "a restaurant chain row may actually be a hotel folio sub-line.\n\n"
    "SECONDARY SIGNALS (use to corroborate or break ties): receipt.supplier, "
    "receipt.attendees (people the expense was for), receipt.receipt_type, "
    "transaction.supplier (statement-side supplier name; sometimes cleaner "
    "than the receipt OCR), and the amount/date pair.\n\n"
    "Allowed buckets (pick exactly one or null if truly uncertain): "
    + ", ".join(repr(b) for b in EDT_BUCKETS) + ".\n"
    "Allowed categories: " + ", ".join(repr(c) for c in EDT_CATEGORIES) + ".\n"
    "Categories map to buckets per the obvious topical grouping: "
    "'Hotel & Travel' covers all lodging + ground-transport + gasoline; "
    "'Air Travel' is just airfare; "
    "'Meals & Entertainment' is the meal+entertainment cluster; "
    "'Other' is admin/supplies/membership/telephone/internet; "
    "'Personal Car' has no buckets and is rarely used.\n\n"
    "Return ONLY a JSON object with exactly these keys:\n"
    "  bucket (one of the allowed buckets above, or null),\n"
    "  category (one of the allowed categories above, or null),\n"
    "  reasoning (one short sentence explaining the pick — cite which "
    "signal you weighted most).\n"
    "Do not invent a bucket or category that is not in the allowed list."
)


# ---------------------------------------------------------------------------
# closed-set validators (shared between match_disambiguate + classify_match_bucket)
# ---------------------------------------------------------------------------


def _validate_edt_bucket(raw: object, *, source_label: str) -> str | None:
    """Coerce a model-returned bucket value to a valid EDT_BUCKETS entry or None.

    Strings in the closed set are accepted. None and empty string are accepted
    as deliberate abstention. Anything else (unknown strings, ints, lists,
    objects) is dropped to None and logged with ``source_label`` for audit
    diagnosis.
    """
    if isinstance(raw, str) and raw in EDT_BUCKETS:
        return raw
    if raw in (None, ""):
        return None
    logger.warning(
        "%s: model returned unknown bucket %r; dropping",
        source_label, raw,
    )
    return None


def _validate_edt_category(raw: object, *, source_label: str) -> str | None:
    """Same as _validate_edt_bucket but against EDT_CATEGORIES."""
    if isinstance(raw, str) and raw in EDT_CATEGORIES:
        return raw
    if raw in (None, ""):
        return None
    logger.warning(
        "%s: model returned unknown category %r; dropping",
        source_label, raw,
    )
    return None


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
            max_completion_tokens=_MAX_COMPLETION_TOKENS,
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
        cls=DecimalEncoder,
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

    # Closed-set validation for the bucket/category fields. Shared with
    # classify_match_bucket via _validate_edt_bucket / _validate_edt_category.
    suggested_bucket = _validate_edt_bucket(
        result.get("suggested_bucket"), source_label="match_disambiguate"
    )
    suggested_category = _validate_edt_category(
        result.get("suggested_category"), source_label="match_disambiguate"
    )

    return MatchDisambiguation(
        transaction_id=chosen,
        confidence=confidence,
        reasoning=reasoning,
        model=MATCHING_MODEL,
        suggested_bucket=suggested_bucket,
        suggested_category=suggested_category,
    )


def classify_match_bucket(
    receipt: dict[str, Any],
    transaction: dict[str, Any],
) -> MatchClassification | None:
    """Classify an already-paired (receipt, transaction) into an EDT bucket.

    Distinct from match_disambiguate: the pair is already chosen by the
    deterministic scorer (or by a prior LLM disambiguation call). This
    function exists purely to ask the model "what bucket fits?" — it does
    not pick among candidates.

    Returns None if the OpenAI call is unavailable (no API key, SDK
    missing, or transient failure), so callers can keep the deterministic
    match without a bucket suggestion. Callers should NOT treat None as
    failure-to-classify-meaningfully — it just means we never got an
    answer; the receipt's existing report_bucket (if any) stands.

    The returned MatchClassification has bucket / category validated
    against EDT_BUCKETS / EDT_CATEGORIES. Hallucinated values are
    dropped to None and logged the same way as in match_disambiguate.
    """
    if not isinstance(receipt, dict) or not isinstance(transaction, dict):
        return None

    payload = json.dumps(
        {"receipt": receipt, "transaction": transaction},
        ensure_ascii=False,
        sort_keys=True,
        cls=DecimalEncoder,
    )
    result = _text_call(MATCHING_MODEL, _CLASSIFY_PROMPT, payload)
    if not isinstance(result, dict):
        return None

    bucket = _validate_edt_bucket(
        result.get("bucket"), source_label="classify_match_bucket"
    )
    category = _validate_edt_category(
        result.get("category"), source_label="classify_match_bucket"
    )
    reasoning = str(result.get("reasoning") or "")[:300]
    return MatchClassification(
        bucket=bucket,
        category=category,
        reasoning=reasoning,
        model=MATCHING_MODEL,
    )


def synthesize_report_summary(report: dict[str, Any]) -> str | None:
    """Generate a Markdown report summary through the synthesis model.

    Returns ``None`` when the model is unavailable or does not provide a usable
    ``summary_md`` string. The report generator supplies a deterministic
    fallback so package creation does not depend on live API availability.
    """
    payload = json.dumps(report, ensure_ascii=False, sort_keys=True, cls=DecimalEncoder, default=str)
    result = _text_call(SYNTHESIS_MODEL, _SYNTHESIS_PROMPT, payload)
    if not isinstance(result, dict):
        return None
    summary = result.get("summary_md")
    if not isinstance(summary, str) or not summary.strip():
        return None
    return summary.strip()


# Bumped from implicit v1 to v1 (first explicit version). Co-located with
# the prompt text so a developer who edits the prompt is forced to consider
# the version bump on review.
TRAVEL_REASON_PROMPT_VERSION = "v1"

_TRAVEL_REASON_PROMPT = (
    "You summarize the purpose of a business trip from individual receipt "
    "notes. Given the business_reason text from N receipts (one per line), "
    "write a single concise sentence (max 100 chars) describing the trip's "
    "purpose. Format like:\n"
    "  'Customer visit to <region>, visiting <customer names if mentioned>'\n"
    "  'Sarajevo trade conference, customer meetings'\n"
    "  'Kartonsan service visit, paper mill maintenance'\n"
    "Cite specific customer/site names from the input when they appear in "
    "multiple receipts (high-signal). Do not invent customers. If receipts "
    "are mixed across multiple unrelated trips, return the dominant theme.\n\n"
    "Return ONLY a JSON object with exactly one key: summary."
)

# Maximum length of the returned summary in characters. Sized to fit the
# Week 1A!G3 cell width without wrapping at the EDT template's default
# column widths.
TRAVEL_REASON_MAX_LEN = 100


def generate_travel_reason_summary(business_reasons: list[str]) -> str | None:
    """Summarize a trip's purpose from per-receipt business_reason text.

    Returns ``None`` when:
      - ``business_reasons`` is empty or all entries are empty/whitespace,
      - the OpenAI call is unavailable (no API key, SDK missing, transient fail),
      - the model's response is malformed or empty.

    Callers (``report_generator.generate_report_package``) fall back to the
    operator-supplied ``title_prefix`` when this returns ``None`` so report
    generation never blocks on live API availability. No retry logic — the
    LLM call is best-effort decoration of the report header, not load-bearing.

    The result is sized to ``TRAVEL_REASON_MAX_LEN`` (100 chars). Anything
    longer is truncated at the last sentence boundary if possible, else hard
    cut with an ellipsis. The model is asked to stay within the limit but
    we don't trust it.
    """
    cleaned = [r.strip() for r in business_reasons if isinstance(r, str) and r.strip()]
    if not cleaned:
        return None

    payload = json.dumps(
        {"business_reasons": cleaned, "receipt_count": len(cleaned)},
        ensure_ascii=False,
        sort_keys=True,
    )
    result = _text_call(SYNTHESIS_MODEL, _TRAVEL_REASON_PROMPT, payload)
    if not isinstance(result, dict):
        return None
    summary = result.get("summary")
    if not isinstance(summary, str):
        return None
    summary = summary.strip()
    if not summary:
        return None
    if len(summary) > TRAVEL_REASON_MAX_LEN:
        # Try to clip at a sentence boundary first; fall back to hard cut + ellipsis.
        for boundary in (". ", "; ", ", "):
            cut = summary.rfind(boundary, 0, TRAVEL_REASON_MAX_LEN - 1)
            if cut > 0:
                summary = summary[:cut].rstrip(".,; ") + "."
                break
        else:
            summary = summary[: TRAVEL_REASON_MAX_LEN - 1].rstrip() + "…"
    return summary


def vision_extract(storage_path: str) -> VisionResult | None:
    """Run the single-tier vision pipeline for one image or PDF.

    For PDFs, every page (capped at ``_PDF_MAX_PAGES``) is rasterized once
    and all page images are sent together in each model call, preserving
    cross-page context (e.g. totals on a later page referencing bookings
    on the first).

    Pipeline:
      1. Call the full vision model with ``_VISION_PROMPT`` and read all
         fields (date, amount, supplier, currency, business_or_personal,
         receipt_type) from the response.
      2. If the first-pass supplier is missing — the
         ``UNREADABLE_MERCHANT`` sentinel, ``None``, or an empty
         string — retry the same model with ``_VISION_PROMPT_STRICT``
         (supplier-only) and merge only supplier.
      3. If date is still missing, retry with ``_VISION_PROMPT_DATE_ONLY``
         and merge only date.
      4. If amount or currency is still missing, retry with
         ``_VISION_PROMPT_AMOUNT_ONLY`` and merge only the missing
         amount/currency fields.

    Focused retries never overwrite non-null first-pass values. They run
    before clarification questions, so the bot only asks the user after
    these narrow recovery attempts fail.

    Returns ``None`` when the file is unsupported or the first-pass
    model call itself produced no parseable response.
    """
    images, notes = _vision_images_for_path(storage_path)
    if images is None:
        return None

    first_fields = _vision_call(VISION_MODEL, images)
    if first_fields is None:
        notes.append(
            f"First pass ({VISION_MODEL}) unavailable or returned invalid JSON; "
            "no focused retries."
        )
        return None

    merged = dict(first_fields)
    retry_attempted = False
    retry_contributed = False

    first_supplier = merged.get("supplier")
    if _supplier_needs_merchant_retry(first_supplier):
        # Supplier-only ambiguity -> run stricter merchant-only retry. Date and
        # amount from the first pass stand: a supplier-side problem must never
        # blank fields that were already extracted cleanly.
        if _is_unreadable_merchant_sentinel(first_supplier):
            retry_reason = f"reported {UNREADABLE_MERCHANT_SENTINEL} for supplier"
        elif first_supplier is None:
            retry_reason = "returned null supplier"
        else:
            retry_reason = "returned empty/whitespace supplier"
        notes.append(
            f"First pass ({VISION_MODEL}) {retry_reason}; "
            "retrying supplier extraction with stricter prompt (other fields preserved)."
        )
        retry_attempted = True
        retry_fields = _vision_call(VISION_MODEL, images, _VISION_PROMPT_STRICT)
        # ``escalated`` reflects whether the retry actually contributed to the
        # returned result. A None retry response means the retry didn't run (or
        # ran and failed to parse) — we keep the first-pass fields unchanged,
        # which is semantically the same as "no retry happened" from the
        # downstream caller's perspective.
        if retry_fields is not None and "supplier" in retry_fields:
            merged["supplier"] = retry_fields.get("supplier")
            retry_contributed = True
            notes.append(
                f"Supplier retry ({VISION_MODEL}) returned supplier="
                f"{retry_fields.get('supplier')!r}; first-pass date/amount/etc. preserved."
            )
        else:
            notes.append(
                "Supplier retry unavailable; keeping first-pass fields with supplier "
                "normalized to None."
            )

    if _date_missing(merged.get("date")):
        notes.append(
            f"Date missing after first pass ({VISION_MODEL}); retrying date-only extraction."
        )
        retry_attempted = True
        retry_fields = _vision_call(VISION_MODEL, images, _VISION_PROMPT_DATE_ONLY)
        retry_date = retry_fields.get("date") if retry_fields is not None else None
        if not _date_missing(retry_date):
            merged["date"] = retry_date
            retry_contributed = True
            notes.append(
                f"Date retry ({VISION_MODEL}) returned date={retry_date!r}; "
                "all other fields preserved."
            )
        else:
            notes.append("Date retry unavailable or returned null; date remains missing.")

    amount_missing = _amount_missing(merged.get("amount"))
    currency_missing = _currency_missing(merged.get("currency"))
    if amount_missing or currency_missing:
        missing_names = [
            name
            for name, missing in (("amount", amount_missing), ("currency", currency_missing))
            if missing
        ]
        notes.append(
            f"{'/'.join(missing_names).capitalize()} missing after first pass ({VISION_MODEL}); "
            "retrying amount-only extraction."
        )
        retry_attempted = True
        retry_fields = _vision_call(VISION_MODEL, images, _VISION_PROMPT_AMOUNT_ONLY)
        retry_filled: list[str] = []
        if retry_fields is not None:
            retry_amount = retry_fields.get("amount")
            retry_currency = retry_fields.get("currency")
            if amount_missing and not _amount_missing(retry_amount):
                merged["amount"] = retry_amount
                retry_filled.append("amount")
            if currency_missing and not _currency_missing(retry_currency):
                merged["currency"] = retry_currency
                retry_filled.append("currency")
        if retry_filled:
            retry_contributed = True
            notes.append(
                f"Amount retry ({VISION_MODEL}) filled {', '.join(retry_filled)}; "
                "non-missing first-pass fields preserved."
            )
        else:
            notes.append(
                "Amount retry unavailable or returned no missing amount/currency values."
            )

    if retry_contributed:
        notes.append(f"Vision extraction completed with focused retry contribution ({VISION_MODEL}).")
    elif retry_attempted:
        notes.append(f"Vision extraction completed with no focused retry contribution ({VISION_MODEL}).")
    else:
        notes.append(f"Vision extraction succeeded on first pass ({VISION_MODEL}).")

    return VisionResult(
        fields=_normalize_unreadable_supplier(merged),
        model=VISION_MODEL, escalated=retry_contributed, notes=notes,
    )


def _vision_images_for_path(storage_path: str) -> tuple[list[tuple[str, str]] | None, list[str]]:
    path = Path(storage_path)
    suffix = path.suffix.lower()
    notes: list[str] = []

    if suffix in _PDF_EXTENSIONS:
        pages_b64 = _read_pdf_pages_b64(storage_path)
        if not pages_b64:
            return None, notes
        images: list[tuple[str, str]] = [("image/png", b64) for b64 in pages_b64]
        notes.append(f"Rasterized PDF into {len(images)} page image(s) at {_PDF_RASTER_DPI} DPI.")
        return images, notes
    if suffix in _IMAGE_EXTENSIONS:
        encoded = _read_image_b64(path)
        if encoded is None:
            return None, notes
        return [encoded], notes
    return None, notes


def vision_retry_date(storage_path: str) -> VisionResult | None:
    """Run only the receipt-date prompt against the configured vision model."""
    images, notes = _vision_images_for_path(storage_path)
    if images is None:
        return None
    fields = _vision_call(VISION_MODEL, images, _VISION_PROMPT_DATE_ONLY)
    if fields is None:
        notes.append(f"Date-only retry ({VISION_MODEL}) unavailable or returned invalid JSON.")
        return None
    notes.append(f"Date-only retry ({VISION_MODEL}) returned date={fields.get('date')!r}.")
    return VisionResult(fields=fields, model=VISION_MODEL, escalated=True, notes=notes)
