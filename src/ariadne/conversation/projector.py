"""Project one Codex turn's semantic events into conversation events."""

from __future__ import annotations

from dataclasses import dataclass

from openai_codex.generated.v2_all import MessagePhase

from ..codex import (
    ActivityCompleted,
    ActivityUpdated,
    AgentMessageCompleted,
    AgentMessageStarted,
    AgentMessageUpdated,
    CapabilityCallCompleted,
    ConversationEvent,
    WorkStarted,
    WorkSummaryUpdated,
)
from ..prompts.activations import SILENT_HANDOFF_RESPONSE
from .events import (
    ActivityChanged,
    LiveTurnView,
    SpeechStarted,
    SpeechUpdated,
    SpeechView,
    StreamEvent,
    WorkSummary,
)
from .models import SpeechPhase, TurnKind, TurnOutcome, TurnState


@dataclass(frozen=True, slots=True)
class CommitSpeech:
    """A completed commentary or final message that must become permanent."""

    phase: SpeechPhase
    text: str


type ProjectorAction = StreamEvent | CommitSpeech


def speech_phase(phase: MessagePhase) -> SpeechPhase:
    return "commentary" if phase == MessagePhase.commentary else "final"


class TurnProjector:
    """Track one turn's live state and decide what clients see.

    Proactive turns publish no provisional speech or reasoning: a handoff may
    turn out to be obsolete, and the silent marker must never reach a screen.
    """

    def __init__(self, turn_id: str, kind: TurnKind) -> None:
        self.turn_id = turn_id
        self.kind = kind
        self.activity: str | None = None
        self.work_summary: str | None = None
        self.speech: SpeechView | None = None
        self.committed = 0
        self.silent = False
        self._activities: dict[str, str] = {}

    def view(self, state: TurnState) -> LiveTurnView:
        return LiveTurnView(
            turn_id=self.turn_id,
            kind=self.kind,
            state=state,
            activity=self.activity,
            work_summary=self.work_summary if self.kind == "direct" else None,
            speech=self.speech if self.kind == "direct" else None,
        )

    def apply(self, event: ConversationEvent) -> list[ProjectorAction]:
        if isinstance(event, WorkStarted):
            self._activities.clear()
            self.work_summary = None
            return self._activity(event.activity)
        if isinstance(event, WorkSummaryUpdated):
            self.work_summary = event.text
            if self.kind != "direct":
                return []
            return [
                WorkSummary(
                    turn_id=self.turn_id, item_id=event.item_id, text=event.text
                )
            ]
        if isinstance(event, ActivityUpdated):
            if event.item_id is not None:
                self._activities.pop(event.item_id, None)
                self._activities[event.item_id] = event.text
            return self._activity(event.text)
        if isinstance(event, ActivityCompleted):
            if event.item_id not in self._activities:
                return []
            was_current = next(reversed(self._activities)) == event.item_id
            self._activities.pop(event.item_id)
            if not was_current:
                return []
            remaining = next(reversed(self._activities.values()), None)
            return self._activity(remaining or event.text)
        if isinstance(event, CapabilityCallCompleted):
            return []
        if isinstance(event, AgentMessageStarted):
            if self.speech is not None:
                raise RuntimeError("Codex started overlapping speech items.")
            phase = speech_phase(event.phase)
            self._activities.clear()
            self.speech = SpeechView(item_id=event.item_id, phase=phase, text="")
            if self.kind != "direct":
                return []
            return [
                SpeechStarted(turn_id=self.turn_id, item_id=event.item_id, phase=phase)
            ]
        if isinstance(event, AgentMessageUpdated):
            speech = self._require_speech(event.item_id, event.phase)
            self.speech = SpeechView(
                item_id=speech.item_id, phase=speech.phase, text=event.text
            )
            if self.kind != "direct":
                return []
            return [
                SpeechUpdated(
                    turn_id=self.turn_id,
                    item_id=event.item_id,
                    phase=speech.phase,
                    text=event.text,
                )
            ]
        if isinstance(event, AgentMessageCompleted):
            speech = self._require_speech(event.item_id, event.phase)
            self.speech = None
            if (
                self.kind == "proactive"
                and event.text.strip() == SILENT_HANDOFF_RESPONSE
            ):
                if self.committed or speech.phase != "final":
                    raise RuntimeError(
                        "A silent handoff response must be the only speech."
                    )
                self.silent = True
                return []
            if self.silent:
                raise RuntimeError("Codex spoke after a silent handoff response.")
            return [CommitSpeech(phase=speech.phase, text=event.text)]
        return []

    def note_committed(self) -> None:
        self.committed += 1

    def complete(self) -> TurnOutcome:
        if self.speech is not None:
            raise RuntimeError("Codex ended with an incomplete speech item.")
        if self.silent:
            return "silent"
        if not self.committed:
            raise RuntimeError("Codex completed without a response.")
        return "completed"

    def partial_speech(self) -> SpeechView | None:
        """Return useful provisional speech to keep when a turn is stopped."""
        if self.kind != "direct" or self.speech is None or not self.speech.text.strip():
            return None
        return self.speech

    def _activity(self, label: str) -> list[ProjectorAction]:
        self.activity = label
        return [ActivityChanged(turn_id=self.turn_id, label=label)]

    def _require_speech(self, item_id: str, phase: MessagePhase) -> SpeechView:
        speech = self.speech
        if (
            speech is None
            or speech.item_id != item_id
            or speech.phase != speech_phase(phase)
        ):
            raise RuntimeError("Codex updated speech outside its declared phase.")
        return speech
