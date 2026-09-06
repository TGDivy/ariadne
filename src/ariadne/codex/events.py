"""Semantic events emitted while Codex works through one turn."""

from dataclasses import dataclass

from openai_codex.generated.v2_all import MessagePhase


@dataclass(frozen=True, slots=True)
class WorkStarted:
    """Codex started a new private reasoning or planning phase."""

    item_id: str
    activity: str


@dataclass(frozen=True, slots=True)
class WorkSummaryUpdated:
    """The explicitly requested reasoning summary changed."""

    item_id: str
    part: int
    text: str


@dataclass(frozen=True, slots=True)
class ActivityUpdated:
    """A concrete tool or execution activity became current."""

    text: str
    item_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActivityCompleted:
    """A concrete activity finished and its result is being considered."""

    item_id: str
    text: str


@dataclass(frozen=True, slots=True)
class CapabilityCallCompleted:
    """One MCP capability finished, without exposing arguments or results."""

    server: str
    tool: str
    status: str
    error: str | None


@dataclass(frozen=True, slots=True)
class AgentMessageStarted:
    """Codex started one model-visible speech item."""

    item_id: str
    phase: MessagePhase


@dataclass(frozen=True, slots=True)
class AgentMessageUpdated:
    """One speech item's complete accumulated text changed."""

    item_id: str
    phase: MessagePhase
    text: str


@dataclass(frozen=True, slots=True)
class AgentMessageCompleted:
    """One commentary or final speech item completed."""

    item_id: str
    phase: MessagePhase
    text: str


type ConversationEvent = (
    WorkStarted
    | WorkSummaryUpdated
    | ActivityUpdated
    | ActivityCompleted
    | CapabilityCallCompleted
    | AgentMessageStarted
    | AgentMessageUpdated
    | AgentMessageCompleted
)
