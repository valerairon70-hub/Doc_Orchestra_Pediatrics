"""
Mx Agent — The Strategic Thinker.

Deep reasoning over NICE/BMJ clinical guidelines.
Generates tiered management plans with weight-based dosing.
Uses Gemini 2.5 Pro with thinking mode for traceable clinical reasoning.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from core.config import Settings
from core.database import Database
from core.schemas import (
    AgentMessage, AgentRole, AssessmentBlock, PatientState,
    PlanBlock, SOAPNote, WeightBasedDose,
)
from agents.base_agent import BaseAgent
from tools.dosing_calculator import calculate_dose

logger = logging.getLogger(__name__)

_GUIDELINES_DIR = Path(__file__).parent.parent / "data" / "guidelines"

_MX_SYSTEM_PROMPT = """
You are a clinical decision support assistant for a pediatric infectious disease specialist.

Your role:
1. Analyze the clinical presentation and available investigations.
2. Generate a differential diagnosis ordered by probability.
3. Propose a tiered management plan following NICE/BMJ guidelines.
4. Calculate weight-based drug dosages (you will be given pre-calculated doses from the dosing tool).
5. Cite specific guideline sources for each recommendation.

OUTPUT FORMAT — return ONLY valid JSON:
{{
  "working_diagnoses": ["Most likely diagnosis", "Second most likely"],
  "differential_diagnoses": ["Other possibilities"],
  "reasoning_summary": "Step-by-step clinical reasoning (2-3 sentences)",
  "first_line_investigations": ["FBC", "Blood film", ...],
  "second_line_investigations": ["Bone marrow aspirate if no response at 4 weeks"],
  "medications": [
    {{
      "drug_name": "Ferrous sulfate",
      "dose_mg_per_kg": 3,
      "frequency": "BD",
      "route": "oral",
      "duration_days": 90,
      "notes": "Give 30 min before meals for better absorption",
      "guideline_source": "NICE NG101"
    }}
  ],
  "safety_netting_advice": "Return urgently if child becomes pale, breathless, or stops feeding",
  "follow_up_days": 28,
  "guideline_citations": ["NICE NG101 Iron Deficiency Anaemia 2021", "BMJ Best Practice Anaemia in Children"]
}}

