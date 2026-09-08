from decimal import Decimal, InvalidOperation

from django import template

register = template.Library()


@register.filter
def rupiah(value) -> str:
    """Decimal/angka -> '1.600.000' atau '-142.000' (pemisah ribuan titik, tanpa desimal)."""
    try:
        d = Decimal(str(value)).quantize(Decimal("1"))
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    return f"{d:,}".replace(",", ".")
