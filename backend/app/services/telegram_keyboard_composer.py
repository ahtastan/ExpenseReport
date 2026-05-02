"""F-AI-Stage1 sub-PR 3 + PR4: inline-keyboard composition + Edit menu.

PR3 introduced the top-level keyboard with three buttons:
  ``[✅ Confirm] [✏️ Edit] [❌ Cancel]``
encoded as ``fai1:<action>:<user_response_id>``.

PR4 replaces the text-parse Edit fallback with a button-driven Edit menu.
New callback prefix ``fai1m`` for menu navigation:
  ``fai1m:<scope>:<choice>:<user_response_id>``
where ``scope`` selects which sub-menu the button lives in:

* ``edit``  - top-level Edit menu (choice in {receipt, category, type, back})
* ``rcpt``  - Receipt info quick-pick (choice in {supplier, date, amount, back})
* ``cat1``  - Tier 1 category picker (choice = numeric index or 'back')
* ``cat2``  - Tier 2 bucket picker (choice = global bucket index or 'back')
* ``type``  - Type toggle (choice in {personal, back})
* ``skip_ra`` - one-button "skip reason/attendees" (no choice; uses
                ``fai1m:skip_ra::<user_response_id>``)

Indices for cat1/cat2 are positions in
``app.category_vocab.categories()`` and ``app.category_vocab.all_buckets()``,
respectively. Using indices keeps callback_data well under Telegram's
64-byte limit even for long bucket names like 'Membership/Subscription
Fees'.

Reference: docs/F-AI-Stage1-Telegram-Inline-Keyboard.md §5.3
"""
from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

from app.models import AgentReceiptRead, ReceiptDocument

logger = logging.getLogger(__name__)

CALLBACK_DATA_PREFIX = "fai1"
CALLBACK_ACTIONS = ("confirm", "edit", "cancel")
_CALLBACK_DATA_MAX_BYTES = 64
_BUSINESS_REASON_DISPLAY_LIMIT = 200

# PR4: menu-navigation callback data.
MENU_CALLBACK_DATA_PREFIX = "fai1m"
MENU_SCOPES = ("edit", "rcpt", "cat1", "cat2", "type", "skip_ra")
EDIT_MENU_CHOICES = ("receipt", "category", "type", "back")
RECEIPT_MENU_CHOICES = ("supplier", "date", "amount", "back")
TYPE_MENU_CHOICES = ("personal", "back")


def build_inline_keyboard_reply(
    receipt: ReceiptDocument,
    agent_read: AgentReceiptRead,
    user_response_id: int,
) -> dict[str, Any]:
    """Build the Telegram ``sendMessage`` payload (text + reply_markup).

    Args:
        receipt: the canonical receipt the keyboard is anchored to.
        agent_read: the ``agent_receipt_read`` row whose ``suggested_*``
            columns hold the AI proposal.
        user_response_id: id of the ``agent_receipt_user_response`` row
            (must already exist with ``user_action='pending'``).

    Returns:
        ``{"text": ..., "reply_markup": {...}}`` ready to be passed as
        the body of a Telegram ``sendMessage`` call (the bot client will
        ``json.dumps`` ``reply_markup`` per Telegram's wire format).
    """
    text = _build_message_body(receipt, agent_read)
    reply_markup = _build_reply_markup(user_response_id)
    return {"text": text, "reply_markup": reply_markup}


def parse_callback_data(data: str | None) -> tuple[str, int] | None:
    """Top-level button parser.

    Returns ``(action, user_response_id)`` for ``fai1:<action>:<id>`` where
    ``action`` is one of :data:`CALLBACK_ACTIONS`. Returns ``None`` for
    malformed data, an unknown action, or any callback that uses the
    menu-navigation prefix :data:`MENU_CALLBACK_DATA_PREFIX` (those go
    through :func:`parse_menu_callback_data` instead).
    """
    if not isinstance(data, str) or not data:
        return None
    parts = data.split(":")
    if len(parts) != 3:
        return None
    prefix, action, raw_id = parts
    if prefix != CALLBACK_DATA_PREFIX:
        return None
    if action not in CALLBACK_ACTIONS:
        return None
    try:
        user_response_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return action, user_response_id


def parse_menu_callback_data(data: str | None) -> tuple[str, str, int] | None:
    """Menu-navigation button parser.

    Format: ``fai1m:<scope>:<choice>:<user_response_id>`` where ``scope`` is
    one of :data:`MENU_SCOPES`. ``choice`` is a free-form string the caller
    interprets per-scope (e.g. category index, ``"back"``, ``"personal"``).
    For the ``skip_ra`` scope the choice slot is empty (one button, no
    sub-choice).

    Returns ``(scope, choice, user_response_id)`` or ``None`` on any
    malformed input. ``choice`` may be the empty string for ``skip_ra``.
    """
    if not isinstance(data, str) or not data:
        return None
    parts = data.split(":")
    if len(parts) != 4:
        return None
    prefix, scope, choice, raw_id = parts
    if prefix != MENU_CALLBACK_DATA_PREFIX:
        return None
    if scope not in MENU_SCOPES:
        return None
    try:
        user_response_id = int(raw_id)
    except (TypeError, ValueError):
        return None
    return scope, choice, user_response_id


