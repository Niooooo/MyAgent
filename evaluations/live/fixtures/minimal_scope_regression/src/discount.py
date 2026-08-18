from constants import MEMBER_RATE


def final_price(price, is_member):
    if is_member:
        return price
    return price * MEMBER_RATE
