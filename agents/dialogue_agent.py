"""
Dialogue Agent — State-Aware History Taking.

3-phase FSM:
  GREETING → COMPLAINT_COLLECTION → GAP_FILLING → CONCLUSION → AWAITING_DOCTOR_APPROVAL

GUARDRAIL: Never communicates diagnosis or management plan to parent.
All clinical conclusions go to Orchestrator only, not to the parent channel.
"""
from __future__ import annotations

import json
import logging

from core.config import Settings
from core.database import Database
from core.schemas import (
    AgentMessage, AgentRole, DialoguePhase, PatientState, SubjectiveBlock
)
from agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

# Fields the dialogue agent must collect before moving to gap-filling
REQUIRED_FIELDS = ["chief_complaint", "duration_days", "associated_symptoms"]
OPTIONAL_GAP_FIELDS = ["weight_kg", "skin_photo", "fever_duration", "urine_output"]


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_SHARED_RULES = """
CRITICAL RULES — ALWAYS FOLLOW:
1. You are an AI assistant for a pediatric infectious disease specialist. Your ONLY job is to collect information from the parent/guardian and provide empathetic support.
2. NEVER give a diagnosis, differential diagnosis, or management plan to the parent — even if asked directly.
3. NEVER suggest specific medications or dosages to the parent.
4. If asked for a diagnosis, say: "Dr. [doctor name] will review all the information and provide guidance shortly."
5. For any urgent safety concern (difficulty breathing, loss of consciousness, severe pallor), immediately instruct the parent to call emergency services (103) and notify the doctor.
6. Tone: warm, empathetic, professional. Use simple language. Avoid medical jargon.
7. Respond in the same language the parent uses.
"""

SYSTEM_PROMPTS = {
    DialoguePhase.GREETING: f"""
You are a caring medical assistant at a pediatric clinic. Greet the parent warmly and ask what brings them here today.
{_SHARED_RULES}
""",
    DialoguePhase.COMPLAINT_COLLECTION: f"""
You are collecting a medical history for a pediatric patient. Your goal is to understand:
- Main complaint (what's wrong)
- Duration (how long)
- Associated symptoms (fever, rash, cough, appetite changes, etc.)
- Severity (is the child active, drinking fluids?)

Ask ONE focused question at a time. Do not bombard with multiple questions.
After 2-3 exchanges, transition to gap-filling by outputting a JSON block:
<phase_transition>
{{"next_phase": "gap_filling", "collected": {{"chief_complaint": "...", "duration_days": ..., "associated_symptoms": [...]}}}}
</phase_transition>
{_SHARED_RULES}
""",
    DialoguePhase.GAP_FILLING: f"""
You are completing the clinical picture for the doctor. Based on what has been collected, identify ONE missing piece of information and ask for it.

Priority gaps to fill (in order):
1. Child's current weight (if not provided)
2. Photo of rash/skin finding (if rash is mentioned) — ask parent to upload a clear photo in good lighting
3. Recent blood test results (if anemia or pallor is mentioned) — ask parent to upload the PDF report
4. Fever duration and pattern (if fever mentioned)
5. Urine output (wet nappies) — for infants/toddlers

Ask only ONE question per turn. After 2 rounds of gap-filling OR all critical gaps are filled, output:
<phase_transition>
{{"next_phase": "conclusion"}}
</phase_transition>
{_SHARED_RULES}
""",
    DialoguePhase.CONCLUSION: f"""
You are wrapping up the information collection. Thank the parent warmly. Explain that:
1. All the information has been sent to the doctor for review.
2. The doctor will review and respond shortly (typically within [timeframe]).
3. While waiting, the parent should [watchful waiting advice — to be filled by doctor].

Then output the structured summary for the doctor:
<subjective_summary>
{{"chief_complaint": "...", "hpi": "...", "duration_days": ..., "associated_symptoms": [...], "relevant_history": "...", "parent_narrative_raw": "..."}}
</subjective_summary>
{_SHARED_RULES}
""",
}


class DialogueAgent(BaseAgent):
    role = AgentRole.DIALOGUE

    def __init__(self, settings: Settings, db: Database, semaphore):
        super().__init__(settings, db, semaphore)
        self.model_name = settings.model_dialogue

    async def process(self, message: AgentMessage) -> AgentMessage:
        try:
            patient = await self.db.get_or_create_patient(message.patient_id)
            phase = DialoguePhase(
                message.payload.get("dialogue_phase", patient.current_dialogue_phase.value)
            )
            conversation_history = message.payload.get("conversation_history", [])
            parent_text = message.payload.get("text", "")

            system_prompt = SYSTEM_PROMPTS.get(phase, SYSTEM_PROMPTS[DialoguePhase.COMPLAINT_COLLECTION])

            # Build context: patient info + conversation history
            context = self._build_context(patient, conversation_history, parent_text)

            response_text = await self.generate(
                system_prompt=system_prompt,
                user_content=context,
                temperature=0.7,        # Higher temp for empathetic, natural responses
                max_output_tokens=1024,
            )

            # Parse phase transitions and structured output
            result = self._parse_response(response_text, phase, patient)
            result["agent_response_to_parent"] = self._extract_parent_message(response_text)
            result["dialogue_phase"] = result.get("next_phase", phase.value)

            return self.make_result(message, result)

        except Exception as exc:
            return self.make_error(message, str(exc))

    def _build_context(
        self,
        patient: PatientState,
        history: list[dict],
        new_message: str,
    ) -> str:
        age_str = f"{patient.age_years():.1f} years" if patient.age_months() >= 24 else f"{patient.age_months()} months"
        patient_context = (
            f"Patient: {patient.name}, {age_str}, {patient.sex}\n"
            f"Allergies: {', '.join(patient.known_allergies) or 'None known'}\n"
            f"Chronic conditions: {', '.join(patient.chronic_conditions) or 'None'}\n"
        )

        history_str = "\n".join(
            f"{'Parent' if h['role'] == 'parent' else 'Assistant'}: {h['content']}"
            for h in history[-10:]   # Last 10 turns for context window efficiency
        )

        return f"PATIENT CONTEXT:\n{patient_context}\n\nCONVERSATION:\n{history_str}\n\nParent: {new_message}"

    def _parse_response(self, text: str, current_phase: DialoguePhase, patient: PatientState) -> dict:
        result: dict = {"raw_response": text}

        # Check for phase transition
        if "<phase_transition>" in text and "</phase_transition>" in text:
            start = text.index("<phase_transition>") + len("<phase_transition>")
            end = text.index("</phase_transition>")
            try:
                transition = json.loads(text[start:end].strip())
                result["next_phase"] = transition.get("next_phase", current_phase.value)
                if "collected" in transition:
                    result["collected_fields"] = transition["collected"]
            except json.JSONDecodeError:
                pass

        # Check for subjective summary (conclusion phase)
        if "<subjective_summary>" in text and "</subjective_summary>" in text:
            start = text.index("<subjective_summary>") + len("<subjective_summary>")
            end = text.index("</subjective_summary>")
            try:
                summary = json.loads(text[start:end].strip())
                result["subjective_block"] = summary
            except json.JSONDecodeError:
                pass

        return result

    def _extract_parent_message(self, text: str) -> str:
        """Strip XML tags — only return the plain text the parent should see."""
        import re
        clean = re.sub(r"<[^>]+>.*?</[^>]+>", "", text, flags=re.DOTALL)
        return clean.strip()
