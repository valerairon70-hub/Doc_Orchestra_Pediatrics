"""
Weight-based pediatric dosing calculator.
Pure Python — no LLM dependency. Unit-testable in isolation.
Sources: BNF for Children (BNFC), NICE guidelines.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

from core.schemas import WeightBasedDose


class DrugProfile(NamedTuple):
    dose_mg_per_kg: float
    max_dose_mg: float
    min_age_months: int = 0
    notes: str = ""
    guideline_source: str = ""


# ---------------------------------------------------------------------------
# Drug lookup table — (drug_name_lower, route) → DrugProfile
# Doses per kg per SINGLE administration (not total daily)
# ---------------------------------------------------------------------------
DRUG_TABLE: dict[tuple[str, str], DrugProfile] = {
    # Antibiotics
    ("amoxicillin", "oral"): DrugProfile(25, 500, notes="Take with or without food", guideline_source="BNFC 2024"),
    ("amoxicillin-clavulanate", "oral"): DrugProfile(25, 625, notes="Take with food to reduce GI upset", guideline_source="BNFC 2024"),
    ("azithromycin", "oral"): DrugProfile(10, 500, min_age_months=6, notes="OD x 3 days for CAP", guideline_source="BNFC 2024"),
    ("cetirizine", "oral"): DrugProfile(0.25, 10, min_age_months=6, notes="OD for allergy", guideline_source="BNFC 2024"),
    ("trimethoprim", "oral"): DrugProfile(4, 200, min_age_months=6, guideline_source="BNFC 2024"),

    # Antipyretics / Analgesics
    ("paracetamol", "oral"): DrugProfile(15, 1000, notes="Max 4 doses per 24h. Minimum 4h between doses", guideline_source="BNFC 2024"),
    ("ibuprofen", "oral"): DrugProfile(5, 400, min_age_months=3, notes="Take with food. Avoid if dehydrated", guideline_source="BNFC 2024"),

    # Iron supplementation
    ("ferrous sulfate", "oral"): DrugProfile(3, 200, min_age_months=1, notes="Give 30min before meals for better absorption. May darken stools", guideline_source="NICE NG101"),
    ("ferrous fumarate", "oral"): DrugProfile(3, 210, min_age_months=1, notes="May give with vitamin C to improve absorption", guideline_source="NICE NG101"),

    # Antihistamines
    ("chlorphenamine", "oral"): DrugProfile(0.1, 4, min_age_months=12, notes="May cause drowsiness", guideline_source="BNFC 2024"),
    ("loratadine", "oral"): DrugProfile(0.1, 10, min_age_months=24, notes="Non-sedating. OD", guideline_source="BNFC 2024"),

    # Bronchodilators
    ("salbutamol", "inhaled"): DrugProfile(100, 400, notes="Dose in micrograms via spacer. 2-10 puffs as needed", guideline_source="BTS/SIGN 2023"),
    ("prednisolone", "oral"): DrugProfile(1, 40, min_age_months=1, notes="Give in the morning with food", guideline_source="NICE NG80"),

    # Oral rehydration
    ("oral rehydration solution", "oral"): DrugProfile(10, 250, notes="10 ml/kg per loose stool. Give slowly with spoon", guideline_source="NICE CG84"),
}


def calculate_dose(
    drug_name: str,
    weight_kg: float,
    dose_mg_per_kg: float | None = None,
    frequency: str = "TDS",
    route: str = "oral",
    duration_days: int = 7,
    notes: str | None = None,
    guideline_source: str | None = None,
) -> WeightBasedDose:
    """
    Calculate weight-based dose for a pediatric patient.
    If dose_mg_per_kg is not provided, looks up DRUG_TABLE.
    """
    key = (drug_name.lower(), route.lower())
    profile = DRUG_TABLE.get(key)

    if dose_mg_per_kg is None:
        if profile is None:
            raise ValueError(f"Drug '{drug_name}' (route: {route}) not found in dosing table. Provide dose_mg_per_kg manually.")
        dose_mg_per_kg = profile.dose_mg_per_kg

    raw_dose = dose_mg_per_kg * weight_kg
    max_dose = profile.max_dose_mg if profile else None
    final_dose = min(raw_dose, max_dose) if max_dose else raw_dose

    return WeightBasedDose(
        drug_name=drug_name,
        dose_mg_per_kg=dose_mg_per_kg,
        patient_weight_kg=weight_kg,
        calculated_dose_mg=round(final_dose, 1),
        frequency=frequency,
        route=route,
        duration_days=duration_days,
        max_dose_mg=max_dose,
        notes=notes or (profile.notes if profile else None),
        guideline_source=guideline_source or (profile.guideline_source if profile else None),
    )


def format_dose_for_display(dose: WeightBasedDose) -> str:
    capped = dose.calculated_dose_mg < (dose.dose_mg_per_kg * dose.patient_weight_kg)
    cap_note = f" (capped at max {dose.max_dose_mg}mg)" if capped else ""
    return (
        f"{dose.drug_name} {dose.calculated_dose_mg}mg {dose.route} {dose.frequency} "
        f"x {dose.duration_days} days{cap_note}"
    )
