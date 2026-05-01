"""Deterministic merchant-name to EDT template bucket suggestions.

Rules come from docs/CLAUDE_HANDOFF_2026-04-21.md.
All bucket strings must exactly match template-native row labels.
Returns None when no confident match exists (user must classify manually).
"""
import re
from typing import NamedTuple


class _Rule(NamedTuple):
    pattern: re.Pattern[str]
    bucket: str


# Ordered from most-specific to most-general.
# Each rule matches case-insensitively against the raw supplier name.
_RULES: list[_Rule] = [
    # Taxi / ride-hailing
    _Rule(re.compile(r"\b(uber|takside|bitaksi|faturamati\s*taksi|havuzlar\s*taksi|taksi)\b", re.I), "Taxi/Parking/Tolls/Uber"),
    # Hotel / lodging
    _Rule(re.compile(r"\b(hampton|hilton|marriott|sheraton|hyatt|hotel|otel|lodg|laundry)\b", re.I), "Hotel/Lodging/Laundry"),
    # Auto gasoline / fuel
    _Rule(re.compile(r"\b(shell|opet|petrol|akaryak[ıi]t|bp\s|total\s*oil|lukoil|bpet)\b", re.I), "Auto Gasoline"),
    # Meals / food delivery / snacks
    _Rule(re.compile(r"\b(yemeksepeti|getir|trendyol\s*yemek|starbucks|cafe|kafeterya|d[oö]ner|k[oö]fteci|restaurant|restoran|lokanta|pizza|burger|mcdonalds|kfc|popeyes|subway|simit)\b", re.I), "Meals/Snacks"),
    # Airfare / intercity transport
    _Rule(re.compile(r"\b(thy|turkish\s*airlines?|pegasus|sunexpress|flypgs|anadolujet|havayollar[ıi]|havaalanı|airport)\b", re.I), "Airfare/Bus/Ferry/Other"),
    # Telephone / internet — only strong, unambiguous signals so that
    # venues like "Vodafone Park" (an Istanbul stadium) and meal slips
    # whose name happens to contain "internet" / "gsm" / "fatura" don't
    # get auto-bucketed as Telephone/Internet by the suggester. Keep this
    # in sync with TELECOM_TEXT_TOKENS in app/services/report_validation.py.
    _Rule(re.compile(r"\b(fatura\s+tahsilat[ıi]|phone\s+bill|superonline|t[uü]rk\s*telekom|t[uü]rknet|turk\.net)\b", re.I), "Telephone/Internet"),
    # Auto rental
    _Rule(re.compile(r"\b(avis|hertz|budget\s*rent|sixt|europcar|oto\s*kiralama|rent\s*a\s*car)\b", re.I), "Auto Rental"),
    # Entertainment
    _Rule(re.compile(r"\b(sinema|cinema|tiyatro|theatre|theater|konser|concert|biletix|biletmaster)\b", re.I), "Entertainment"),
    # Admin / office supplies
    _Rule(re.compile(r"\b(kırtasiye|ofis\s*depo|officedepot|staples|carrefour\s*ofis)\b", re.I), "Admin Supplies"),
    # Grocery / market / pharmacy / retail → Other (low confidence, explicit fallthrough)
    _Rule(re.compile(r"\b(migros|bim|a101|sok\s|file\s|market|eczane|pharmacy|tekel|petshop|pet\s*shop|retail|migros)\b", re.I), "Other"),
]


def suggest_bucket(supplier_raw: str | None) -> str | None:
    """Return an EDT template bucket suggestion for the given supplier name, or None."""
    if not supplier_raw:
        return None
    text = supplier_raw.strip()
    for rule in _RULES:
        if rule.pattern.search(text):
            return rule.bucket
    return None
