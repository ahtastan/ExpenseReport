from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


class AppUserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    telegram_user_id: int | None
    username: str | None
    first_name: str | None
    last_name: str | None
    display_name: str | None
    created_at: datetime


class ReceiptRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uploader_user_id: int | None
    source: str
    status: str
    content_type: str
    original_file_name: str | None
    mime_type: str | None
    caption: str | None
    extracted_date: date | None
    extracted_supplier: str | None
    extracted_local_amount: float | None
    extracted_currency: str | None
    business_or_personal: str | None
    report_bucket: str | None
    business_reason: str | None
    attendees: str | None
    needs_clarification: bool
    created_at: datetime


class ReceiptUpdate(BaseModel):
    extracted_date: date | None = None
    extracted_supplier: str | None = None
    extracted_local_amount: float | None = None
    extracted_currency: str | None = None
    ocr_confidence: float | None = None
    business_or_personal: str | None = None
    report_bucket: str | None = None
    business_reason: str | None = None
    attendees: str | None = None
    needs_clarification: bool | None = None


class ReceiptExtractionRead(BaseModel):
    receipt_id: int
    status: str
    extracted_date: date | None
    extracted_supplier: str | None
    extracted_local_amount: float | None
    extracted_currency: str | None
    business_or_personal: str | None
    confidence: float | None
    missing_fields: list[str]
    notes: list[str]


class ClarificationQuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    receipt_document_id: int | None
    user_id: int | None
    question_key: str
    question_text: str
    answer_text: str | None
    status: str
    created_at: datetime
    answered_at: datetime | None


class ClarificationAnswer(BaseModel):
    answer_text: str


class StatementImportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    uploader_user_id: int | None
    source_filename: str
    statement_date: date | None
    period_start: date | None
    period_end: date | None
    cardholder_name: str | None
    company_name: str | None
    row_count: int
    created_at: datetime


class StatementTransactionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    statement_import_id: int
    transaction_date: date | None
    posting_date: date | None
    supplier_raw: str
    supplier_normalized: str
    local_currency: str
    local_amount: float | None
    usd_amount: float | None
    source_row_ref: str | None
    source_kind: str
    created_at: datetime


class ManualStatementDraft(BaseModel):
    receipt_id: int
    status: str
    extracted_date: date | None
    extracted_supplier: str | None
    extracted_local_amount: float | None
    extracted_currency: str | None
    business_or_personal: str | None
    confidence: float | None
    missing_fields: list[str]
    notes: list[str]


class ManualStatementCreate(BaseModel):
    statement_import_id: int | None = None
    receipt_id: int | None = None
    transaction_date: date
    supplier: str
    amount: float
    currency: str = "TRY"
    business_reason: str | None = None


class MatchDecisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    statement_transaction_id: int
    receipt_document_id: int
    confidence: str
    match_method: str
    approved: bool
    rejected: bool
    reason: str
    created_at: datetime
    updated_at: datetime


class MatchRunRequest(BaseModel):
    statement_import_id: int | None = None
    receipt_id: int | None = None
    auto_approve_high_confidence: bool = True


class MatchRunSummary(BaseModel):
    receipts_considered: int
    candidates_created: int
    high_confidence: int
    medium_confidence: int
    low_confidence: int
    auto_approved: int
    skipped_receipts: int


class ReviewSummary(BaseModel):
    receipts_total: int
    receipts_needing_clarification: int
    statements_total: int
    transactions_total: int
    match_decisions_total: int
    approved_matches: int
    rejected_matches: int
    open_questions: int


class ReviewRowUpdate(BaseModel):
    fields: dict[str, Any] | None = None
    attention_required: bool | None = None
    attention_note: str | None = None


class ReviewBulkUpdateRequest(BaseModel):
    fields: dict[str, Any]
    scope: str = "attention_required"
    row_ids: list[int] | None = None


class ReviewBulkUpdateResult(BaseModel):
    updated_rows: int
    remaining_attention_rows: int


class ReviewConfirmRequest(BaseModel):
    confirmed_by_user_id: int | None = None
    confirmed_by_label: str | None = None


class ReviewRowRead(BaseModel):
    id: int
    status: str
    attention_required: bool
    attention_note: str | None
    source: dict[str, Any]
    suggested: dict[str, Any]
    confirmed: dict[str, Any]


class ReviewSessionRead(BaseModel):
    id: int
    statement_import_id: int
    status: str
    confirmed_at: datetime | None
    confirmed_by_user_id: int | None
    confirmed_by_label: str | None
    snapshot_hash: str | None
    rows: list[ReviewRowRead]


class ManualStatementCreateResult(BaseModel):
    transaction: StatementTransactionRead
    review_session: ReviewSessionRead


class ReportValidationIssue(BaseModel):
    severity: str
    code: str
    message: str
    receipt_id: int | None = None
    statement_transaction_id: int | None = None
    match_decision_id: int | None = None
    review_row_id: int | None = None
    supplier: str | None = None
    transaction_date: str | None = None
    report_bucket: str | None = None
    air_travel_date: str | None = None
    air_travel_return_date: str | None = None
    air_travel_rt_or_oneway: str | None = None


class ReportValidationResult(BaseModel):
    statement_import_id: int
    ready: bool
    issue_count: int
    warning_count: int
    included_transactions: int
    approved_matches: int
    business_receipts: int
    personal_receipts: int
    issues: list[ReportValidationIssue]


class ReportGenerateRequest(BaseModel):
    statement_import_id: int
    employee_name: str = "Ahmet Hakan Tastan"
    title_prefix: str = "Diners Club Expense Report"
    allow_warnings: bool = True


class ReportRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    statement_import_id: int
    template_name: str
    status: str
    output_workbook_path: str | None
    output_pdf_path: str | None
    created_at: datetime


class ReportCreate(BaseModel):
    owner_user_id: int
    report_kind: str
    title: str
    report_currency: str = "USD"
    period_start: date | None = None
    period_end: date | None = None
    notes: str | None = None

    @field_validator("report_kind")
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in {"diners_statement", "personal_reimbursement"}:
            raise ValueError(
                f"report_kind must be diners_statement or personal_reimbursement, got {v}"
            )
        return v

    @field_validator("report_currency")
    @classmethod
    def _validate_currency(cls, v: str) -> str:
        if v not in {"USD", "EUR"}:
            raise ValueError(f"report_currency must be USD or EUR, got {v}")
        return v


class ReportRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    owner_user_id: int
    report_kind: str
    title: str
    status: str
    report_currency: str
    period_start: date | None
    period_end: date | None
    statement_import_id: int | None
    notes: str | None
    created_at: datetime
    updated_at: datetime


class ReportDetail(ReportRead):
    receipt_count: int
    review_session_id: int | None
    report_run_ids: list[int]


class ReceiptAttachResponse(BaseModel):
    receipt_id: int
    expense_report_id: int
    message: str


class ReceiptDetachResponse(BaseModel):
    receipt_id: int
    message: str


class TelegramWebhookResult(BaseModel):
    ok: bool
    action: str
    receipt_id: int | None = None
    statement_import_id: int | None = None
    user_id: int | None = None
    questions_created: int = 0
    transactions_imported: int = 0
    message: str | None = None
