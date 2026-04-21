from pathlib import Path
import sys

from sqlmodel import Session

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.db import create_db_and_tables, engine  # noqa: E402
from app.services.legacy_receipts import import_legacy_receipt_mapping  # noqa: E402


def main() -> None:
    expense_root = ROOT.parent
    csv_path = expense_root / "Authoritative_Receipt_Mapping_Table_Combined_Images.csv"
    receipt_root = expense_root / "03_11_Receipts" / "Receipts"
    if len(sys.argv) > 1:
        csv_path = Path(sys.argv[1])
    if len(sys.argv) > 2:
        receipt_root = Path(sys.argv[2])

    create_db_and_tables()
    with Session(engine) as session:
        summary = import_legacy_receipt_mapping(session, csv_path, receipt_root=receipt_root)

    print(f"source={summary.source_path}")
    print(f"rows_read={summary.rows_read}")
    print(f"receipts_created={summary.receipts_created}")
    print(f"receipts_updated={summary.receipts_updated}")
    print(f"rows_skipped={summary.rows_skipped}")


if __name__ == "__main__":
    main()
