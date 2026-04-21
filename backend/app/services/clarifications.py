from datetime import datetime, timezone

from sqlmodel import Session, select

from app.models import ClarificationQuestion, ReceiptDocument


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

    for new_question in new_questions:
        session.add(new_question)

    session.commit()
    for new_question in new_questions:
        session.refresh(new_question)
    return new_questions
