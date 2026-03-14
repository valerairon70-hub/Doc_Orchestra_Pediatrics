"""
All Pydantic v2 data models for Doc Orchestra.
These are the canonical data contracts shared across all agents.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    DIALOGUE = "dialogue"
    VISION = "vision"
    MX = "mx"
    LONGITUDINAL = "longitudinal"
    COCKPIT = "cockpit"


class MessageType(str, Enum):
    PARENT_INPUT = "parent_input"
    AGENT_RESULT = "agent_result"
    TASK_DISPATCH = "task_dispatch"
    COCKPIT_ACTION = "cockpit_action"
    RED_FLAG_ALERT = "red_flag_alert"
    ERROR = "error"


class DialoguePhase(str, Enum):
    GREETING = "greeting"
    COMPLAINT_COLLECTION = "complaint_collection"
    GAP_FILLING = "gap_filling"
    CONCLUSION = "conclusion"
    AWAITING_DOCTOR_APPROVAL = "awaiting_doctor_approval"
    RESPONSE_DELIVERED = "response_delivered"


class CockpitStatus(str, Enum):
    PENDING_REVIEW = "pending_review"
    DOCTOR_EDITING = "doctor_editing"
    APPROVED = "approved"
    RESPONSE_SENT = "response_sent"
    NEEDS_MANUAL_REVIEW = "needs_manual_review"


# ---------------------------------------------------------------------------
# Clinical building blocks
# ---------------------------------------------------------------------------

class Vitals(BaseModel):
    weight_kg: float | None = None
    height_cm: float | None = None
    temperature_c: float | None = None
    heart_rate: int | None = None
    spo2_percent: float | None = None
    respiratory_rate: int | None = None


class WeightBasedDose(BaseModel):
    drug_name: str
    dose_mg_per_kg: float
    patient_weight_kg: float
    calculated_dose_mg: float
    frequency: str                 # e.g. "BD", "TDS", "OD"
    route: str                     # e.g. "oral", "IV"
    duration_days: int
    max_dose_mg: float | None = None
    notes: str | None = None       # e.g. "Take with food"
    guideline_source: str | None = None  # e.g. "NICE NG201"


# ---------------------------------------------------------------------------
# SOAP Note blocks
# ---------------------------------------------------------------------------

class SubjectiveBlock(BaseModel):
    chief_complaint: str = ""
    hpi: str = ""                          # History of Present Illness
    duration_days: int | None = None
    associated_symptoms: list[str] = []
    relevant_history: str = ""             # Allergies, prior episodes
    parent_narrative_raw: str = ""         # Verbatim parent input, preserved for audit


class ObjectiveBlock(BaseModel):
    vitals: Vitals = Field(default_factory=Vitals)
    # Filled by VisionAgent
    skin_findings: str | None = None       # e.g. "Maculopapular, erythematous, blanching"
    skin_distribution: str | None = None
    image_paths: list[str] = []
    # Filled by VisionAgent from PDF blood report
    hb_gdl: float | None = None
    ferritin_ugL: float | None = None
    mcv_fl: float | None = None
    mchc_gdl: float | None = None
    rdw_percent: float | None = None
    wbc_count: float | None = None
    crp_mgL: float | None = None
    raw_report_path: str | None = None


class AssessmentBlock(BaseModel):
    working_diagnoses: list[str] = []
    differential_diagnoses: list[str] = []
    red_flags_triggered: list[str] = []   # Union-merged, never overwritten
    reasoning_summary: str = ""
    growth_z_scores: dict[str, float] = {}  # waz, haz, bmiz


class PlanBlock(BaseModel):
    first_line_investigations: list[str] = []
    second_line_investigations: list[str] = []
    medications: list[WeightBasedDose] = []
    watchful_waiting_advice: str = ""      # Sent to parent while awaiting doctor review
    safety_netting_advice: str = ""        # Approved text for parent
    follow_up_days: int | None = None
    guideline_citations: list[str] = []


class SOAPNote(BaseModel):
    note_id: str = Field(default_factory=lambda: str(uuid4()))
    patient_id: str
    session_id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)
    last_modified: datetime = Field(default_factory=datetime.utcnow)

    subjective: SubjectiveBlock = Field(default_factory=SubjectiveBlock)
    objective: ObjectiveBlock = Field(default_factory=ObjectiveBlock)
    assessment: AssessmentBlock = Field(default_factory=AssessmentBlock)
    plan: PlanBlock = Field(default_factory=PlanBlock)

    # Cockpit workflow
    cockpit_status: CockpitStatus = CockpitStatus.PENDING_REVIEW
    cockpit_priority: int = 5             # 1 = urgent red flag, 5 = normal
    doctor_edits: dict = {}
    approved_by: str | None = None
    approved_at: datetime | None = None
    parent_message_draft: str | None = None   # GuardRail: only sent after approval

    model_config = ConfigDict(json_encoders={date: str, datetime: str})


# ---------------------------------------------------------------------------
# Patient State (persistent across visits)
# ---------------------------------------------------------------------------

class VisitSummary(BaseModel):
    visit_id: str = Field(default_factory=lambda: str(uuid4()))
    date: datetime
    chief_complaint: str
    soap_note_id: str | None = None
    approved_by_doctor: bool = False
    diagnosis_summary: str = ""


class PatientState(BaseModel):
    patient_id: str = Field(default_factory=lambda: str(uuid4()))
    name: str
    dob: date
    sex: Literal["M", "F", "Other"]
    parent_contact: str                    # Phone or messenger handle

    # Longitudinal time-series data (date as ISO string, value as float)
    weight_history: list[tuple[str, float]] = []
    height_history: list[tuple[str, float]] = []
    hb_history: list[tuple[str, float]] = []
    ferritin_history: list[tuple[str, float]] = []
    crp_history: list[tuple[str, float]] = []

    visits: list[VisitSummary] = []
    active_medications: list[str] = []
    known_allergies: list[str] = []
    chronic_conditions: list[str] = []

    # Current session scratch-pad (cleared after visit closure)
    current_session_id: str | None = None
    current_dialogue_phase: DialoguePhase = DialoguePhase.GREETING
    pending_clarifications: list[str] = []

    model_config = ConfigDict(json_encoders={date: str, datetime: str})

    def age_months(self) -> int:
        today = date.today()
        delta = (today.year - self.dob.year) * 12 + (today.month - self.dob.month)
        return max(0, delta)

    def age_years(self) -> float:
        return self.age_months() / 12.0


# ---------------------------------------------------------------------------
# Agent message bus
# ---------------------------------------------------------------------------

class AgentMessage(BaseModel):
    message_id: str = Field(default_factory=lambda: str(uuid4()))
    session_id: str
    patient_id: str
    sender: AgentRole
    recipient: AgentRole
    message_type: MessageType
    priority: int = 5              # 1=urgent, 5=normal, 10=background
    payload: dict = {}
    created_at: datetime = Field(default_factory=datetime.utcnow)
    correlation_id: str | None = None   # Links response to original request
    retry_count: int = 0


# ---------------------------------------------------------------------------
# Priority queue wrapper (for asyncio.PriorityQueue)
# ---------------------------------------------------------------------------

@dataclass(order=True)
class PrioritizedMessage:
    priority: int
    message: AgentMessage = field(compare=False)