def build_menu_callback_data(scope: str, choice: str, user_response_id: int) -> str:
    """Construct ``callback_data`` for one menu button. Validates length."""
    if scope not in MENU_SCOPES:
        raise ValueError(f"unknown menu scope: {scope!r}")
    data = f"{MENU_CALLBACK_DATA_PREFIX}:{scope}:{choice}:{user_response_id}"
    if len(data.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
        raise ValueError(
            f"callback_data exceeds Telegram's 64-byte limit: {len(data)} chars"
        )
    return data


def build_edit_menu_markup(
    user_response_id: int,
    *,
    include_type_button: bool,
) -> dict[str, Any]:
    """Top-level Edit menu: Receipt info / Category / Type (allowlist) / Back."""
    row1: list[dict[str, str]] = [
        {
            "text": "📝 Receipt info",
            "callback_data": build_menu_callback_data("edit", "receipt", user_response_id),
        },
        {
            "text": "🏷 Category",
            "callback_data": build_menu_callback_data("edit", "category", user_response_id),
        },
    ]
    if include_type_button:
        row1.append(
            {
                "text": "🔄 Type",
                "callback_data": build_menu_callback_data("edit", "type", user_response_id),
            }
        )
    row2: list[dict[str, str]] = [
        {
            "text": "⬅ Back",
            "callback_data": build_menu_callback_data("edit", "back", user_response_id),
        }
    ]
    return {"inline_keyboard": [row1, row2]}


def build_receipt_menu_markup(user_response_id: int) -> dict[str, Any]:
    """Receipt info quick-pick: Supplier / Date / Amount / Back."""
    row1 = [
        {
            "text": "🏪 Supplier",
            "callback_data": build_menu_callback_data("rcpt", "supplier", user_response_id),
        },
        {
            "text": "📅 Date",
            "callback_data": build_menu_callback_data("rcpt", "date", user_response_id),
        },
        {
            "text": "💵 Amount",
            "callback_data": build_menu_callback_data("rcpt", "amount", user_response_id),
        },
    ]
    row2 = [
        {
            "text": "⬅ Back",
            "callback_data": build_menu_callback_data("rcpt", "back", user_response_id),
        }
    ]
    return {"inline_keyboard": [row1, row2]}


def build_category_tier1_markup(user_response_id: int) -> dict[str, Any]:
    """Category Tier 1 picker. Reads from ``category_vocab.categories()``.

    Buttons are sized to fit one or two per row; each is labelled with the
    Tier 1 name (with a small emoji prefix for scanability). Callback data
    encodes the index to keep the payload short.
    """
    from app.category_vocab import categories  # local import: tight cycle-free

    emoji_map = {
        "Hotel & Travel": "🏨",
        "Meals & Entertainment": "🍽",
        "Air Travel": "✈",
        "Other": "📦",
    }
    cats = categories()
    rows: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for idx, name in enumerate(cats):
        emoji = emoji_map.get(name, "•")
        current.append(
            {
                "text": f"{emoji} {name}",
                "callback_data": build_menu_callback_data("cat1", str(idx), user_response_id),
            }
        )
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append(
        [
            {
                "text": "⬅ Back",
                "callback_data": build_menu_callback_data("cat1", "back", user_response_id),
            }
        ]
    )
    return {"inline_keyboard": rows}


def build_category_tier2_markup(
    user_response_id: int, category: str
) -> dict[str, Any]:
    """Category Tier 2 picker for a given Tier 1 ``category``. Reads from
    ``category_vocab.buckets_for(category)`` for the labels and from
    ``category_vocab.all_buckets()`` for the global index used in
    callback_data."""
    from app.category_vocab import all_buckets, buckets_for  # local import

    labels = buckets_for(category)
    if not labels:
        raise ValueError(f"unknown or empty category for tier 2: {category!r}")
    global_buckets = all_buckets()
    rows: list[list[dict[str, str]]] = []
    current: list[dict[str, str]] = []
    for label in labels:
        try:
            global_idx = global_buckets.index(label)
        except ValueError:  # pragma: no cover — drift detector should catch
            raise RuntimeError(
                f"bucket {label!r} listed under {category!r} but missing from all_buckets()"
            )
        current.append(
            {
                "text": label,
                "callback_data": build_menu_callback_data(
                    "cat2", str(global_idx), user_response_id
                ),
            }
        )
        if len(current) == 2:
            rows.append(current)
            current = []
    if current:
        rows.append(current)
    rows.append(
        [
            {
                "text": "⬅ Back",
                "callback_data": build_menu_callback_data("cat2", "back", user_response_id),
            }
        ]
    )
    return {"inline_keyboard": rows}


def build_type_menu_markup(user_response_id: int) -> dict[str, Any]:
    """Type toggle (allowlist-only): Mark Personal / Back."""
    row = [
        {
            "text": "Mark Personal",
            "callback_data": build_menu_callback_data("type", "personal", user_response_id),
        },
        {
            "text": "⬅ Back",
            "callback_data": build_menu_callback_data("type", "back", user_response_id),
        },
    ]
    return {"inline_keyboard": [row]}


def build_skip_reason_attendees_markup(user_response_id: int) -> dict[str, Any]:
    """Single 'Skip for now' button shown after a Meals bucket is committed."""
    row = [
        {
            "text": "⏭ Skip for now",
            "callback_data": build_menu_callback_data("skip_ra", "", user_response_id),
        }
    ]
    return {"inline_keyboard": [row]}


def _build_message_body(receipt: ReceiptDocument, agent_read: AgentReceiptRead) -> str:
    lines: list[str] = ["Receipt received.", ""]
    read_lines = _canonical_read_lines(receipt)
    if read_lines:
        lines.append("I read:")
        lines.extend(read_lines)
        lines.append("")
    suggestion_lines = _suggestion_lines(agent_read)
    if suggestion_lines:
        lines.append("AI suggests:")
        lines.extend(suggestion_lines)
    # Trim trailing blank lines.
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _canonical_read_lines(receipt: ReceiptDocument) -> list[str]:
    lines: list[str] = []
    if receipt.extracted_supplier:
        lines.append(f"Supplier: {receipt.extracted_supplier}")
    if receipt.extracted_date is not None:
        lines.append(f"Date: {receipt.extracted_date.isoformat()}")
    amount_str = _format_amount(receipt.extracted_local_amount, receipt.extracted_currency)
    if amount_str:
        lines.append(f"Amount: {amount_str}")
    return lines


def _suggestion_lines(agent_read: AgentReceiptRead) -> list[str]:
    lines: list[str] = []
    if agent_read.suggested_business_or_personal:
        lines.append(f"Type: {agent_read.suggested_business_or_personal}")
    if agent_read.suggested_report_bucket:
        lines.append(f"Bucket: {agent_read.suggested_report_bucket}")
    attendees = _decode_attendees(agent_read.suggested_attendees_json)
    if attendees:
        lines.append("Attendees: " + " + ".join(attendees))
    if agent_read.suggested_customer:
        lines.append(f"Customer: {agent_read.suggested_customer}")
    if agent_read.suggested_business_reason:
        reason = agent_read.suggested_business_reason
        if len(reason) > _BUSINESS_REASON_DISPLAY_LIMIT:
            reason = reason[: _BUSINESS_REASON_DISPLAY_LIMIT - 1].rstrip() + "…"
        lines.append(f"Reason: {reason}")
    return lines


def _decode_attendees(raw_json: str | None) -> list[str]:
    if not raw_json:
        return []
    try:
        decoded = json.loads(raw_json)
    except json.JSONDecodeError:
        logger.warning("invalid suggested_attendees_json: %r", raw_json[:200])
        return []
    if not isinstance(decoded, list):
        return []
    return [item.strip() for item in decoded if isinstance(item, str) and item.strip()]


def _format_amount(amount: Decimal | None, currency: str | None) -> str | None:
    if amount is None:
        return None
    quantized = amount.quantize(Decimal("0.01")) if isinstance(amount, Decimal) else amount
    text = format(quantized, "f") if isinstance(quantized, Decimal) else str(quantized)
    if currency:
        return f"{text} {currency}"
    return text


def _build_reply_markup(user_response_id: int) -> dict[str, Any]:
    buttons: list[dict[str, str]] = []
    for label, action in (
        ("✅ Confirm", "confirm"),
        ("✏️ Edit", "edit"),
        ("❌ Cancel", "cancel"),
    ):
        callback_data = build_callback_data(action, user_response_id)
        buttons.append({"text": label, "callback_data": callback_data})
    return {"inline_keyboard": [buttons]}


def build_callback_data(action: str, user_response_id: int) -> str:
    """Construct the ``callback_data`` string for one button.

    Defensive: actions outside :data:`CALLBACK_ACTIONS` raise — that is a
    programmer error, not user input.
    """
    if action not in CALLBACK_ACTIONS:
        raise ValueError(f"unknown inline-keyboard action: {action!r}")
    data = f"{CALLBACK_DATA_PREFIX}:{action}:{user_response_id}"
    if len(data.encode("utf-8")) > _CALLBACK_DATA_MAX_BYTES:
        raise ValueError(
            f"callback_data exceeds Telegram's 64-byte limit: {len(data)} chars"
        )
    return data
