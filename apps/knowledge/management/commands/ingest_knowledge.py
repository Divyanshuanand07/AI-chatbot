"""
Ingest SOP/policy markdown into the vector store.

    python manage.py ingest_knowledge
    python manage.py ingest_knowledge --force        # re-embed everything

Idempotent by design. Each document's checksum is stored, so re-running after
a deploy is a no-op for unchanged files. That matters on a paid embedding
backend, where blindly re-embedding the corpus on every deploy is a recurring
bill for no benefit.

Chunks are replaced atomically per document: a failed embedding call leaves
the previously indexed version in place rather than a half-updated document.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.knowledge.chunking import chunk_markdown, parse_document
from apps.knowledge.embeddings import EmbeddingError, get_embedder
from apps.knowledge.models import Chunk, Document, DocumentCategory
from apps.knowledge.vocabulary import invalidate_vocabulary

DEFAULT_DOCUMENT_DIR = Path(__file__).resolve().parents[2] / "documents"


class Command(BaseCommand):
    help = "Load SOP and policy markdown files into the pgvector knowledge base."

    def add_arguments(self, parser):
        parser.add_argument(
            "--path",
            type=str,
            default=str(DEFAULT_DOCUMENT_DIR),
            help="Directory containing markdown documents.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Re-embed even when the checksum is unchanged.",
        )

    def handle(self, *args, **options):
        directory = Path(options["path"])
        if not directory.is_dir():
            raise CommandError(f"No such directory: {directory}")

        files = sorted(directory.glob("*.md"))
        if not files:
            raise CommandError(f"No markdown files found in {directory}")

        try:
            embedder = get_embedder()
        except EmbeddingError as exc:
            raise CommandError(str(exc)) from exc

        self.stdout.write(
            f"Embedding backend: {embedder.signature} ({embedder.dimensions} dims)"
        )

        ingested = skipped = 0
        total_chunks = 0

        for path in files:
            result = self._ingest_file(path, embedder, force=options["force"])
            if result is None:
                skipped += 1
                self.stdout.write(f"  = {path.name} (unchanged)")
            else:
                ingested += 1
                total_chunks += result
                self.stdout.write(
                    self.style.SUCCESS(f"  + {path.name} -> {result} chunks")
                )

        # The vocabulary gate is cached; a newly ingested document must not be
        # rejected as "undocumented" because the cache predates it.
        invalidate_vocabulary()

        self.stdout.write(
            self.style.SUCCESS(
                f"\nIngested {ingested} document(s), {skipped} unchanged, "
                f"{total_chunks} chunk(s) embedded. "
                f"Corpus now holds {Chunk.objects.count()} chunk(s) across "
                f"{Document.objects.count()} document(s)."
            )
        )

    # ------------------------------------------------------------------
    def _ingest_file(self, path: Path, embedder, *, force: bool) -> int | None:
        parsed = parse_document(path.read_text(encoding="utf-8"))
        metadata = parsed.metadata
        slug = metadata.get("slug") or path.stem

        existing = Document.objects.filter(slug=slug).first()
        unchanged = (
            existing
            and existing.checksum == parsed.checksum
            and existing.chunks.filter(embedding_provider=embedder.signature).exists()
        )
        if unchanged and not force:
            return None

        title = metadata.get("title") or slug.replace("-", " ").title()
        category = (metadata.get("category") or DocumentCategory.SOP).upper()
        if category not in DocumentCategory.values:
            raise CommandError(
                f"{path.name}: unknown category '{category}'. "
                f"Valid: {', '.join(DocumentCategory.values)}"
            )

        effective_from = None
        if raw_date := metadata.get("effective_from"):
            try:
                effective_from = dt.date.fromisoformat(raw_date)
            except ValueError as exc:
                raise CommandError(
                    f"{path.name}: effective_from must be YYYY-MM-DD, got '{raw_date}'"
                ) from exc

        chunks = chunk_markdown(parsed.body, document_title=title)
        if not chunks:
            raise CommandError(f"{path.name}: produced no chunks — is it empty?")

        # Embed before touching the database, so a provider failure cannot
        # leave the document with no chunks at all.
        try:
            vectors = embedder.embed_documents([chunk.content for chunk in chunks])
        except EmbeddingError as exc:
            raise CommandError(f"{path.name}: embedding failed — {exc}") from exc

        with transaction.atomic():
            document, _ = Document.objects.update_or_create(
                slug=slug,
                defaults={
                    "title": title,
                    "category": category,
                    "version": metadata.get("version", "1.0"),
                    "owner_team": metadata.get("owner_team", ""),
                    "effective_from": effective_from,
                    "source_path": str(path.name),
                    "content": parsed.body,
                    "checksum": parsed.checksum,
                    "is_active": True,
                },
            )
            document.chunks.all().delete()
            Chunk.objects.bulk_create(
                [
                    Chunk(
                        document=document,
                        sequence=chunk.sequence,
                        heading=chunk.heading,
                        content=chunk.content,
                        char_count=len(chunk.content),
                        embedding=vector,
                        embedding_provider=embedder.signature,
                    )
                    for chunk, vector in zip(chunks, vectors, strict=False)
                ]
            )

        return len(chunks)
