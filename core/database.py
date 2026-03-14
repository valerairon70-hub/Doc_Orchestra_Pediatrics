"""
Async SQLite persistence layer using SQLAlchemy 2.0.
Tables: patients, soap_notes, sessions, audit_log
"""
from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from core.schemas import (
    AgentMessage, CockpitStatus, DialoguePhase, PatientState, SOAPNote
)


# ---------------------------------------------------------------------------
# DDL — table definitions as raw SQL for portability
# ---------------------------------------------------------------------------

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS patients (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    dob         TEXT NOT NULL,
    sex         TEXT NOT NULL,
    state_json  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS soap_notes (
    note_id         TEXT PRIMARY KEY,
    patient_id      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    cockpit_status  TEXT NOT NULL DEFAULT 'pending_review',
    cockpit_priority INTEGER NOT NULL DEFAULT 5,
    created_at      TEXT NOT NULL,
    note_json       TEXT NOT NULL,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    patient_id      TEXT NOT NULL,
    dialogue_phase  TEXT NOT NULL DEFAULT 'greeting',
    started_at      TEXT NOT NULL,
    closed_at       TEXT,
    FOREIGN KEY (patient_id) REFERENCES patients(id)
);

CREATE TABLE IF NOT EXISTS audit_log (
    log_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id      TEXT NOT NULL,
    session_id      TEXT NOT NULL,
    sender          TEXT NOT NULL,
    recipient       TEXT NOT NULL,
    message_type    TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_soap_status ON soap_notes(cockpit_status, cockpit_priority, created_at);
CREATE INDEX IF NOT EXISTS idx_soap_patient ON soap_notes(patient_id);
CREATE INDEX IF NOT EXISTS idx_session_patient ON sessions(patient_id);
"""


class Database:
    def __init__(self, db_url: str):
        self.engine = create_async_engine(db_url, echo=False)
        self._session_factory = async_sessionmaker(
            self.engine, expire_on_commit=False, class_=AsyncSession
        )

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            for statement in _SCHEMA_SQL.strip().split(";"):
                stmt = statement.strip()
                if stmt:
                    await conn.execute(text(stmt))

    def session(self) -> AsyncGenerator[AsyncSession, None]:
        return self._session_factory()

    async def run_cleanup_loop(self, interval_seconds: int = 3600) -> None:
        """Periodically close stale sessions (>24h without activity)."""
        while True:
            await asyncio.sleep(interval_seconds)
            cutoff = datetime.now(timezone.utc).isoformat()
            async with self._session_factory() as sess:
                await sess.execute(
                    text(
                        "UPDATE sessions SET closed_at = :now "
                        "WHERE closed_at IS NULL AND started_at < datetime(:now, '-24 hours')"
                    ),
                    {"now": cutoff},
                )
                await sess.commit()

    # ------------------------------------------------------------------
    # Patient State
    # ------------------------------------------------------------------

    async def get_or_create_patient(self, patient_id: str, defaults: dict | None = None) -> PatientState:
        async with self._session_factory() as sess:
            row = (await sess.execute(
                text("SELECT state_json FROM patients WHERE id = :id"),
                {"id": patient_id},
            )).fetchone()
            if row:
                return PatientState.model_validate_json(row[0])
            if defaults is None:
                raise ValueError(f"Patient {patient_id} not found and no defaults provided")
            state = PatientState(patient_id=patient_id, **defaults)
            await self._insert_patient(sess, state)
            await sess.commit()
            return state

    async def save_patient_state(self, state: PatientState) -> None:
        async with self._session_factory() as sess:
            now = datetime.utcnow().isoformat()
            await sess.execute(
                text(
                    "INSERT INTO patients (id, name, dob, sex, state_json, updated_at) "
                    "VALUES (:id, :name, :dob, :sex, :json, :now) "
                    "ON CONFLICT(id) DO UPDATE SET state_json=excluded.state_json, updated_at=excluded.updated_at"
                ),
                {
                    "id": state.patient_id,
                    "name": state.name,
                    "dob": str(state.dob),
                    "sex": state.sex,
                    "json": state.model_dump_json(),
                    "now": now,
                },
            )
            await sess.commit()

    async def _insert_patient(self, sess: AsyncSession, state: PatientState) -> None:
        now = datetime.utcnow().isoformat()
        await sess.execute(
            text(
                "INSERT OR IGNORE INTO patients (id, name, dob, sex, state_json, updated_at) "
                "VALUES (:id, :name, :dob, :sex, :json, :now)"
            ),
            {
                "id": state.patient_id,
                "name": state.name,
                "dob": str(state.dob),
                "sex": state.sex,
                "json": state.model_dump_json(),
                "now": now,
            },
        )

    # ------------------------------------------------------------------
    # SOAP Notes
    # ------------------------------------------------------------------

    async def save_soap_note(self, note: SOAPNote) -> None:
        async with self._session_factory() as sess:
            await sess.execute(
                text(
                    "INSERT INTO soap_notes (note_id, patient_id, session_id, cockpit_status, "
                    "cockpit_priority, created_at, note_json) "
                    "VALUES (:note_id, :patient_id, :session_id, :status, :priority, :created_at, :json) "
                    "ON CONFLICT(note_id) DO UPDATE SET cockpit_status=excluded.cockpit_status, "
                    "cockpit_priority=excluded.cockpit_priority, note_json=excluded.note_json"
                ),
                {
                    "note_id": note.note_id,
                    "patient_id": note.patient_id,
                    "session_id": note.session_id,
                    "status": note.cockpit_status.value,
                    "priority": note.cockpit_priority,
                    "created_at": note.created_at.isoformat(),
                    "json": note.model_dump_json(),
                },
            )
            await sess.commit()

    async def get_soap_note(self, note_id: str) -> SOAPNote | None:
        async with self._session_factory() as sess:
            row = (await sess.execute(
                text("SELECT note_json FROM soap_notes WHERE note_id = :id"),
                {"id": note_id},
            )).fetchone()
            return SOAPNote.model_validate_json(row[0]) if row else None

    async def get_working_soap(self, session_id: str) -> SOAPNote | None:
        async with self._session_factory() as sess:
            row = (await sess.execute(
                text(
                    "SELECT note_json FROM soap_notes WHERE session_id = :sid "
                    "ORDER BY created_at DESC LIMIT 1"
                ),
                {"sid": session_id},
            )).fetchone()
            return SOAPNote.model_validate_json(row[0]) if row else None

    async def get_pending_cockpit_queue(self) -> list[SOAPNote]:
        """Returns notes ordered by priority ASC, then created_at ASC."""
        async with self._session_factory() as sess:
            rows = (await sess.execute(
                text(
                    "SELECT note_json FROM soap_notes "
                    "WHERE cockpit_status = 'pending_review' "
                    "ORDER BY cockpit_priority ASC, created_at ASC"
                )
            )).fetchall()
            return [SOAPNote.model_validate_json(r[0]) for r in rows]

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def get_or_create_session(self, session_id: str, patient_id: str) -> dict:
        async with self._session_factory() as sess:
            row = (await sess.execute(
                text("SELECT session_id, dialogue_phase FROM sessions WHERE session_id = :id"),
                {"id": session_id},
            )).fetchone()
            if row:
                return {"session_id": row[0], "dialogue_phase": row[1]}
            now = datetime.utcnow().isoformat()
            await sess.execute(
                text(
                    "INSERT INTO sessions (session_id, patient_id, dialogue_phase, started_at) "
                    "VALUES (:sid, :pid, 'greeting', :now)"
                ),
                {"sid": session_id, "pid": patient_id, "now": now},
            )
            await sess.commit()
            return {"session_id": session_id, "dialogue_phase": "greeting"}

    async def update_session_phase(self, session_id: str, phase: DialoguePhase) -> None:
        async with self._session_factory() as sess:
            await sess.execute(
                text("UPDATE sessions SET dialogue_phase = :phase WHERE session_id = :id"),
                {"phase": phase.value, "id": session_id},
            )
            await sess.commit()

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def log_message(self, msg: AgentMessage) -> None:
        async with self._session_factory() as sess:
            await sess.execute(
                text(
                    "INSERT INTO audit_log "
                    "(message_id, session_id, sender, recipient, message_type, payload_json, created_at) "
                    "VALUES (:mid, :sid, :sender, :recv, :mtype, :payload, :ts)"
                ),
                {
                    "mid": msg.message_id,
                    "sid": msg.session_id,
                    "sender": msg.sender.value,
                    "recv": msg.recipient.value,
                    "mtype": msg.message_type.value,
                    "payload": json.dumps(msg.payload),
                    "ts": msg.created_at.isoformat(),
                },
            )
            await sess.commit()
