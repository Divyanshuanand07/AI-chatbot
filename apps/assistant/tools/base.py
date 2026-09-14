"""
Tool contracts and registry.

Three ideas drive this design:

1. **A tool never raises into the model.** Every outcome — success, "order
   not found", "you lack permission", a timeout — comes back as the same
   JSON envelope with an `ok` flag and a machine-readable `error.code`. The
   model is then told plainly that the order does not exist, instead of
   receiving an exception it has to guess about. Hallucination on missing
   data is usually a *tool design* failure, not a prompting failure.

2. **Authorization happens at the tool boundary, by omission.** Each tool
   declares `required_scopes`. The orchestrator builds the tool list from the
   caller's scopes, so a viewer-role operator's model never sees
   `diagnose_order_blockers` at all. You cannot jailbreak your way into a
   tool that was never offered.

3. **The tool list is deterministic.** Definitions are emitted sorted by
   name, because Anthropic prompt caching is a prefix match over
   `tools -> system -> messages`. A tool set that reorders between requests
   silently destroys the cache hit rate.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from apps.accounts.models import User


# ---------------------------------------------------------------------------
# Execution context
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolContext:
    """Everything a tool needs to know about who is asking, and why."""

    user: User
    trace_id: str
    conversation_id: str | None = None

    @property
    def scopes(self) -> frozenset[str]:
        return self.user.scopes

    def has_scope(self, scope: str) -> bool:
        return self.user.has_scope(scope)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------
@dataclass
class ToolResult:
    """
    Uniform tool outcome.

    `ok=False` still returns HTTP-200-equivalent content to the model, marked
    `is_error` in the tool_result block so the model knows the call failed
    without having to infer it from the payload.
    """

    ok: bool
    data: dict | None = None
    error_code: str | None = None
    error_message: str | None = None
    error_details: dict = field(default_factory=dict)
    #: Non-authoritative metadata for observability (never shown as fact).
    meta: dict = field(default_factory=dict)

    @classmethod
    def success(cls, data: dict, **meta) -> ToolResult:
        return cls(ok=True, data=data, meta=meta)

    @classmethod
    def failure(
        cls,
        code: str,
        message: str,
        *,
        details: dict | None = None,
        **meta,
    ) -> ToolResult:
        return cls(
            ok=False,
            error_code=code,
            error_message=message,
            error_details=details or {},
            meta=meta,
        )

    def to_model_payload(self) -> dict:
        """
        The JSON the model actually sees.

        Deliberately small and flat. Anything the model does not need to
        answer the question is noise that costs tokens and invites
        over-interpretation.
        """
        if self.ok:
            return {"ok": True, "data": self.data}
        return {
            "ok": False,
            "error": {
                "code": self.error_code,
                "message": self.error_message,
                **({"details": self.error_details} if self.error_details else {}),
            },
        }


# ---------------------------------------------------------------------------
# Tool specification
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., ToolResult]
    required_scopes: tuple[str, ...] = ()
    #: Seconds to cache a successful result for. 0 disables caching.
    #: Operational data changes constantly, so TTLs here are deliberately
    #: short — a stale payment status is worse than a slow one.
    cache_ttl: int = 0
    #: True if the payload can contain customer contact details. Used to keep
    #: PII out of the shared tool-result cache.
    returns_pii: bool = False

    def anthropic_definition(self) -> dict:
        """
        Render as an Anthropic tool definition.

        `strict: True` plus `additionalProperties: False` guarantees the
        arguments validate exactly against the schema, which removes a whole
        class of "the model passed a string where we wanted an int" bugs.
        """
        return {
            "name": self.name,
            "description": self.description,
            "strict": True,
            "input_schema": self.input_schema,
        }


class ToolRegistry:
    """In-process registry of every tool the assistant can call."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}

    def register(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict,
        required_scopes: tuple[str, ...] = (),
        cache_ttl: int = 0,
        returns_pii: bool = False,
    ):
        """Decorator registering a function as a callable tool."""

        def decorator(handler: Callable[..., ToolResult]):
            if name in self._tools:
                raise ValueError(f"Tool '{name}' is already registered.")
            self._tools[name] = ToolSpec(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=handler,
                required_scopes=required_scopes,
                cache_ttl=cache_ttl,
                returns_pii=returns_pii,
            )
            return handler

        return decorator

    # -- lookup ---------------------------------------------------------
    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def all(self) -> list[ToolSpec]:
        return sorted(self._tools.values(), key=lambda t: t.name)

    def available_to(self, scopes: Iterable[str]) -> list[ToolSpec]:
        """Only the tools this caller's scopes permit — sorted, for caching."""
        held = set(scopes)
        return [
            spec
            for spec in self.all()
            if set(spec.required_scopes).issubset(held)
        ]

    def definitions_for(self, scopes: Iterable[str]) -> list[dict]:
        return [spec.anthropic_definition() for spec in self.available_to(scopes)]

    def names(self) -> list[str]:
        return [spec.name for spec in self.all()]


#: The single registry instance. Tool modules import this and decorate.
registry = ToolRegistry()


# ---------------------------------------------------------------------------
# Schema helpers — keep tool schemas short and consistent
# ---------------------------------------------------------------------------
def order_id_schema(extra: dict[str, Any] | None = None) -> dict:
    """
    Standard single-argument schema: an order id.

    Accepts integer or string because operators write "#1243" and models
    pass both forms; `selectors.parse_order_id` normalises it. Rejecting a
    string here would just turn a recoverable input into a hard failure.
    """
    properties: dict[str, Any] = {
        "order_id": {
            "type": ["integer", "string"],
            "description": "The numeric order id, e.g. 1243. The '#' prefix is optional.",
        }
    }
    required = ["order_id"]
    if extra:
        properties.update(extra)
        required.extend(extra.keys())
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }
