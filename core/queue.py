"""
Async queue bundle for the Doc Orchestra message bus.
All inter-component communication flows through these queues.
"""
import asyncio
from core.schemas import AgentMessage, PrioritizedMessage


class QueueBundle:
    def __init__(self):
        # Inbound from parents/external — priority queue (red flags jump the line)
        self.parent_input: asyncio.PriorityQueue[PrioritizedMessage] = asyncio.PriorityQueue()

        # Agent results flowing back to Orchestrator
        self.agent_results: asyncio.Queue[AgentMessage] = asyncio.Queue()

        # Doctor actions from Cockpit → Orchestrator
        self.cockpit_actions: asyncio.Queue[AgentMessage] = asyncio.Queue()

        # Orchestrator → Cockpit display updates
        self.cockpit_display: asyncio.Queue[dict] = asyncio.Queue()

    async def put_parent_message(self, msg: AgentMessage) -> None:
        await self.parent_input.put(PrioritizedMessage(priority=msg.priority, message=msg))

    async def put_agent_result(self, msg: AgentMessage) -> None:
        await self.agent_results.put(msg)

    async def put_cockpit_action(self, msg: AgentMessage) -> None:
        await self.cockpit_actions.put(msg)

    async def push_cockpit_update(self, update: dict) -> None:
        await self.cockpit_display.put(update)
