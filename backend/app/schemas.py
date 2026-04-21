from datetime import date, datetime

from pydantic import BaseModel, ConfigDict


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
    storage_path: str | None
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
    storage_path: str | None
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


class ReportValidationIssue(BaseModel):
    severity: str
    code: str
    message: str
    receipt_id: int | None = None
    statement_transaction_id: int | None = None
    match_decision_id: int | None = None


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


class TelegramWebhookResult(BaseModel):
    ok: bool
    action: str
    receipt_id: int | None = None
    user_id: int | None = None
    questions_created: int = 0
    message: str | None = None
