"""Drift-catcher + content tests for the matching-model prompt's EDT bucket list.

The matching prompt embeds the closed-set list of EDT template buckets +
categories. The list MUST match the frontend's CATEGORY_GROUPS definition
in review-table.html — operator-confirmed buckets and LLM-suggested buckets
must come from the same vocabulary or the audit trail diverges.

Two tests:
  - prompt body literally contains every bucket+category string
  - prompt vocabulary equals the set extracted from the frontend HTML
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def _read_match_prompt() -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    ROOT = Path(__file__).resolve().parents[1]
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from app.services import model_router  # noqa: E402

    return (
        model_router._MATCH_PROMPT,
        model_router.EDT_BUCKETS,
        model_router.EDT_CATEGORIES,
    )


def run_prompt_includes_bucket_list() -> None:
    """The prompt body must literally embed every bucket and category string.

    Without this, the model has no closed set to pick from and the
    closed-set validation in match_disambiguate would reject everything
    the model returns.
    """
    prompt, buckets, categories = _read_match_prompt()
    for bucket in buckets:
        assert bucket in prompt, (
            f"bucket {bucket!r} missing from _MATCH_PROMPT — model has no "
            f"way to pick it"
        )
    for category in categories:
        assert category in prompt, (
            f"category {category!r} missing from _MATCH_PROMPT"
        )
    print("prompt-includes-bucket-list: OK")


def _extract_frontend_bucket_set() -> tuple[set[str], set[str]]:
    """Parse CATEGORY_GROUPS from frontend/review-table.html.

    Looks for the single line ``const CATEGORY_GROUPS = [`` and parses the
    array literal that follows up to the matching closing bracket. Returns
    (buckets, categories).
    """
    html_path = Path(__file__).resolve().parents[2] / "frontend" / "review-table.html"
    text = html_path.read_text(encoding="utf-8")

    # Find the array literal — the file uses a fixed shape:
    #   const CATEGORY_GROUPS = [
    #     { key:'X', buckets:['a','b',...] },
    #     ...
    #   ];
    start = text.find("const CATEGORY_GROUPS =")
    assert start != -1, "CATEGORY_GROUPS not found in review-table.html"
    # Take up to the next "];" closer (no nested ] arrays inside the buckets list, just strings).
    end = text.find("];", start)
    assert end != -1, "CATEGORY_GROUPS array end not found"
    block = text[start : end + 2]

    # Pull out every quoted string. Each row's `key:'...'` is a category;
    # each `buckets:['...','...']` element is a bucket.
    categories: set[str] = set()
    buckets: set[str] = set()
    # Capture: key: 'Hotel & Travel'
    for m in re.finditer(r"key:\s*'([^']+)'", block):
        categories.add(m.group(1))
    # Capture each bucket: 'Hotel/Lodging/Laundry' inside any buckets:[ ... ]
    for buckets_match in re.finditer(r"buckets:\s*\[([^\]]*)\]", block):
        inner = buckets_match.group(1)
        for s in re.finditer(r"'([^']+)'", inner):
            buckets.add(s.group(1))

    return buckets, categories


def run_prompt_buckets_match_category_map() -> None:
    """EDT_BUCKETS / EDT_CATEGORIES must equal the frontend's CATEGORY_GROUPS.

    Drift detector: if either side updates without the other, this fails
    with a pointer to both sets so the operator can reconcile. Without
    this, an operator-edited bucket on the frontend could fail to match
    the closed-set check on the backend (or vice versa) and the LLM's
    suggestion would silently get dropped.
    """
    _prompt, backend_buckets, backend_categories = _read_match_prompt()
    frontend_buckets, frontend_categories = _extract_frontend_bucket_set()

    assert set(backend_buckets) == frontend_buckets, (
        f"EDT_BUCKETS drift detected.\n"
        f"  Backend only:  {set(backend_buckets) - frontend_buckets}\n"
        f"  Frontend only: {frontend_buckets - set(backend_buckets)}\n"
        f"Update model_router.EDT_BUCKETS or frontend CATEGORY_GROUPS "
        f"so both sides agree."
    )
    assert set(backend_categories) == frontend_categories, (
        f"EDT_CATEGORIES drift detected.\n"
        f"  Backend only:  {set(backend_categories) - frontend_categories}\n"
        f"  Frontend only: {frontend_categories - set(backend_categories)}\n"
        f"Update model_router.EDT_CATEGORIES or frontend CATEGORY_GROUPS."
    )
    print("prompt-buckets-match-category-map: OK")


def main() -> None:
    run_prompt_includes_bucket_list()
    run_prompt_buckets_match_category_map()
    print("match_prompt_bucket_tests=passed")


if __name__ == "__main__":
    main()
