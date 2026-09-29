from decimal import ROUND_HALF_UP, Decimal


def round_money(x) -> float:
    """Round to cents, halves up."""
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
