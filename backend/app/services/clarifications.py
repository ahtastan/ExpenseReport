from datetime import date, datetime, timezone

from sqlmodel import Session, select

from app.models import ClarificationQuestion, ReceiptDocument


def _question_exists(session: Session, receipt_id: int | None, key: str) -> bool:
    return bool(
        session.exec(
            select(ClarificationQuestion).where(
                ClarificationQuestion.receipt_document_id == receipt_id,
                ClarificationQuestion.question_key == key,
            )
        ).first()
    )


def _parse_date(value: str) -> date | None:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _parse_amount(value: str) -> float | None:
    text = value.replace("TRY", "").replace("TL", "").replace("USD", "").replace("EUR", "").strip()
    if "," in text and "." in text:
        text = text.replace(".", "").replace(",", ".") if text.rfind(",") > text.rfind(".") else text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def ensure_initial_receipt_question(
    session: Session,
    receipt: ReceiptDocument,
    user_id: int | None,
) -> list[ClarificationQuestion]:
    existing = session.exec(
        select(ClarificationQuestion).where(
            ClarificationQuestion.receipt_document_id == receipt.id,
            ClarificationQuestion.question_key == "business_or_personal",
        )
    ).first()
    if existing:
        return []

    question = ClarificationQuestion(
        receipt_document_id=receipt.id,
        user_id=user_id,
        question_key="business_or_personal",
        question_text=(
            "I received this receipt. Is it Business or Personal? "
            "Reply with Business, Personal, or add context like 'Business - Kartonsan dinner'."
        ),
    )
    session.add(question)
    session.commit()
    session.refresh(question)
    return [question]


def ensure_receipt_review_questions(
    session: Session,
    receipt: ReceiptDocument,
    user_id: int | None,
) -> list[ClarificationQuestion]:
    questions: list[ClarificationQuestion] = []
    specs: list[tuple[bool, str, str]] = [
        (
            receipt.extracted_date is None,
            "receipt_date",
            "I could not read the receipt date. What date is on it? Use YYYY-MM-DD if you can.",
        ),
        (
            receipt.extracted_local_amount is None,
            "local_amount",
            "I could not read the receipt amount. What is the total amount and currency?",
        ),
        (
            receipt.extracted_supplier is None,
            "supplier",
            "I could not read the merchant name. Which store, restaurant, or vendor is this?",
        ),
        (
            receipt.business_or_personal is None,
            "business_or_personal",
            "Is this Business or Personal? Reply with Business, Personal, or add context like 'Business - Kartonsan dinner'.",
        ),
        (
            receipt.business_or_personal == "Business" and not receipt.business_reason,
            "business_reason",
            "What project, customer, or trip should this receipt be attached to?",
        ),
        (
            receipt.business_or_personal == "Business" and not receipt.attendees,
            "attendees",
            "Who attended or benefited from this expense? If not applicable, reply 'N/A'.",
        ),
    ]
    for should_ask, key, text in specs:
        if should_ask and not _question_exists(session, receipt.id, key):
            questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=user_id,
                    question_key=key,
                    question_text=text,
                )
            )
    for question in questions:
        session.add(question)
    receipt.needs_clarification = bool(questions) or receipt.needs_clarification
    receipt.updated_at = datetime.now(timezone.utc)
    session.add(receipt)
    session.commit()
    for question in questions:
        session.refresh(question)
    return questions


def next_open_question_for_user(session: Session, user_id: int) -> ClarificationQuestion | None:
    return session.exec(
        select(ClarificationQuestion)
        .where(
            ClarificationQuestion.user_id == user_id,
            ClarificationQuestion.status == "open",
        )
        .order_by(ClarificationQuestion.created_at)
    ).first()


