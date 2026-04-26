from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import Column, Index, Numeric, Text, text
from sqlmodel import Field, SQLModel


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class AppUser(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    telegram_user_id: int | None = Field(default=None, index=True)
    username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    display_name: str | None = None
    # Sticky 30-min current-report session context (M1 Day 2+).
    current_report_id: int | None = Field(
        default=None, foreign_key="expensereport.id", index=True
    )
    current_report_set_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReceiptDocument(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    uploader_user_id: int | None = Field(default=None, foreign_key="appuser.id", index=True)
    source: str = Field(default="telegram", index=True)
    status: str = Field(default="received", index=True)
    content_type: str = Field(default="photo", index=True)
    telegram_chat_id: int | None = Field(default=None, index=True)
    telegram_message_id: int | None = None
    telegram_file_id: str | None = Field(default=None, index=True)
    telegram_file_unique_id: str | None = Field(default=None, index=True)
    original_file_name: str | None = None
    mime_type: str | None = None
    storage_path: str | None = None
    caption: str | None = None
    extracted_date: date | None = None
    extracted_supplier: str | None = None
    extracted_local_amount: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(18, 4))
    )
    extracted_currency: str | None = None
    ocr_confidence: float | None = None
    # Addition B: classification from the vision model so validation can flag
    # payment_receipt-only rows on hotel-chain suppliers (hotel_needs_itemized_folio).
    # Allowed: 'itemized' | 'payment_receipt' | 'invoice' | 'confirmation' | 'unknown'.
    # NULL means "not yet classified" (pre-Addition-B rows; classifier script
    # backfills on demand).
    receipt_type: str | None = Field(default=None, index=True)
    business_or_personal: str | None = Field(default=None, index=True)
    report_bucket: str | None = None
    business_reason: str | None = None
    attendees: str | None = None
    needs_clarification: bool = Field(default=True, index=True)
    # Attach a receipt to an expense report; NULL until M1 Day 4+ endpoint links it.
    expense_report_id: int | None = Field(
        default=None, foreign_key="expensereport.id", index=True
    )
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ClarificationQuestion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    receipt_document_id: int | None = Field(default=None, foreign_key="receiptdocument.id", index=True)
    user_id: int | None = Field(default=None, foreign_key="appuser.id", index=True)
    question_key: str = Field(index=True)
    question_text: str
    answer_text: str | None = None
    status: str = Field(default="open", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    answered_at: datetime | None = None


class StatementImport(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    uploader_user_id: int | None = Field(default=None, foreign_key="appuser.id", index=True)
    source_filename: str
    storage_path: str | None = None
    statement_date: date | None = None
    period_start: date | None = None
    period_end: date | None = None
    cardholder_name: str | None = Field(default=None, index=True)
    company_name: str | None = None
    row_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)


class StatementTransaction(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    statement_import_id: int = Field(foreign_key="statementimport.id", index=True)
    transaction_date: date | None = Field(default=None, index=True)
    posting_date: date | None = None
    supplier_raw: str = Field(index=True)
    supplier_normalized: str = Field(index=True)
    local_currency: str = Field(default="TRY", index=True)
    local_amount: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(18, 4), index=True)
    )
    usd_amount: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(18, 4))
    )
    source_row_ref: str | None = None
    source_kind: str = Field(default="excel")
    created_at: datetime = Field(default_factory=utc_now)


