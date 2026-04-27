from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from difflib import SequenceMatcher

from sqlmodel import Session, select

from app.models import MatchDecision, ReceiptDocument, StatementTransaction
from app.services import model_router


@dataclass(frozen=True)
class MatchScore:
    transaction: StatementTransaction
    score: float
    confidence: str
    reason: str


@dataclass
class MatchRunStats:
    receipts_considered: int = 0
    candidates_created: int = 0
    high_confidence: int = 0
    medium_confidence: int = 0
    low_confidence: int = 0
    auto_approved: int = 0
    skipped_receipts: int = 0
    llm_disambiguated: int = 0  # hard cases resolved by the matching model
    llm_abstained: int = 0  # hard cases where the model declined to pick
    llm_classification_calls: int = 0  # LLM bucket-classify calls made on approved matches
    bucket_auto_applied: int = 0  # LLM-suggested EDT buckets copied to receipt.report_bucket


def normalize_text(value: str | None) -> str:
    text = (value or "").upper()
    replacements = {
        "İ": "I",
        "İ": "I",
        "Ş": "S",
        "Ğ": "G",
        "Ü": "U",
        "Ö": "O",
        "Ç": "C",
        "ı": "I",
        "ş": "S",
        "ğ": "G",
        "ü": "U",
        "ö": "O",
        "ç": "C",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return " ".join(text.split())


def merchant_similarity(a: str | None, b: str | None) -> float:
    left = normalize_text(a)
    right = normalize_text(b)
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _date_gap(receipt_date: date | None, transaction_date: date | None) -> int | None:
    if not receipt_date or not transaction_date:
        return None
    return abs((receipt_date - transaction_date).days)


def score_receipt_against_transaction(
    receipt: ReceiptDocument,
    transaction: StatementTransaction,
) -> MatchScore | None:
    if receipt.extracted_local_amount is None or transaction.local_amount is None:
        return None

    amount_delta = abs(receipt.extracted_local_amount - transaction.local_amount)
    if amount_delta > 1.0:
        return None

    score = 0.0
    reasons: list[str] = []
    if amount_delta <= 0.01:
        score += 55
        reasons.append("exact local amount")
    elif amount_delta <= 0.15:
        score += 45
        reasons.append(f"near local amount delta={amount_delta:.2f}")
    else:
        score += 25
        reasons.append(f"loose local amount delta={amount_delta:.2f}")

    gap = _date_gap(receipt.extracted_date, transaction.transaction_date)
    if gap is None:
        reasons.append("missing receipt or statement date")
    elif gap == 0:
        score += 30
        reasons.append("same transaction date")
    elif gap <= 1:
        score += 20
        reasons.append(f"date within {gap} day")
    elif gap <= 3:
        score += 10
        reasons.append(f"date within {gap} days")
    else:
        score -= 15
        reasons.append(f"date gap {gap} days")

    similarity = merchant_similarity(receipt.extracted_supplier, transaction.supplier_raw)
    if similarity >= 0.75:
        score += 15
        reasons.append(f"strong merchant similarity {similarity:.2f}")
    elif similarity >= 0.45:
        score += 8
        reasons.append(f"merchant similarity {similarity:.2f}")
    elif receipt.extracted_supplier:
        score -= 5
        reasons.append(f"weak merchant similarity {similarity:.2f}")

    if amount_delta <= 0.15 and gap == 0:
        confidence = "high"
    elif amount_delta <= 0.15 and gap is not None and gap <= 3:
        confidence = "medium"
    elif score >= 90:
        confidence = "high"
    elif score >= 70:
        confidence = "medium"
    else:
        confidence = "low"

    reason = "; ".join(reasons)
    return MatchScore(transaction=transaction, score=score, confidence=confidence, reason=reason)


def _existing_decision(
    session: Session,
    receipt_id: int,
    transaction_id: int,
) -> MatchDecision | None:
    return session.exec(
        select(MatchDecision).where(
            MatchDecision.receipt_document_id == receipt_id,
            MatchDecision.statement_transaction_id == transaction_id,
        )
    ).first()


def run_matching(
    session: Session,
    statement_import_id: int | None = None,
    receipt_id: int | None = None,
    auto_approve_high_confidence: bool = True,
) -> MatchRunStats:
    stats = MatchRunStats()
    receipt_query = select(ReceiptDocument)
    if receipt_id is not None:
        receipt_query = receipt_query.where(ReceiptDocument.id == receipt_id)
    receipts = session.exec(receipt_query).all()

    transaction_query = select(StatementTransaction)
    if statement_import_id is not None:
        transaction_query = transaction_query.where(StatementTransaction.statement_import_id == statement_import_id)
    transactions = session.exec(transaction_query).all()

    scores_by_receipt_id: dict[int, list[MatchScore]] = {}
    high_receipt_count_by_transaction_id: dict[int, int] = {}
    for receipt in receipts:
        if receipt.id is None or receipt.extracted_local_amount is None:
            continue
        scores = [
            score
            for transaction in transactions
            if (score := score_receipt_against_transaction(receipt, transaction)) is not None
        ]
        scores.sort(key=lambda item: item.score, reverse=True)
        scores_by_receipt_id[receipt.id] = scores
        for score in scores:
            if score.confidence == "high" and score.transaction.id is not None:
                high_receipt_count_by_transaction_id[score.transaction.id] = (
                    high_receipt_count_by_transaction_id.get(score.transaction.id, 0) + 1
                )

    now = datetime.now(timezone.utc)
    for receipt in receipts:
        if receipt.id is None:
            continue
        if receipt.extracted_local_amount is None:
            stats.skipped_receipts += 1
            continue
        stats.receipts_considered += 1
        scores = scores_by_receipt_id.get(receipt.id, [])
        if not scores:
            stats.skipped_receipts += 1
            continue

        high_scores = [score for score in scores if score.confidence == "high"]
        unique_high = len(high_scores) == 1

        # LLM disambiguation: when deterministic scoring cannot pick a unique
        # high candidate but there are multiple plausible candidates (high or
        # medium), ask the matching model. A confident pick promotes that
        # candidate to "high" and demotes the competing plausible picks to
        # "medium" so only one high remains. Low-confidence scores are left
        # untouched.
        promoted_transaction_id: int | None = None
        promoted_disambiguation: model_router.MatchDisambiguation | None = None
        if not unique_high:
            plausible_scores = [
                s for s in scores[:5] if s.confidence in {"high", "medium"}
            ]
            if len(plausible_scores) >= 2:
                candidates_payload = [
                    {
                        "transaction_id": s.transaction.id,
                        "supplier": s.transaction.supplier_raw,
                        "date": s.transaction.transaction_date.isoformat()
                        if s.transaction.transaction_date
                        else None,
                        "local_amount": s.transaction.local_amount,
                        "local_currency": s.transaction.local_currency,
                        "deterministic_reason": s.reason,
                    }
                    for s in plausible_scores
                    if s.transaction.id is not None
                ]
                receipt_payload = {
                    "supplier": receipt.extracted_supplier,
                    "date": receipt.extracted_date.isoformat()
                    if receipt.extracted_date
                    else None,
                    "local_amount": receipt.extracted_local_amount,
                    "local_currency": receipt.extracted_currency,
                }
                dis = model_router.match_disambiguate(receipt_payload, candidates_payload)
                if dis is not None and dis.transaction_id is not None and dis.confidence == "high":
                    promoted_transaction_id = dis.transaction_id
                    promoted_disambiguation = dis
                    new_scores: list[MatchScore] = []
                    for s in scores:
                        if s.transaction.id == dis.transaction_id:
                            new_scores.append(
                                replace(
                                    s,
                                    confidence="high",
                                    reason=f"{s.reason}; llm({dis.model}): {dis.reasoning}",
                                )
                            )
                        elif s.confidence == "high":
                            # Demote rival highs so the LLM's pick is the
                            # unique high for this receipt.
                            new_scores.append(
                                replace(
                                    s,
                                    confidence="medium",
                                    reason=f"{s.reason}; llm({dis.model}) preferred another candidate",
                                )
                            )
                        else:
                            new_scores.append(s)
                    scores = new_scores
                    scores_by_receipt_id[receipt.id] = new_scores
                    unique_high = True
                    stats.llm_disambiguated += 1
                else:
                    stats.llm_abstained += 1

        for score in scores[:5]:
            transaction = score.transaction
            if transaction.id is None:
                continue
            decision = _existing_decision(session, receipt.id, transaction.id)
            method = (
                "llm_disambiguated_v1"
                if transaction.id == promoted_transaction_id
                else "date_amount_merchant_v1"
            )
            llm_promoted_here = transaction.id == promoted_transaction_id
            # LLM bucket+category suggestion lives on the row that the LLM
            # actually picked (the promoted transaction's decision row).
            # Other decision rows under the same receipt — e.g. the demoted
            # rivals or the low-confidence alternates — get NULL because the
            # LLM's bucket guess is anchored to its chosen transaction.
            llm_suggested_bucket: str | None = None
            llm_suggested_category: str | None = None
            if llm_promoted_here and promoted_disambiguation is not None:
                llm_suggested_bucket = promoted_disambiguation.suggested_bucket
                llm_suggested_category = promoted_disambiguation.suggested_category

            if decision:
                decision.confidence = score.confidence
                decision.reason = score.reason
                decision.match_method = method
                decision.updated_at = now
                if llm_promoted_here:
                    decision.suggested_bucket = llm_suggested_bucket
                    decision.suggested_category = llm_suggested_category
            else:
                decision = MatchDecision(
                    receipt_document_id=receipt.id,
                    statement_transaction_id=transaction.id,
                    confidence=score.confidence,
                    match_method=method,
                    reason=score.reason,
                    suggested_bucket=llm_suggested_bucket,
                    suggested_category=llm_suggested_category,
                )
            unique_transaction_high = (
                transaction.id is not None
                and high_receipt_count_by_transaction_id.get(transaction.id, 0) == 1
            )
            # LLM-promoted picks should not auto-approve on transaction
            # uniqueness alone because ``high_receipt_count_by_transaction_id``
            # was computed before the promotion. Require deterministic
            # uniqueness OR a confident LLM pick for auto-approval.
            if auto_approve_high_confidence and score.confidence == "high" and unique_high and (
                unique_transaction_high or llm_promoted_here
            ):
                decision.approved = True
                decision.rejected = False
                stats.auto_approved += 1

                # Scope C: every approved match gets a bucket+category from
                # the LLM. If disambiguation already populated decision.suggested_bucket
                # (LLM-promoted path), reuse it. Otherwise (deterministic path),
                # call classify_match_bucket — a separate, classify-only call
                # that doesn't ask the model to pick among candidates.
                #
                # Latency: ~500ms-1.5s per call on the mini model. With ~10-15
                # approved matches per statement, this adds 5-20s to /matching/run.
                # Sync-in-handler is acceptable because: (a) the route is operator-
                # triggered (not user-facing), (b) the "Run Matching" toast in
                # PR #28 already covers this UX, (c) Caddy default timeout is
                # 3min — well above the worst case. If matchdecision counts grow
                # past ~50 per statement, revisit with a background-task pattern.
                if decision.suggested_bucket is None:
                    classification = model_router.classify_match_bucket(
                        receipt={
                            "supplier": receipt.extracted_supplier,
                            "date": receipt.extracted_date.isoformat()
                            if receipt.extracted_date
                            else None,
                            "local_amount": receipt.extracted_local_amount,
                            "local_currency": receipt.extracted_currency,
                            "business_or_personal": receipt.business_or_personal,
                            "receipt_type": receipt.receipt_type,
                            # Operator-supplied trip/customer context. The
                            # classifier prompt treats this as the primary
                            # signal — see _CLASSIFY_PROMPT in model_router.py.
                            # The supplier name alone often misleads (e.g. a
                            # gas station's mini-mart that's actually a snack
                            # stop, or a market entry that's a coffee meeting).
                            "business_reason": receipt.business_reason,
                            "attendees": receipt.attendees,
                        },
                        transaction={
                            "supplier": transaction.supplier_raw,
                            "date": transaction.transaction_date.isoformat()
                            if transaction.transaction_date
                            else None,
                            "local_amount": transaction.local_amount,
                            "local_currency": transaction.local_currency,
                        },
                    )
                    if classification is not None:
                        stats.llm_classification_calls += 1
                        decision.suggested_bucket = classification.bucket
                        decision.suggested_category = classification.category
                        # Append the classifier's reasoning to the audit reason
                        # so M3 UI can show "deterministic match; LLM bucket
                        # rationale: <text>" without a separate column.
                        if classification.reasoning:
                            decision.reason = (
                                f"{decision.reason}; "
                                f"classify({classification.model}): {classification.reasoning}"
                            )

                # Auto-apply the suggested bucket onto the receipt when
                # (a) we have a bucket (from disambiguation OR classification),
                # and (b) the receipt has no operator-set bucket. We never
                # clobber an existing report_bucket — operator wins always.
                #
                # NOTE: direct setattr on a tracked field. Day 3b PR-1 will
                # refactor through write_tracked_field with Source.LLM_MATCH
                # provenance. Until then the column write is untracked.
                if (
                    decision.suggested_bucket is not None
                    and receipt.report_bucket is None
                ):
                    receipt.report_bucket = decision.suggested_bucket
                    receipt.updated_at = now
                    session.add(receipt)
                    stats.bucket_auto_applied += 1
            session.add(decision)
            stats.candidates_created += 1
            if score.confidence == "high":
                stats.high_confidence += 1
            elif score.confidence == "medium":
                stats.medium_confidence += 1
            else:
                stats.low_confidence += 1

    session.commit()
    return stats