def answer_question(session: Session, question: ClarificationQuestion, answer: str) -> list[ClarificationQuestion]:
    question.answer_text = answer.strip()
    question.status = "answered"
    question.answered_at = datetime.now(timezone.utc)
    session.add(question)

    new_questions: list[ClarificationQuestion] = []
    receipt = None
    if question.receipt_document_id:
        receipt = session.get(ReceiptDocument, question.receipt_document_id)

    if receipt and question.question_key == "business_or_personal":
        lowered = answer.lower()
        if "personal" in lowered:
            receipt.business_or_personal = "Personal"
            receipt.needs_clarification = False
        elif "business" in lowered:
            receipt.business_or_personal = "Business"
            receipt.needs_clarification = True
            if not _question_exists(session, receipt.id, "business_reason"):
                new_questions.append(
                    ClarificationQuestion(
                        receipt_document_id=receipt.id,
                        user_id=question.user_id,
                        question_key="business_reason",
                        question_text="What project, customer, or trip should this receipt be attached to?",
                    )
                )
        else:
            new_questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=question.user_id,
                    question_key="business_or_personal_retry",
                    question_text="I could not tell if that means Business or Personal. Which one should I use?",
                )
            )
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    elif receipt and question.question_key in {"business_or_personal_retry", "business_reason"}:
        if question.question_key == "business_or_personal_retry":
            lowered = answer.lower()
            if "personal" in lowered:
                receipt.business_or_personal = "Personal"
                receipt.needs_clarification = False
            elif "business" in lowered:
                receipt.business_or_personal = "Business"
                receipt.needs_clarification = True
                if not _question_exists(session, receipt.id, "business_reason"):
                    new_questions.append(
                        ClarificationQuestion(
                            receipt_document_id=receipt.id,
                            user_id=question.user_id,
                            question_key="business_reason",
                            question_text="What project, customer, or trip should this receipt be attached to?",
                        )
                    )
        else:
            receipt.business_reason = answer.strip()
            if not _question_exists(session, receipt.id, "attendees"):
                new_questions.append(
                    ClarificationQuestion(
                        receipt_document_id=receipt.id,
                        user_id=question.user_id,
                        question_key="attendees",
                        question_text="Who attended or benefited from this expense? If not applicable, reply 'N/A'.",
                    )
                )
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    elif receipt and question.question_key == "attendees":
        receipt.attendees = answer.strip()
        receipt.needs_clarification = False
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    elif receipt and question.question_key == "receipt_date":
        parsed = _parse_date(answer)
        if parsed:
            receipt.extracted_date = parsed
            receipt.updated_at = datetime.now(timezone.utc)
            session.add(receipt)
        else:
            new_questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=question.user_id,
                    question_key="receipt_date_retry",
                    question_text="I could not parse that date. Please send it like 2026-03-11.",
                )
            )

    elif receipt and question.question_key == "receipt_date_retry":
        parsed = _parse_date(answer)
        if parsed:
            receipt.extracted_date = parsed
            receipt.updated_at = datetime.now(timezone.utc)
            session.add(receipt)

    elif receipt and question.question_key == "local_amount":
        parsed = _parse_amount(answer)
        if parsed is not None:
            receipt.extracted_local_amount = parsed
            if "usd" in answer.lower():
                receipt.extracted_currency = "USD"
            elif "eur" in answer.lower():
                receipt.extracted_currency = "EUR"
            else:
                receipt.extracted_currency = receipt.extracted_currency or "TRY"
            receipt.updated_at = datetime.now(timezone.utc)
            session.add(receipt)
        else:
            new_questions.append(
                ClarificationQuestion(
                    receipt_document_id=receipt.id,
                    user_id=question.user_id,
                    question_key="local_amount_retry",
                    question_text="I could not parse that amount. Please send it like 419.58 TRY.",
                )
            )

    elif receipt and question.question_key == "local_amount_retry":
        parsed = _parse_amount(answer)
        if parsed is not None:
            receipt.extracted_local_amount = parsed
            receipt.extracted_currency = receipt.extracted_currency or "TRY"
            receipt.updated_at = datetime.now(timezone.utc)
            session.add(receipt)

    elif receipt and question.question_key == "supplier":
        receipt.extracted_supplier = answer.strip()
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    for new_question in new_questions:
        session.add(new_question)

    if receipt:
        still_missing = not receipt.extracted_date or receipt.extracted_local_amount is None or not receipt.extracted_supplier or not receipt.business_or_personal
        needs_business_context = receipt.business_or_personal == "Business" and (not receipt.business_reason or not receipt.attendees)
        receipt.needs_clarification = still_missing or needs_business_context or bool(new_questions)
        receipt.updated_at = datetime.now(timezone.utc)
        session.add(receipt)

    session.commit()
    for new_question in new_questions:
        session.refresh(new_question)
    return new_questions
