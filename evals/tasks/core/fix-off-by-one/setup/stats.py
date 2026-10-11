def moving_average(values, window):
    """Return the averages of each run of `window` consecutive values.

    moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
    """
    if window <= 0:
        raise ValueError("window must be positive")
    result = []
    for i in range(len(values) - window):
        chunk = values[i:i + window]
        result.append(sum(chunk) / window)
    return result


def normalize(values):
    """Scale values linearly so the smallest becomes 0 and the largest 1."""
    lo, hi = min(values), max(values)
    if hi == lo:
        return [0.0 for _ in values]
    return [(v - lo) / (hi - lo) for v in values]
