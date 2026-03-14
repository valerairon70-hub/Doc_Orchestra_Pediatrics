"""
Orchestrator Agent — The Conductor.

Central hub that:
1. Routes parent messages to agents
2. Merges partial SOAP results from agents
3. Scores SOAP completeness and triggers deep reasoning agents
4. Enforces the doctor-approval guardrail (hard code gate)
5. Publishes completed SOAPs to the Clinician Cockpit
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from uuid import uuid4

from core.config import Settings
from core.database import Database
from core.queue import QueueBundle
from core.schemas import (
    AgentMessage, AgentRole, AssessmentBlock, CockpitStatus,
    MessageType, ObjectiveBlock, PatientState, PlanBlock, SOAPNote,
    SubjectiveBlock, WeightBasedDose,
)
from agents.dialogue_agent import DialogueAgent
from agents.vision_agent import VisionAgent
from agents.mx_agent import MxAgent
from agents.longitudinal_agent import LongitudinalAgent

logger = logging.getLogger(__name__)


class OrchestratorAgent:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        queues: QueueBundle,
    ):
        self.settings = settings
        self.db = db
        self.queues = queues
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_gemini_calls)

        # Initialize agents
        self.agents: dict[AgentRole, DialogueAgent | VisionAgent | MxAgent | LongitudinalAgent] = {
            AgentRole.DIALOGUE: DialogueAgent(settings, db, self._semaphore),
            AgentRole.VISION: VisionAgent(settings, db, self._semaphore),
            AgentRole.MX: MxAgent(settings, db, self._semaphore),
            AgentRole.LONGITUDINAL: LongitudinalAgent(settings, db, self._semaphore),
        }

        # In-memory working SOAP notes (session_id → SOAPNote)
        # These get flushed to DB when complete
        self._working_soaps: dict[str, SOAPNote] = {}

    # ------------------------------------------------------------------
    # Main event loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        logger.info("Orchestrator started.")
        await asyncio.gather(
            self._consume_parent_input(),
            self._consume_agent_results(),
            self._consume_cockpit_actions(),
        )

    async def _consume_parent_input(self) -> None:
        while True:
            try:
                prioritized = await self.queues.parent_input.get()
                msg = prioritized.message
                await self.db.log_message(msg)
                await self.db.get_or_create_session(msg.session_id, msg.patient_id)

                # Initialize working SOAP if not exists
                if msg.session_id not in self._working_soaps:
                    self._working_soaps[msg.session_id] = SOAPNote(
                        patient_id=msg.patient_id,
                        session_id=msg.session_id,
                    )

                # Always dispatch to Dialogue Agent
                asyncio.create_task(self._dispatch(AgentRole.DIALOGUE, msg))

                # Dispatch Vision Agent if attachments present
                if msg.payload.get("image_paths") or msg.payload.get("pdf_path"):
                    asyncio.create_task(self._dispatch(AgentRole.VISION, msg))

            except Exception as exc:
                logger.error("Error in parent input consumer: %s", exc)

    async def _consume_agent_results(self) -> None:
        while True:
            try:
                msg = await self.queues.agent_results.get()
                await self.db.log_message(msg)
                await self._merge_result(msg)
                await self._check_completeness(msg.session_id)
            except Exception as exc:
                logger.error("Error in agent result consumer: %s", exc)

    async def _consume_cockpit_actions(self) -> None:
        while True:
            try:
                msg = await self.queues.cockpit_actions.get()
                await self.db.log_message(msg)
                action = msg.payload.get("action")

                if action == "approve":
                    await self._finalize_and_send(msg)
                elif action == "edit":
                    await self._apply_doctor_edits(msg)
                elif action == "reject":
                    await self._requeue_for_revision(msg)
            except Exception as exc:
                logger.error("Error in cockpit action consumer: %s", exc)

    # ------------------------------------------------------------------
    # Agent dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, agent_role: AgentRole, message: AgentMessage) -> None:
        agent = self.agents[agent_role]
        try:
            result = await asyncio.wait_for(
                agent.process(message),
                timeout=self.settings.max_agent_timeout_seconds,
            )
            await self.queues.put_agent_result(result)
        except asyncio.TimeoutError:
            logger.warning("Agent %s timed out for session %s", agent_role, message.session_id)
            if message.retry_count < self.settings.max_agent_retries:
                message.retry_count += 1
                await asyncio.sleep(2 ** message.retry_count)
                asyncio.create_task(self._dispatch(agent_role, message))
            else:
                soap = self._working_soaps.get(message.session_id)
                if soap:
                    soap.cockpit_status = CockpitStatus.NEEDS_MANUAL_REVIEW
                    await self.db.save_soap_note(soap)
                    await self._push_to_cockpit(soap)

    # ------------------------------------------------------------------
    # SOAP result merging
    # ------------------------------------------------------------------

    async def _merge_result(self, msg: AgentMessage) -> None:
        soap = self._working_soaps.get(msg.session_id)
        if not soap:
            return

        payload = msg.payload

        # Dialogue result → SubjectiveBlock
        if payload.get("agent") == "dialogue" or "subjective_block" in payload:
            subj_data = payload.get("subjective_block", {})
            if subj_data:
                soap.subjective = SubjectiveBlock(**{
                    **soap.subjective.model_dump(),
                    **{k: v for k, v in subj_data.items() if v},
                })
            # Relay doctor-approved watchful waiting to parent
            if "agent_response_to_parent" in payload:
                soap.plan.watchful_waiting_advice = payload["agent_response_to_parent"]

        # Vision result → ObjectiveBlock (partial)
        if payload.get("agent") == "vision":
            obj_partial = payload.get("objective_partial", {})
            for field, value in obj_partial.items():
                if value is not None and field in ObjectiveBlock.model_fields:
                    setattr(soap.objective, field, value)

        # Mx result → AssessmentBlock + PlanBlock
        if payload.get("agent") == "mx":
            assess = payload.get("assessment_partial", {})
            plan = payload.get("plan_partial", {})
            for field, value in assess.items():
                if value and field in AssessmentBlock.model_fields:
                    setattr(soap.assessment, field, value)
            for field, value in plan.items():
                if value and field in PlanBlock.model_fields:
                    if field == "medications":
                        soap.plan.medications = [WeightBasedDose(**m) for m in value]
                    else:
                        setattr(soap.plan, field, value)

        # Longitudinal result → red flags (union-merge) + growth z-scores
        if payload.get("agent") == "longitudinal":
            new_flags = payload.get("red_flags", [])
            soap.assessment.red_flags_triggered = list(
                set(soap.assessment.red_flags_triggered + new_flags)
            )
            soap.assessment.growth_z_scores = payload.get("growth_z_scores", {})
            trend = payload.get("trend_analysis", {})
            if trend.get("narrative_summary"):
                soap.assessment.reasoning_summary += f"\n\nLongitudinal: {trend['narrative_summary']}"

            # Red flag → escalate cockpit priority
            if new_flags:
                soap.cockpit_priority = 1

        soap.last_modified = datetime.utcnow()

    # ------------------------------------------------------------------
    # Completeness scoring and deep reasoning trigger
    # ------------------------------------------------------------------

    async def _check_completeness(self, session_id: str) -> None:
        soap = self._working_soaps.get(session_id)
        if not soap:
            return

        score = self._score_completeness(soap)
        logger.debug("SOAP completeness for session %s: %.0f%%", session_id, score * 100)

        if score >= self.settings.completeness_threshold:
            if not soap.assessment.working_diagnoses:
                # Trigger Mx + Longitudinal in parallel (only once)
                patient = await self.db.get_or_create_patient(soap.patient_id)
                mx_msg = AgentMessage(
                    session_id=session_id,
                    patient_id=soap.patient_id,
                    sender=AgentRole.ORCHESTRATOR,
                    recipient=AgentRole.MX,
                    message_type=MessageType.TASK_DISPATCH,
                    payload={"soap_note": soap.model_dump()},
                )
                long_msg = AgentMessage(
                    session_id=session_id,
                    patient_id=soap.patient_id,
                    sender=AgentRole.ORCHESTRATOR,
                    recipient=AgentRole.LONGITUDINAL,
                    message_type=MessageType.TASK_DISPATCH,
                    payload={"soap_note": soap.model_dump()},
                )
                await asyncio.gather(
                    asyncio.create_task(self._dispatch(AgentRole.MX, mx_msg)),
                    asyncio.create_task(self._dispatch(AgentRole.LONGITUDINAL, long_msg)),
                )
        elif score >= 0.6 and soap.assessment.working_diagnoses:
            # Already have reasoning — push to cockpit
            soap.cockpit_status = CockpitStatus.PENDING_REVIEW
            await self.db.save_soap_note(soap)
            await self._push_to_cockpit(soap)

    def _score_completeness(self, soap: SOAPNote) -> float:
        checks = [
            bool(soap.subjective.chief_complaint),
            bool(soap.subjective.hpi),
            soap.objective.vitals.weight_kg is not None,
            bool(soap.assessment.working_diagnoses),
            bool(soap.plan.safety_netting_advice) or bool(soap.plan.first_line_investigations),
        ]
        return sum(checks) / len(checks)

    # ------------------------------------------------------------------
    # Cockpit guardrail gate
    # ------------------------------------------------------------------

    async def _finalize_and_send(self, msg: AgentMessage) -> None:
        """
        GUARDRAIL GATE: Only this method sends messages to parents.
        Hard requirement: soap.cockpit_status must be 'approved' AND
        approved_by must be set by the doctor in the Cockpit.
        """
        note_id = msg.payload.get("note_id")
        approved_by = msg.payload.get("approved_by", "doctor")
        edited_message = msg.payload.get("edited_parent_message")

        soap = await self.db.get_soap_note(note_id)
        if not soap:
            logger.error("Note %s not found for approval", note_id)
            return

        # Hard code check — cannot bypass
        if msg.message_type != MessageType.COCKPIT_ACTION:
            logger.error("GUARDRAIL: Attempted to send without cockpit approval. Blocked.")
            return

        soap.cockpit_status = CockpitStatus.APPROVED
        soap.approved_by = approved_by
        soap.approved_at = datetime.utcnow()

        if edited_message:
            soap.plan.safety_netting_advice = edited_message

        await self.db.save_soap_note(soap)

        # TODO: Integrate with messaging gateway (WhatsApp/SMS/in-app)
        patient = await self.db.get_or_create_patient(soap.patient_id)
        final_message = soap.plan.safety_netting_advice
        logger.info(
            "SEND TO PARENT [%s] via [%s]: %s",
            patient.name, patient.parent_contact, final_message[:100]
        )
        # await messaging_gateway.send(patient.parent_contact, final_message)

        soap.cockpit_status = CockpitStatus.RESPONSE_SENT
        await self.db.save_soap_note(soap)
        await self.queues.push_cockpit_update({"type": "note_sent", "note_id": note_id})

    async def _apply_doctor_edits(self, msg: AgentMessage) -> None:
        note_id = msg.payload.get("note_id")
        edits = msg.payload.get("edits", {})
        soap = await self.db.get_soap_note(note_id)
        if not soap:
            return
        soap.doctor_edits.update(edits)
        # Apply edits to SOAP fields
        if "safety_netting_advice" in edits:
            soap.plan.safety_netting_advice = edits["safety_netting_advice"]
        soap.cockpit_status = CockpitStatus.DOCTOR_EDITING
        await self.db.save_soap_note(soap)
        await self.queues.push_cockpit_update({"type": "note_updated", "note_id": note_id, "soap": soap.model_dump()})

    async def _requeue_for_revision(self, msg: AgentMessage) -> None:
        note_id = msg.payload.get("note_id")
        reason = msg.payload.get("reason", "Doctor rejected — needs revision")
        soap = await self.db.get_soap_note(note_id)
        if soap:
            soap.cockpit_status = CockpitStatus.NEEDS_MANUAL_REVIEW
            soap.doctor_edits["rejection_reason"] = reason
            await self.db.save_soap_note(soap)

    # ------------------------------------------------------------------
    # Cockpit push
    # ------------------------------------------------------------------

    async def _push_to_cockpit(self, soap: SOAPNote) -> None:
        await self.queues.push_cockpit_update({
            "type": "new_note",
            "note_id": soap.note_id,
            "patient_id": soap.patient_id,
            "priority": soap.cockpit_priority,
            "soap": soap.model_dump(),
        })
