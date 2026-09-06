"""Wake Iris for durable one-off revisits when their time arrives."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from ..codex import CodexConversation, CodexTurnSettings
from ..codex.resolver import resolve_profile
from ..config import RevisitSettings
from ..profile import profile_for_attention
from ..prompts.activations import build_revisit_turn_prompt
from ..telemetry import Telemetry
from .models import Attention, Revisit
from .state import RevisitState

LOGGER = logging.getLogger(__name__)

ConversationFactory = Callable[[Revisit], CodexConversation]
SettingsResolver = Callable[[Attention], CodexTurnSettings]


class RevisitLoop:
    """Claim due revisits sequentially and give each a fresh Codex conversation."""

    def __init__(
        self,
        settings: RevisitSettings,
        vault: Path,
        turn_settings: SettingsResolver,
        *,
        human: str,
        personality: Path | None = None,
        mcp_environment: Mapping[str, str] | None = None,
        network_domains: tuple[str, ...] = (),
        telemetry: Telemetry | None = None,
        state: RevisitState | None = None,
        conversation_factory: ConversationFactory | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.vault = vault
        self.turn_settings = turn_settings
        self.human = human
        self.personality = personality
        self.mcp_environment = dict(mcp_environment or {})
        self.network_domains = network_domains
        self.telemetry = telemetry or Telemetry()
        self.state = state or RevisitState(settings.state)
        self.state.initialize()
        self._stop = asyncio.Event()
        self._conversation_factory = conversation_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    def stop(self) -> None:
        self._stop.set()

    def _conversation(self, revisit: Revisit) -> CodexConversation:
        if self._conversation_factory is not None:
            return self._conversation_factory(revisit)
        declaration = profile_for_attention(revisit.attention)
        return CodexConversation(
            resolve_profile(
                declaration,
                vault=self.vault,
                settings=self.turn_settings(revisit.attention),
                human=self.human,
                personality=self.personality,
                knowledge_root=self.vault,
                mcp_environment=self.mcp_environment,
                network_domains=self.network_domains,
            ),
            telemetry=self.telemetry,
        )

    async def process_due(self) -> bool:
        """Process one due revisit and report whether one was claimed."""
        awakened_at = self._clock()
        revisit = self.state.claim_due(now=awakened_at)
        if revisit is None:
            return False
        started_at = time.monotonic()
        conversation: CodexConversation | None = None
        try:
            conversation = self._conversation(revisit)
            LOGGER.info(
                "Revisit turn started id=%s attention=%s model=%s effort=%s",
                revisit.id,
                revisit.attention.value,
                conversation.profile.model,
                conversation.profile.effort.value,
            )
            prompt = build_revisit_turn_prompt(
                note=revisit.note,
                created_at=revisit.created_at,
                due_at=revisit.due_at,
                awakened_at=awakened_at,
                attention=revisit.attention.value,
                human=self.human,
            )
            async for _event in conversation.stream_turn(prompt):
                pass
            self.state.complete(revisit.id)
        except asyncio.CancelledError:
            self.state.release(revisit.id)
            self.telemetry.background_job(source="revisit", status="cancelled")
            raise
        except Exception as error:
            LOGGER.exception("Revisit turn failed id=%s", revisit.id)
            try:
                self.state.fail(revisit.id, error)
                self.telemetry.background_job(source="revisit", status="failure")
            except Exception:
                LOGGER.exception("Failed to retain revisit failure id=%s", revisit.id)
                raise
        else:
            self.telemetry.background_job(source="revisit", status="success")
            LOGGER.info(
                "Revisit turn completed id=%s duration=%.2fs",
                revisit.id,
                time.monotonic() - started_at,
            )
        finally:
            if conversation is not None:
                try:
                    await conversation.close()
                except Exception:
                    LOGGER.exception(
                        "Failed to close revisit Codex client id=%s", revisit.id
                    )
        return True

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                while not self._stop.is_set() and await self.process_due():
                    pass
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Revisit source failed; continuing after poll interval"
                )
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.settings.poll_interval_seconds,
                )
            except TimeoutError:
                pass
