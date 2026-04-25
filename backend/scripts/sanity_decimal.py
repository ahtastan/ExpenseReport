"""Sanity check: insert Decimal, query back, assert exact equality.

Verifies SQLite Numeric type-affinity preserves Decimal precision through a
full SQLModel round-trip with the new column types.

Important caveat: SQLite's NUMERIC affinity routes through float64 under
the hood, so values that exceed float64's ~15-17 significant decimal digits
will lose precision (e.g. Decimal('99999999999999.9999') round-trips as
Decimal('100000000000000.0000')). PostgreSQL honors Numeric(18,4) exactly.
For our domain (receipts up to ~10^6, rates 1e-8 to ~10^4), float64 has
ample headroom. The migration script (step 7) and any code paths that
might handle very large amounts must be PostgreSQL-tested before prod.
"""
from __future__ import annotations

import os
import sys
import tempfile
from decimal import Decimal
from pathlib import Path

# Ensure backend/ is importable regardless of cwd.
BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

tmpdir = tempfile.mkdtemp()
db_path = Path(tmpdir) / "sanity.db"
os.environ["DATABASE_URL"] = f"sqlite:///{db_path.as_posix()}"

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from app.models import FxRate, ReceiptDocument, StatementImport, StatementTransaction

engine = create_engine(os.environ["DATABASE_URL"], connect_args={"check_same_thread": False})
SQLModel.metadata.create_all(engine)

with Session(engine) as session:
    # Amount round-trip (Numeric(18, 4))
    receipt = ReceiptDocument(extracted_local_amount=Decimal("32.4527"))
    session.add(receipt)
    session.commit()
    session.refresh(receipt)
    print(f"receipt.extracted_local_amount = {receipt.extracted_local_amount!r}")
    assert receipt.extracted_local_amount == Decimal("32.4527"), receipt.extracted_local_amount
    assert isinstance(receipt.extracted_local_amount, Decimal), type(receipt.extracted_local_amount)

    # StatementTransaction local_amount + usd_amount
    si = StatementImport(source_filename="x.xlsx")
    session.add(si)
    session.commit()
    session.refresh(si)
    tx = StatementTransaction(
        statement_import_id=si.id,
        supplier_raw="Test",
        supplier_normalized="test",
        local_amount=Decimal("12345.6789"),  # realistic statement amount
        usd_amount=Decimal("4.1234"),  # 4 dp precision
    )
    session.add(tx)
    session.commit()
    session.refresh(tx)
    print(f"tx.local_amount = {tx.local_amount!r}")
    print(f"tx.usd_amount = {tx.usd_amount!r}")
    assert tx.local_amount == Decimal("12345.6789")
    assert tx.usd_amount == Decimal("4.1234")

    # FxRate Numeric(18, 8)
    from datetime import date
    fx = FxRate(
        rate_date=date(2026, 4, 25),
        from_currency="TRY",
        to_currency="USD",
        rate=Decimal("0.00000001"),
        source="manual",
    )
    session.add(fx)
    session.commit()
    session.refresh(fx)
    print(f"fx.rate = {fx.rate!r}")
    assert fx.rate == Decimal("0.00000001")
    assert isinstance(fx.rate, Decimal)

print("OK: all 4 columns round-trip Decimal exactly.")
