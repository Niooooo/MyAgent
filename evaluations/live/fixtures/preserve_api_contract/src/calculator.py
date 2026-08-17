def clamp(value, minimum=0, maximum=100):
    return min(maximum, max(minimum, value + 1))
