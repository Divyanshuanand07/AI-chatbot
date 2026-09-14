"""
Assistant API.

`POST /api/ai/query/` is the one endpoint operators (or a UI) actually call.
The rest exist for transparency: conversation history, the tool catalogue the
caller's role can reach, and — for managers — the audit trail.

The tool-catalogue endpoint is not decoration. When someone disputes an
answer, the first question is "what could it even see?", and being able to
answer that from the API rather than from the source is what makes the
permission model reviewable.
"""

from __future__ import annotations

import logging

from django.db.models import Count
from drf_spectacular.utils import extend_schema
from rest_framework import status
from rest_framework.generics import ListAPIView
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.drf import HasScope
from apps.accounts.permissions import Scope
from apps.common.exceptions import NotFoundError, UpstreamError

from .llm import LLMError
from .models import AuditLog, Conversation
from .orchestrator import Orchestrator
from .serializers import (
    AskSerializer,
    AuditLogSerializer,
    ConversationDetailSerializer,
    ConversationListSerializer,
)
from .tools import registry

logger = logging.getLogger(__name__)


def _request_meta(request) -> dict:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    ip = (
        forwarded.split(",")[0].strip()
        if forwarded
        else request.META.get("REMOTE_ADDR")
    )
    return {"ip": ip, "user_agent": request.META.get("HTTP_USER_AGENT", "")}


@extend_schema(
    summary="Ask the operations assistant a question",
    request=AskSerializer,
    responses={200: dict},
    description=(
        "Answers a natural-language operational question. The model selects "
        "and calls read-only tools; every operational fact in the answer "
        "comes from the operations database. The response includes the tools "
        "that actually ran, so any claim can be traced."
    ),
)
class AskView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "ai_query"

    def post(self, request):
        serializer = AskSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        question = serializer.validated_data["question"]
        conversation_id = serializer.validated_data.get("conversation_id")

        conversation = None
        if conversation_id:
            # Scoped to the requesting user: conversation ids are UUIDs, but
            # authorization must never rest on an identifier being hard to
            # guess.
            conversation = Conversation.objects.filter(
                pk=conversation_id, user=request.user
            ).first()
            if conversation is None:
                raise NotFoundError(
                    "No such conversation for this user.",
                    details={"conversation_id": str(conversation_id)},
                )

        orchestrator = Orchestrator(
            request.user, trace_id=getattr(request, "trace_id", None)
        )
        try:
            answer = orchestrator.ask(
                question,
                conversation=conversation,
                request_meta=_request_meta(request),
            )
        except LLMError as exc:
            # The provider is a dependency we do not control; surface it as a
            # 502 with the trace id rather than a generic 500.
            raise UpstreamError(
                exc.message, details={"code": exc.code, "retryable": exc.retryable}
            ) from exc

        return Response(answer.to_dict(), status=status.HTTP_200_OK)


@extend_schema(summary="List my conversations", responses={200: ConversationListSerializer})
class ConversationListView(ListAPIView):
    serializer_class = ConversationListSerializer
    permission_classes = [IsAuthenticated]
    throttle_scope = "read_api"

    def get_queryset(self):
        return (
            Conversation.objects.filter(user=self.request.user)
            .annotate(message_count=Count("messages"))
            .order_by("-last_active_at")
        )


@extend_schema(summary="Conversation transcript", responses={200: ConversationDetailSerializer})
class ConversationDetailView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "read_api"

    def get(self, request, conversation_id):
        conversation = (
            Conversation.objects.filter(pk=conversation_id, user=request.user)
            .prefetch_related("messages")
            .first()
        )
        if conversation is None:
            raise NotFoundError("No such conversation for this user.")
        return Response(ConversationDetailSerializer(conversation).data)

    def delete(self, request, conversation_id):
        """Close a conversation. History is retained — it is an audit record."""
        conversation = Conversation.objects.filter(
            pk=conversation_id, user=request.user
        ).first()
        if conversation is None:
            raise NotFoundError("No such conversation for this user.")
        conversation.is_active = False
        conversation.save(update_fields=["is_active", "last_active_at"])
        return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(
    summary="Tools available to my role",
    responses={200: dict},
    description=(
        "Introspection: exactly which tools the assistant can use on this "
        "caller's behalf, and which are withheld by their role. Useful when "
        "an operator asks why the assistant would not answer something."
    ),
)
class ToolCatalogueView(APIView):
    permission_classes = [IsAuthenticated]
    throttle_scope = "read_api"

    def get(self, request):
        scopes = request.user.scopes
        available = registry.available_to(scopes)
        available_names = {spec.name for spec in available}

        return Response(
            {
                "role": request.user.role,
                "scopes": sorted(str(s) for s in scopes),
                "available": [
                    {
                        "name": spec.name,
                        "description": spec.description,
                        "required_scopes": [str(s) for s in spec.required_scopes],
                        "cache_ttl_seconds": spec.cache_ttl,
                    }
                    for spec in available
                ],
                "withheld": [
                    {
                        "name": spec.name,
                        "missing_scopes": sorted(
                            str(s) for s in spec.required_scopes if s not in scopes
                        ),
                    }
                    for spec in registry.all()
                    if spec.name not in available_names
                ],
            }
        )


@extend_schema(summary="Assistant audit trail", responses={200: AuditLogSerializer})
class AuditLogView(ListAPIView):
    """
    Who asked what, which tools ran, and what came back.

    Gated on `audit:read` (ops managers and admins). An audit log readable by
    everyone it records is not much of a control.
    """

    serializer_class = AuditLogSerializer
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = (Scope.AUDIT_READ,)
    throttle_scope = "read_api"

    def get_queryset(self):
        queryset = AuditLog.objects.select_related("user").order_by("-created_at")
        params = self.request.query_params

        if username := params.get("username"):
            queryset = queryset.filter(username=username)
        if action := params.get("action"):
            queryset = queryset.filter(action=action)
        if trace_id := params.get("trace_id"):
            queryset = queryset.filter(trace_id=trace_id)
        if order_id := params.get("order_id"):
            try:
                queryset = queryset.filter(order_ids_touched__contains=int(order_id))
            except ValueError:
                pass
        if params.get("failed_only") in ("1", "true", "True"):
            queryset = queryset.filter(succeeded=False)
        return queryset
