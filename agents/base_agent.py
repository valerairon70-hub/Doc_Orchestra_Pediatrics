"""
Base agent class.
All agents inherit from BaseAgent which wraps the Google Gemini async client
with retry logic, semaphore rate-limiting, and timeout protection.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import Any

import google.generativeai as genai
from google.generativeai.types import GenerationConfig

from core.config import Settings
from core.database import Database
from core.schemas import AgentMessage, AgentRole, MessageType

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    role: AgentRole
    model_name: str

    def __init__(
        self,
        settings: Settings,
        db: Database,
        semaphore: asyncio.Semaphore,
    ):
        self.settings = settings
        self.db = db
        self._semaphore = semaphore
        genai.configure(api_key=settings.gemini_api_key)

    # ------------------------------------------------------------------
    # Core LLM call
    # ------------------------------------------------------------------

    async def generate(
        self,
        system_prompt: str,
        user_content: str | list,
        *,
        temperature: float = 0.3,
        max_output_tokens: int = 4096,
        thinking_budget: int | None = None,
    ) -> str:
        """
        Async call to Gemini. Handles semaphore, retries, and timeout.
        `user_content` can be a string or a list of parts (for multimodal).
        """
        async with self._semaphore:
            for attempt in range(self.settings.max_agent_retries):
                try:
                    return await asyncio.wait_for(
                        self._call_gemini(
                            system_prompt, user_content,
                            temperature, max_output_tokens, thinking_budget
                        ),
                        timeout=self.settings.max_agent_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    logger.warning("%s timed out (attempt %d)", self.role, attempt + 1)
                    if attempt == self.settings.max_agent_retries - 1:
                        raise
                except Exception as exc:
                    logger.error("%s error (attempt %d): %s", self.role, attempt + 1, exc)
                    if attempt == self.settings.max_agent_retries - 1:
                        raise
                    await asyncio.sleep(2 ** attempt)   # Exponential backoff
        return ""

    async def _call_gemini(
        self,
        system_prompt: str,
        user_content: str | list,
        temperature: float,
        max_output_tokens: int,
        thinking_budget: int | None,
    ) -> str:
        model = genai.GenerativeModel(
            model_name=self.model_name,
            system_instruction=system_prompt,
        )
        generation_config = GenerationConfig(
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
        # Wrap in asyncio.to_thread since google-generativeai SDK uses sync I/O
        response = await asyncio.to_thread(
            model.generate_content,
            user_content,
            generation_config=generation_config,
        )
        return response.text

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @abstractmethod
    async def process(self, message: AgentMessage) -> AgentMessage:
        """Each agent implements its own processing logic."""
        ...

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def make_result(
        self,
        source_message: AgentMessage,
        payload: dict,
        priority: int | None = None,
    ) -> AgentMessage:
        return AgentMessage(
            session_id=source_message.session_id,
            patient_id=source_message.patient_id,
            sender=self.role,
            recipient=AgentRole.ORCHESTRATOR,
            message_type=MessageType.AGENT_RESULT,
            priority=priority or source_message.priority,
            payload=payload,
            correlation_id=source_message.message_id,
        )

    def make_error(self, source_message: AgentMessage, error: str) -> AgentMessage:
        logger.error("[%s] Error processing message %s: %s", self.role, source_message.message_id, error)
        return AgentMessage(
            session_id=source_message.session_id,
            patient_id=source_message.patient_id,
            sender=self.role,
            recipient=AgentRole.ORCHESTRATOR,
            message_type=MessageType.ERROR,
            priority=source_message.priority,
            payload={"error": error, "source_message_id": source_message.message_id},
            correlation_id=source_message.message_id,
        )
