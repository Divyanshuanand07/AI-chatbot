"""
Presentation helpers.

Money is handed to the model as both a raw number and a pre-formatted string.
That is deliberate: LLMs are unreliable at Indian digit grouping (lakh/crore)
and at arithmetic. Every figure the assistant might quote is computed and
formatted in Python first, so the model only ever copies a string.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal


def indian_currency(value: Decimal | float | int | None) -> str:
    """Format 285000 as '₹2,85,000.00' (Indian 2-2-3 grouping)."""
    if value is None:
        return "—"
    amount = Decimal(str(value)).quantize(Decimal("0.01"))
    negative = amount < 0
    amount = abs(amount)

    whole, _, frac = f"{amount:.2f}".partition(".")

    if len(whole) <= 3:
        grouped = whole
    else:
        last3 = whole[-3:]
        rest = whole[:-3]
        pairs = []
        while len(rest) > 2:
            pairs.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            pairs.insert(0, rest)
        grouped = ",".join([*pairs, last3])

    return f"{'-' if negative else ''}₹{grouped}.{frac}"


def money(value: Decimal | float | int | None) -> dict:
    """Tool-facing money representation: numeric value plus display string."""
    if value is None:
        return {"value": None, "display": "—"}
    return {
        "value": float(Decimal(str(value)).quantize(Decimal("0.01"))),
        "display": indian_currency(value),
    }


def iso(value: dt.datetime | dt.date | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def humanize_age(value: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    """'3 days ago' / '4 hours ago' — gives the model relative time for free."""
    if value is None:
        return "—"
    from django.utils import timezone

    now = now or timezone.now()
    delta = now - value
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "in the future"
    if seconds < 3600:
        minutes = seconds // 60
        return f"{minutes} minute{'s' if minutes != 1 else ''} ago"
    if seconds < 86400:
        hours = seconds // 3600
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    days = seconds // 86400
    return f"{days} day{'s' if days != 1 else ''} ago"


def label(choices_cls, value: str) -> str:
    """Human label for a TextChoices value, falling back to the raw value."""
    try:
        return choices_cls(value).label
    except ValueError:
        return value


def mask_phone(phone: str) -> str:
    digits = "".join(ch for ch in phone if ch.isdigit())
    if len(digits) < 4:
        return "****"
    return f"{'*' * (len(digits) - 4)}{digits[-4:]}"


def mask_email(email: str) -> str:
    if "@" not in email:
        return "****"
    local, _, domain = email.partition("@")
    visible = local[:1] if local else ""
    return f"{visible}{'*' * max(len(local) - 1, 3)}@{domain}"
