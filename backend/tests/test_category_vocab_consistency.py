"""Drift-detector tests for the shared category vocabulary module.

The Telegram inline-keyboard Edit menu, the model_router matching prompt,
and the frontend review-table all reference the same EDT category->bucket
hierarchy. Drift between any of these breaks data integrity (operator picks
a bucket that the LLM doesn't know, or vice versa).

This module is the canonical Python source. Tests:
  - frontend HTML CATEGORY_GROUPS matches Python module (skip categories
    with empty buckets like Personal Car)
  - model_router.EDT_BUCKETS equals all_buckets()
  - category_for_bucket() works for every bucket
  - buckets_for() returns non-empty for every category in
    CATEGORIES_REQUIRING_REASON_AND_ATTENDEES
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _ensure_path() -> None:
    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def _extract_frontend_groups() -> dict[str, tuple[str, ...]]:
    """Parse CATEGORY_GROUPS from frontend/review-table.html.

    Returns a dict {category_name: (bucket1, bucket2, ...)}. Categories with
    empty bucket lists (e.g. Personal Car) are still returned with an empty
    tuple so the caller can decide whether to compare them.
    """
    html_path = (
        Path(__file__).resolve().parents[2] / "frontend" / "review-table.html"
    )
    text = html_path.read_text(encoding="utf-8")

    start = text.find("const CATEGORY_GROUPS =")
    assert start != -1, "CATEGORY_GROUPS not found in review-table.html"
    end = text.find("];", start)
    assert end != -1, "CATEGORY_GROUPS array end not found"
    block = text[start : end + 2]

    # Each entry shape: { key:'<name>', buckets:['a','b',...] }
    groups: dict[str, tuple[str, ...]] = {}
    for entry in re.finditer(
        r"key:\s*'([^']+)'\s*,\s*buckets:\s*\[([^\]]*)\]", block
    ):
        category = entry.group(1)
        inner = entry.group(2)
        buckets = tuple(m.group(1) for m in re.finditer(r"'([^']+)'", inner))
        groups[category] = buckets
    return groups


def test_category_vocab_drift_detection() -> None:
    """Python module CATEGORY_GROUPS must match the frontend, except for
    frontend-only categories with empty bucket lists (e.g. Personal Car).

    Empty-bucket categories are deferred per the docstring on
    category_vocab.CATEGORY_GROUPS — they are NOT in the Python module.
    """
    _ensure_path()
    from app import category_vocab  # noqa: E402

    frontend_groups = _extract_frontend_groups()
    py_groups = dict(category_vocab.CATEGORY_GROUPS)

    # Skip frontend categories with empty bucket lists; the Python module
    # excludes them by design.
    frontend_non_empty = {
        name: buckets
        for name, buckets in frontend_groups.items()
        if buckets
    }

    py_only = set(py_groups) - set(frontend_non_empty)
    fe_only = set(frontend_non_empty) - set(py_groups)
    assert not py_only, (
        f"Python-only categories: {py_only}. Update frontend or remove."
    )
    assert not fe_only, (
        f"Frontend-only non-empty categories: {fe_only}. Update "
        f"category_vocab.CATEGORY_GROUPS or strip buckets in frontend."
    )

    for name, fe_buckets in frontend_non_empty.items():
        py_buckets = py_groups[name]
        assert set(py_buckets) == set(fe_buckets), (
            f"Bucket drift for category {name!r}.\n"
            f"  Python only:  {set(py_buckets) - set(fe_buckets)}\n"
            f"  Frontend only: {set(fe_buckets) - set(py_buckets)}\n"
            f"Update category_vocab.CATEGORY_GROUPS or frontend."
        )


def test_category_vocab_matches_model_router_edt_buckets() -> None:
    """all_buckets() must equal model_router.EDT_BUCKETS as a set."""
    _ensure_path()
    from app import category_vocab  # noqa: E402
    from app.services import model_router  # noqa: E402

    assert set(category_vocab.all_buckets()) == set(model_router.EDT_BUCKETS), (
        f"all_buckets() != EDT_BUCKETS.\n"
        f"  Vocab only:  {set(category_vocab.all_buckets()) - set(model_router.EDT_BUCKETS)}\n"
        f"  Router only: {set(model_router.EDT_BUCKETS) - set(category_vocab.all_buckets())}"
    )


def test_category_vocab_inverse_lookup() -> None:
    """category_for_bucket() must return the parent category for every
    bucket in all_buckets()."""
    _ensure_path()
    from app import category_vocab  # noqa: E402

    cats = set(category_vocab.categories())
    for bucket in category_vocab.all_buckets():
        cat = category_vocab.category_for_bucket(bucket)
        assert cat is not None, f"bucket {bucket!r} has no parent category"
        assert cat in cats, f"category {cat!r} not in categories()"
        assert bucket in category_vocab.buckets_for(cat), (
            f"bucket {bucket!r} not listed under its own parent {cat!r}"
        )


def test_category_vocab_inverse_lookup_unknown_bucket() -> None:
    """category_for_bucket() must return None for unknown buckets."""
    _ensure_path()
    from app import category_vocab  # noqa: E402

    assert category_vocab.category_for_bucket("Bogus Bucket") is None
    assert category_vocab.category_for_bucket("") is None


def test_categories_requiring_reason_attendees_have_buckets() -> None:
    """Every Tier 1 in CATEGORIES_REQUIRING_REASON_AND_ATTENDEES must have
    a non-empty bucket list (else the Tier 2 menu would be empty and the
    reason/attendees prompt would never fire)."""
    _ensure_path()
    from app import category_vocab  # noqa: E402

    for cat in category_vocab.CATEGORIES_REQUIRING_REASON_AND_ATTENDEES:
        buckets = category_vocab.buckets_for(cat)
        assert buckets, (
            f"category {cat!r} requires reason/attendees but has no buckets"
        )


def test_buckets_for_unknown_category_is_empty() -> None:
    """Unknown category returns empty tuple, not raise."""
    _ensure_path()
    from app import category_vocab  # noqa: E402

    assert category_vocab.buckets_for("Bogus Category") == ()
