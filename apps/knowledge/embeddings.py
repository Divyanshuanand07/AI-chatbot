"""
Embedding backends.

Anthropic does not offer an embeddings endpoint, so RAG needs a second
provider. Two backends are supported:

* **hashing** (default) — deterministic feature hashing, computed locally.
  No network, no key, no cost, and identical results on every machine, which
  is what lets the knowledge-base tests be hermetic. It is lexical, not
  semantic: it matches vocabulary overlap, so "refund timeline" finds the
  refund section but a pure paraphrase with no shared words may not.
* **voyage** — Voyage AI embeddings, genuinely semantic, for production.
  Requested at 512 dimensions to match the column, so switching backends
  needs a re-ingest but no migration.

A critical implementation detail: the hashing backend uses `blake2b`, not
Python's built-in `hash()`. `hash()` on strings is salted per process
(PYTHONHASHSEED), so vectors written by one worker would not match queries
embedded by another — stored embeddings must be reproducible across processes
and restarts.
"""

from __future__ import annotations

import abc
import functools
import hashlib
import json
import logging
import math
import re
import urllib.error
import urllib.request

from django.conf import settings

logger = logging.getLogger(__name__)

WORD_PATTERN = re.compile(r"[a-z0-9]+")

#: Character n-grams let morphological variants ("refund"/"refunds",
#: "cancel"/"cancellation") share signal in the lexical backend.
NGRAM_SIZE = 4
NGRAM_WEIGHT = 0.35

#: Generic English that carries no topical meaning in this corpus.
#:
#: This list is deliberately broad. It feeds the out-of-vocabulary gate in
#: `vocabulary.py`, where a generic word counted as "missing subject matter"
#: is actively harmful: with a short list, "how long does a refund take?"
#: looked 33% off-topic purely because "take" does not appear in any SOP.
#: Domain nouns and verbs (order, payment, refund, escalate, hub, policy,
#: document, delivery, finance, transfer, verify, ...) must never be added.
STOPWORDS = frozenset(
    """
    a an and are as at be been being am but by for from had has have how i if
    in into is it its of on or our so than that the their them then there
    these this those to was were what when where which who whom whose why
    will with you your we us my me he she they do does did doing done not
    can may must should shall would could about over under per
    long many much more most less least some any all each every other another
    same different new old first last next previous before after during while
    since until again once twice here now today tomorrow yesterday
    take takes taken taking get gets got give given gives go goes going
    allowed allow consider considered valid work works working need needs
    want tell told say said ask asks asked use used using make made makes put
    happen happens happened know think mean means meaning
    still already also just only thing things way ways case cases time times
    day days week weeks month months year years
    please help see look show tell find
    """.split()
)


class EmbeddingError(Exception):
    pass


class Embedder(abc.ABC):
    name: str = "base"
    dimensions: int = 512
    #: Bump whenever anything that changes the output vectors changes —
    #: tokenisation, the stopword list, n-gram settings, the upstream model.
    #:
    #: This exists because of a real failure: editing STOPWORDS silently
    #: changed how queries are embedded while 47 already-stored chunk vectors
    #: kept the old geometry. Nothing errors — similarity scores just quietly
    #: degrade, which is the worst kind of bug. Stored chunks record the full
    #: signature, retrieval filters on it, so stale vectors become invisible
    #: and the retriever reports "not indexed for the active backend. Run
    #: ingest_knowledge" instead of returning bad matches.
    version: str = "v1"
    #: True when similarity comes from shared vocabulary rather than meaning.
    #: Lexical backends need the out-of-vocabulary gate in `vocabulary.py`;
    #: semantic ones must not have it, since matching paraphrases with no
    #: shared words is precisely their value.
    is_lexical: bool = False

    @property
    def signature(self) -> str:
        """Identity stored against every vector this backend produces."""
        return f"{self.name}:{self.version}"

    @abc.abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Embed corpus passages (indexing side)."""

    @abc.abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a search query (retrieval side)."""


