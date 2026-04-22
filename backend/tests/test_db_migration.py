import os
import sqlite3
from pathlib import Path
from uuid import uuid4

VERIFY_ROOT = Path.cwd() / ".verify_data"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
DB_PATH = VERIFY_ROOT / f"db_migration_{uuid4().hex}.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB_PATH}"
os.environ["EXPENSE_STORAGE_ROOT"] = str(VERIFY_ROOT)


REVIEWROW_COLUMNS = """
    id INTEGER NOT NULL PRIMARY KEY,
    review_session_id INTEGER NOT NULL,
    statement_transaction_id INTEGER NOT NULL,
    receipt_document_id INTEGER,
    match_decision_id INTEGER,
    status VARCHAR NOT NULL,
    attention_required BOOLEAN NOT NULL,
    attention_note VARCHAR,
    source_json TEXT,
    suggested_json TEXT,
    confirmed_json TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
"""


OLD_REVIEWROW_COLUMNS = REVIEWROW_COLUMNS.replace(
    "receipt_document_id INTEGER,", "receipt_document_id INTEGER NOT NULL,"
).replace("match_decision_id INTEGER,", "match_decision_id INTEGER NOT NULL,")


def seed_interrupted_reviewrow_migration() -> None:
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(f"CREATE TABLE reviewrow ({REVIEWROW_COLUMNS})")
        conn.execute(f"CREATE TABLE reviewrow_old ({OLD_REVIEWROW_COLUMNS})")
        conn.execute("CREATE INDEX ix_reviewrow_review_session_id ON reviewrow_old (review_session_id)")
        conn.execute("CREATE INDEX ix_reviewrow_statement_transaction_id ON reviewrow_old (statement_transaction_id)")
        conn.execute("CREATE INDEX ix_reviewrow_receipt_document_id ON reviewrow_old (receipt_document_id)")
        conn.execute("CREATE INDEX ix_reviewrow_match_decision_id ON reviewrow_old (match_decision_id)")
        conn.commit()


def main() -> None:
    seed_interrupted_reviewrow_migration()

    from app.db import create_db_and_tables

    create_db_and_tables()

    with sqlite3.connect(DB_PATH) as conn:
        indexes = list(
            conn.execute(
                """
                SELECT name, tbl_name
                FROM sqlite_master
                WHERE type = 'index' AND name LIKE 'ix_reviewrow_%'
                ORDER BY name
                """
            )
        )
        old_table = list(conn.execute("PRAGMA table_info(reviewrow_old)"))
        table_info = list(conn.execute("PRAGMA table_info(reviewrow)"))

    assert old_table == []
    assert indexes
    assert all(tbl_name == "reviewrow" for _name, tbl_name in indexes)
    notnull_by_name = {row[1]: row[3] for row in table_info}
    assert notnull_by_name["receipt_document_id"] == 0
    assert notnull_by_name["match_decision_id"] == 0

    print("db_migration_tests=passed")


if __name__ == "__main__":
    main()
