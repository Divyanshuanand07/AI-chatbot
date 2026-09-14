"""
Tests for the knowledge base: chunking, embeddings, retrieval and the two
guards that stop the assistant inventing policy.

The guards get the most attention. A RAG system that returns *something* for
every question is worse than no RAG system, because a model handed plausible
context will use it — and an operator will then act on a policy that does not
exist.
"""

from __future__ import annotations

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.test import override_settings

from apps.knowledge.chunking import chunk_markdown, parse_document
from apps.knowledge.embeddings import (
    HashingEmbedder,
    cosine_similarity,
    get_embedder,
    reset_embedder_cache,
)
from apps.knowledge.models import Chunk, Document
from apps.knowledge.retriever import search, threshold_for
from apps.knowledge.vocabulary import (
    invalidate_vocabulary,
    out_of_vocabulary_ratio,
    stem,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def corpus():
    """Ingest the real SOP corpus once per test that needs it."""
    cache.clear()
    call_command("ingest_knowledge", verbosity=0)
    invalidate_vocabulary()
    yield
    cache.clear()


# ---------------------------------------------------------------------------
class TestChunking:
    def test_frontmatter_is_parsed(self):
        parsed = parse_document(
            "---\ntitle: Refund Policy\nversion: 2.1\n---\n\n## Scope\n\nBody text."
        )
        assert parsed.metadata["title"] == "Refund Policy"
        assert parsed.metadata["version"] == "2.1"
        assert parsed.body.startswith("## Scope")

    def test_checksum_is_stable_and_content_sensitive(self):
        a = parse_document("---\ntitle: X\n---\n\nsame")
        b = parse_document("---\ntitle: X\n---\n\nsame")
        c = parse_document("---\ntitle: X\n---\n\ndifferent")
        assert a.checksum == b.checksum
        assert a.checksum != c.checksum

    def test_chunks_split_on_headings_and_keep_the_heading(self):
        body = "## First\n\nAlpha content here.\n\n## Second\n\nBeta content here."
        chunks = chunk_markdown(body, document_title="Doc")
        assert [c.heading for c in chunks] == ["First", "Second"]

    def test_heading_is_prefixed_into_the_chunk_text(self):
        """
        Without the prefix, a chunk under "PENDING" that never repeats the
        word is unfindable by a question about PENDING.
        """
        body = "## PENDING\n\nDo not ask the customer to pay again right now."
        chunk = chunk_markdown(body, document_title="Payment Status Reference")[0]
        assert chunk.content.startswith("Payment Status Reference — PENDING")

    def test_long_sections_split_with_overlap(self):
        paragraph = "word " * 120
        body = "## Long\n\n" + "\n\n".join([paragraph] * 6)
        chunks = chunk_markdown(body, document_title="Doc")
        assert len(chunks) > 1
        assert all(len(c.content) < 2000 for c in chunks)


# ---------------------------------------------------------------------------
class TestHashingEmbedder:
    def test_is_deterministic_across_instances(self):
        """
        Uses blake2b, not Python's salted hash(). If this breaks, vectors
        written by one worker stop matching queries embedded by another.
        """
        a = HashingEmbedder().embed_query("refund timeline policy")
        b = HashingEmbedder().embed_query("refund timeline policy")
        assert a == b

    def test_vectors_are_unit_length(self):
        vector = HashingEmbedder().embed_query("payment status pending")
        assert pytest.approx(sum(v * v for v in vector), rel=1e-6) == 1.0

    def test_related_text_scores_above_unrelated_text(self):
        embedder = HashingEmbedder()
        query = embedder.embed_query("refund timeline for a cancelled order")
        related = embedder.embed_documents(
            ["Refunds must be initiated within two working days of cancellation."]
        )[0]
        unrelated = embedder.embed_documents(
            ["The vehicle inspection score is recorded against the listing."]
        )[0]
        assert cosine_similarity(query, related) > cosine_similarity(query, unrelated)

    def test_dimension_matches_the_column(self):
        assert len(HashingEmbedder().embed_query("x")) == 512

    def test_signature_identifies_the_backend_version(self):
        embedder = HashingEmbedder()
        assert embedder.signature == f"{embedder.name}:{embedder.version}"
        assert embedder.is_lexical is True


# ---------------------------------------------------------------------------
class TestVocabularyGate:
    def test_stemmer_matches_plurals_without_collapsing_distinct_terms(self):
        assert stem("blockers") == stem("blocker")
        assert stem("payments") == stem("payment")
        assert stem("policies") == stem("policy")
        # Must not merge genuinely different domain words.
        assert stem("delivery") != stem("document")

    def test_on_topic_question_has_low_out_of_vocabulary_ratio(self, corpus):
        ratio, unknown = out_of_vocabulary_ratio("how long does a refund take")
        assert ratio <= 0.2, unknown

    def test_adversarial_question_is_mostly_unknown_vocabulary(self, corpus):
        """
        The case cosine similarity cannot catch: real operational words
        ("policy", "hub") about a topic the corpus has never covered.
        """
        ratio, unknown = out_of_vocabulary_ratio(
            "what is our policy on flying drones over the hub"
        )
        assert ratio > 0.22
        assert "drone" in unknown

    def test_empty_query_is_not_penalised(self):
        assert out_of_vocabulary_ratio("")[0] == 0.0


# ---------------------------------------------------------------------------
class TestRetrieval:
    def test_ingest_indexes_every_document(self, corpus):
        assert Document.objects.count() == 7
        assert Chunk.objects.count() > 30
        assert all(c.embedding_provider == "hashing:v2" for c in Chunk.objects.all())

    def test_ingest_is_idempotent(self, corpus):
        before = Chunk.objects.count()
        call_command("ingest_knowledge", verbosity=0)
        assert Chunk.objects.count() == before

    def test_payment_status_question_finds_the_reference(self, corpus):
        result = search("What does a PENDING payment status mean?")
        assert result["result_count"] > 0
        assert result["results"][0]["document_slug"] == "payment-status-reference"
        assert "Payment Status Reference" in result["sources"][0]

    def test_refund_question_finds_the_policy(self, corpus):
        result = search("how long does a refund take")
        slugs = {r["document_slug"] for r in result["results"]}
        assert "refund-and-cancellation-policy" in slugs

    def test_results_carry_citable_provenance(self, corpus):
        result = search("RC transfer rejected by the RTO")
        passage = result["results"][0]
        for field in ("document_title", "version", "heading", "similarity"):
            assert passage[field] not in (None, "")

    def test_unrelated_question_returns_nothing(self, corpus):
        result = search("what is the capital of France")
        assert result["result_count"] == 0
        assert result["results"] == []
        assert "note" in result

    def test_adversarial_question_returns_nothing(self, corpus):
        result = search("what is our policy on flying drones over the hub")
        assert result["result_count"] == 0
        assert "unknown_terms" in result

    def test_category_filter_narrows_the_search(self, corpus):
        result = search("refund", category="POLICY")
        assert all(r["category"] == "POLICY" for r in result["results"])

    def test_threshold_is_per_backend(self):
        assert threshold_for("hashing") != threshold_for("voyage")
        assert threshold_for("unknown-backend") == 0.25

    def test_stale_vectors_are_invisible_rather_than_wrong(self, corpus):
        """
        The silent-corruption guard. Vectors from an older backend version
        must be ignored, not compared against — mixing embedding spaces
        produces numbers that look like scores and mean nothing.
        """
        Chunk.objects.update(embedding_provider="hashing:v1")
        result = search("how long does a refund take")
        assert result["result_count"] == 0
        assert "ingest_knowledge" in result["note"]

    def test_empty_query_is_rejected(self, corpus):
        assert search("   ")["result_count"] == 0


# ---------------------------------------------------------------------------
class TestKnowledgeTool:
    def test_tool_returns_passages_for_a_policy_question(self, agent, corpus):
        from apps.assistant.tools import ToolContext, ToolExecutor

        executor = ToolExecutor(ToolContext(user=agent, trace_id="t"))
        invocation = executor.execute(
            "search_knowledge_base",
            {"query": "what does a PENDING payment status mean", "category": None},
        )
        assert invocation.ok is True
        assert invocation.result.data["result_count"] > 0

    def test_no_match_is_a_success_not_an_error(self, agent, corpus):
        """
        "Nothing documented covers this" is a real answer. Returning it as an
        error would push the model toward retrying or apologising instead of
        reporting the gap.
        """
        from apps.assistant.tools import ToolContext, ToolExecutor

        executor = ToolExecutor(ToolContext(user=agent, trace_id="t"))
        invocation = executor.execute(
            "search_knowledge_base",
            {"query": "policy on flying drones over the hub", "category": None},
        )
        assert invocation.ok is True
        assert invocation.result.data["result_count"] == 0

    def test_assistant_says_it_is_undocumented(self, agent, corpus):
        from apps.assistant.orchestrator import Orchestrator

        answer = Orchestrator(agent).ask(
            "What is our policy on flying drones over the hub?"
        )
        assert "search_knowledge_base" in [c["tool"] for c in answer.tool_calls]
        assert "could not find" in answer.answer.lower()
        assert "invent" in answer.answer.lower()

    def test_assistant_cites_the_source_document(self, agent, corpus):
        from apps.assistant.orchestrator import Orchestrator

        answer = Orchestrator(agent).ask("How long should a refund take?")
        assert "Refund and Cancellation Policy" in answer.answer


# ---------------------------------------------------------------------------
class TestCalibrationHarness:
    """
    Smoke test for `manage.py calibrate_knowledge`.

    It is a development tool, not production code, but it encodes the quality
    bar for retrieval — and it is a large literal query set that is easy to
    break with a careless edit. Running it here means a malformed labelled set
    fails the suite instead of failing silently the next time someone tunes a
    threshold.
    """

    def test_labelled_set_is_well_formed(self):
        from apps.knowledge.management.commands.calibrate_knowledge import (
            LABELLED_QUERIES,
        )

        assert len(LABELLED_QUERIES) >= 25
        on_topic = [q for q, expected in LABELLED_QUERIES if expected]
        off_topic = [q for q, expected in LABELLED_QUERIES if not expected]
        assert len(on_topic) >= 15
        # Negatives are what keep the guards honest.
        assert len(off_topic) >= 8
        assert len({q for q, _ in LABELLED_QUERIES}) == len(LABELLED_QUERIES)

    def test_expected_slugs_refer_to_real_documents(self, corpus):
        from apps.knowledge.management.commands.calibrate_knowledge import (
            LABELLED_QUERIES,
        )

        real = set(Document.objects.values_list("slug", flat=True))
        for question, expected in LABELLED_QUERIES:
            for slug in expected:
                assert slug in real, f"{question} -> unknown document {slug}"

    def test_command_runs_and_meets_the_quality_bar(self, corpus, capsys):
        call_command("calibrate_knowledge")
        output = capsys.readouterr().out

        assert "Recall" in output
        assert "Off-topic leakage" in output
        # The thresholds committed in settings must actually hold.
        assert "Off-topic leakage (wrongly answered): 0/" in output
        assert "Clean separation" in output


class TestEmbedderSelection:
    def test_unknown_provider_is_rejected(self):
        from apps.knowledge.embeddings import EmbeddingError

        reset_embedder_cache()
        with pytest.raises(EmbeddingError):
            get_embedder("does-not-exist")

    @override_settings(
        KNOWLEDGE={
            **{
                "EMBEDDING_PROVIDER": "voyage",
                "EMBEDDING_DIM": 512,
                "VOYAGE_API_KEY": "",
                "VOYAGE_MODEL": "voyage-3.5",
                "TOP_K": 4,
                "MIN_SIMILARITY": 0.25,
                "MIN_SIMILARITY_BY_PROVIDER": {},
                "MAX_OOV_RATIO": 0.22,
            }
        }
    )
    def test_voyage_without_a_key_fails_with_a_useful_message(self):
        from apps.knowledge.embeddings import EmbeddingError

        reset_embedder_cache()
        with pytest.raises(EmbeddingError, match="VOYAGE_API_KEY"):
            get_embedder("voyage")
        reset_embedder_cache()
