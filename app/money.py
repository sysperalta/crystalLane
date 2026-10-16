"""Money is handled internally as integer cents to avoid float comparison bugs."""

from decimal import ROUND_HALF_UP, Decimal


def to_cents(dollars: float) -> int:
    return int((Decimal(str(dollars)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_dollars(cents: int) -> float:
    return cents / 100


def usd(dollars: float) -> str:
    """'$2,250' for whole dollars, '$2,250.50' otherwise."""
    return f"${dollars:,.0f}" if float(dollars).is_integer() else f"${dollars:,.2f}"
