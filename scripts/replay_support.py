from __future__ import annotations

import csv
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


SUMMARY_FIELDS = [
    "receipts_processed",
    "extraction_pass",
    "extraction_fail",
    "matched_count",
    "unmatched_count",
    "amount_mismatch_count",
    "date_mismatch_count",
    "supplier_mismatch_count",
]

_MONEY_QUANT = Decimal("0.0001")


@dataclass(frozen=True)
class ExpectedReceipt:
    filename: str
    expected_date: date | None = None
    expected_supplier_contains: str | None = None
    expected_amount: Decimal | None = None
    expected_currency: str | None = None
    expected_bucket: str | None = None
    expected_business_or_personal: str | None = None


@dataclass(frozen=True)
class ReceiptReplayResult:
    filename: str
    receipt_id: int | None = None
    extraction_status: str = "not_processed"
    extraction_error: str | None = None
    observed_date: date | None = None
    observed_supplier: str | None = None
    observed_amount: Decimal | None = None
    observed_currency: str | None = None
    observed_bucket: str | None = None
    observed_business_or_personal: str | None = None
    missing_fields: str | None = None
    matched: bool = False
    matched_transaction_id: int | None = None
    matched_transaction_date: date | None = None
    matched_supplier: str | None = None
    matched_amount: Decimal | None = None
    matched_currency: str | None = None
    match_confidence: str | None = None
    match_score: float | None = None
    match_reason: str | None = None
    date_match: bool | None = None
    supplier_match: bool | None = None
    amount_match: bool | None = None
    currency_match: bool | None = None
    bucket_match: bool | None = None
    business_or_personal_match: bool | None = None


def _blank_to_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_date(value: Any) -> date | None:
    text = _blank_to_none(value)
    if text is None:
        return None
    return date.fromisoformat(text)


def _parse_decimal(value: Any) -> Decimal | None:
    text = _blank_to_none(value)
    if text is None:
        return None
    try:
        return Decimal(text.replace(",", "")).quantize(_MONEY_QUANT)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid expected_amount {text!r}") from exc


def _normalize_currency(value: Any) -> str | None:
    text = _blank_to_none(value)
    return text.upper() if text else None


def parse_expected_manifest(path: Path | None) -> dict[str, ExpectedReceipt]:
    if path is None or not path.exists():
        return {}

    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        rows: dict[str, ExpectedReceipt] = {}
        for row_number, row in enumerate(reader, start=2):
            filename = _blank_to_none(row.get("filename"))
            if filename is None:
                raise ValueError(f"expected_manifest row {row_number} is missing filename")
            rows[filename] = ExpectedReceipt(
                filename=filename,
                expected_date=_parse_date(row.get("expected_date")),
                expected_supplier_contains=_blank_to_none(row.get("expected_supplier_contains")),
                expected_amount=_parse_decimal(row.get("expected_amount")),
                expected_currency=_normalize_currency(row.get("expected_currency")),
                expected_bucket=_blank_to_none(row.get("expected_bucket")),
                expected_business_or_personal=_blank_to_none(row.get("expected_business_or_personal")),
            )
    return rows


def _norm_text(value: str | None) -> str:
    return " ".join((value or "").casefold().split())


def expected_matches(result: ReceiptReplayResult, expected: ExpectedReceipt) -> dict[str, bool | None]:
    amount_match = None
    if expected.expected_amount is not None:
        amount_match = result.observed_amount == expected.expected_amount

    date_match = None
    if expected.expected_date is not None:
        date_match = result.observed_date == expected.expected_date

    supplier_match = None
    if expected.expected_supplier_contains is not None:
        supplier_match = _norm_text(expected.expected_supplier_contains) in _norm_text(result.observed_supplier)

    currency_match = None
    if expected.expected_currency is not None:
        currency_match = (result.observed_currency or "").upper() == expected.expected_currency

    bucket_match = None
    if expected.expected_bucket is not None:
        bucket_match = _norm_text(result.observed_bucket) == _norm_text(expected.expected_bucket)

    business_or_personal_match = None
    if expected.expected_business_or_personal is not None:
        business_or_personal_match = _norm_text(result.observed_business_or_personal) == _norm_text(
            expected.expected_business_or_personal
        )

    return {
        "date_match": date_match,
        "supplier_match": supplier_match,
        "amount_match": amount_match,
        "currency_match": currency_match,
        "bucket_match": bucket_match,
        "business_or_personal_match": business_or_personal_match,
    }


def with_expected_matches(
    result: ReceiptReplayResult, expected: ExpectedReceipt | None
) -> ReceiptReplayResult:
    if expected is None:
        return result
    return replace(result, **expected_matches(result, expected))


def summarize_results(results: list[ReceiptReplayResult]) -> dict[str, int]:
    counts = {field: 0 for field in SUMMARY_FIELDS}
    counts["receipts_processed"] = len(results)
    counts["extraction_pass"] = sum(1 for result in results if result.extraction_status == "extracted")
    counts["extraction_fail"] = counts["receipts_processed"] - counts["extraction_pass"]
    counts["matched_count"] = sum(1 for result in results if result.matched)
    counts["unmatched_count"] = counts["receipts_processed"] - counts["matched_count"]
    counts["amount_mismatch_count"] = sum(1 for result in results if result.amount_match is False)
    counts["date_mismatch_count"] = sum(1 for result in results if result.date_match is False)
    counts["supplier_mismatch_count"] = sum(1 for result in results if result.supplier_match is False)
    return counts


def result_to_csv_row(result: ReceiptReplayResult) -> dict[str, Any]:
    return asdict(result)


def json_default(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    return str(value)