class MatchDecision(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    statement_transaction_id: int = Field(foreign_key="statementtransaction.id", index=True)
    receipt_document_id: int = Field(foreign_key="receiptdocument.id", index=True)
    confidence: str = Field(default="low", index=True)
    match_method: str
    approved: bool = Field(default=False, index=True)
    rejected: bool = Field(default=False, index=True)
    reason: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class PolicyDecision(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    statement_transaction_id: int = Field(foreign_key="statementtransaction.id", index=True)
    business_or_personal: str = Field(index=True)
    report_bucket: str | None = None
    include_in_report: bool = Field(default=True, index=True)
    justification: str | None = None
    decided_by_user_id: int | None = Field(default=None, foreign_key="appuser.id")
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReviewSession(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # Nullable at the SQLModel layer for M1; existing SQLite NOT NULL constraint
    # on pre-migration rows is retained (SQLite cannot relax NOT NULL via ALTER),
    # so legacy rows keep their non-null value and only NEW inserts may omit it.
    statement_import_id: int | None = Field(
        default=None, foreign_key="statementimport.id", index=True
    )
    expense_report_id: int | None = Field(
        default=None, foreign_key="expensereport.id", index=True
    )
    status: str = Field(default="draft", index=True)
    snapshot_json: str | None = Field(default=None, sa_column=Column(Text))
    snapshot_hash: str | None = Field(default=None, index=True)
    confirmed_by_user_id: int | None = Field(default=None, foreign_key="appuser.id")
    confirmed_by_label: str | None = None
    confirmed_at: datetime | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReviewRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    review_session_id: int = Field(foreign_key="reviewsession.id", index=True)
    statement_transaction_id: int = Field(foreign_key="statementtransaction.id", index=True)
    receipt_document_id: int | None = Field(default=None, foreign_key="receiptdocument.id", index=True)
    match_decision_id: int | None = Field(default=None, foreign_key="matchdecision.id", index=True)
    status: str = Field(default="suggested", index=True)
    attention_required: bool = Field(default=False, index=True)
    attention_note: str | None = None
    source_json: str = Field(default="{}", sa_column=Column(Text))
    suggested_json: str = Field(default="{}", sa_column=Column(Text))
    confirmed_json: str = Field(default="{}", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ReportRun(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    # See ReviewSession note on nullable vs. SQLite NOT NULL retention.
    statement_import_id: int | None = Field(
        default=None, foreign_key="statementimport.id", index=True
    )
    expense_report_id: int | None = Field(
        default=None, foreign_key="expensereport.id", index=True
    )
    template_name: str = "corporate_expense_report"
    status: str = Field(default="draft", index=True)
    output_workbook_path: str | None = None
    output_pdf_path: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


class ExpenseReport(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    owner_user_id: int = Field(foreign_key="appuser.id", index=True)
    report_kind: str = Field(index=True)  # 'diners_statement' | 'personal_reimbursement'
    title: str
    status: str = Field(default="draft", index=True)
    period_start: date | None = None
    period_end: date | None = None
    report_currency: str = Field(default="USD")  # USD or EUR only, enforced in app layer
    statement_import_id: int | None = Field(
        default=None, foreign_key="statementimport.id", index=True
    )
    notes: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class FxRate(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rate_date: date = Field(index=True)
    from_currency: str = Field(index=True)
    to_currency: str = Field(index=True)
    rate: Decimal = Field(sa_column=Column(Numeric(18, 8), nullable=False))
    source: str  # "openexchangerates" | "ecb" | "manual"
    fetched_at: datetime = Field(default_factory=utc_now)


class FieldProvenanceEvent(SQLModel, table=True):
    """Append-only audit ledger for tracked-field writes (M1 Day 3a).

    Invariant (enforced in service layer, see app.services.field_provenance):
    every write to a tracked field on receiptdocument / reviewrow /
    expensereport produces at least one event in the same DB transaction.
    Current state continues to live on the product columns; this table is
    the lineage record.

    All enum-typed columns (entity_type, field_name, event_type, source,
    actor_type) are str-typed at the DB layer; the corresponding Python
    Enums in app.provenance_enums constrain values at the application
    layer per the design's "no DB CHECK constraints" decision.

    See docs/M1_DAY3A_DESIGN.md for the full schema rationale.
    """

    __tablename__ = "fieldprovenanceevent"

    # Composite index for the load-bearing query: "give me the most recent
    # event for (entity_type, entity_id, field_name)." Per-column indexes
    # are auto-generated from Field(index=True) below; this is the one
    # composite that has to be declared explicitly.
    __table_args__ = (
        Index(
            "ix_fieldprovenanceevent_lookup",
            "entity_type",
            "entity_id",
            "field_name",
            text("created_at DESC"),
        ),
    )

    id: int | None = Field(default=None, primary_key=True)

    # — entity reference (generic; integrity at app layer) —
    entity_type: str = Field(index=True)
    entity_id: int = Field(index=True)

    # — what changed —
    field_name: str = Field(index=True)
    event_type: str
    source: str = Field(index=True)

    # — value (TEXT-shaped; Decimals via DecimalEncoder convention from
    #   M1 Day 2.5; dates as ISO-8601; strings as-is) —
    value: str | None = Field(default=None, sa_column=Column(Text))

    # — denormalized money-shape value, populated only for fields in
    #   app.provenance_enums.MONEY_FIELDS — enables aggregation queries
    #   like SUM(value_decimal) WHERE field_name='extracted_local_amount'.
    #   Same precision as Day 2.5 money columns; NOT (18, 8). —
    value_decimal: Decimal | None = Field(
        default=None, sa_column=Column(Numeric(18, 4))
    )

    # — only meaningful for source IN ('vision', 'deterministic') —
    confidence: float | None = None

    # — grouping (UUID hex stored as TEXT; always set, see design Q1) —
    decision_group_id: str = Field(index=True)

    # — actor (pre-SSO; see design §4 for the post-SSO migration story) —
    actor_type: str
    actor_user_id: int | None = Field(
        default=None, foreign_key="appuser.id", index=True
    )
    actor_label: str  # durable identifier; e.g. "telegram:12345"

    # — extra structured detail; serialized via app.json_utils.dumps so
    #   Decimal-bearing payloads round-trip safely. Validated at write
    #   time to be dict[str, Any] | None per design Q5. —
    metadata_json: str | None = Field(default=None, sa_column=Column(Text))

    created_at: datetime = Field(default_factory=utc_now, index=True)
