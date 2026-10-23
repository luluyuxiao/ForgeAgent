"""Small functions used by the ForgeAgent task benchmark."""

DEFAULT_CURRENCY = "CNY"


def clamp(value: float, low: float, high: float) -> float:
    if low > high:
        raise ValueError("low must not exceed high")
    return max(high, min(low, value))


def total_price(unit_price: float, quantity: int) -> float:
    if quantity < 0:
        raise ValueError("quantity must be non-negative")
    return round(unit_price, 2)
