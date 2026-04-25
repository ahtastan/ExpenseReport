"""Tests for the central Decimal-aware JSON encoder/decoder (M1 Day 2.5).

This module is the single source of truth for how money/rate values cross
the JSON boundary. If these tests pass, every adopting site inherits the
right behavior.
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.json_utils import DecimalEncoder, decode_decimal, dumps


# ---------------------------------------------------------------------------
# Encoder
# ---------------------------------------------------------------------------


def test_encoder_emits_decimal_as_string():
    assert dumps(Decimal("4.20")) == '"4.20"'


def test_encoder_preserves_trailing_zeros():
    # Quantization at write time produces e.g. Decimal("4.2000"); the
    # serialized form must keep those zeros so the value reads back identical.
    assert dumps(Decimal("4.2000")) == '"4.2000"'
    assert dumps(Decimal("0.00000001")) == '"0.00000001"'


def test_encoder_handles_nested_structures():
    payload = {
        "local_amount": Decimal("123.4500"),
        "usd_amount": Decimal("4.1234"),
        "currency": "TRY",
        "lines": [
            {"amount": Decimal("10.0000")},
            {"amount": Decimal("20.5000")},
        ],
    }
    encoded = dumps(payload)
    decoded = json.loads(encoded)
    assert decoded["local_amount"] == "123.4500"
    assert decoded["usd_amount"] == "4.1234"
    assert decoded["currency"] == "TRY"
    assert decoded["lines"][0]["amount"] == "10.0000"
    assert decoded["lines"][1]["amount"] == "20.5000"


def test_encoder_passes_through_non_decimal_scalars():
    payload = {"int": 1, "float": 1.5, "str": "x", "bool": True, "null": None}
    encoded = dumps(payload)
    assert json.loads(encoded) == payload


def test_encoder_works_via_cls_kwarg():
    # Sanity check: callers that prefer json.dumps directly with cls=
    # get the same behavior.
    assert json.dumps(Decimal("4.20"), cls=DecimalEncoder) == '"4.20"'


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------


def test_decode_none_returns_none():
    assert decode_decimal(None) is None


def test_decode_string_exact():
    assert decode_decimal("4.2000") == Decimal("4.2000")


def test_decode_string_preserves_trailing_zeros():
    # Decimal("4.2000") and Decimal("4.2") compare equal but are not
    # identical in representation; the decoder must keep the source string's
    # precision so quantization downstream sees the right exponent.
    assert str(decode_decimal("4.2000")) == "4.2000"


def test_decode_int():
    assert decode_decimal(42) == Decimal(42)
    assert decode_decimal(0) == Decimal(0)


def test_decode_float_via_str_no_binary_noise():
    # The whole point: Decimal(0.1) is 0.1000000000000000055511151231257827...
    # whereas Decimal("0.1") is exactly 0.1. The decoder must take the
    # string route for floats so legacy float-shaped JSON doesn't poison
    # downstream arithmetic.
    assert decode_decimal(0.1) == Decimal("0.1")
    assert decode_decimal(123.45) == Decimal("123.45")


def test_decode_existing_decimal_is_idempotent():
    d = Decimal("7.7777")
    assert decode_decimal(d) is d


def test_decode_bool_rejected():
    # bool is an int subclass; without an explicit guard True would silently
    # become Decimal("1"), which would mask data-quality bugs.
    with pytest.raises(TypeError):
        decode_decimal(True)
    with pytest.raises(TypeError):
        decode_decimal(False)


def test_decode_unsupported_type_raises():
    with pytest.raises(TypeError):
        decode_decimal([1, 2])
    with pytest.raises(TypeError):
        decode_decimal({"a": 1})


# ---------------------------------------------------------------------------
# Round-trip — the load-bearing assertion for the migration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        Decimal("0.0001"),
        Decimal("4.2000"),
        Decimal("123.4567"),
        Decimal("99999999999999.9999"),  # 14 + 4 = 18 digits, Numeric(18,4) max
        Decimal("0.00000001"),  # 8 dp rate precision
        Decimal("12345678.12345678"),  # rate, 8 dp
    ],
)
def test_roundtrip_preserves_precision(value):
    encoded = dumps({"x": value})
    raw = json.loads(encoded)
    decoded = decode_decimal(raw["x"])
    assert decoded == value
    assert str(decoded) == str(value)


def test_roundtrip_tolerates_legacy_float_shaped_blob():
    # Pre-migration ReviewRow.source_json blobs were written with raw float
    # values. The new reader must accept them and produce the same Decimal
    # it would have produced from a string-shaped blob, so we don't have to
    # backfill the existing 13 receipts.
    legacy_blob = '{"local_amount": 4.2, "usd_amount": 0.15}'
    parsed = json.loads(legacy_blob)
    assert decode_decimal(parsed["local_amount"]) == Decimal("4.2")
    assert decode_decimal(parsed["usd_amount"]) == Decimal("0.15")


def test_roundtrip_mixed_legacy_and_new_in_same_blob():
    # A blob written half before / half after the migration (shouldn't
    # actually happen, but the tolerant parser makes it harmless).
    mixed = '{"old": 4.2, "new": "4.2000"}'
    parsed = json.loads(mixed)
    assert decode_decimal(parsed["old"]) == Decimal("4.2")
    assert decode_decimal(parsed["new"]) == Decimal("4.2000")
