"""
Doc Orchestra — Pediatric Async Patient Management System
==========================================================

Entry point. Starts:
  1. Database (SQLite async)
  2. Orchestrator (multi-agent routing hub)
  3. Clinician Cockpit (Rich terminal UI)
  4. Cleanup loop (stale session pruning)

Usage:
  python doc_orchestra.py

Dev simulation:
  python doc_orchestra.py --simulate

Environment:
  Set GEMINI_API_KEY in .env (see .env.example)
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from uuid import uuid4
from datetime import date

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("doc_orchestra")

from core.config import Settings
from core.database import Database
from core.queue import QueueBundle
from core.schemas import AgentMessage, AgentRole, MessageType, PatientState
from orchestrator.orchestrator import OrchestratorAgent
from cockpit.cli_cockpit import ClinicianCockpit


async def main(simulate: bool = False) -> None:
    logger.info("Starting Doc Orchestra...")

    settings = Settings()
    db = Database(settings.db_url)
    await db.init()
    logger.info("Database initialized at %s", settings.db_url)

    queues = QueueBundle()

    orchestrator = OrchestratorAgent(settings, db, queues)
    cockpit = ClinicianCockpit(queues, db)

    tasks = [
        asyncio.create_task(orchestrator.run(), name="orchestrator"),
        asyncio.create_task(cockpit.run(), name="cockpit"),
        asyncio.create_task(db.run_cleanup_loop(), name="db_cleanup"),
    ]

    if simulate:
        logger.info("Running in simulation mode — injecting test messages")
        tasks.append(asyncio.create_task(_simulate_inbound(db, queues), name="simulator"))

    logger.info("Doc Orchestra is running. Open Clinician Cockpit...")

    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        for task in tasks:
            task.cancel()


# ---------------------------------------------------------------------------
# Dev simulator — inject realistic test messages
# ---------------------------------------------------------------------------

async def _simulate_inbound(db: Database, queues: QueueBundle) -> None:
    """Simulates a parent sending messages about a child with anemia."""
    await asyncio.sleep(2)  # Let the system start up

    # Create a test patient
    patient = PatientState(
        patient_id=str(uuid4()),
        name="Amara Osei",
        dob=date(2020, 3, 15),     # ~4 years old
        sex="F",
        parent_contact="+447700900123",
        known_allergies=["Penicillin"],
        hb_history=[
            ("2024-09-01", 10.2),
            ("2024-12-01", 9.6),
        ],
        weight_history=[
            ("2024-09-01", 14.5),
        ],
    )
    await db.save_patient_state(patient)

    session_id = str(uuid4())

    # Simulate a series of parent messages
    scenarios = [
        {
            "text": "Hello, my daughter Amara has been looking very pale for the last 2 weeks. She's also tired all the time and not eating well.",
            "dialogue_phase": "complaint_collection",
        },
        {
            "text": "She is 4 years old. The paleness started gradually. She had a cold about 3 weeks ago but that resolved. No fever now.",
            "dialogue_phase": "complaint_collection",
            "delay": 3,
        },
        {
            "text": "Her weight at last visit was 14.2 kg. I have her blood test from 2 weeks ago — I can upload the PDF.",
            "dialogue_phase": "gap_filling",
            "delay": 3,
            "pdf_path": None,  # In real use: path to uploaded PDF
        },
    ]

    for scenario in scenarios:
        await asyncio.sleep(scenario.get("delay", 1))
        msg = AgentMessage(
            session_id=session_id,
            patient_id=patient.patient_id,
            sender=AgentRole.COCKPIT,     # Simulating inbound from messaging gateway
            recipient=AgentRole.ORCHESTRATOR,
            message_type=MessageType.PARENT_INPUT,
            priority=5,
            payload={
                "text": scenario["text"],
                "dialogue_phase": scenario["dialogue_phase"],
                "conversation_history": [],
                "image_paths": scenario.get("image_paths", []),
                "pdf_path": scenario.get("pdf_path"),
            },
        )
        logger.info("SIM → Parent message: %s...", scenario["text"][:60])
        await queues.put_parent_message(msg)

    logger.info("Simulation complete — check Cockpit for results.")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Doc Orchestra Pediatric AI System")
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="Inject test patient messages for development",
    )
    args = parser.parse_args()

    asyncio.run(main(simulate=args.simulate))
