from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from sqlmodel import Session, select

from app.config import get_settings
from app.db import engine
from app.models import (
    AgentReceiptComparison,
    AgentReceiptRead,
    AgentReceiptReviewRun,
    AppUser,
    ClarificationQuestion,
    ReceiptDocument,
)
from app.services.receipt_extraction import ReceiptExtraction
from app.services import telegram as telegram_service
from app.services import telegram_receipt_reply


VERIFY_ROOT = Path(tempfile.gettempdir()) / "expense_telegram_receipt_reply_tests"
VERIFY_ROOT.mkdir(parents=True, exist_ok=True)
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")
os.environ.setdefault("EXPENSE_STORAGE_ROOT", str(VERIFY_ROOT))


class _FakeTelegramClient:
    def __init__(self, token: str | None = None):
        self.token = token
        self.messages: list[str] = []

    def send_message(self, chat_id: int, text: str) -> None:
        self.messages.append(text)

    def download_file(self, file_id: str, user_id: int | None, fallback_name: str) -> Path:
        target = VERIFY_ROOT / f"{uuid4().hex}.jpg"
        target.write_bytes(b"\x00\x01\x02")
        return target


def _photo_payload(telegram_user_id: int = 41001, chat_id: int = 51001) -> dict:
    return {
        "message": {
            "message_id": 9001,
            "from": {"id": telegram_user_id, "first_name": "Op"},
            "chat": {"id": chat_id},
            "photo": [{"file_id": "receipt-file", "file_unique_id": str(uuid4()), "file_size": 2048}],
        }
    }


def _receipt(
    *,
    business_or_personal: str | None = "Business",
    business_reason: str | None = None,
    attendees: str | None = None,
    report_bucket: str | None = "Meals/Snacks",
    storage_path: str = "/var/lib/dcexpense/private/receipt.jpg",
) -> ReceiptDocument:
    return ReceiptDocument(
        uploader_user_id=1,
        source="telegram",
        status="extracted",
        content_type="photo",
        original_file_name="telegram_photo_1.jpg",
        storage_path=storage_path,
        extracted_date=date(2025, 12, 27),
        extracted_supplier="Tiramisu Cup",
        extracted_local_amount=Decimal("300.0000"),
        extracted_currency="TRY",
        business_or_personal=business_or_personal,
        report_bucket=report_bucket,
        business_reason=business_reason,
        attendees=attendees,
        receipt_type="payment_receipt",
    )


def _install_fake_client(monkeypatch) -> _FakeTelegramClient:
    fake = _FakeTelegramClient("test-token")
    monkeypatch.setattr(telegram_service, "TelegramClient", lambda token: fake)
    return fake


def _patch_extraction(
    monkeypatch,
    *,
    business_or_personal="Business",
    business_reason=None,
    attendees=None,
    supplier="Tiramisu Cup",
    report_bucket="Meals/Snacks",
):
    def fake_apply_receipt_extraction(session: Session, receipt: ReceiptDocument):
        receipt.status = "extracted"
        receipt.extracted_date = date(2025, 12, 27)
        receipt.extracted_supplier = supplier
        receipt.extracted_local_amount = Decimal("300.0000")
        receipt.extracted_currency = "TRY"
        receipt.business_or_personal = business_or_personal
        receipt.report_bucket = report_bucket
        receipt.business_reason = business_reason
        receipt.attendees = attendees
        receipt.receipt_type = "payment_receipt"
        receipt.needs_clarification = business_or_personal == "Business"
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        return ReceiptExtraction(
            receipt_id=receipt.id or 0,
            status=receipt.status,
            extracted_date=receipt.extracted_date,
            extracted_supplier=receipt.extracted_supplier,
            extracted_local_amount=receipt.extracted_local_amount,
            extracted_currency=receipt.extracted_currency,
            business_or_personal=receipt.business_or_personal,
            receipt_type=receipt.receipt_type,
            confidence=1.0,
            missing_fields=["supplier"] if supplier is None else [],
        )

    monkeypatch.setattr(telegram_service, "apply_receipt_extraction", fake_apply_receipt_extraction)


