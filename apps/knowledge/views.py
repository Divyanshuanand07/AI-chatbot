"""
Knowledge base REST API.

Exists independently of the assistant for the same reason the order APIs do:
the retrieval layer should be inspectable without going through a model. When
an operator disputes a policy answer, you want to run the same search they
triggered and see the passages and scores directly.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.drf import HasScope
from apps.accounts.permissions import Scope
from apps.common.exceptions import ValidationError

from .embeddings import EmbeddingError
from .retriever import list_documents, search


@extend_schema(
    summary="Search SOPs and policies",
    parameters=[
        OpenApiParameter("q", str, required=True),
        OpenApiParameter("category", str),
        OpenApiParameter("top_k", int),
    ],
    responses={200: dict},
    description=(
        "Vector search over the indexed SOP corpus. Returns passages with "
        "similarity scores and the relevance threshold applied, so a 'not "
        "covered' result can be distinguished from a retrieval failure."
    ),
)
class KnowledgeSearchView(APIView):
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = (Scope.KNOWLEDGE_READ,)
    throttle_scope = "read_api"

    def get(self, request):
        query = request.query_params.get("q", "").strip()
        if not query:
            raise ValidationError("Provide a search query in the 'q' parameter.")
        try:
            result = search(
                query,
                category=request.query_params.get("category"),
                top_k=request.query_params.get("top_k"),
            )
        except EmbeddingError as exc:
            raise ValidationError(str(exc)) from exc
        return Response(result)


@extend_schema(
    summary="List indexed documents",
    parameters=[OpenApiParameter("category", str)],
    responses={200: dict},
)
class KnowledgeDocumentListView(APIView):
    permission_classes = [IsAuthenticated, HasScope]
    required_scopes = (Scope.KNOWLEDGE_READ,)
    throttle_scope = "read_api"

    def get(self, request):
        return Response(
            list_documents(category=request.query_params.get("category"))
        )
