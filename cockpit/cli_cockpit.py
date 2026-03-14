"""
Clinician Cockpit — Terminal UI built with Rich.

Doctor interface:
- Prioritized SOAP queue (Red Flags at top)
- One-click approve / edit / reject
- 30-60 second review workflow per case

Controls:
  [A] Approve and send to parent
  [E] Edit parent message before sending
  [R] Reject / request revision
  [N] Next case in queue
  [Q] Quit
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from core.database import Database
from core.queue import QueueBundle
from core.schemas import AgentMessage, AgentRole, CockpitStatus, MessageType, SOAPNote
from tools.dosing_calculator import format_dose_for_display

logger = logging.getLogger(__name__)
console = Console()


class ClinicianCockpit:
    def __init__(self, queues: QueueBundle, db: Database):
        self.queues = queues
        self.db = db
        self._queue: list[SOAPNote] = []
        self._current_index: int = 0
        self._running = True

    async def run(self) -> None:
        """Main cockpit loop — refreshes display and processes keyboard input."""
        # Load initial queue from DB
        self._queue = await self.db.get_pending_cockpit_queue()

        # Start display update listener
        asyncio.create_task(self._listen_for_updates())

        await self._interactive_loop()

    async def _listen_for_updates(self) -> None:
        """Listen for Orchestrator updates and refresh queue."""
        while self._running:
            update = await self.queues.cockpit_display.get()
            update_type = update.get("type")

            if update_type == "new_note":
                self._queue = await self.db.get_pending_cockpit_queue()
                if update.get("priority", 5) == 1:
                    console.print(
                        f"\n[bold red]🚨 RED FLAG ALERT — {update.get('patient_id', 'Unknown patient')}[/bold red]"
                    )
            elif update_type in ("note_sent", "note_updated"):
                self._queue = await self.db.get_pending_cockpit_queue()

    async def _interactive_loop(self) -> None:
        """Non-blocking keyboard input loop using asyncio."""
        with Live(self._render(), refresh_per_second=2, console=console) as live:
            while self._running:
                live.update(self._render())
                try:
                    key = await asyncio.wait_for(
                        asyncio.get_event_loop().run_in_executor(None, self._read_key),
                        timeout=1.0,
                    )
                    await self._handle_key(key)
                    live.update(self._render())
                except asyncio.TimeoutError:
                    pass
                except Exception as exc:
                    logger.error("Cockpit error: %s", exc)

    def _read_key(self) -> str:
        """Blocking read of a single keypress."""
        try:
            import readchar
            return readchar.readkey().lower()
        except ImportError:
            return input("Action [a/e/r/n/q]: ").strip().lower()[:1]

    async def _handle_key(self, key: str) -> None:
        if not self._queue:
            return
        note = self._current_note

        if key == "a":
            await self._approve_current()
        elif key == "e":
            await self._edit_current()
        elif key == "r":
            await self._reject_current()
        elif key == "n":
            self._current_index = (self._current_index + 1) % len(self._queue)
        elif key == "q":
            self._running = False

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _approve_current(self) -> None:
        note = self._current_note
        if not note:
            return
        msg = AgentMessage(
            session_id=note.session_id,
            patient_id=note.patient_id,
            sender=AgentRole.COCKPIT,
            recipient=AgentRole.ORCHESTRATOR,
            message_type=MessageType.COCKPIT_ACTION,
            priority=note.cockpit_priority,
            payload={
                "action": "approve",
                "note_id": note.note_id,
                "approved_by": "doctor",
            },
        )
        await self.queues.put_cockpit_action(msg)
        console.print(f"[green]✓ Approved and queued for sending — {note.patient_id}[/green]")
        self._queue = [n for n in self._queue if n.note_id != note.note_id]
        self._current_index = max(0, self._current_index - 1)

    async def _edit_current(self) -> None:
        note = self._current_note
        if not note:
            return
        console.print("\n[yellow]Current parent message:[/yellow]")
        console.print(Panel(note.plan.safety_netting_advice or "(empty)", border_style="yellow"))
        console.print("[yellow]Enter edited message (press Enter twice to confirm):[/yellow]")
        lines = []
        while True:
            line = await asyncio.get_event_loop().run_in_executor(None, input)
            if line == "" and lines:
                break
            lines.append(line)
        edited = "\n".join(lines)
        msg = AgentMessage(
            session_id=note.session_id,
            patient_id=note.patient_id,
            sender=AgentRole.COCKPIT,
            recipient=AgentRole.ORCHESTRATOR,
            message_type=MessageType.COCKPIT_ACTION,
            payload={
                "action": "approve",
                "note_id": note.note_id,
                "approved_by": "doctor",
                "edited_parent_message": edited,
            },
        )
        await self.queues.put_cockpit_action(msg)
        console.print("[green]✓ Edited message approved.[/green]")

    async def _reject_current(self) -> None:
        note = self._current_note
        if not note:
            return
        reason = await asyncio.get_event_loop().run_in_executor(
            None, lambda: input("Rejection reason: ")
        )
        msg = AgentMessage(
            session_id=note.session_id,
            patient_id=note.patient_id,
            sender=AgentRole.COCKPIT,
            recipient=AgentRole.ORCHESTRATOR,
            message_type=MessageType.COCKPIT_ACTION,
            payload={"action": "reject", "note_id": note.note_id, "reason": reason},
        )
        await self.queues.put_cockpit_action(msg)
        console.print(f"[red]✗ Rejected — {reason}[/red]")

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    @property
    def _current_note(self) -> SOAPNote | None:
        if not self._queue:
            return None
        return self._queue[min(self._current_index, len(self._queue) - 1)]

    def _render(self) -> Layout:
        layout = Layout()
        layout.split_row(
            Layout(self._render_queue(), name="queue", ratio=2),
            Layout(self._render_soap(), name="soap", ratio=3),
        )
        layout.split(
            Layout(layout, name="main"),
            Layout(self._render_controls(), name="controls", size=3),
        )
        return layout

    def _render_queue(self) -> Panel:
        table = Table(box=box.SIMPLE, show_header=True, header_style="bold cyan")
        table.add_column("!", width=2)
        table.add_column("Patient")
        table.add_column("Complaint", max_width=20)
        table.add_column("Status")

        if not self._queue:
            table.add_row("", "[dim]Queue empty[/dim]", "", "")
        else:
            for i, note in enumerate(self._queue[:15]):
                flag = "[bold red]🚨[/bold red]" if note.cockpit_priority == 1 else " "
                style = "bold white on blue" if i == self._current_index else ""
                table.add_row(
                    flag,
                    Text(note.patient_id[:12], style=style),
                    Text(note.subjective.chief_complaint[:20], style=style),
                    Text(note.cockpit_status.value.replace("_", " "), style=style),
                )

        return Panel(table, title=f"[bold]Queue ({len(self._queue)} pending)[/bold]", border_style="cyan")

    def _render_soap(self) -> Panel:
        note = self._current_note
        if not note:
            return Panel("[dim]No cases in queue. Take a break![/dim]", title="SOAP Note", border_style="green")

        s = note.subjective
        o = note.objective
        a = note.assessment
        p = note.plan

        lines = [
            f"[bold]Patient:[/bold] {note.patient_id} | [bold]Priority:[/bold] {'🚨 RED FLAG' if note.cockpit_priority == 1 else 'Normal'}",
            "",
            f"[bold cyan]S —[/bold cyan] {s.chief_complaint}",
            f"  {s.hpi[:200]}" if s.hpi else "",
            f"  Duration: {s.duration_days}d | Symptoms: {', '.join(s.associated_symptoms[:5])}",
            "",
            f"[bold cyan]O —[/bold cyan] Wt: {o.vitals.weight_kg}kg | Temp: {o.vitals.temperature_c}°C | HR: {o.vitals.heart_rate}",
        ]
        if o.hb_gdl:
            lines.append(f"  Hb: [bold]{o.hb_gdl}[/bold] g/dL | Ferritin: {o.ferritin_ugL} µg/L | MCV: {o.mcv_fl} fL")
        if o.skin_findings:
            lines.append(f"  Skin: {o.skin_findings}")

        lines += [
            "",
            f"[bold cyan]A —[/bold cyan] {', '.join(a.working_diagnoses) or 'Pending...'}",
        ]
        if a.red_flags_triggered:
            for flag in a.red_flags_triggered:
                lines.append(f"  [bold red]⚠ {flag}[/bold red]")
        if a.reasoning_summary:
            lines.append(f"  [dim]{a.reasoning_summary[:200]}[/dim]")

        lines += ["", "[bold cyan]P —[/bold cyan]"]
        for med in p.medications[:3]:
            lines.append(f"  💊 {format_dose_for_display(med)}")
        if p.first_line_investigations:
            lines.append(f"  🔬 {' | '.join(p.first_line_investigations[:3])}")

        lines += [
            "",
            "[bold cyan]Parent message draft:[/bold cyan]",
            f"[italic]{(p.safety_netting_advice or p.watchful_waiting_advice or '(generating...)')[:300]}[/italic]",
        ]

        content = "\n".join(filter(lambda l: l is not None, lines))
        return Panel(content, title="[bold]SOAP Note[/bold]", border_style="green")

    def _render_controls(self) -> Panel:
        return Panel(
            "[A] Approve & Send  [E] Edit  [R] Reject  [N] Next  [Q] Quit",
            border_style="dim",
        )