def _set_reply_env(monkeypatch, *, enabled: bool, allowlist: str = "", live: bool = False) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("AI_TELEGRAM_REPLY_ALLOWLIST", allowlist)
    monkeypatch.setenv("AI_TELEGRAM_LIVE_MODEL_ENABLED", "true" if live else "false")
    get_settings.cache_clear()


def test_parse_telegram_allowlist_accepts_commas_and_spaces():
    assert telegram_receipt_reply.parse_telegram_allowlist(" 41, 42  43,,") == {41, 42, 43}


def test_gate_off_sends_no_ai_assisted_reply(monkeypatch):
    _set_reply_env(monkeypatch, enabled=False, allowlist="41001")
    fake = _install_fake_client(monkeypatch)
    _patch_extraction(monkeypatch, business_or_personal="Personal")

    with Session(engine) as session:
        result = telegram_service.handle_update(session, _photo_payload(telegram_user_id=41001))

    assert result["action"] == "receipt_captured"
    assert "Receipt received." not in "\n".join(fake.messages)
    assert fake.messages[-1] == "Receipt saved."


def test_user_not_allowlisted_sends_no_ai_assisted_reply(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="99999")
    fake = _install_fake_client(monkeypatch)
    _patch_extraction(monkeypatch, business_or_personal="Personal")

    with Session(engine) as session:
        result = telegram_service.handle_update(session, _photo_payload(telegram_user_id=41001))

    assert result["action"] == "receipt_captured"
    assert "Receipt received." not in "\n".join(fake.messages)
    assert fake.messages[-1] == "Receipt saved."


