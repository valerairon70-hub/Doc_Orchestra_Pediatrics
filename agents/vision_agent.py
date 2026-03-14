"""
Multimodal Reasoning Agent — The Vision.

Analyzes:
- Skin photos: morphology, color, distribution, age of lesion
- PDF blood reports: extracts Hb, ferritin, MCV, MCHC, RDW, WBC, CRP

Auto-fills the Objective block of the SOAP note.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path

import google.generativeai as genai

from core.config import Settings
from core.database import Database
from core.schemas import AgentMessage, AgentRole, ObjectiveBlock
from agents.base_agent import BaseAgent
from tools.pdf_extractor import extract_blood_report

logger = logging.getLogger(__name__)

_SKIN_ANALYSIS_PROMPT = """
You are assisting a pediatric infectious disease specialist by analyzing a clinical photograph.

Describe the skin findings using this exact JSON structure. Be precise and objective.
Do NOT suggest a diagnosis.

{
  "lesion_morphology": "macule|papule|vesicle|pustule|plaque|wheal|other",
  "lesion_color": "erythematous|violaceous|hypopigmented|brown|other",
  "blanching": "yes|no|not_assessed",
  "distribution": "localized|generalized|centripetal|peripheral|flexural|other",
  "estimated_lesion_age": "acute_<48h|subacute_2-7d|chronic_>7d|unknown",
  "notable_features": "e.g. central clearing, crusting, satellite lesions, purpura",
  "image_quality": "good|poor_lighting|blurred|partial"
}

Return ONLY valid JSON. No explanations.
"""

_LAB_EXTRACTION_PROMPT = """
You are extracting laboratory values from a blood test report for a pediatric patient.

Extract these values and return ONLY valid JSON. Use null for any value not found.

{
  "hb_gdl": null,
  "ferritin_ugL": null,
  "mcv_fl": null,
  "mchc_gdl": null,
  "rdw_percent": null,
  "wbc_count": null,
  "crp_mgL": null,
  "report_date": null,
  "lab_name": null
}

The report text is below:
"""


class VisionAgent(BaseAgent):
    role = AgentRole.VISION

    def __init__(self, settings: Settings, db: Database, semaphore):
        super().__init__(settings, db, semaphore)
        self.model_name = settings.model_vision

    async def process(self, message: AgentMessage) -> AgentMessage:
        try:
            payload = message.payload
            objective_partial: dict = {}

            # Process skin images
            image_paths: list[str] = payload.get("image_paths", [])
            if image_paths:
                skin_result = await self._analyze_images(image_paths)
                objective_partial.update(skin_result)
                objective_partial["image_paths"] = image_paths

            # Process PDF blood report
            pdf_path: str | None = payload.get("pdf_path")
            if pdf_path:
                lab_result = await self._analyze_pdf(pdf_path)
                objective_partial.update(lab_result)
                objective_partial["raw_report_path"] = pdf_path

            return self.make_result(message, {
                "objective_partial": objective_partial,
                "agent": "vision",
            })

        except Exception as exc:
            return self.make_error(message, str(exc))

    # ------------------------------------------------------------------
    # Image analysis
    # ------------------------------------------------------------------

    async def _analyze_images(self, image_paths: list[str]) -> dict:
        parts = []
        for path in image_paths[:3]:    # Max 3 images per call
            image_data = self._load_image_as_part(path)
            if image_data:
                parts.append(image_data)

        if not parts:
            return {}

        parts.append(_SKIN_ANALYSIS_PROMPT)

        try:
            response_text = await self.generate(
                system_prompt="You are a clinical image analysis assistant. Return only valid JSON.",
                user_content=parts,
                temperature=0.1,
                max_output_tokens=512,
            )
            data = json.loads(response_text.strip().strip("```json").strip("```"))
            return {
                "skin_findings": f"{data.get('lesion_morphology', '')} — {data.get('notable_features', '')}".strip(" —"),
                "skin_distribution": data.get("distribution"),
                "_skin_raw": data,
            }
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Image analysis failed: %s", exc)
            return {"skin_findings": "Image analysis failed — manual review required"}

    def _load_image_as_part(self, path: str) -> dict | None:
        try:
            p = Path(path)
            if not p.exists():
                return None
            mime_type, _ = mimetypes.guess_type(str(p))
            if mime_type not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                return None
            with open(p, "rb") as f:
                data = base64.b64encode(f.read()).decode("utf-8")
            return {"inline_data": {"mime_type": mime_type, "data": data}}
        except Exception as exc:
            logger.warning("Could not load image %s: %s", path, exc)
            return None

    # ------------------------------------------------------------------
    # PDF / lab report analysis
    # ------------------------------------------------------------------

    async def _analyze_pdf(self, pdf_path: str) -> dict:
        # Step 1: Extract raw text with pdfplumber
        raw_text = extract_blood_report(pdf_path)
        if not raw_text:
            return {}

        # Step 2: Use LLM to parse structured values
        try:
            response_text = await self.generate(
                system_prompt="You extract laboratory values from medical reports. Return only valid JSON.",
                user_content=_LAB_EXTRACTION_PROMPT + "\n\n" + raw_text[:8000],  # Truncate for token budget
                temperature=0.0,
                max_output_tokens=256,
            )
            data = json.loads(response_text.strip().strip("```json").strip("```"))
            return {k: v for k, v in data.items() if v is not None and k in ObjectiveBlock.model_fields}
        except (json.JSONDecodeError, Exception) as exc:
            logger.warning("Lab report parsing failed: %s", exc)
            return {}