# ---------------------------------------------------------------------------
# Local, deterministic
# ---------------------------------------------------------------------------
class HashingEmbedder(Embedder):
    name = "hashing"
    # v2: expanded STOPWORDS and added stemming for the vocabulary gate.
    version = "v2"
    is_lexical = True

    def __init__(self, dimensions: int | None = None):
        self.dimensions = dimensions or settings.KNOWLEDGE["EMBEDDING_DIM"]

    # -- helpers --------------------------------------------------------
    def _bucket(self, token: str) -> tuple[int, float]:
        """
        Map a token to (index, sign) via a stable hash.

        The sign bit is the signed-hashing trick: collisions then cancel out
        on average instead of always inflating a bucket.
        """
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        index = value % self.dimensions
        sign = 1.0 if (value >> 63) & 1 else -1.0
        return index, sign

    def _features(self, text: str) -> dict[str, float]:
        counts: dict[str, float] = {}
        for word in WORD_PATTERN.findall(text.lower()):
            if len(word) < 2 or word in STOPWORDS:
                continue
            counts[word] = counts.get(word, 0.0) + 1.0
            if len(word) > NGRAM_SIZE:
                padded = f"#{word}#"
                for i in range(len(padded) - NGRAM_SIZE + 1):
                    gram = padded[i : i + NGRAM_SIZE]
                    counts[f"~{gram}"] = counts.get(f"~{gram}", 0.0) + NGRAM_WEIGHT
        return counts

    def _vectorise(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token, count in self._features(text).items():
            # Sublinear scaling: a term appearing 20 times is not 20x as
            # informative as one appearing once.
            weight = 1.0 + math.log(count) if count > 1 else count
            index, sign = self._bucket(token)
            vector[index] += sign * weight

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    # -- interface ------------------------------------------------------
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vectorise(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vectorise(text)


# ---------------------------------------------------------------------------
# Voyage AI
# ---------------------------------------------------------------------------
class VoyageEmbedder(Embedder):
    name = "voyage"
    is_lexical = False
    ENDPOINT = "https://api.voyageai.com/v1/embeddings"
    BATCH_SIZE = 64

    def __init__(self):
        config = settings.KNOWLEDGE
        self.api_key = config["VOYAGE_API_KEY"]
        self.model = config["VOYAGE_MODEL"]
        self.dimensions = config["EMBEDDING_DIM"]
        # The upstream model name *is* the version: vectors from voyage-3.5
        # and voyage-3 are not comparable, so switching models must invalidate
        # the indexed corpus rather than silently mixing embedding spaces.
        self.version = f"{self.model}-{self.dimensions}"
        if not self.api_key:
            raise EmbeddingError(
                "EMBEDDING_PROVIDER=voyage requires VOYAGE_API_KEY. "
                "Use EMBEDDING_PROVIDER=hashing for a local, offline setup."
            )

    def _post(self, texts: list[str], input_type: str) -> list[list[float]]:
        payload = json.dumps(
            {
                "input": texts,
                "model": self.model,
                "input_type": input_type,
                # Match the vector column width so no migration is needed.
                "output_dimension": self.dimensions,
            }
        ).encode()

        request = urllib.request.Request(
            self.ENDPOINT,
            data=payload,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise EmbeddingError(
                f"Voyage API returned {exc.code}: {exc.read()[:200]!r}"
            ) from exc
        except urllib.error.URLError as exc:
            raise EmbeddingError(f"Could not reach Voyage API: {exc.reason}") from exc

        rows = sorted(body["data"], key=lambda item: item["index"])
        return [row["embedding"] for row in rows]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.BATCH_SIZE):
            vectors.extend(
                self._post(texts[start : start + self.BATCH_SIZE], "document")
            )
        return vectors

    def embed_query(self, text: str) -> list[float]:
        # input_type="query" is not cosmetic: Voyage embeds queries and
        # documents into deliberately different regions of the space.
        return self._post([text], "query")[0]


# ---------------------------------------------------------------------------
@functools.lru_cache(maxsize=4)
def _build(provider: str) -> Embedder:
    if provider == "hashing":
        return HashingEmbedder()
    if provider == "voyage":
        return VoyageEmbedder()
    raise EmbeddingError(
        f"Unknown embedding provider '{provider}'. Use 'hashing' or 'voyage'."
    )


def get_embedder(provider: str | None = None) -> Embedder:
    return _build(provider or settings.KNOWLEDGE["EMBEDDING_PROVIDER"])


def reset_embedder_cache() -> None:
    _build.cache_clear()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Used in tests and for scoring already-normalised vectors."""
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)
