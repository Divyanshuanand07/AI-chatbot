"""
The AI orchestrator: the tool-calling control loop.

    question
      -> build system blocks (cached prefix + volatile context)
      -> build tool list from the caller's scopes
      -> [ model turn -> tool calls? -> execute in parallel -> feed back ] *
      -> final answer + citations built from what actually ran
      -> persist turn, tool logs, audit row

Why a manual loop rather than the SDK's `tool_runner` helper: every tool call
here must be re-authorized against the caller's scopes, wall-clock bounded,
cached with a scope-aware key, recorded as an audit row, and counted toward a
per-request iteration budget. The runner does not expose hooks for all of
that, and the loop is only ~60 lines. The tradeoff is that we own `pause_turn`
and `max_tokens` handling, which is done explicitly below.

One deliberate design choice worth calling out: **tool results are not
replayed into later turns.** Each stored history turn is just the operator's
question and the assistant's prose. Follow-ups resolve through the
conversation's `context` (the remembered order id) and then re-read the
database. That trades context size for freshness, which is the right trade in
operations — an answer built from a payment status fetched four turns ago is a
correctness bug, not an optimisation.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from django.conf import settings
from django.db import transaction

from apps.accounts.models import User
from apps.common.tracing import get_trace_id, new_trace_id

from .llm import LLMError, LLMProvider, LLMResponse, Usage, get_provider
from .models import (
    AuditAction,
    AuditLog,
    Conversation,
    Message,
    MessageRole,
    ToolInvocationLog,
)
from .prompts import build_system_blocks
from .tools import Invocation, ToolContext, ToolExecutor, registry

logger = logging.getLogger(__name__)

MAX_TITLE_LENGTH = 120
ANSWER_PREVIEW_LENGTH = 2000


@dataclass
class AssistantAnswer:
    """What the API returns, and what the audit row records."""

    answer: str
    conversation_id: str
    trace_id: str
    #: Citations: the tools that actually ran, from execution records — never
    #: from the model's description of what it did.
    tool_calls: list[dict] = field(default_factory=list)
    iterations: int = 0
    usage: dict = field(default_factory=dict)
    provider: dict = field(default_factory=dict)
    duration_ms: float = 0.0
    order_ids: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "answer": self.answer,
            "conversation_id": self.conversation_id,
            "trace_id": self.trace_id,
            "tools_used": self.tool_calls,
            "iterations": self.iterations,
            "usage": self.usage,
            "provider": self.provider,
            "duration_ms": round(self.duration_ms, 2),
            "order_ids": self.order_ids,
            "warnings": self.warnings,
        }


class Orchestrator:
    def __init__(
        self,
        user: User,
        *,
        trace_id: str | None = None,
        provider: LLMProvider | None = None,
    ):
        self.user = user
        self.trace_id = trace_id or get_trace_id() or new_trace_id()
        self.provider = provider or get_provider()
        self.max_iterations = settings.LLM["MAX_TOOL_ITERATIONS"]
        self.history_turns = settings.LLM["HISTORY_TURNS"]

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------
    def ask(
        self,
        question: str,
        *,
        conversation: Conversation | None = None,
        request_meta: dict | None = None,
    ) -> AssistantAnswer:
        started = time.perf_counter()
        conversation = conversation or self._new_conversation(question)

        ctx = ToolContext(
            user=self.user,
            trace_id=self.trace_id,
            conversation_id=str(conversation.id),
        )
        executor = ToolExecutor(ctx)

        tools = registry.definitions_for(self.user.scopes)
        system = build_system_blocks(
            self.user, conversation_context=conversation.context
        )
        messages = self._build_history(conversation)
        messages.append({"role": "user", "content": question})

        usage = Usage()
        invocations: list[Invocation] = []
        warnings: list[str] = []
        final_text = ""
        iterations = 0
        last_response: LLMResponse | None = None

        try:
            while iterations < self.max_iterations:
                iterations += 1
                response = self.provider.complete(
                    system=system, messages=messages, tools=tools
                )
                last_response = response
                usage.add(response.usage)

                if response.stop_reason == "refusal":
                    final_text = (
                        "I can't answer that request. If you believe this is a "
                        "legitimate operational question, rephrase it or "
                        "escalate to your operations manager."
                    )
                    warnings.append(
                        f"model_refusal:{response.refusal_category or 'unspecified'}"
                    )
                    break

                if response.stop_reason == "pause_turn":
                    # A long-running server-side turn. Replay it to resume.
                    messages.append(
                        {"role": "assistant", "content": response.raw_content}
                    )
                    continue

                if response.stop_reason == "max_tokens":
                    warnings.append("answer_truncated_at_max_tokens")

                if not response.wants_tools:
                    final_text = response.text
                    break

                # Execute the requested tools concurrently.
                batch = [
                    {
                        "name": call.name,
                        "arguments": call.arguments,
                        "tool_use_id": call.id,
                    }
                    for call in response.tool_calls
                ]
                results = executor.execute_many(batch)
                for result in results:
                    result.meta["iteration"] = iterations
                invocations.extend(results)

                messages.append(
                    {"role": "assistant", "content": response.raw_content}
                )
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            self._tool_result_block(inv) for inv in results
                        ],
                    }
                )
            else:
                # Budget exhausted with tools still pending. Force one final
                # answer with no tools offered, so the operator gets whatever
                # was established rather than an empty response.
                warnings.append("tool_iteration_budget_exhausted")
                logger.warning(
                    "tool_budget_exhausted",
                    extra={
                        "iterations": iterations,
                        "tools_run": len(invocations),
                    },
                )
                closing = self.provider.complete(
                    system=system, messages=messages, tools=[]
                )
                usage.add(closing.usage)
                last_response = closing
                final_text = closing.text

        except LLMError as exc:
            self._audit_failure(conversation, question, exc, started, request_meta)
            raise

        final_text = (final_text or "").strip()
        if not final_text:
            final_text = (
                "I could not produce an answer for that. Please rephrase the "
                "question, or quote the trace id to operations engineering."
            )
            warnings.append("empty_model_answer")

        warnings.extend(self._grounding_warnings(final_text, invocations))

        order_ids = self._order_ids_touched(invocations)
        duration_ms = (time.perf_counter() - started) * 1000

        answer = AssistantAnswer(
            answer=final_text,
            conversation_id=str(conversation.id),
            trace_id=self.trace_id,
            tool_calls=[self._citation(inv) for inv in invocations],
            iterations=iterations,
            usage=usage.to_dict(),
            provider=self.provider.describe(),
            duration_ms=duration_ms,
            order_ids=order_ids,
            warnings=warnings,
        )

        self._persist(
            conversation=conversation,
            question=question,
            answer=answer,
            invocations=invocations,
            response=last_response,
            request_meta=request_meta,
        )
        return answer

    # ------------------------------------------------------------------
    # Message assembly
    # ------------------------------------------------------------------
    def _build_history(self, conversation: Conversation) -> list[dict]:
        """
        Replay recent turns as plain text.

        Tool blocks are intentionally excluded (see the module docstring).
        History is trimmed to whole turns and must begin with a user message,
        which the API requires.
        """
        limit = self.history_turns * 2
        recent = list(
            conversation.messages.order_by("-sequence")[:limit]
        )
        recent.reverse()

        while recent and recent[0].role != MessageRole.USER:
            recent.pop(0)

        return [
            {"role": message.role, "content": message.content}
            for message in recent
            if message.content
        ]

    def _tool_result_block(self, invocation: Invocation) -> dict:
        """
        Build the tool_result block the model reads.

        The tool name is included in the payload alongside the data. The model
        could infer it from `tool_use_id`, but naming it explicitly makes
        provenance unambiguous when several results arrive in one message.
        """
        import json

        payload = invocation.result.to_model_payload()
        payload["tool"] = invocation.tool_name

        block = {
            "type": "tool_result",
            "tool_use_id": invocation.tool_use_id,
            "content": json.dumps(payload, default=str),
        }
        if not invocation.ok:
            # Flagging the error explicitly stops the model having to infer
            # failure from the payload shape.
            block["is_error"] = True
        return block

    # ------------------------------------------------------------------
    # Post-processing
    # ------------------------------------------------------------------
    def _citation(self, invocation: Invocation) -> dict:
        return {
            "tool": invocation.tool_name,
            "arguments": invocation.arguments,
            "ok": invocation.ok,
            "error_code": invocation.result.error_code,
            "duration_ms": round(invocation.duration_ms, 2),
            "cache_hit": invocation.cache_hit,
        }

    def _order_ids_touched(self, invocations: list[Invocation]) -> list[int]:
        found: list[int] = []
        for invocation in invocations:
            raw = invocation.arguments.get("order_id")
            if raw is None:
                continue
            try:
                value = int(str(raw).lstrip("#"))
            except (TypeError, ValueError):
                continue
            if value not in found:
                found.append(value)
        return found

    def _grounding_warnings(
        self, text: str, invocations: list[Invocation]
    ) -> list[str]:
        """
        Cheap, high-signal check for an answer asserting unsourced facts.

        If no tool succeeded yet the answer quotes rupee amounts, the model
        has almost certainly invented them. We do not rewrite the answer —
        silently editing model output hides the failure — we flag it so it
        surfaces in the response payload, the logs and the audit row.
        """
        succeeded = [inv for inv in invocations if inv.ok]
        if succeeded:
            return []
        if "₹" in text:
            logger.error(
                "ungrounded_answer_detected",
                extra={"trace_id": self.trace_id, "preview": text[:200]},
            )
            return ["ungrounded_answer:money_without_tool_result"]
        return []

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    def _new_conversation(self, question: str) -> Conversation:
        return Conversation.objects.create(
            user=self.user,
            title=question.strip()[:MAX_TITLE_LENGTH],
        )

    @transaction.atomic
    def _persist(
        self,
        *,
        conversation: Conversation,
        question: str,
        answer: AssistantAnswer,
        invocations: list[Invocation],
        response: LLMResponse | None,
        request_meta: dict | None,
    ) -> None:
        next_sequence = (
            conversation.messages.order_by("-sequence")
            .values_list("sequence", flat=True)
            .first()
            or 0
        ) + 1

        Message.objects.create(
            conversation=conversation,
            sequence=next_sequence,
            role=MessageRole.USER,
            content=question,
            trace_id=self.trace_id,
        )

        assistant_message = Message.objects.create(
            conversation=conversation,
            sequence=next_sequence + 1,
            role=MessageRole.ASSISTANT,
            content=answer.answer,
            blocks=[{"type": "text", "text": answer.answer}],
            provider=answer.provider.get("provider", ""),
            model=(response.model if response else ""),
            stop_reason=(response.stop_reason if response else ""),
            input_tokens=answer.usage.get("input_tokens", 0),
            output_tokens=answer.usage.get("output_tokens", 0),
            cache_read_tokens=answer.usage.get("cache_read_input_tokens", 0),
            cache_creation_tokens=answer.usage.get("cache_creation_input_tokens", 0),
            latency_ms=answer.duration_ms,
            tool_iterations=answer.iterations,
            trace_id=self.trace_id,
        )

        ToolInvocationLog.objects.bulk_create(
            [
                ToolInvocationLog(
                    conversation=conversation,
                    message=assistant_message,
                    user=self.user,
                    tool_name=inv.tool_name,
                    arguments=inv.arguments,
                    ok=inv.ok,
                    error_code=inv.result.error_code or "",
                    duration_ms=inv.duration_ms,
                    cache_hit=inv.cache_hit,
                    iteration=inv.meta.get("iteration", 1),
                    trace_id=self.trace_id,
                )
                for inv in invocations
            ]
        )

        # Carry the resolved order forward so the next turn can say "it".
        if answer.order_ids:
            conversation.remember_order(answer.order_ids[-1])
        if not conversation.title:
            conversation.title = question.strip()[:MAX_TITLE_LENGTH]
        conversation.save(update_fields=["context", "title", "last_active_at"])

        meta = request_meta or {}
        AuditLog.objects.create(
            user=self.user,
            username=self.user.username,
            role=self.user.role,
            action=AuditAction.AI_QUERY,
            conversation=conversation,
            query_text=question,
            answer_preview=answer.answer[:ANSWER_PREVIEW_LENGTH],
            tools_called=[inv.to_audit_dict() for inv in invocations],
            order_ids_touched=answer.order_ids,
            succeeded=True,
            duration_ms=answer.duration_ms,
            total_input_tokens=answer.usage.get("input_tokens", 0),
            total_output_tokens=answer.usage.get("output_tokens", 0),
            trace_id=self.trace_id,
            ip_address=meta.get("ip"),
            user_agent=(meta.get("user_agent") or "")[:300],
            metadata={
                "provider": answer.provider,
                "iterations": answer.iterations,
                "warnings": answer.warnings,
            },
        )

    def _audit_failure(
        self,
        conversation: Conversation,
        question: str,
        exc: LLMError,
        started: float,
        request_meta: dict | None,
    ) -> None:
        """Record provider failures too — an outage is an audit event."""
        meta = request_meta or {}
        AuditLog.objects.create(
            user=self.user,
            username=self.user.username,
            role=self.user.role,
            action=AuditAction.AI_QUERY_FAILED,
            conversation=conversation,
            query_text=question,
            succeeded=False,
            error_code=exc.code,
            duration_ms=(time.perf_counter() - started) * 1000,
            trace_id=self.trace_id,
            ip_address=meta.get("ip"),
            user_agent=(meta.get("user_agent") or "")[:300],
            metadata={"retryable": exc.retryable, "message": exc.message},
        )
        logger.error(
            "assistant_provider_failure",
            extra={"code": exc.code, "retryable": exc.retryable},
        )
