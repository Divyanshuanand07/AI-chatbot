"""
Corpus vocabulary gate for the lexical embedding backend.

The problem this solves: a bag-of-words embedder scores similarity on shared
vocabulary, so "what is our policy on flying drones over the hub?" matches the
Refund and Cancellation Policy on the words "policy" and "hub" alone — well
above any threshold that still admits genuine questions. Raising the floor
until drones are rejected also rejects "how long does a refund take".

The distinguishing signal is not the score, it is the *unmatched* words.
"Drones" and "flying" appear nowhere in the corpus. A question whose
distinctive terms are mostly absent from every document cannot be answered by
those documents, whatever the cosine score says.

So: before trusting a lexical match, measure the fraction of the query's
content words that exist nowhere in the corpus. Above `MAX_OOV_RATIO`, report
the topic as undocumented.

This gate applies **only** to lexical backends. A semantic embedder is
supposed to match paraphrases with no shared vocabulary, and this check would
break exactly the capability you pay it for.
"""

from __future__ import annotations

import logging

from django.core.cache import cache

from .embeddings import STOPWORDS, WORD_PATTERN

logger = logging.getLogger(__name__)

CACHE_KEY = "knowledge:vocabulary:v1"
CACHE_TTL = 3600
MIN_TERM_LENGTH = 3


def stem(word: str) -> str:
    """
    Conservative suffix stripping.

    Not linguistics — just enough that "blockers" matches "blocker" and
    "payments" matches "payment". Without it, ordinary plurals in a question
    read as vocabulary the corpus has never seen. Deliberately timid: an
    over-eager stemmer collapses distinct domain terms into each other, which
    is a worse failure than missing a plural.
    """
    if len(word) > 5:
        for suffix in ("ations", "ation", "ings", "ing", "ments", "ment"):
            if word.endswith(suffix) and len(word) - len(suffix) >= 4:
                return word[: -len(suffix)]
    if len(word) > 4:
        if word.endswith("ies"):
            return word[:-3] + "y"
        if word.endswith("ed") and not word.endswith("eed"):
            return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _content_terms(text: str) -> set[str]:
    """Topical terms only: stopwords removed, stems compared."""
    return {
        stem(word)
        for word in WORD_PATTERN.findall(text.lower())
        if len(word) >= MIN_TERM_LENGTH and word not in STOPWORDS
    }


def build_vocabulary() -> set[str]:
    """Every content term appearing anywhere in the active corpus."""
    from .models import Chunk

    vocabulary: set[str] = set()
    for content in Chunk.objects.filter(document__is_active=True).values_list(
        "content", flat=True
    ):
        vocabulary |= _content_terms(content)
    return vocabulary


def get_vocabulary() -> set[str]:
    """
    Cached corpus vocabulary.

    Cached rather than recomputed per query, and invalidated by the ingest
    command rather than by a TTL guess — a stale vocabulary would reject
    questions about a document that was just added.
    """
    cached = cache.get(CACHE_KEY)
    if cached is not None:
        return set(cached)

    vocabulary = build_vocabulary()
    try:
        cache.set(CACHE_KEY, list(vocabulary), CACHE_TTL)
    except Exception:  # pragma: no cover - cache outage must not break search
        logger.warning("vocabulary_cache_write_failed")
    return vocabulary


def invalidate_vocabulary() -> None:
    cache.delete(CACHE_KEY)


def out_of_vocabulary_ratio(query: str) -> tuple[float, list[str]]:
    """
    Fraction of the query's content terms absent from the corpus.

    Returns (ratio, the unknown terms) so the caller can log *which* words
    made a question undocumented — useful for spotting genuine corpus gaps
    ("everyone keeps asking about insurance and we have no insurance SOP").
    """
    terms = _content_terms(query)
    if not terms:
        return 0.0, []

    vocabulary = get_vocabulary()
    if not vocabulary:
        # Nothing indexed: the ratio is meaningless, let the caller's
        # empty-corpus handling deal with it.
        return 0.0, []

    unknown = sorted(term for term in terms if term not in vocabulary)
    return len(unknown) / len(terms), unknown
