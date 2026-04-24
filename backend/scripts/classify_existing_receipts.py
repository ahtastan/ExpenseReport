"""Retroactive ReceiptDocument.receipt_type classifier.

Walks every ``ReceiptDocument`` row that currently has ``receipt_type IS NULL``
and ``storage_path IS NOT NULL``, calls the vision model, and stores the
returned classification. Other extracted fields (supplier, amount, date,
currency, business_or_personal) are NOT touched — this script only fills
the new ``receipt_type`` column.

Idempotent: re-running the script skips rows already classified.

Default is DRY-RUN. Pass ``--apply`` to actually update the database.

Usage:

    # inspect what would change
    python backend/scripts/classify_existing_receipts.py

    # actually update
    python backend/scripts/classify_existing_receipts.py --apply

    # target a non-default DB (otherwise read from DATABASE_URL env var)
    python backend/scripts/classify_existing_receipts.py --db-url sqlite:///path/to/app.db
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path


def _bootstrap_paths() -> None:
    here = Path(__file__).resolve()
    backend_root = here.parents[1]
    if str(backend_root) not in sys.path:
        sys.path.insert(0, str(backend_root))


def main(argv: list[str] | None = None) -> int:
    _bootstrap_paths()

    parser = argparse.ArgumentParser(
        description="Classify ReceiptDocument.receipt_type on rows where it's still NULL."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write to the database. Default is dry-run.",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help="Override the DATABASE_URL env var for this run.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Stop after N receipts. Useful for a sanity check before full run.",
    )
    args = parser.parse_args(argv)

    if args.db_url:
        os.environ["DATABASE_URL"] = args.db_url

    from sqlmodel import Session, select

    from app.db import engine
    from app.models import ReceiptDocument
    from app.services import model_router
    from app.services.receipt_extraction import RECEIPT_TYPES, _coerce_receipt_type

    mode = "APPLY (writes enabled)" if args.apply else "DRY-RUN (no writes)"
    print(f"classify_existing_receipts — mode: {mode}")
    if args.db_url:
        print(f"  db_url = {args.db_url}")

    counts: Counter[str] = Counter()
    skipped_no_vision = 0
    skipped_no_type = 0
    examined = 0

    with Session(engine) as session:
        stmt = select(ReceiptDocument).where(
            ReceiptDocument.receipt_type.is_(None),  # type: ignore[attr-defined]
            ReceiptDocument.storage_path.is_not(None),  # type: ignore[attr-defined]
        ).order_by(ReceiptDocument.id)
        receipts = session.exec(stmt).all()

        if args.limit is not None:
            receipts = receipts[: args.limit]

        print(f"  candidates: {len(receipts)} receipt(s) with receipt_type IS NULL and storage_path present")

        for receipt in receipts:
            examined += 1
            vision_result = model_router.vision_extract(receipt.storage_path or "")
            if vision_result is None:
                skipped_no_vision += 1
                print(
                    f"  receipt id={receipt.id} supplier={receipt.extracted_supplier!r} "
                    "— vision returned None (unreadable or unavailable)"
                )
                continue
            raw = vision_result.fields.get("receipt_type")
            classification = _coerce_receipt_type(raw)
            if classification is None:
                skipped_no_type += 1
                print(
                    f"  receipt id={receipt.id} supplier={receipt.extracted_supplier!r} "
                    f"— vision did not return a receipt_type (raw={raw!r})"
                )
                continue
            counts[classification] += 1
            print(
                f"  receipt id={receipt.id} supplier={receipt.extracted_supplier!r} "
                f"file={receipt.original_file_name!r} → {classification}"
            )
            if args.apply:
                receipt.receipt_type = classification
                session.add(receipt)

        if args.apply and counts:
            session.commit()
            print(f"  committed {sum(counts.values())} classification(s) to the database")

    print()
    print("Summary:")
    print(f"  examined:            {examined}")
    for kind in sorted(RECEIPT_TYPES):
        print(f"  {kind:20s} {counts.get(kind, 0)}")
    print(f"  skipped_no_vision:   {skipped_no_vision}")
    print(f"  skipped_no_type:     {skipped_no_type}")
    if not args.apply and examined:
        print()
        print("This was a DRY-RUN. Pass --apply to actually write the classifications.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