GUARDRAILS:
- Do NOT include diagnosis in text that will be sent to the parent.
- Flag any RED FLAGS in reasoning_summary.
- If data is insufficient, request specific additional investigations rather than guessing.
"""


class MxAgent(BaseAgent):
    role = AgentRole.MX

    def __init__(self, settings: Settings, db: Database, semaphore):
        super().__init__(settings, db, semaphore)
        self.model_name = settings.model_mx

    async def process(self, message: AgentMessage) -> AgentMessage:
        try:
            soap: SOAPNote = SOAPNote.model_validate(message.payload.get("soap_note"))
            patient: PatientState = await self.db.get_or_create_patient(message.patient_id)

            # Build the clinical prompt with guideline context
            guideline_context = self._load_relevant_guidelines(soap)
            clinical_summary = self._build_clinical_summary(soap, patient)

            prompt = (
                f"CLINICAL PRESENTATION:\n{clinical_summary}\n\n"
                f"RELEVANT GUIDELINES:\n{guideline_context}\n\n"
                f"Patient weight: {soap.objective.vitals.weight_kg} kg\n"
                "Generate the management plan as JSON per the format specified."
            )

            response_text = await self.generate(
                system_prompt=_MX_SYSTEM_PROMPT,
                user_content=prompt,
                temperature=0.1,
                max_output_tokens=2048,
                thinking_budget=8000,   # Enable deep reasoning for Gemini 2.5 Pro
            )

            plan_data = json.loads(response_text.strip().strip("```json").strip("```"))

            # Post-process: add calculated doses
            weight_kg = soap.objective.vitals.weight_kg
            medications = self._calculate_doses(plan_data.get("medications", []), weight_kg)
            plan_data["medications"] = [m.model_dump() for m in medications]

            return self.make_result(message, {
                "assessment_partial": {
                    "working_diagnoses": plan_data.get("working_diagnoses", []),
                    "differential_diagnoses": plan_data.get("differential_diagnoses", []),
                    "reasoning_summary": plan_data.get("reasoning_summary", ""),
                },
                "plan_partial": {
                    "first_line_investigations": plan_data.get("first_line_investigations", []),
                    "second_line_investigations": plan_data.get("second_line_investigations", []),
                    "medications": plan_data.get("medications", []),
                    "safety_netting_advice": plan_data.get("safety_netting_advice", ""),
                    "follow_up_days": plan_data.get("follow_up_days"),
                    "guideline_citations": plan_data.get("guideline_citations", []),
                },
                "agent": "mx",
            })

        except Exception as exc:
            return self.make_error(message, str(exc))

    def _build_clinical_summary(self, soap: SOAPNote, patient: PatientState) -> str:
        s = soap.subjective
        o = soap.objective
        return (
            f"Chief complaint: {s.chief_complaint}\n"
            f"HPI: {s.hpi}\n"
            f"Duration: {s.duration_days} days\n"
            f"Symptoms: {', '.join(s.associated_symptoms)}\n"
            f"Vitals: weight={o.vitals.weight_kg}kg, temp={o.vitals.temperature_c}°C, HR={o.vitals.heart_rate}\n"
            f"Hb: {o.hb_gdl} g/dL | Ferritin: {o.ferritin_ugL} µg/L | MCV: {o.mcv_fl} fL\n"
            f"Skin: {o.skin_findings or 'None noted'}\n"
            f"Age: {patient.age_years():.1f}y | Allergies: {', '.join(patient.known_allergies) or 'None'}\n"
            f"Chronic conditions: {', '.join(patient.chronic_conditions) or 'None'}"
        )

    def _load_relevant_guidelines(self, soap: SOAPNote) -> str:
        """Keyword-based guideline loading. Upgrade path: swap for RAG retrieval."""
        complaint = (soap.subjective.chief_complaint + " " + " ".join(soap.subjective.associated_symptoms)).lower()

        # Keyword → guideline file mapping
        keyword_map = {
            ("anemia", "anaemia", "pallor", "hb", "hemoglobin", "ferritin"): "nice_anemia.md",
            ("fever", "temperature", "febrile"): "bmj_fever.md",
            ("rash", "skin", "spots", "lesion"): "bmj_rash_paediatric.md",
            ("cough", "wheeze", "respiratory", "bronchitis"): "nice_respiratory.md",
            ("diarrhea", "diarrhoea", "vomiting", "gastro"): "nice_gastroenteritis.md",
        }

        loaded = []
        for keywords, filename in keyword_map.items():
            if any(kw in complaint for kw in keywords):
                path = _GUIDELINES_DIR / filename
                if path.exists():
                    loaded.append(path.read_text(encoding="utf-8")[:3000])  # 3K chars per guideline

        return "\n\n---\n\n".join(loaded) if loaded else "No specific guidelines loaded. Use general pediatric principles."

    def _calculate_doses(self, med_list: list[dict], weight_kg: float | None) -> list[WeightBasedDose]:
        if not weight_kg:
            return []
        result = []
        for med in med_list:
            try:
                dose = calculate_dose(
                    drug_name=med["drug_name"],
                    weight_kg=weight_kg,
                    dose_mg_per_kg=med.get("dose_mg_per_kg"),
                    frequency=med.get("frequency", "OD"),
                    route=med.get("route", "oral"),
                    duration_days=med.get("duration_days", 7),
                    notes=med.get("notes"),
                    guideline_source=med.get("guideline_source"),
                )
                result.append(dose)
            except Exception as exc:
                logger.warning("Dose calculation failed for %s: %s", med.get("drug_name"), exc)
        return result
