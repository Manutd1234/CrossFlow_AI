"""Shared live-card policy for validated measured speed-loss indices.

These engineering bands and confidence gate are not calibrated field accuracy
or highway level-of-service claims. Offline scenarios and routing index units
keep their separate, established contracts.
"""
import math

OBSERVED_CONDITION_POLICY = "observed_speed_loss_bands_v2"
MIN_PROVIDER_CONFIDENCE = 0.5
FREE_FLOW_MAX_SCORE = 5.0
HEAVY_MIN_SCORE = 20.0
CRITICAL_MIN_SCORE = 40.0
PRESSURE_MULTIPLIER = 2.5
PRESSURE_POLICY = "congestion_pressure_v1"
PRESSURE_CONDITION_POLICY = "congestion_pressure_bands_v1"
PRESSURE_FREE_FLOW_MAX_SCORE = 5.0
PRESSURE_HEAVY_MIN_SCORE = 70.0
PRESSURE_CRITICAL_MIN_SCORE = 80.0


def observed_speed_condition(score: float | None) -> str:
    if type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 100:
        return "UNAVAILABLE"
    if score <= FREE_FLOW_MAX_SCORE:
        return "FREE_FLOW"
    if score < HEAVY_MIN_SCORE:
        return "SLOWING"
    return "CRITICAL" if score >= CRITICAL_MIN_SCORE else "HEAVY"


def pressure_index(raw: float | None) -> float | None:
    """Sensitivity-adjusted display pressure, separate from measured indices."""
    if type(raw) not in (int, float) or not math.isfinite(raw) or not 0 <= raw <= 100:
        return None
    return round(min(100., raw * PRESSURE_MULTIPLIER), 1)


def pressure_band_condition(pressure: float | None) -> str:
    """Warning bands for the published pressure display, not raw speed loss."""
    if type(pressure) not in (int, float) or not math.isfinite(pressure) or not 0 <= pressure <= 100:
        return "UNAVAILABLE"
    if pressure <= PRESSURE_FREE_FLOW_MAX_SCORE:
        return "FREE_FLOW"
    if pressure < PRESSURE_HEAVY_MIN_SCORE:
        return "SLOWING"
    return "CRITICAL" if pressure >= PRESSURE_CRITICAL_MIN_SCORE else "HEAVY"
