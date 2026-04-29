from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.models import ReceiptDocument, StatementTransaction


AMOUNT_TOLERANCE = Decimal("0.01")
DATE_TOLERANCE_DAYS = 3


@dataclass(frozen=True)
class ReceiptStatementIssue:
    code: str
    severity: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }


def _normalize_currency(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().upper()
    if normalized in {"TL", "₺"}:
        return "TRY"
    if normalized == "$":
        return "USD"
    return normalized or None


def _fmt_amount(value: Decimal | None, currency: str | None) -> str:
    if value is None:
        return "missing"
    suffix = f" {currency}" if currency else ""
    return f"{value}{suffix}"


def _date_gap(left: date | None, right: date | None) -> int | None:
    if left is None or right is None:
        return None
    return abs((left - right).days)


def receipt_statement_issues(
    receipt: ReceiptDocument,
    transaction: StatementTransaction,
) -> list[ReceiptStatementIssue]:
    issues: list[ReceiptStatementIssue] = []

    receipt_amount = receipt.extracted_local_amount
    statement_amount = transaction.local_amount
    receipt_currency = _normalize_currency(receipt.extracted_currency)
    statement_currency = _normalize_currency(transaction.local_currency)

    if receipt_amount is None and statement_amount is not None:
        issues.append(
            ReceiptStatementIssue(
                code="receipt_statement_amount_missing",
                severity="error",
                message="receipt amount missing while statement amount is present",
            )
        )
    elif (
        receipt_amount is not None
        and statement_amount is not None
        and abs(receipt_amount - statement_amount) > AMOUNT_TOLERANCE
    ):
        issues.append(
            ReceiptStatementIssue(
                code="receipt_statement_amount_mismatch",
                severity="error",
                message=(
                    "receipt/statement amount mismatch: "
                    f"receipt {_fmt_amount(receipt_amount, receipt_currency)} vs "
                    f"statement {_fmt_amount(statement_amount, statement_currency)}"
                ),
            )
        )

    if (
        receipt_currency is None
        and statement_currency is not None
        and receipt_amount is not None
    ):
        issues.append(
            ReceiptStatementIssue(
                code="receipt_statement_currency_missing",
                severity="error",
                message="receipt currency missing while statement currency is present",
            )
        )
    elif (
        receipt_currency is not None
        and statement_currency is not None
        and receipt_currency != statement_currency
    ):
        issues.append(
            ReceiptStatementIssue(
                code="receipt_statement_currency_mismatch",
                severity="error",
                message=(
                    "receipt/statement currency mismatch: "
                    f"receipt {receipt_currency} vs statement {statement_currency}"
                ),
            )
        )

    if receipt.extracted_date is None and transaction.transaction_date is not None:
        issues.append(
            ReceiptStatementIssue(
                code="receipt_statement_date_missing",
                severity="error",
                message="receipt date missing while statement date is present",
            )
        )
    else:
        gap = _date_gap(receipt.extracted_date, transaction.transaction_date)
        if gap is not None and gap > DATE_TOLERANCE_DAYS:
            issues.append(
                ReceiptStatementIssue(
                    code="receipt_statement_date_mismatch",
                    severity="error",
                    message=(
                        "receipt/statement date mismatch: "
                        f"receipt {receipt.extracted_date} vs "
                        f"statement {transaction.transaction_date}"
                    ),
                )
            )

    return issues


def receipt_statement_issue_note(issues: list[ReceiptStatementIssue] | list[dict]) -> str:
    messages = []
    for issue in issues:
        if isinstance(issue, ReceiptStatementIssue):
            messages.append(issue.message)
        elif isinstance(issue, dict) and issue.get("message"):
            messages.append(str(issue["message"]))
    return "; ".join(messages)
