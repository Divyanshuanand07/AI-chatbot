"""
Assistant persistence: conversations, turns, tool invocations, audit trail.

Why conversation state lives in PostgreSQL and not only in Redis: these rows
are the audit record. When an operator says "the assistant told me order
#2325 was paid", you need the exact question, the exact tools that ran, their
arguments, and the exact answer — months later, after the Redis TTL expired.
Redis caches tool *results* for seconds; Postgres keeps the history.
"""

from __future__ import annotations

import uuid

from django.conf import settings
from django.db import models


class Conversation(models.Model):
    """A single operator's ongoing thread with the assistant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="conversations",
    )
    title = models.CharField(max_length=200, blank=True)

    #: Resolved entities carried across turns, e.g.
    #: {"last_order_id": 2325, "mentioned_order_ids": [1243, 2325]}.
    #: This is what makes "why is it stuck?" work as a follow-up. It holds
    #: only resolved *references*, never copies of operational data — stale
    #: cached facts are exactly what we are trying to avoid.
    context = models.JSONField(default=dict, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_active_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "assistant_conversations"
        ordering = ["-last_active_at"]
        indexes = [models.Index(fields=["user", "-last_active_at"])]

    def __str__(self) -> str:
        return f"Conversation {self.id} ({self.user_id})"

    # -- context helpers -------------------------------------------------
    @property
    def last_order_id(self) -> int | None:
        return self.context.get("last_order_id")

    def remember_order(self, order_id: int) -> None:
        """Record an order as the conversation's current subject."""
        mentioned = self.context.get("mentioned_order_ids") or []
        if order_id in mentioned:
            mentioned.remove(order_id)
        mentioned.append(order_id)
        self.context["last_order_id"] = order_id
        self.context["mentioned_order_ids"] = mentioned[-10:]


class MessageRole(models.TextChoices):
    USER = "user", "Operator"
    ASSISTANT = "assistant", "Assistant"


class Message(models.Model):
    """One turn. Stores both display text and the provider content blocks."""

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    sequence = models.PositiveIntegerField()
    role = models.CharField(max_length=16, choices=MessageRole.choices)

    #: Human-readable text shown in the UI and in audit review.
    content = models.TextField(blank=True)

    #: Provider-native content blocks, replayed verbatim to the model on the
    #: next turn. Kept separate from `content` because thinking and tool_use
    #: blocks must be echoed back unchanged and cannot be rebuilt from prose.
    blocks = models.JSONField(default=list, blank=True)

    provider = models.CharField(max_length=32, blank=True)
    model = models.CharField(max_length=64, blank=True)
    stop_reason = models.CharField(max_length=32, blank=True)

    input_tokens = models.PositiveIntegerField(default=0)
    output_tokens = models.PositiveIntegerField(default=0)
    cache_read_tokens = models.PositiveIntegerField(default=0)
    cache_creation_tokens = models.PositiveIntegerField(default=0)

    latency_ms = models.FloatField(default=0)
    tool_iterations = models.PositiveSmallIntegerField(default=0)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "assistant_messages"
        ordering = ["conversation_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["conversation", "sequence"], name="uniq_conversation_sequence"
            )
        ]
        indexes = [models.Index(fields=["conversation", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.conversation_id}.{self.sequence} {self.role}"


class ToolInvocationLog(models.Model):
    """
    One executed tool call.

    Recorded for every call, including failures and cache hits. This is the
    table that answers "what did the assistant actually look at?" — the
    citations shown to the operator are built from these rows, not from the
    model's claims about which tools it used.
    """

    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.CASCADE,
        related_name="tool_invocations",
        null=True,
        blank=True,
    )
    message = models.ForeignKey(
        Message,
        on_delete=models.CASCADE,
        related_name="tool_invocations",
        null=True,
        blank=True,
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="tool_invocations",
    )
    tool_name = models.CharField(max_length=64, db_index=True)
    arguments = models.JSONField(default=dict, blank=True)
    ok = models.BooleanField(default=True)
    error_code = models.CharField(max_length=48, blank=True)
    duration_ms = models.FloatField(default=0)
    cache_hit = models.BooleanField(default=False)
    iteration = models.PositiveSmallIntegerField(default=1)
    trace_id = models.CharField(max_length=64, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "assistant_tool_invocations"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["tool_name", "-created_at"]),
            models.Index(fields=["ok", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.tool_name} ok={self.ok}"


class AuditAction(models.TextChoices):
    AI_QUERY = "ai_query", "AI query"
    AI_QUERY_DENIED = "ai_query_denied", "AI query denied"
    AI_QUERY_FAILED = "ai_query_failed", "AI query failed"
    CONVERSATION_CREATED = "conversation_created", "Conversation created"


class AuditLog(models.Model):
    """
    Append-only audit of assistant usage.

    `role` is denormalised deliberately: roles change, and an audit entry must
    record the authority the user held *at the time*, not whatever they hold
    when someone reads the log.
    """

    id = models.BigAutoField(primary_key=True)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        related_name="audit_entries",
    )
    username = models.CharField(max_length=150, blank=True)
    role = models.CharField(max_length=32, blank=True)

    action = models.CharField(max_length=32, choices=AuditAction.choices, db_index=True)
    conversation = models.ForeignKey(
        Conversation,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="audit_entries",
    )

    query_text = models.TextField(blank=True)
    answer_preview = models.TextField(blank=True)
    tools_called = models.JSONField(default=list, blank=True)
    order_ids_touched = models.JSONField(default=list, blank=True)

    succeeded = models.BooleanField(default=True)
    error_code = models.CharField(max_length=48, blank=True)

    duration_ms = models.FloatField(default=0)
    total_input_tokens = models.PositiveIntegerField(default=0)
    total_output_tokens = models.PositiveIntegerField(default=0)

    trace_id = models.CharField(max_length=64, blank=True, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=300, blank=True)
    metadata = models.JSONField(default=dict, blank=True)

    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = "assistant_audit_log"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["user", "-created_at"]),
            models.Index(fields=["action", "-created_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.created_at:%Y-%m-%d %H:%M} {self.username} {self.action}"
