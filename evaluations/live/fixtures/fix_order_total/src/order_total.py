def calculate_total(prices_cents):
    """Return the total price in cents."""
    prices_cents.sort()
    return sum(prices_cents[:-1])
