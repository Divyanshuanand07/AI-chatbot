"""
Tool execution: authorization, caching, timeouts, parallelism, observability.

The orchestrator decides *what* to call; this module owns *how* it is called.
Separating them keeps the orchestrator readable and makes the execution
guarantees testable on their own.

Guarantees:
  * Every invocation is re-authorized, even though the tool list was already
    filtered by scope. A replayed conversation or a future streaming client
    must not be able to smuggle in a tool the caller cannot use.
  * Every invocation is bounded by a wall-clock timeout.
  * No exception escapes. Anything unexpected becomes an INTERNAL_ERROR
    envelope, logged with the stack trace and the trace id.
  * Independent calls in one model turn run concurrently, because a
    six-tool order summary should not cost six sequential round trips.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field

from django.conf import settings
from django.core.cache import cache
from django.db import close_old_connections

from .base import ToolContext, ToolResult, ToolSpec, registry

logger = logging.getLogger(__name__)


@dataclass
class Invocation:
    """One executed tool call, with everything needed for audit and metrics."""

    tool_name: str
    arguments: dict
    result: ToolResult
    duration_ms: float
    cache_hit: bool = False
    tool_use_id: str | None = None
    meta: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.result.ok

    def to_audit_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "arguments": self.arguments,
            "ok": self.result.ok,
            "error_code": self.result.error_code,
            "duration_ms": round(self.duration_ms, 2),
            "cache_hit": self.cache_hit,
        }


class ToolExecutor:
    def __init__(self, ctx: ToolContext):
        self.ctx = ctx
        self.timeout = settings.TOOLS["TIMEOUT_SECONDS"]
        self.max_parallel = settings.TOOLS["MAX_PARALLEL"]
        self.cache_enabled = settings.TOOLS["CACHE_ENABLED"]
        #: TIMEOUT_SECONDS <= 0 runs every tool inline on the calling thread:
        #: no wall-clock bound and no parallelism.
        #:
        #: This is not a test-only hack, though the test suite is why it
        #: exists. Django wraps each test in an uncommitted transaction, and a
        #: worker thread opens its own connection which cannot see it — so
        #: threaded execution makes every fixture invisible. Rather than
        #: silently branching on "am I under test", the behaviour is a
        #: documented configuration value. It is also genuinely useful in
        #: production for reproducing a tool bug under a debugger.
        self.inline = self.timeout <= 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def execute(
        self, tool_name: str, arguments: dict, *, tool_use_id: str | None = None
    ) -> Invocation:
        started = time.perf_counter()

        spec = registry.get(tool_name)
        if spec is None:
            # The model invented a tool name. Tell it plainly which tools
            # exist rather than failing silently — it usually self-corrects.
            return self._finish(
                tool_name,
                arguments,
                ToolResult.failure(
                    "UNKNOWN_TOOL",
                    f"No tool named '{tool_name}' exists.",
                    details={"available_tools": registry.names()},
                ),
                started,
                tool_use_id=tool_use_id,
            )

        # Defence in depth: the tool list was filtered by scope, but re-check.
        missing = [s for s in spec.required_scopes if not self.ctx.has_scope(s)]
        if missing:
            logger.warning(
                "tool_authorization_denied",
                extra={
                    "tool": tool_name,
                    "user_id": self.ctx.user.id,
                    "role": self.ctx.user.role,
                    "missing_scopes": [str(s) for s in missing],
                },
            )
            return self._finish(
                tool_name,
                arguments,
                ToolResult.failure(
                    "FORBIDDEN",
                    (
                        "The operator's role does not permit this lookup. "
                        "Tell them their role lacks access and do not attempt "
                        "to obtain the data another way."
                    ),
                    details={"missing_scopes": [str(s) for s in missing]},
                ),
                started,
                tool_use_id=tool_use_id,
            )

        cache_key = self._cache_key(spec, arguments)
        if cache_key:
            cached = cache.get(cache_key)
            if cached is not None:
                return self._finish(
                    tool_name,
                    arguments,
                    ToolResult(ok=True, data=cached),
                    started,
                    cache_hit=True,
                    tool_use_id=tool_use_id,
                )

        result = self._run_with_timeout(spec, arguments)

        if result.ok and cache_key and result.data is not None:
            cache.set(cache_key, result.data, spec.cache_ttl)

        return self._finish(
            tool_name, arguments, result, started, tool_use_id=tool_use_id
        )

    def execute_many(self, calls: list[dict]) -> list[Invocation]:
        """
        Run several tool calls concurrently.

        `calls` items are {"name": str, "arguments": dict, "tool_use_id": str}.
        Order of the returned list matches the input, because tool_result
        blocks must line up with the tool_use blocks that requested them.
        """
        if not calls:
            return []
        if len(calls) == 1 or self.inline:
            return [
                self.execute(
                    call["name"],
                    call.get("arguments") or {},
                    tool_use_id=call.get("tool_use_id"),
                )
                for call in calls
            ]

        with ThreadPoolExecutor(
            max_workers=min(len(calls), self.max_parallel),
            thread_name_prefix="tool",
        ) as pool:
            futures = [
                pool.submit(
                    self.execute,
                    call["name"],
                    call.get("arguments") or {},
                    tool_use_id=call.get("tool_use_id"),
                )
                for call in calls
            ]
            return [f.result() for f in futures]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _run_with_timeout(self, spec: ToolSpec, arguments: dict) -> ToolResult:
        """
        Execute the handler under a wall-clock budget.

        Note the honest caveat: a timed-out handler's thread is not killed —
        Python cannot safely do that — so a pathological query keeps running
        until Postgres or the connection gives up. What this guarantees is
        that the *user* is not made to wait for it. Query-level protection
        belongs in `statement_timeout` on the database role.
        """
        if self.inline:
            return self._invoke(spec, arguments)

        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="tool-run") as pool:
            future = pool.submit(self._invoke, spec, arguments)
            try:
                return future.result(timeout=self.timeout)
            except FuturesTimeout:
                logger.error(
                    "tool_timeout",
                    extra={"tool": spec.name, "timeout_s": self.timeout},
                )
                return ToolResult.failure(
                    "TIMEOUT",
                    (
                        f"The {spec.name} lookup did not complete within "
                        f"{self.timeout:g} seconds. Report that the system is "
                        f"slow right now; do not invent the data."
                    ),
                )

    def _invoke(self, spec: ToolSpec, arguments: dict) -> ToolResult:
        try:
            result = spec.handler(self.ctx, **arguments)
            if not isinstance(result, ToolResult):  # pragma: no cover - guard
                raise TypeError(
                    f"Tool '{spec.name}' returned {type(result).__name__}, "
                    f"expected ToolResult."
                )
            return result
        except TypeError as exc:
            # Wrong/extra arguments from the model. strict schemas make this
            # rare, but a clear message lets the model retry correctly.
            logger.warning(
                "tool_bad_arguments",
                extra={"tool": spec.name, "arguments": arguments, "error": str(exc)},
            )
            return ToolResult.failure(
                "BAD_ARGUMENTS",
                f"Invalid arguments for {spec.name}: {exc}",
                details={"schema": spec.input_schema},
            )
        except Exception as exc:
            logger.exception(
                "tool_internal_error",
                extra={"tool": spec.name, "arguments": arguments},
            )
            return ToolResult.failure(
                "INTERNAL_ERROR",
                (
                    f"The {spec.name} lookup failed unexpectedly "
                    f"({exc.__class__.__name__}). Report that the data could "
                    f"not be retrieved; do not guess it."
                ),
            )
        finally:
            # Each pool thread opens its own DB connection; without this they
            # accumulate for the life of the process. Guarded on the thread
            # because closing the *calling* thread's connection in inline mode
            # would tear down the caller's transaction.
            if threading.current_thread() is not threading.main_thread():
                close_old_connections()

    def _cache_key(self, spec: ToolSpec, arguments: dict) -> str | None:
        if not self.cache_enabled or spec.cache_ttl <= 0:
            return None
        # Scopes are part of the key: the same tool and arguments can legally
        # return different payloads to different roles (PII masking), so a
        # single shared entry would leak across roles.
        payload = json.dumps(
            {
                "tool": spec.name,
                "args": arguments,
                "scopes": sorted(str(s) for s in self.ctx.scopes),
            },
            sort_keys=True,
            default=str,
        )
        digest = hashlib.sha256(payload.encode()).hexdigest()[:32]
        return f"tool:{spec.name}:{digest}"

    def _finish(
        self,
        tool_name: str,
        arguments: dict,
        result: ToolResult,
        started: float,
        *,
        cache_hit: bool = False,
        tool_use_id: str | None = None,
    ) -> Invocation:
        duration_ms = (time.perf_counter() - started) * 1000
        logger.info(
            "tool_invocation",
            extra={
                "tool": tool_name,
                "ok": result.ok,
                "error_code": result.error_code,
                "duration_ms": round(duration_ms, 2),
                "cache_hit": cache_hit,
                "user_id": self.ctx.user.id,
                "role": self.ctx.user.role,
            },
        )
        return Invocation(
            tool_name=tool_name,
            arguments=arguments,
            result=result,
            duration_ms=duration_ms,
            cache_hit=cache_hit,
            tool_use_id=tool_use_id,
        )
