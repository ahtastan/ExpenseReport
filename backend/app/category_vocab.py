"""Single source of truth for the EDT category to bucket vocabulary used by
the Telegram inline-keyboard Edit menu.

Mirror of frontend/review-table.html CATEGORY_GROUPS. The frontend continues
to embed its own copy for now; a drift-detector test enforces agreement.

When EDT updates the report template, BOTH this module AND the frontend
constant must be updated together.
"""

from typing import Final


# Tier 1 categories (excluding "Personal Car" which has empty buckets and is
# not surfaced in the Telegram Edit menu - deferred until EDT productizes it).
CATEGORY_GROUPS: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    (
        "Hotel & Travel",
        (
            "Hotel/Lodging/Laundry",
            "Auto Rental",
            "Auto Gasoline",
            "Taxi/Parking/Tolls/Uber",
            "Other Travel Related",
        ),
    ),
    (
        "Meals & Entertainment",
        (
            "Meals/Snacks",
            "Breakfast",
            "Lunch",
            "Dinner",
            "Entertainment",
        ),
    ),
    (
        "Air Travel",
        (
            "Airfare/Bus/Ferry/Other",
        ),
    ),
    (
        "Other",
        (
            "Membership/Subscription Fees",
            "Customer Gifts",
            "Telephone/Internet",
            "Postage/Shipping",
            "Admin Supplies",
            "Lab Supplies",
            "Field Service Supplies",
            "Assets",
            "Other",
        ),
    ),
)


# Tier 1 names that trigger the Reason/Attendees prompt after a Tier 2 pick.
CATEGORIES_REQUIRING_REASON_AND_ATTENDEES: Final[frozenset[str]] = frozenset(
    {"Meals & Entertainment"}
)


def categories() -> tuple[str, ...]:
    """Tier 1 category names in display order."""
    return tuple(name for name, _ in CATEGORY_GROUPS)


def buckets_for(category: str) -> tuple[str, ...]:
    """Tier 2 buckets for a given Tier 1 category. Empty tuple if unknown."""
    for name, buckets in CATEGORY_GROUPS:
        if name == category:
            return buckets
    return ()


def category_for_bucket(bucket: str) -> str | None:
    """Inverse lookup: given a bucket name, return its Tier 1 category, or None."""
    for category, buckets in CATEGORY_GROUPS:
        if bucket in buckets:
            return category
    return None


def all_buckets() -> tuple[str, ...]:
    """Flat list of all buckets across all categories. For validation."""
    return tuple(b for _, buckets in CATEGORY_GROUPS for b in buckets)
