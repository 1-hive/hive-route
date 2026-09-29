"""Small statistics helpers."""


def mean(xs):
    """The arithmetic mean of a non-empty sequence."""
    if not xs:
        raise ValueError("mean of an empty sequence")
    return sum(xs) / len(xs)


def median(xs):
    """The median of a non-empty sequence; for an even count, the mean of the middle two."""
    if not xs:
        raise ValueError("median of an empty sequence")
    s = sorted(xs)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return (s[mid] + s[mid + 1]) / 2


def moving_average(xs, n):
    """The mean of each window of n consecutive values: len(xs) - n + 1 values."""
    if n < 1:
        raise ValueError("window must be at least 1")
    return [mean(xs[i:i + n]) for i in range(len(xs) - n)]
