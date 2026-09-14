"""
Provider selection.

The provider is built once per process and cached. Constructing the Anthropic
client is not free (it sets up an HTTP connection pool), and doing it per
request would throw away keep-alive connections on a latency-sensitive path.
"""

from __future__ import annotations

import functools

from django.conf import settings

from .base import LLMError, LLMProvider, LLMResponse, ToolCall, Usage

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResponse",
    "ToolCall",
    "Usage",
    "get_provider",
    "reset_provider_cache",
]


@functools.lru_cache(maxsize=4)
def _build(provider_name: str) -> LLMProvider:
    if provider_name == "anthropic":
        from .anthropic_provider import AnthropicProvider

        return AnthropicProvider()
    if provider_name == "stub":
        from .stub_provider import StubProvider

        return StubProvider()
    raise LLMError(
        f"Unknown LLM provider '{provider_name}'. Use 'anthropic' or 'stub'.",
        code="llm_misconfigured",
    )


def get_provider(name: str | None = None) -> LLMProvider:
    return _build(name or settings.LLM["PROVIDER"])


def reset_provider_cache() -> None:
    """Used by tests that switch providers mid-run."""
    _build.cache_clear()
