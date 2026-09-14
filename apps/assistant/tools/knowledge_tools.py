"""
Knowledge-base tool.

Lives in the assistant app rather than in `apps.knowledge` so the dependency
runs one way only: the assistant knows about the knowledge base, and the
knowledge base knows nothing about tool calling. That keeps `apps.knowledge`
usable on its own (by the REST search endpoint, by the calibration command)
and avoids an import cycle through the registry.
"""

from __future__ import annotations

from apps.accounts.permissions import Scope
from apps.knowledge.embeddings import EmbeddingError
from apps.knowledge.models import DocumentCategory
from apps.knowledge.retriever import search

from .base import ToolContext, ToolResult, registry


@registry.register(
    name="search_knowledge_base",
    description=(
        "Search internal Cars24 standard operating procedures, policies and "
        "reference documents. Use this for questions about process, policy or "
        "terminology rather than about a specific order — for example what a "
        "payment status means, how long a refund should take, what the SLA for "
        "document verification is, who to escalate a delayed delivery to, or "
        "what to do when a loan is rejected. Returns passages with the "
        "document title, version and section heading; cite those in your "
        "answer. If it returns no results, say the topic is not covered in "
        "the documented SOPs — never substitute general knowledge about how "
        "used-car businesses usually work, because the operator will act on "
        "it as if it were Cars24 policy."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The question or topic to look up. Use the operator's own "
                    "wording where possible."
                ),
            },
            "category": {
                "type": ["string", "null"],
                "description": (
                    "Optional filter to one kind of document. Pass null to "
                    "search everything."
                ),
                "enum": [None, *DocumentCategory.values],
            },
        },
        "required": ["query", "category"],
        "additionalProperties": False,
    },
    required_scopes=(Scope.KNOWLEDGE_READ,),
    # SOPs change rarely, so a longer TTL is safe here — unlike order data.
    cache_ttl=300,
)
def search_knowledge_base(ctx: ToolContext, query: str, category=None) -> ToolResult:
    try:
        result = search(query, category=category)
    except EmbeddingError as exc:
        # The embedding backend is a dependency we do not control. Fail
        # explicitly rather than returning zero hits, which the model would
        # correctly report as "not covered" — a misleading answer.
        return ToolResult.failure(
            "KNOWLEDGE_UNAVAILABLE",
            (
                f"The knowledge base could not be searched: {exc} "
                f"Say that the policy lookup is unavailable right now."
            ),
        )

    if not result.get("result_count"):
        # Deliberately a success, not an error: "nothing documented covers
        # this" is a real and useful answer, and marking it as an error would
        # push the model toward retrying or apologising instead of reporting it.
        return ToolResult.success(result)

    return ToolResult.success(result)