def test_gate_on_allowlisted_sends_deterministic_receipt_summary(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001")
    fake = _install_fake_client(monkeypatch)
    _patch_extraction(monkeypatch, business_or_personal="Personal")

    with Session(engine) as session:
        result = telegram_service.handle_update(session, _photo_payload(telegram_user_id=41001))

    assert result["action"] == "receipt_captured"
    reply = fake.messages[-1]
    assert reply.startswith("Receipt received.")
    assert "Supplier: Tiramisu Cup" in reply
    assert "Date: 2025-12-27" in reply
    assert "Amount: 300.00 TRY" in reply


def test_non_meal_missing_business_reason_reply_does_not_ask_for_business_purpose():
    reply = telegram_receipt_reply.build_telegram_receipt_reply(
        _receipt(
            business_reason=None,
            attendees="Hakan",
            report_bucket="Auto Gasoline",
            storage_path="/var/lib/dcexpense/private/fuel.jpg",
        )
    )
    assert reply is not None
    assert "business purpose" not in reply.lower()
    assert "project, customer, or trip" not in reply.lower()


def test_meal_reply_copy_still_does_not_inline_attendee_prompt():
    reply = telegram_receipt_reply.build_telegram_receipt_reply(
        _receipt(business_reason="Customer visit", attendees=None, report_bucket="Lunch")
    )
    assert reply is not None
    assert "attendees" not in reply.lower()


def test_ai_receipt_reply_upload_does_not_seed_business_context_questions_for_non_meal(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001")
    fake = _install_fake_client(monkeypatch)
    _patch_extraction(
        monkeypatch,
        business_or_personal="Business",
        business_reason=None,
        attendees=None,
        supplier="KAPTANLAR TURIZM PAZ.",
        report_bucket="Auto Gasoline",
    )

    with Session(engine) as session:
        result = telegram_service.handle_update(session, _photo_payload(telegram_user_id=41001))
        questions = session.exec(select(ClarificationQuestion)).all()
        receipt = session.get(ReceiptDocument, result["receipt_id"])

    assert result["action"] == "receipt_captured"
    assert result["ai_receipt_reply_sent"] is True
    assert questions == []
    assert receipt is not None
    assert receipt.needs_clarification is False
    reply = fake.messages[-1]
    assert "Receipt received." in reply
    assert "business purpose" not in reply.lower()
    assert "attendees" not in reply.lower()


def test_ai_receipt_reply_upload_seeds_business_context_questions_for_meal(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001")
    fake = _install_fake_client(monkeypatch)
    _patch_extraction(
        monkeypatch,
        business_or_personal="Business",
        business_reason=None,
        attendees=None,
        supplier="BOSNAK DONER SERBAY",
        report_bucket=None,
    )

    with Session(engine) as session:
        result = telegram_service.handle_update(session, _photo_payload(telegram_user_id=41001))
        questions = session.exec(select(ClarificationQuestion).order_by(ClarificationQuestion.id)).all()
        receipt = session.get(ReceiptDocument, result["receipt_id"])

    assert result["action"] == "receipt_captured"
    assert result["ai_receipt_reply_sent"] is True
    assert [question.question_key for question in questions] == ["business_reason", "attendees"]
    assert receipt is not None
    assert receipt.needs_clarification is True
    assert fake.messages[-2].startswith("Receipt received.")
    assert fake.messages[-1] == "What project, customer, or trip should this receipt be attached to?"


def test_ai_receipt_reply_meal_context_flow_advances_on_latest_receipt(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001")
    fake = _install_fake_client(monkeypatch)

    with Session(engine) as session:
        user = AppUser(telegram_user_id=41001, display_name="Op")
        session.add(user)
        session.commit()
        session.refresh(user)
        receipt = _receipt(
            business_reason=None,
            attendees=None,
            report_bucket=None,
        )
        receipt.extracted_supplier = "BOSNAK DÖNER SERBAY"
        receipt.uploader_user_id = user.id
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        session.add(
            ClarificationQuestion(
                receipt_document_id=receipt.id,
                user_id=user.id,
                question_key="business_reason",
                question_text="What project, customer, or trip should this receipt be attached to?",
            )
        )
        session.add(
            ClarificationQuestion(
                receipt_document_id=receipt.id,
                user_id=user.id,
                question_key="attendees",
                question_text="Who attended or benefited from this expense? If not applicable, reply 'N/A'.",
            )
        )
        session.commit()

    payload = {
        "message": {
            "message_id": 9004,
            "from": {"id": 41001, "first_name": "Op"},
            "chat": {"id": 51001},
            "text": "Lunch with MRS",
        }
    }
    with Session(engine) as session:
        result = telegram_service.handle_update(session, payload)
        receipt_row = session.exec(select(ReceiptDocument)).one()
        questions = session.exec(select(ClarificationQuestion).order_by(ClarificationQuestion.id)).all()

    assert result["action"] == "answered_clarification"
    assert receipt_row.business_reason == "Lunch with MRS"
    assert questions[0].status == "answered"
    assert questions[1].status == "open"
    assert fake.messages[-1] == "Who attended or benefited from this expense? If not applicable, reply 'N/A'."


def test_ai_receipt_reply_text_ignores_stale_business_context_questions(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001")
    fake = _install_fake_client(monkeypatch)

    with Session(engine) as session:
        user = AppUser(telegram_user_id=41001, display_name="Op")
        session.add(user)
        session.commit()
        session.refresh(user)
        receipt = _receipt(business_reason=None, attendees=None, report_bucket="Auto Gasoline")
        receipt.extracted_supplier = "SHELL PETROL"
        receipt.uploader_user_id = user.id
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        session.add(
            ClarificationQuestion(
                receipt_document_id=receipt.id,
                user_id=user.id,
                question_key="business_reason",
                question_text="What project, customer, or trip should this receipt be attached to?",
            )
        )
        session.add(
            ClarificationQuestion(
                receipt_document_id=receipt.id,
                user_id=user.id,
                question_key="attendees",
                question_text="Who attended or benefited from this expense? If not applicable, reply 'N/A'.",
            )
        )
        session.commit()

    payload = {
        "message": {
            "message_id": 9002,
            "from": {"id": 41001, "first_name": "Op"},
            "chat": {"id": 51001},
            "text": "MRS",
        }
    }
    with Session(engine) as session:
        result = telegram_service.handle_update(session, payload)
        questions = session.exec(select(ClarificationQuestion).order_by(ClarificationQuestion.id)).all()

    assert result["action"] == "text_acknowledged"
    assert fake.messages[-1] == "Send me a receipt photo/PDF or a Diners statement, and I will file it for review."
    assert [question.status for question in questions] == ["open", "open"]
    assert all(question.answer_text is None for question in questions)


def test_ai_receipt_reply_text_ignores_stale_ocr_question_from_older_receipt(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001")
    fake = _install_fake_client(monkeypatch)

    with Session(engine) as session:
        user = AppUser(telegram_user_id=41001, display_name="Op")
        session.add(user)
        session.commit()
        session.refresh(user)

        old_receipt = _receipt(business_reason=None, attendees=None)
        old_receipt.uploader_user_id = user.id
        old_receipt.extracted_supplier = None
        session.add(old_receipt)
        session.commit()
        session.refresh(old_receipt)
        old_receipt_id = old_receipt.id
        session.add(
            ClarificationQuestion(
                receipt_document_id=old_receipt_id,
                user_id=user.id,
                question_key="supplier",
                question_text="I could not read the merchant name. Which store, restaurant, or vendor is this?",
            )
        )

        latest_receipt = _receipt(business_reason=None, attendees=None)
        latest_receipt.uploader_user_id = user.id
        session.add(latest_receipt)
        session.commit()

    payload = {
        "message": {
            "message_id": 9003,
            "from": {"id": 41001, "first_name": "Op"},
            "chat": {"id": 51001},
            "text": "MRS",
        }
    }
    with Session(engine) as session:
        result = telegram_service.handle_update(session, payload)
        question = session.exec(select(ClarificationQuestion)).one()
        old_receipt_row = session.get(ReceiptDocument, old_receipt_id)

    assert result["action"] == "text_acknowledged"
    assert fake.messages[-1] == "Send me a receipt photo/PDF or a Diners statement, and I will file it for review."
    assert question.status == "open"
    assert question.answer_text is None
    assert old_receipt_row is not None
    assert old_receipt_row.extracted_supplier is None


def test_ai_receipt_reply_still_asks_critical_ocr_questions(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001")
    fake = _install_fake_client(monkeypatch)
    _patch_extraction(monkeypatch, business_or_personal="Business", supplier=None, report_bucket=None)

    with Session(engine) as session:
        result = telegram_service.handle_update(session, _photo_payload(telegram_user_id=41001))
        questions = session.exec(select(ClarificationQuestion)).all()

    assert result["ai_receipt_reply_sent"] is True
    assert [question.question_key for question in questions] == ["supplier"]
    assert fake.messages[-2].startswith("Receipt received.")
    assert fake.messages[-1] == "I could not read the merchant name. Which store, restaurant, or vendor is this?"


def test_ai_receipt_reply_duplicate_ignores_stale_business_context_questions(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001")
    fake = _install_fake_client(monkeypatch)
    file_unique_id = str(uuid4())
    payload = _photo_payload(telegram_user_id=41001)
    payload["message"]["photo"][0]["file_unique_id"] = file_unique_id

    with Session(engine) as session:
        user = AppUser(telegram_user_id=41001, display_name="Op")
        session.add(user)
        session.commit()
        session.refresh(user)
        receipt = _receipt(business_reason=None, attendees=None)
        receipt.uploader_user_id = user.id
        receipt.telegram_file_unique_id = file_unique_id
        receipt.status = "extracted"
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        session.add(
            ClarificationQuestion(
                receipt_document_id=receipt.id,
                user_id=user.id,
                question_key="business_reason",
                question_text="What project, customer, or trip should this receipt be attached to?",
            )
        )
        session.add(
            ClarificationQuestion(
                receipt_document_id=receipt.id,
                user_id=user.id,
                question_key="attendees",
                question_text="Who attended or benefited from this expense? If not applicable, reply 'N/A'.",
            )
        )
        session.commit()

    with Session(engine) as session:
        result = telegram_service.handle_update(session, payload)

    assert result["action"] == "receipt_duplicate"
    assert fake.messages[-1] == "Receipt saved."


def test_ai_advisory_result_included_uses_advisory_only_copy():
    reply = telegram_receipt_reply.build_telegram_receipt_reply(
        _receipt(business_reason="Customer visit", attendees="Hakan"),
        ai_review={"status": "warn"},
    )
    assert reply is not None
    assert "AI second read is advisory only." in reply


def test_reply_never_contains_forbidden_phrases_or_private_fields():
    reply = telegram_receipt_reply.build_telegram_receipt_reply(
        _receipt(storage_path="/var/lib/dcexpense/secret/receipt.jpg"),
        ai_review={"status": "block", "prompt_text": "hidden", "raw_model_json": {"x": 1}},
    )
    assert reply is not None
    forbidden = (
        "AI approved",
        "AI rejected",
        "report blocked by AI",
        "storage_path",
        "receipt_path",
        "prompt_text",
        "raw_model_json",
        "model_debug",
        "/var/lib/dcexpense",
        "OPENAI_API_KEY",
    )
    for text in forbidden:
        assert text.lower() not in reply.lower()


def test_live_model_provider_is_mocked_and_writes_only_agentdb(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001", live=True)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    calls: list[int] = []

    def fake_live(**kwargs):
        calls.append(kwargs["receipt"].id)
        return telegram_receipt_reply.agent_receipt_live_provider.LiveAgentReceiptReviewResult(
            agent_payload={
                "merchant_name": "Different AI Supplier",
                "merchant_address": None,
                "receipt_date": "2025-12-27",
                "receipt_time": None,
                "total_amount": "999.99",
                "currency": "TRY",
                "amount_text": "999.99 TRY",
                "line_items": [],
                "tax_amount": None,
                "payment_method": None,
                "receipt_category": "payment_receipt",
                "confidence": 0.91,
                "raw_text_summary": "mocked live result",
            },
            raw_response_json=json.dumps({"amount": "999.99"}),
            prompt_text="hidden prompt",
            model_name="gpt-live-test",
        )

    monkeypatch.setattr(
        telegram_receipt_reply.agent_receipt_live_provider,
        "call_live_agent_receipt_review",
        fake_live,
    )

    with Session(engine) as session:
        user = AppUser(telegram_user_id=41001, display_name="Op")
        session.add(user)
        session.commit()
        session.refresh(user)
        receipt = _receipt(business_reason="Customer visit", attendees="Hakan")
        receipt.uploader_user_id = user.id
        session.add(receipt)
        session.commit()
        session.refresh(receipt)
        before = receipt.model_dump()

        sent = telegram_receipt_reply.maybe_send_telegram_receipt_reply(
            session,
            _FakeTelegramClient("test-token"),
            settings=get_settings(),
            receipt=receipt,
            telegram_user_id=41001,
            chat_id=51001,
        )
        session.refresh(receipt)
        after = receipt.model_dump()

        assert sent is True
        assert calls == [receipt.id]
        assert before == after
        assert len(session.exec(select(AgentReceiptReviewRun)).all()) == 1
        assert len(session.exec(select(AgentReceiptRead)).all()) == 1
        assert len(session.exec(select(AgentReceiptComparison)).all()) == 1


def test_live_model_failure_falls_back_to_deterministic_reply(monkeypatch):
    _set_reply_env(monkeypatch, enabled=True, allowlist="41001", live=True)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    def fail_live(**kwargs):
        raise telegram_receipt_reply.agent_receipt_live_provider.LiveAgentReceiptProviderError("model unavailable")

    monkeypatch.setattr(
        telegram_receipt_reply.agent_receipt_live_provider,
        "call_live_agent_receipt_review",
        fail_live,
    )
    client = _FakeTelegramClient("test-token")
    with Session(engine) as session:
        user = AppUser(telegram_user_id=41001, display_name="Op")
        session.add(user)
        session.commit()
        session.refresh(user)
        receipt = _receipt(business_reason="Customer visit", attendees="Hakan")
        receipt.uploader_user_id = user.id
        session.add(receipt)
        session.commit()
        session.refresh(receipt)

        sent = telegram_receipt_reply.maybe_send_telegram_receipt_reply(
            session,
            client,
            settings=get_settings(),
            receipt=receipt,
            telegram_user_id=41001,
            chat_id=51001,
        )

        assert sent is True
        assert "AI second read is advisory only." not in client.messages[-1]
        assert session.exec(select(AgentReceiptReviewRun)).all() == []


def test_tests_do_not_import_telegram_network_or_live_model_modules_when_gate_disabled(monkeypatch):
    _set_reply_env(monkeypatch, enabled=False)
    before = set(sys.modules)

    reply = telegram_receipt_reply.build_telegram_receipt_reply(_receipt(business_or_personal="Personal"))

    newly_loaded = set(sys.modules) - before
    assert reply is not None
    assert "openai" not in newly_loaded
    assert "anthropic" not in newly_loaded
    assert "deepseek" not in newly_loaded
