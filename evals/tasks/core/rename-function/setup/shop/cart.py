def get_total(items):
    """Sum of price * quantity for (price, quantity) pairs."""
    return round(sum(price * qty for price, qty in items), 2)


def apply_discount(total, percent):
    return round(total * (1 - percent / 100), 2)
