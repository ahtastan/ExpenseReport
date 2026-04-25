"""Decimal-aware JSON helpers for money/rate fields.

Money columns moved from float to Decimal in M1 Day 2.5. The serialized JSON
blobs in ReviewRow / ReviewSession (source_json, suggested_json,
confirmed_json, snapshot_json) and the API response bodies must round-trip
those values without precision loss. The convention enforced here is:

  Decimals serialize as JSON *strings*. On read, accept legacy float-shaped
  numbers as well so pre-migration blobs keep working without a backfill.

All JSON writes that may contain amount/rate values must go through
``dumps`` (or pass ``DecimalEncoder`` to ``json.dumps``). All reads of
amount/rate fields must pass the value through ``decode_decimal``.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any


class DecimalEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            # format(..., 'f') forces fixed-point so very small values like
            # Decimal("0.00000001") don't serialize as "1E-8". The decoder
            # would still accept either form, but consistent fixed-point
            # makes stored blobs easier to read at debug time.
            return format(o, "f")
        return super().default(o)


def dumps(obj: Any, **kwargs: Any) -> str:
    kwargs.setdefault("cls", DecimalEncoder)
    return json.dumps(obj, **kwargs)


def decode_decimal(value: Any) -> Decimal | None:
    """Coerce a JSON-decoded scalar into Decimal.

    Tolerates both new string-shaped values and legacy float/int values left
    over in pre-M1-Day-2.5 blobs. Floats route through ``str()`` to avoid
    binary-representation artifacts (Decimal(0.1) != Decimal("0.1")).
    """
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        # bool is a subclass of int; reject it explicitly so True/False can't
        # silently coerce to Decimal("1")/Decimal("0").
        raise TypeError(f"cannot decode bool as Decimal: {value!r}")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, str):
        return Decimal(value)
    raise TypeError(f"cannot decode {type(value).__name__} as Decimal: {value!r}")
