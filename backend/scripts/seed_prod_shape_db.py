"""Build a synthetic SQLite DB that matches the production shape.

Used as a stand-in when the live production DB is not yet copied to the
local machine. Produces 13 receiptdocument rows, 13 statementtransaction
rows, and 0 fxrate rows with realistic Turkish-statement amount values
and the OLD-shape (FLOAT) money/rate columns.

This file represents a *substitute* for /tmp/expense_app.db pulled from
the VPS — useful for verifying the migration script's behavior end-to-end
against production-cardinality data without touching the live system.
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

OUTPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("/tmp/expense_app_synthetic.db")

# Pre-migration schema: money columns are FLOAT, rate column would be FLOAT
# in fxrate (no fxrate rows in production yet, so the table is empty but
# its schema is created for migration completeness).

SCHEMA_SQL = [
    """
    CREATE TABLE appuser (
        id INTEGER NOT NULL PRIMARY KEY,
        telegram_user_id INTEGER,
        username VARCHAR,
        first_name VARCHAR,
        last_name VARCHAR,
        display_name VARCHAR,
        current_report_id INTEGER,
        current_report_set_at DATETIME,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE statementimport (
        id INTEGER NOT NULL PRIMARY KEY,
        uploader_user_id INTEGER,
        source_filename VARCHAR NOT NULL,
        storage_path VARCHAR,
        statement_date DATE,
        period_start DATE,
        period_end DATE,
        cardholder_name VARCHAR,
        company_name VARCHAR,
        row_count INTEGER NOT NULL,
        created_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE expensereport (
        id INTEGER NOT NULL PRIMARY KEY,
        owner_user_id INTEGER NOT NULL,
        report_kind VARCHAR NOT NULL,
        title VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        period_start DATE,
        period_end DATE,
        report_currency VARCHAR NOT NULL,
        statement_import_id INTEGER,
        notes VARCHAR,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE receiptdocument (
        id INTEGER NOT NULL PRIMARY KEY,
        uploader_user_id INTEGER,
        source VARCHAR NOT NULL,
        status VARCHAR NOT NULL,
        content_type VARCHAR NOT NULL,
        telegram_chat_id INTEGER,
        telegram_message_id INTEGER,
        telegram_file_id VARCHAR,
        telegram_file_unique_id VARCHAR,
        original_file_name VARCHAR,
        mime_type VARCHAR,
        storage_path VARCHAR,
        caption VARCHAR,
        extracted_date DATE,
        extracted_supplier VARCHAR,
        extracted_local_amount FLOAT,
        extracted_currency VARCHAR,
        ocr_confidence FLOAT,
        receipt_type VARCHAR,
        business_or_personal VARCHAR,
        report_bucket VARCHAR,
        business_reason VARCHAR,
        attendees VARCHAR,
        needs_clarification BOOLEAN NOT NULL,
        expense_report_id INTEGER,
        created_at DATETIME NOT NULL,
        updated_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE statementtransaction (
        id INTEGER NOT NULL PRIMARY KEY,
        statement_import_id INTEGER NOT NULL,
        transaction_date DATE,
        posting_date DATE,
        supplier_raw VARCHAR NOT NULL,
        supplier_normalized VARCHAR NOT NULL,
        local_currency VARCHAR NOT NULL,
        local_amount FLOAT,
        usd_amount FLOAT,
        source_row_ref VARCHAR,
        source_kind VARCHAR NOT NULL,
        created_at DATETIME NOT NULL
    )
    """,
    """
    CREATE TABLE fxrate (
        id INTEGER NOT NULL PRIMARY KEY,
        rate_date DATE NOT NULL,
        from_currency VARCHAR NOT NULL,
        to_currency VARCHAR NOT NULL,
        rate FLOAT NOT NULL,
        source VARCHAR NOT NULL,
        fetched_at DATETIME NOT NULL
    )
    """,
    "CREATE INDEX ix_statementtransaction_local_amount ON statementtransaction (local_amount)",
    "CREATE INDEX ix_statementtransaction_supplier_normalized ON statementtransaction (supplier_normalized)",
    "CREATE INDEX ix_receiptdocument_extracted_date ON receiptdocument (extracted_date)",
]

NOW = datetime.now(timezone.utc).isoformat(timespec="seconds")

# 13 representative receipt amounts mixing TRY and USD, including the kind
# of values the OCR pipeline produces (some at 4 dp, some at 2 dp, some
# with extra precision from float-binary representation).
RECEIPT_AMOUNTS: list[tuple[float | None, str]] = [
    (419.58, "TRY"),
    (1250.00, "TRY"),
    (45.75, "USD"),
    (None, "TRY"),  # extraction failed
    (89.99, "TRY"),
    (3450.00, "TRY"),
    (12.50, "USD"),
    (567.34, "TRY"),
    (89.95, "USD"),
    (725.00, "TRY"),
    (15.00, "USD"),
    (1899.50, "TRY"),
    (250.00, "TRY"),
]

# 13 statement transactions; one row mirrors the missing-receipt case with
# local_amount=None (rare in real data but exercises NULL path).
TX_ROWS: list[tuple[float | None, float | None]] = [
    (419.58, None),
    (1250.00, None),
    (135.00, 4.5),
    (None, None),
    (89.99, None),
    (3450.00, None),
    (37.00, 1.23),
    (567.34, None),
    (270.00, 9.0),
    (725.00, None),
    (44.50, 1.48),
    (1899.50, None),
    (250.00, None),
]


def main() -> None:
    if OUTPUT.exists():
        OUTPUT.unlink()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(OUTPUT) as conn:
        for ddl in SCHEMA_SQL:
            conn.execute(ddl)

        conn.execute(
            "INSERT INTO appuser (id, display_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            (1, "Demo User", NOW, NOW),
        )
        conn.execute(
            "INSERT INTO statementimport (id, uploader_user_id, source_filename, "
            "row_count, created_at) VALUES (?, ?, ?, ?, ?)",
            (1, 1, "march_2026_statement.xlsx", 13, NOW),
        )
        conn.execute(
            "INSERT INTO expensereport (id, owner_user_id, report_kind, title, "
            "status, report_currency, statement_import_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (1, 1, "diners_statement", "March 2026", "submitted", "USD", 1, NOW, NOW),
        )

        for i, (amount, currency) in enumerate(RECEIPT_AMOUNTS, start=1):
            conn.execute(
                """
                INSERT INTO receiptdocument
                  (id, uploader_user_id, source, status, content_type,
                   extracted_date, extracted_supplier, extracted_local_amount,
                   extracted_currency, needs_clarification, expense_report_id,
                   created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    i, 1, "telegram", "extracted", "photo",
                    date(2026, 3, ((i - 1) % 28) + 1).isoformat(),
                    f"Supplier {i}", amount, currency, 0, 1, NOW, NOW,
                ),
            )

        for i, (local, usd) in enumerate(TX_ROWS, start=1):
            conn.execute(
                """
                INSERT INTO statementtransaction
                  (id, statement_import_id, transaction_date, supplier_raw,
                   supplier_normalized, local_currency, local_amount, usd_amount,
                   source_kind, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    i, 1, date(2026, 3, ((i - 1) % 28) + 1).isoformat(),
                    f"VENDOR {i}", f"vendor {i}", "TRY", local, usd,
                    "excel", NOW,
                ),
            )

        conn.commit()

    print(f"wrote {OUTPUT}")
    with sqlite3.connect(OUTPUT) as conn:
        for table in ("receiptdocument", "statementtransaction", "fxrate"):
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"  {table}: {count} rows")


if __name__ == "__main__":
    main()
