"""Shared units for measured traffic, training labels and congestion costs."""
import math


def speed_ratio_congestion_score(speed_ratio: float) -> float:
    """0 is free-flow; 95 is stopped, retaining a finite routing-cost floor.

    A stopped raw observation is valid. The .05 ratio floor belongs only to
    the derived calculation and does not censor the raw measurement.
    """
    if isinstance(speed_ratio, bool) or not math.isfinite(speed_ratio) or speed_ratio < 0:
        raise ValueError("Speed ratio must be finite and nonnegative.")
    return 100.0 * (1.0 - min(1.0, max(0.05, speed_ratio)))
