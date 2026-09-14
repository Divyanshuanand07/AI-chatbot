"""
Knowledge base: SOPs, policies and reference documents, chunked and embedded.

Two tables rather than one, because the retrieval unit and the citation unit
are different. Operators trust an answer that says "per the Refund &
Cancellation Policy v2.1, section 'Refund timelines'" — so chunks keep a
foreign key to a versioned document and carry their own heading.

Embeddings are fixed at 512 dimensions. Both supported embedding backends are
configured to emit 512 (the local hashing backend natively, Voyage via its
`output_dimension` parameter), so switching providers does not require a
schema migration — only a re-ingest.
"""

from __future__ import annotations

from django.db import models
from pgvector.django import HnswIndex, VectorField

EMBEDDING_DIMENSIONS = 512


class DocumentCategory(models.TextChoices):
    SOP = "SOP", "Standard operating procedure"
    POLICY = "POLICY", "Policy"
    REFERENCE = "REFERENCE", "Reference / glossary"
    PLAYBOOK = "PLAYBOOK", "Playbook"


class Document(models.Model):
    slug = models.SlugField(max_length=120, unique=True)
    title = models.CharField(max_length=200)
    category = models.CharField(
        max_length=16, choices=DocumentCategory.choices, default=DocumentCategory.SOP
    )
    version = models.CharField(max_length=16, default="1.0")
    owner_team = models.CharField(max_length=64, blank=True)
    effective_from = models.DateField(null=True, blank=True)
    source_path = models.CharField(max_length=300, blank=True)

    content = models.TextField()
    #: Hash of `content`. Re-ingesting an unchanged file is then a no-op,
    #: which matters because re-embedding costs money on a paid backend.
    checksum = models.CharField(max_length=64, db_index=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "knowledge_documents"
        ordering = ["title"]

    def __str__(self) -> str:
        return f"{self.title} v{self.version}"


class Chunk(models.Model):
    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="chunks"
    )
    sequence = models.PositiveIntegerField()
    #: The markdown heading this chunk sits under — the citable section name.
    heading = models.CharField(max_length=200, blank=True)
    content = models.TextField()
    char_count = models.PositiveIntegerField(default=0)

    embedding = VectorField(dimensions=EMBEDDING_DIMENSIONS)
    #: Which backend produced the vector. Mixing backends in one table would
    #: silently ruin similarity scores, so retrieval filters on this.
    embedding_provider = models.CharField(max_length=32, db_index=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "knowledge_chunks"
        ordering = ["document_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "sequence"], name="uniq_document_chunk_sequence"
            )
        ]
        indexes = [
            # HNSW with cosine distance. Built for a small corpus; revisit
            # ef_construction/m if the SOP library grows past a few thousand
            # chunks.
            HnswIndex(
                name="knowledge_chunk_embedding_idx",
                fields=["embedding"],
                m=16,
                ef_construction=64,
                opclasses=["vector_cosine_ops"],
            ),
        ]

    def __str__(self) -> str:
        return f"{self.document_id}.{self.sequence} {self.heading[:40]}"
