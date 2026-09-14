"""
Tool layer public surface.

Importing this package registers every tool as a side effect of importing
`catalog`. That is why the orchestrator imports from here rather than from
`base` — importing `base` alone gives you an empty registry.
"""

from . import (
    catalog,  # noqa: F401  (import registers the order tools)
    knowledge_tools,  # noqa: F401  (registers search_knowledge_base)
)
from .base import ToolContext, ToolResult, ToolSpec, registry
from .executor import Invocation, ToolExecutor

__all__ = [
    "Invocation",
    "ToolContext",
    "ToolExecutor",
    "ToolResult",
    "ToolSpec",
    "registry",
]
