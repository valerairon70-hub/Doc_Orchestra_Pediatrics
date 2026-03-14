"""
Longitudinal Data Agent — The Tracker.

Analyzes time-series data:
- Hemoglobin trends
- Growth charts (weight, height)
- Treatment response monitoring

Fires Red Flag alerts when clinical thresholds are breached.
Uses Gemini 2.5 Flash with 1M context for full patient history.
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime

from core.config import Settings
from core.database import Database
from core.schemas import AgentMessage, AgentRole, MessageType, PatientState, SOAPNote
from agents.base_agent import BaseAgent
from tools.growth_chart import calculate_growth_zscores

logger = logging.getLogger(__name__)

_LONGITUDINAL_SYSTEM_PROMPT = """
You are a pediatric data analyst reviewing a patient's longitudinal clinical data for a specialist doctor.

Your tasks:
1. Identify trends in hemoglobin, ferritin, weight, and height over time.
2. Classify each trend: improving / stable / declining / acute_change.
3. Identify any patterns of concern.
4. Provide a concise narrative summary for the doctor (2-3 sentences).

Return ONLY valid JSON:
{
  "hb_trend": "improving|stable|declining|acute_drop|insufficient_data",
  "ferritin_trend": "improving|stable|declining|insufficient_data",
  "growth_trend": "normal|faltering|catch_up_growth|insufficient_data",
  "treatment_response": "good|partial|none|no_prior_treatment",
  "narrative_summary": "2-3 sentence summary for the doctor",
  "suggested_monitoring": ["Repeat FBC in 4 weeks", "Weigh at next visit"]
}
"""


class LongitudinalAgent(BaseAgent):
    role = AgentRole.LONGITUDINAL

    def __init__(self, settings: Settings, db: Database, semaphore):
        super().__init__(settings, db, semaphore)
        self.model_name = settings.model_longitudinal

    async def process(self, message: AgentMessage) -> AgentMessage:
        try:
            soap: SOAPNote = SOAPNote.model_validate(message.payload.get("soap_note"))
            patient: PatientState = await self.db.get_or_create_patient(message.patient_id)

            # 1. Deterministic red flag checks (no LLM needed)
            red_flags = self._check_red_flags(soap, patient)

            # 2. Growth z-scores
            z_scores = self._compute_growth_zscores(soap, patient)

            # 3. LLM trend analysis
            trend_data = await self._analyze_trends(soap, patient)

            # 4. Update patient history with new data points
            self._append_to_history(soap, patient)
            await self.db.save_patient_state(patient)

            result_payload = {
                "red_flags": red_flags,
                "growth_z_scores": z_scores,
                "trend_analysis": trend_data,
                "agent": "longitudinal",
            }

            # If red flags found: escalate priority
            if red_flags:
                return AgentMessage(
                    session_id=message.session_id,
                    patient_id=message.patient_id,
                    sender=self.role,
                    recipient=AgentRole.ORCHESTRATOR,
                    message_type=MessageType.RED_FLAG_ALERT,
                    priority=1,     # Highest priority — jumps the queue
                    payload=result_payload,
                    correlation_id=message.message_id,
                )

            return self.make_result(message, result_payload)

        except Exception as exc:
            return self.make_error(message, str(exc))

    # ------------------------------------------------------------------
    # Red flag detection (deterministic — no LLM)
    # ------------------------------------------------------------------

    def _check_red_flags(self, soap: SOAPNote, patient: PatientState) -> list[str]:
        flags = []
        cfg = self.settings

        hb = soap.objective.hb_gdl
        ferritin = soap.objective.ferritin_ugL

        if hb is not None and hb < cfg.red_flag_hb_gdl:
            flags.append(f"CRITICAL: Hb {hb:.1f} g/dL (threshold < {cfg.red_flag_hb_gdl})")

        if hb is not None and patient.hb_history:
            drop = self._calculate_hb_drop_4weeks(hb, patient.hb_history)
            if drop is not None and drop >= cfg.red_flag_hb_drop_gdl:
                flags.append(f"CRITICAL: Hb dropped {drop:.1f} g/dL in last 4 weeks")

        if ferritin is not None and ferritin < cfg.red_flag_ferritin_ugL and hb is not None and hb < 11.0:
            flags.append(f"ALERT: Ferritin {ferritin:.1f} µg/L with symptomatic anemia")

        return flags

    def _calculate_hb_drop_4weeks(
        self,
        current_hb: float,
        history: list[tuple[str, float]],
    ) -> float | None:
        from datetime import timedelta
        cutoff = date.today() - timedelta(days=28)
        recent = [
            (date.fromisoformat(d), v)
            for d, v in history
            if date.fromisoformat(d) >= cutoff
        ]
        if not recent:
            return None
        earliest_in_window = min(recent, key=lambda x: x[0])
        return earliest_in_window[1] - current_hb   # Positive = drop

    # ------------------------------------------------------------------
    # Growth z-scores
    # ------------------------------------------------------------------

    def _compute_growth_zscores(self, soap: SOAPNote, patient: PatientState) -> dict[str, float]:
        weight = soap.objective.vitals.weight_kg
        height = soap.objective.vitals.height_cm
        age_months = patient.age_months()
        sex = patient.sex

        if not (weight and age_months):
            return {}

        return calculate_growth_zscores(
            age_months=age_months,
            weight_kg=weight,
            height_cm=height,
            sex=sex,
        )

    # ------------------------------------------------------------------
    # LLM trend analysis
    # ------------------------------------------------------------------

    async def _analyze_trends(self, soap: SOAPNote, patient: PatientState) -> dict:
        if len(patient.hb_history) < 2 and len(patient.weight_history) < 2:
            return {
                "hb_trend": "insufficient_data",
                "ferritin_trend": "insufficient_data",
                "growth_trend": "insufficient_data",
                "treatment_response": "no_prior_treatment",
                "narrative_summary": "Insufficient longitudinal data for trend analysis. First recorded visit.",
                "suggested_monitoring": ["Repeat FBC in 4 weeks if anemia confirmed"],
            }

        history_summary = self._format_history_for_llm(patient)
        current = (
            f"Current visit: Hb={soap.objective.hb_gdl}, "
            f"Ferritin={soap.objective.ferritin_ugL}, "
            f"Weight={soap.objective.vitals.weight_kg}kg"
        )

        try:
            response_text = await self.generate(
                system_prompt=_LONGITUDINAL_SYSTEM_PROMPT,
                user_content=f"PATIENT HISTORY:\n{history_summary}\n\nCURRENT VISIT:\n{current}",
                temperature=0.1,
                max_output_tokens=512,
            )
            return json.loads(response_text.strip().strip("```json").strip("```"))
        except Exception as exc:
            logger.warning("Trend analysis failed: %s", exc)
            return {"narrative_summary": "Trend analysis unavailable"}

    def _format_history_for_llm(self, patient: PatientState) -> str:
        lines = []
        for d, v in sorted(patient.hb_history[-12:]):    # Last 12 readings
            lines.append(f"  Hb: {v} g/dL on {d}")
        for d, v in sorted(patient.ferritin_history[-6:]):
            lines.append(f"  Ferritin: {v} µg/L on {d}")
        for d, v in sorted(patient.weight_history[-12:]):
            lines.append(f"  Weight: {v} kg on {d}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Update patient history
    # ------------------------------------------------------------------

    def _append_to_history(self, soap: SOAPNote, patient: PatientState) -> None:
        today = str(date.today())
        o = soap.objective

        if o.hb_gdl is not None:
            patient.hb_history.append((today, o.hb_gdl))
        if o.ferritin_ugL is not None:
            patient.ferritin_history.append((today, o.ferritin_ugL))
        if o.vitals.weight_kg is not None:
            patient.weight_history.append((today, o.vitals.weight_kg))
        if o.vitals.height_cm is not None:
            patient.height_history.append((today, o.vitals.height_cm))
