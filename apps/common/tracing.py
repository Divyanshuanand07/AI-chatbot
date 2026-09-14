"""
Request-scoped trace id.

Every inbound request gets one. It flows into structured logs, audit rows,
tool-invocation records and the API response body, so a single operator
complaint ("the assistant gave me a weird answer at 14:32") can be traced
through the orchestrator, every tool call and the SQL it ran.
"""

from __future__ import annotations

import uuid
from contextvars import ContextVar

_trace_id: ContextVar[str | None] = ContextVar("trace_id", default=None)


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def set_trace_id(trace_id: str | None = None) -> str:
    value = trace_id or new_trace_id()
    _trace_id.set(value)
    return value


def get_trace_id() -> str | None:
    return _trace_id.get()


def clear_trace_id() -> None:
    _trace_id.set(None)
