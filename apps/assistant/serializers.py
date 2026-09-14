from __future__ import annotations

from rest_framework import serializers

from .models import AuditLog, Conversation, Message, ToolInvocationLog


class AskSerializer(serializers.Serializer):
    """Input validation for the natural-language endpoint."""

    question = serializers.CharField(
        min_length=2,
        max_length=2000,
        trim_whitespace=True,
        help_text="The operator's question in plain English.",
    )
    conversation_id = serializers.UUIDField(
        required=False,
        allow_null=True,
        help_text=(
            "Continue an existing conversation so follow-up questions resolve "
            "against its context. Omit to start a new one."
        ),
    )


class MessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = Message
        fields = (
            "sequence",
            "role",
            "content",
            "model",
            "stop_reason",
            "input_tokens",
            "output_tokens",
            "cache_read_tokens",
            "latency_ms",
            "tool_iterations",
            "trace_id",
            "created_at",
        )
        read_only_fields = fields


class ToolInvocationSerializer(serializers.ModelSerializer):
    class Meta:
        model = ToolInvocationLog
        fields = (
            "tool_name",
            "arguments",
            "ok",
            "error_code",
            "duration_ms",
            "cache_hit",
            "iteration",
            "trace_id",
            "created_at",
        )
        read_only_fields = fields


class ConversationListSerializer(serializers.ModelSerializer):
    message_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Conversation
        fields = (
            "id",
            "title",
            "context",
            "is_active",
            "message_count",
            "created_at",
            "last_active_at",
        )
        read_only_fields = fields


class ConversationDetailSerializer(serializers.ModelSerializer):
    messages = MessageSerializer(many=True, read_only=True)

    class Meta:
        model = Conversation
        fields = (
            "id",
            "title",
            "context",
            "is_active",
            "messages",
            "created_at",
            "last_active_at",
        )
        read_only_fields = fields


class AuditLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = AuditLog
        fields = (
            "id",
            "username",
            "role",
            "action",
            "conversation",
            "query_text",
            "answer_preview",
            "tools_called",
            "order_ids_touched",
            "succeeded",
            "error_code",
            "duration_ms",
            "total_input_tokens",
            "total_output_tokens",
            "trace_id",
            "ip_address",
            "created_at",
        )
        read_only_fields = fields
