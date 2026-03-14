"""
WHO Child Growth Standards z-score calculator.
Simplified implementation using WHO reference data tables.
For production: use the full WHO tables or the `who-growth-standards` library.

Source: WHO Child Growth Standards, Geneva 2006.
"""
from __future__ import annotations

import math


# ---------------------------------------------------------------------------
# Simplified WHO reference medians and SDs for z-score estimation.
# Format: age_months → (median_weight_kg, sd_weight_kg) for M/F
# This is a simplified table — replace with full WHO dataset in production.
# ---------------------------------------------------------------------------

_WAZ_REFS = {
    # age_months: (M_median, M_sd, F_median, F_sd)
    0:  (3.35, 0.56, 3.23, 0.53),
    1:  (4.50, 0.66, 4.19, 0.61),
    2:  (5.57, 0.73, 5.12, 0.68),
    3:  (6.39, 0.79, 5.83, 0.73),
    6:  (7.93, 0.90, 7.28, 0.87),
    9:  (8.90, 0.98, 8.21, 0.95),
    12: (9.65, 1.03, 8.95, 1.00),
    18: (10.82, 1.12, 10.16, 1.10),
    24: (12.15, 1.24, 11.48, 1.18),
    36: (14.30, 1.45, 13.85, 1.38),
    48: (16.27, 1.70, 15.95, 1.66),
    60: (18.26, 1.97, 17.66, 1.88),
}

_HAZ_REFS = {
    # age_months: (M_median_cm, M_sd_cm, F_median_cm, F_sd_cm)
    0:  (49.9, 1.89, 49.1, 1.86),
    3:  (61.4, 2.27, 59.8, 2.22),
    6:  (67.6, 2.42, 65.7, 2.45),
    12: (75.7, 2.70, 74.0, 2.74),
    18: (82.3, 2.95, 80.7, 2.97),
    24: (87.1, 3.24, 85.7, 3.16),
    36: (96.1, 3.63, 95.1, 3.54),
    48: (103.3, 3.97, 102.7, 3.88),
    60: (110.0, 4.26, 109.4, 4.15),
}


def _interpolate_ref(refs: dict, age_months: int, sex: str) -> tuple[float, float] | None:
    """Linear interpolation between nearest reference age brackets."""
    ages = sorted(refs.keys())
    if age_months <= ages[0]:
        row = refs[ages[0]]
    elif age_months >= ages[-1]:
        row = refs[ages[-1]]
    else:
        lower = max(a for a in ages if a <= age_months)
        upper = min(a for a in ages if a >= age_months)
        if lower == upper:
            row = refs[lower]
        else:
            t = (age_months - lower) / (upper - lower)
            r_low = refs[lower]
            r_high = refs[upper]
            # Interpolate for male (indices 0,1) or female (indices 2,3)
            offset = 0 if sex == "M" else 2
            median = r_low[offset] + t * (r_high[offset] - r_low[offset])
            sd = r_low[offset+1] + t * (r_high[offset+1] - r_low[offset+1])
            return median, sd

    offset = 0 if sex == "M" else 2
    return row[offset], row[offset+1]


def _zscore(value: float, median: float, sd: float) -> float:
    if sd == 0:
        return 0.0
    return round((value - median) / sd, 2)


def calculate_growth_zscores(
    age_months: int,
    weight_kg: float | None,
    height_cm: float | None,
    sex: str,
) -> dict[str, float]:
    """
    Calculate WHO weight-for-age (WAZ) and height-for-age (HAZ) z-scores.
    Also calculates BMI z-score if both weight and height are available.

    Returns dict with keys: waz, haz, bmiz (where calculable).
    """
    result: dict[str, float] = {}
    sex_norm = "M" if sex == "M" else "F"

    if weight_kg is not None:
        ref = _interpolate_ref(_WAZ_REFS, age_months, sex_norm)
        if ref:
            result["waz"] = _zscore(weight_kg, ref[0], ref[1])

    if height_cm is not None:
        ref = _interpolate_ref(_HAZ_REFS, age_months, sex_norm)
        if ref:
            result["haz"] = _zscore(height_cm, ref[0], ref[1])

    if weight_kg is not None and height_cm is not None and height_cm > 0:
        bmi = weight_kg / (height_cm / 100) ** 2
        result["bmi"] = round(bmi, 1)
        # Simplified BMI z-score — use WHO BAZ tables in production
        # Placeholder: rough estimate
        if age_months >= 24:
            bmi_median = 15.5 if sex_norm == "M" else 15.3
            bmi_sd = 1.8
            result["bmiz"] = _zscore(bmi, bmi_median, bmi_sd)

    return result


def classify_zscore(z: float) -> str:
    if z < -3:
        return "severe_deficit"
    elif z < -2:
        return "moderate_deficit"
    elif z < -1:
        return "mild_deficit"
    elif z <= 1:
        return "normal"
    elif z <= 2:
        return "above_normal"
    else:
        return "obese_range"
