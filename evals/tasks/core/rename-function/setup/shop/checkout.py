from shop.cart import apply_discount, get_total


def checkout(items, coupon=None):
    total = get_total(items)
    if coupon == "SAVE10":
        total = apply_discount(total, 10)
    return total
