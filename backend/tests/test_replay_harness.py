from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
import subprocess
import sys

from openpyxl import Workbook


ROOT_DIR = Path(__file__).resolve().parents[2]
SCRIPTS_DIR = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from replay_support import (  # noqa: E402
    ReceiptReplayResult,
    expected_matches,
    parse_expected_manifest,
    summarize_results,
)


def test_parse_expected_manifest_normalizes_rows(tmp_path: Path) -> None:
    manifest = tmp_path / "expected_manifest.csv"
    manifest.write_text(
        "\n".join(
            [
                "filename,expected_date,expected_supplier_contains,expected_amount,expected_currency,expected_bucket,expected_business_or_personal",
                "  Taxi.JPG ,2026-03-14,  Taksi , 439.56 , try ,Auto Gasoline, Business ",
                "empty.png,,,,,,",
            ]
        ),
        encoding="utf-8",
    )

    rows = parse_expected_manifest(manifest)

    assert set(rows) == {"Taxi.JPG", "empty.png"}
    assert rows["Taxi.JPG"].expected_date == date(2026, 3, 14)
    assert rows["Taxi.JPG"].expected_supplier_contains == "Taksi"
    assert rows["Taxi.JPG"].expected_amount == Decimal("439.5600")
    assert rows["Taxi.JPG"].expected_currency == "TRY"
    assert rows["Taxi.JPG"].expected_bucket == "Auto Gasoline"
    assert rows["Taxi.JPG"].expected_business_or_personal == "Business"
    assert rows["empty.png"].expected_amount is None


def test_parse_expected_manifest_missing_path_returns_empty(tmp_path: Path) -> None:
    assert parse_expected_manifest(None) == {}
    assert parse_expected_manifest(tmp_path / "missing.csv") == {}


def test_replay_month_help_runs() -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT_DIR / "scripts" / "replay_month.py"), "--help"],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "--receipts-dir" in result.stdout
    assert "--statement-xlsx" in result.stdout
    assert "--output-dir" in result.stdout


def test_replay_month_report_attempt_keeps_audit_stable(tmp_path: Path) -> None:
    receipts_dir = tmp_path / "receipts"
    receipts_dir.mkdir()
    (receipts_dir / "2026-03-14_Taksi_439.56TRY_business.jpg").write_bytes(b"fake jpg bytes")

    statement_xlsx = tmp_path / "statement.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.append(["Tran Date", "Supplier", "Source Amount", "Amount Incl"])
    ws.append(["03/14/2026", "Taksi", "439.56 TRY", 10.00])
    wb.save(statement_xlsx)
    wb.close()

    manifest = tmp_path / "expected_manifest.csv"
    manifest.write_text(
        "\n".join(
            [
                "filename,expected_date,expected_supplier_contains,expected_amount,expected_currency,expected_bucket,expected_business_or_personal",
                "2026-03-14_Taksi_439.56TRY_business.jpg,2026-03-14,Taksi,439.56,TRY,Auto Gasoline,Business",
            ]
        ),
        encoding="utf-8",
    )

    output_dir = tmp_path / "output"
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT_DIR / "scripts" / "replay_month.py"),
            "--receipts-dir",
            str(receipts_dir),
            "--statement-xlsx",
            str(statement_xlsx),
            "--expected-manifest",
            str(manifest),
            "--output-dir",
            str(output_dir),
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    audit = json.loads((output_dir / "replay_audit.json").read_text(encoding="utf-8"))
    assert audit["statement"]["row_count"] == 1
    assert audit["summary"]["receipts_processed"] == 1
    assert audit["report_generation"]["status"] in {"completed", "skipped", "failed"}


def test_expected_matches_reports_field_mismatches(tmp_path: Path) -> None:
    manifest = tmp_path / "expected_manifest.csv"
    manifest.write_text(
        "\n".join(
            [
                "filename,expected_date,expected_supplier_contains,expected_amount,expected_currency,expected_bucket,expected_business_or_personal",
                "receipt.jpg,2026-03-14,Taksi,439.56,TRY,Auto Gasoline,Business",
            ]
        ),
        encoding="utf-8",
    )
    expected = parse_expected_manifest(manifest)["receipt.jpg"]

    result = ReceiptReplayResult(
        filename="receipt.jpg",
        extraction_status="extracted",
        observed_date=date(2026, 3, 15),
        observed_supplier="Uber Trip",
        observed_amount=Decimal("440.0000"),
        observed_currency="TRY",
        matched=True,
    )

    matches = expected_matches(result, expected)

    assert matches["date_match"] is False
    assert matches["supplier_match"] is False
    assert matches["amount_match"] is False
    assert matches["currency_match"] is True


def test_summarize_results_counts_failures_and_manifest_mismatches() -> None:
    results = [
        ReceiptReplayResult(
            filename="matched.jpg",
            extraction_status="extracted",
            matched=True,
            date_match=True,
            supplier_match=True,
            amount_match=True,
        ),
        ReceiptReplayResult(
            filename="unmatched.jpg",
            extraction_status="needs_extraction_review",
            matched=False,
            date_match=False,
            supplier_match=None,
            amount_match=False,
        ),
        ReceiptReplayResult(
            filename="errored.jpg",
            extraction_status="error",
            matched=False,
            date_match=None,
            supplier_match=False,
            amount_match=None,
        ),
    ]

    counts = summarize_results(results)

    assert counts == {
        "receipts_processed": 3,
        "extraction_pass": 1,
        "extraction_fail": 2,
        "matched_count": 1,
        "unmatched_count": 2,
        "amount_mismatch_count": 1,
        "date_mismatch_count": 1,
        "supplier_mismatch_count": 1,
    }
