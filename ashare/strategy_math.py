"""Pure production formulas shared with offline diagnostics."""


def global_trend(prices_micros):
    """Completed observations only; include the latest close in MA20."""
    return (len(prices_micros) >= 20
            and prices_micros[-1] > sum(prices_micros[-20:]) // 20
            and prices_micros[-1] > prices_micros[-5])
