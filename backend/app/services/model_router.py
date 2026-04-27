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
    "  business_or_personal (\"Business\" or \"Personal\" or null),\n"
    "  receipt_type (one of \"itemized\", \"payment_receipt\", \"invoice\", "
    "\"confirmation\", \"unknown\").\n"
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


def _call_openai(model: str, images: list[tuple[str, str]]) -> dict[str, Any] | None:
    """Invoke the OpenAI chat-completions vision API for one or more images.

    ``images`` is a list of ``(media_type, base64_payload)`` tuples. All
    images are sent in a single user message as separate ``image_url``
    content blocks so the model sees them together (preserves cross-page
    context for multi-page PDFs).

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
        content: list[dict[str, Any]] = [{"type": "text", "text": _VISION_PROMPT}]
        for media_type, b64 in images:
            data_url = f"data:{media_type};base64,{b64}"
            content.append({"type": "image_url", "image_url": {"url": data_url}})
        response = client.chat.completions.create(
            model=model,
            max_completion_tokens=256,
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
            max_completion_tokens=256,
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
    """Run the staged vision pipeline (mini → full) for one image or PDF.

    For PDFs, every page (capped at ``_PDF_MAX_PAGES``) is rasterized once
    and all page images are sent together in each model call, preserving
    cross-page context (e.g. totals on a later page referencing bookings
    on the first).

    Returns ``None`` when the file is unsupported or no model responded.
    """
    path = Path(storage_path)
    suffix = path.suffix.lower()
    notes: list[str] = []

    if suffix in _PDF_EXTENSIONS:
        pages_b64 = _read_pdf_pages_b64(storage_path)
        if not pages_b64:
            return None
        images: list[tuple[str, str]] = [("image/png", b64) for b64 in pages_b64]
        notes.append(f"Rasterized PDF into {len(images)} page image(s) at {_PDF_RASTER_DPI} DPI.")
    elif suffix in _IMAGE_EXTENSIONS:
        encoded = _read_image_b64(path)
        if encoded is None:
            return None
        images = [encoded]
    else:
        return None

    mini_fields = _vision_call(MINI_MODEL, images)
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

    full_fields = _vision_call(FULL_MODEL, images)
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
