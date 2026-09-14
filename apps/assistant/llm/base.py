"""
LLM provider contract.

Scope of this abstraction, stated honestly: it exists to swap **drivers**
(live Anthropic vs. a deterministic offline stub), not to be vendor-neutral.
The message and content-block shapes below are Anthropic-native, and
pretending otherwise would mean a translation layer that buys nothing and
loses features like adaptive thinking and prompt-cache breakpoints.

What the abstraction does buy is significant:
  * the whole orchestrator, including multi-tool flows, follow-ups and error
    handling, is testable with no API key and no spend;
  * a deployment with no model access still serves the REST API;
  * provider failures surface as one exception type the API layer maps to a
    502, instead of SDK-specific errors leaking into views.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolCall:
    """A tool the model asked us to run."""

    id: str
    name: str
    arguments: dict


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens

    def to_dict(self) -> dict:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


@dataclass
class LLMResponse:
    """One model turn."""

    #: Concatenated visible text (may be empty when the turn is tool calls only).
    text: str
    #: Tools the model wants executed before it can continue.
    tool_calls: list[ToolCall] = field(default_factory=list)
    #: end_turn | tool_use | max_tokens | refusal | pause_turn
    stop_reason: str = "end_turn"
    #: Provider-native content blocks, replayed verbatim on the next request.
    #: Thinking blocks in particular must be echoed back unchanged, so we
    #: never reconstruct this from `text`.
    raw_content: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    request_id: str | None = None
    #: Populated when a safety classifier declined the request.
    refusal_category: str | None = None

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LLMError(Exception):
    """A provider failure the caller should surface as an upstream error."""

    def __init__(self, message: str, *, retryable: bool = False, code: str = "llm_error"):
        super().__init__(message)
        self.message = message
        self.retryable = retryable
        self.code = code


class LLMProvider(abc.ABC):
    """Minimal surface the orchestrator depends on."""

    name: str = "base"

    @abc.abstractmethod
    def complete(
        self,
        *,
        system: list[dict],
        messages: list[dict],
        tools: list[dict],
    ) -> LLMResponse:
        """
        Run one model turn.

        `system` is a list of content blocks so the caller can place a
        prompt-cache breakpoint between stable and volatile sections.
        `messages` and `tools` are Anthropic-shaped.
        """

    def describe(self) -> dict:
        """Provider metadata recorded on every conversation turn."""
        return {"provider": self.name}
