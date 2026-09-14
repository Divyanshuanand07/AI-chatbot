"""
Vector retrieval over the SOP corpus.

Two details matter more than the similarity maths:

1. **A relevance floor.** Vector search always returns its top-k, however
   irrelevant. Handing the model weak passages is how a RAG system invents a
   policy: given something that looks like context, a model will use it. Below
   `MIN_SIMILARITY` we return *no* results and say so, so the assistant
   answers "that is not covered in the documented SOPs".

2. **Provider-scoped vectors.** Chunks record which backend embedded them.
   Comparing a hashing-backend query against Voyage-backend vectors produces
   numbers that look like scores and mean nothing, so retrieval filters on the
   active backend and reports the mismatch rather than returning noise.
"""

from __future__ import annotations

import logging

from django.conf import settings
from pgvector.django import CosineDistance

from .embeddings import get_embedder
from .models import Chunk
from .vocabulary import out_of_vocabulary_ratio

logger = logging.getLogger(__name__)

MAX_TOP_K = 10


def threshold_for(provider: str) -> float:
    """
    Relevance floor for a given embedding backend.

    Cosine scores are only comparable inside one embedding space, so the
    threshold belongs to the backend rather than to the application.
    """
    per_provider = settings.KNOWLEDGE.get("MIN_SIMILARITY_BY_PROVIDER") or {}
    return float(
        per_provider.get(provider, settings.KNOWLEDGE["MIN_SIMILARITY"])
    )


def search(
    query: str,
    *,
    top_k: int | None = None,
    category: str | None = None,
    min_similarity: float | None = None,
) -> dict:
    """
    Retrieve the passages most relevant to `query`.

    Returns a dict ready to hand to the model, including the scores and the
    threshold used — so the answer can be explicit about how confident the
    retrieval was.
    """
    config = settings.KNOWLEDGE
    top_k = max(1, min(int(top_k or config["TOP_K"]), MAX_TOP_K))

    query = (query or "").strip()
    embedder = get_embedder()
    threshold = (
        float(min_similarity)
        if min_similarity is not None
        else threshold_for(embedder.name)
    )

    if not query:
        return _empty(query, threshold, reason="An empty query cannot be searched.")

    # Vocabulary gate, lexical backends only. A high cosine score on shared
    # common words is not evidence the corpus covers the topic.
    if embedder.is_lexical:
        oov_ratio, unknown_terms = out_of_vocabulary_ratio(query)
        if oov_ratio > config["MAX_OOV_RATIO"]:
            logger.info(
                "knowledge_out_of_vocabulary",
                extra={
                    "query": query[:120],
                    "oov_ratio": round(oov_ratio, 3),
                    # Logged so recurring gaps in the corpus are visible.
                    "unknown_terms": unknown_terms[:10],
                },
            )
            return _empty(
                query,
                threshold,
                reason=(
                    "This topic is not covered by any documented SOP or "
                    "policy. Say so plainly rather than answering from "
                    "general knowledge."
                ),
                unknown_terms=unknown_terms[:10],
            )

    vector = embedder.embed_query(query)

    queryset = Chunk.objects.filter(
        document__is_active=True,
        # Signature, not just name: a vector produced by an earlier version of
        # the same backend lives in a different geometry and must be ignored.
        embedding_provider=embedder.signature,
    ).select_related("document")

    if category:
        queryset = queryset.filter(document__category=str(category).upper())

    if not queryset.exists():
        logger.warning(
            "knowledge_base_empty_for_provider",
            extra={"provider": embedder.signature, "category": category},
        )
        return _empty(
            query,
            threshold,
            reason=(
                "The knowledge base has no documents indexed for the active "
                "embedding backend. Run `manage.py ingest_knowledge`."
            ),
        )

    rows = list(
        queryset.annotate(distance=CosineDistance("embedding", vector))
        .order_by("distance")[:top_k]
    )

    scored = [
        {
            "document_slug": row.document.slug,
            "document_title": row.document.title,
            "category": row.document.category,
            "version": row.document.version,
            "owner_team": row.document.owner_team,
            "heading": row.heading,
            "content": row.content,
            # pgvector cosine distance is 1 - cosine similarity.
            "similarity": round(1.0 - float(row.distance), 4),
        }
        for row in rows
    ]

    relevant = [item for item in scored if item["similarity"] >= threshold]

    if not relevant:
        best = scored[0]["similarity"] if scored else 0.0
        logger.info(
            "knowledge_below_threshold",
            extra={"query": query[:120], "best_similarity": best, "threshold": threshold},
        )
        return _empty(
            query,
            threshold,
            reason=(
                "Nothing in the documented SOPs or policies is a close enough "
                "match to answer this reliably."
            ),
            best_similarity=best,
        )

    return {
        "query": query,
        "result_count": len(relevant),
        "threshold": threshold,
        "embedding_provider": embedder.signature,
        "results": relevant,
        "sources": sorted(
            {
                f"{item['document_title']} v{item['version']}"
                for item in relevant
            }
        ),
    }


def _empty(
    query: str,
    threshold: float,
    *,
    reason: str,
    best_similarity: float = 0.0,
    unknown_terms: list[str] | None = None,
) -> dict:
    payload = {
        "query": query,
        "result_count": 0,
        "threshold": threshold,
        "best_similarity": best_similarity,
        "results": [],
        "sources": [],
        "note": reason,
    }
    if unknown_terms:
        payload["unknown_terms"] = unknown_terms
    return payload


def list_documents(*, category: str | None = None) -> dict:
    """Catalogue of indexed documents — what the assistant could cite."""
    from .models import Document

    queryset = Document.objects.filter(is_active=True)
    if category:
        queryset = queryset.filter(category=str(category).upper())

    return {
        "count": queryset.count(),
        "documents": [
            {
                "slug": doc.slug,
                "title": doc.title,
                "category": doc.category,
                "version": doc.version,
                "owner_team": doc.owner_team,
                "effective_from": (
                    doc.effective_from.isoformat() if doc.effective_from else None
                ),
                "chunk_count": doc.chunks.count(),
            }
            for doc in queryset.order_by("title")
        ],
    }
