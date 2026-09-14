"""
Evaluate knowledge-base retrieval against a labelled query set.

    python manage.py calibrate_knowledge

Why this exists: the relevance floor is the single most consequential number
in the RAG path. Set it too high and the assistant says "not covered in the
SOPs" for questions the SOPs answer; set it too low and it confidently cites
an irrelevant policy. Picking it by intuition is guessing, and the failure is
invisible until an operator is misinformed.

So there is a small labelled set — questions paired with the document that
should answer them, plus deliberately off-topic questions that must be
rejected — and this command reports:

  * recall:  on-topic questions whose correct document was retrieved
  * leakage: off-topic questions that were wrongly answered
  * margin:  the gap between the worst on-topic score and the best off-topic
             score. A positive margin means a threshold exists that separates
             them; the command suggests its midpoint.

Re-run it after changing the corpus, the chunker, or the embedding backend.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.knowledge.embeddings import get_embedder
from apps.knowledge.retriever import search, threshold_for

#: (question, acceptable document slugs). Empty tuple = must be rejected.
LABELLED_QUERIES: list[tuple[str, tuple[str, ...]]] = [
    # -- payments -------------------------------------------------------
    ("What does a PENDING payment status mean?", ("payment-status-reference",)),
    (
        "customer says money was debited but we show nothing received",
        ("payment-status-reference",),
    ),
    ("what does INSUFFICIENT_FUNDS mean on a payment", ("payment-status-reference",)),
    (
        "should I ask the customer to pay again if payment is pending",
        ("payment-status-reference",),
    ),
    (
        "order says ready for delivery but money is still due",
        ("payment-status-reference", "order-stuck-triage-sop"),
    ),
    # -- refunds --------------------------------------------------------
    ("How long does a refund take?", ("refund-and-cancellation-policy",)),
    (
        "is the booking amount refundable after RC transfer is filed",
        ("refund-and-cancellation-policy",),
    ),
    ("who is allowed to cancel an order", ("refund-and-cancellation-policy",)),
    # -- RC transfer ----------------------------------------------------
    ("RC transfer was rejected by the RTO, what do I do", ("rc-transfer-sop",)),
    ("how many days does RC transfer usually take", ("rc-transfer-sop",)),
    # -- finance --------------------------------------------------------
    (
        "the loan application was rejected, what options do I give the customer",
        ("finance-rejection-playbook",),
    ),
    (
        "loan has been under review for more than a week",
        ("finance-rejection-playbook", "order-stuck-triage-sop"),
    ),
    # -- documents ------------------------------------------------------
    (
        "PAN name does not match Aadhaar, can we waive it",
        ("document-verification-sop",),
    ),
    (
        "how long does the customer get to upload documents",
        ("document-verification-sop", "order-stuck-triage-sop"),
    ),
    (
        "what are valid reasons to reject a document",
        ("document-verification-sop",),
    ),
    # -- delivery -------------------------------------------------------
    (
        "two delivery attempts have failed, what should happen next",
        ("delivery-escalation-sop",),
    ),
    ("delivery is overdue, who do I escalate to", ("delivery-escalation-sop",)),
    # -- triage ---------------------------------------------------------
    ("when is an order considered stuck", ("order-stuck-triage-sop",)),
    (
        "what order should I work the blockers in",
        ("order-stuck-triage-sop",),
    ),
    # -- must be rejected: plainly unrelated ---------------------------
    ("what is the weather in Mumbai tomorrow", ()),
    ("who won the cricket match last night", ()),
    ("book me a flight to Delhi", ()),
    ("what is the capital of France", ()),
    ("how do I reset my laptop password", ()),
    ("write me a poem about cars", ()),
    # -- must be rejected: adversarial ---------------------------------
    # These are the hard negatives. They use real operational vocabulary
    # ("policy", "hub", "SOP", "escalate", "customer") about topics the
    # corpus does not cover, so a lexical embedder scores them highly on
    # shared words alone. Each one was an actual observed false positive.
    ("what is our policy on flying drones over the hub", ()),
    ("what is the SOP for an earthquake at the hub", ()),
    ("what is the policy on employee parking at the hub", ()),
    ("how do I escalate a cafeteria complaint", ()),
    ("what is our customer policy on cryptocurrency payments", ()),
]


class Command(BaseCommand):
    help = "Measure knowledge-base retrieval quality and suggest a threshold."

    def add_arguments(self, parser):
        parser.add_argument("--top-k", type=int, default=4)
        parser.add_argument(
            "--verbose-misses",
            action="store_true",
            help="Show what was retrieved for each failing query.",
        )

    def handle(self, *args, **options):
        top_k = options["top_k"]
        embedder = get_embedder()
        threshold = threshold_for(embedder.name)

        self.stdout.write(
            f"Backend: {embedder.name} | active threshold: {threshold} | "
            f"top_k: {top_k}\n"
        )

        on_topic_scores: list[float] = []
        off_topic_scores: list[float] = []
        recall_hits = 0
        on_topic_total = 0
        rejected_on_topic: list[str] = []
        wrong_document: list[str] = []
        leaked: list[str] = []
        gated_off_topic = 0

        for question, expected in LABELLED_QUERIES:
            # Scored with the floor removed, so the raw separation is visible.
            # A query stopped by the vocabulary gate carries "unknown_terms"
            # and never reaches scoring at all.
            raw = search(question, top_k=top_k, min_similarity=-1.0)
            gated = "unknown_terms" in raw
            best = raw["results"][0]["similarity"] if raw["results"] else 0.0
            slugs = [item["document_slug"] for item in raw["results"]]
            answered = (not gated) and bool(slugs) and best >= threshold

            if expected:
                on_topic_total += 1
                if not gated:
                    on_topic_scores.append(best)
                found = any(slug in expected for slug in slugs)

                if not answered:
                    why = (
                        f"vocabulary gate: {raw.get('unknown_terms')}"
                        if gated
                        else f"below floor, best {best:.3f}"
                    )
                    rejected_on_topic.append(f"{question}  ({why})")
                elif not found:
                    wrong_document.append(
                        f"{question}  -> {slugs[:2]} (wanted {list(expected)})"
                    )
                else:
                    recall_hits += 1
            else:
                if gated:
                    gated_off_topic += 1
                else:
                    off_topic_scores.append(best)
                if answered:
                    leaked.append(f"{question}  (best {best:.3f} -> {slugs[:1]})")

            if options["verbose_misses"] and expected and not answered:
                for item in raw["results"]:
                    self.stdout.write(
                        f"    {item['similarity']:.3f} {item['document_slug']}"
                        f" / {item['heading']}"
                    )

        worst_on_topic = min(on_topic_scores) if on_topic_scores else 0.0
        best_off_topic = max(off_topic_scores) if off_topic_scores else 0.0
        margin = worst_on_topic - best_off_topic

        self._report_oov_separation()

        self.stdout.write("-" * 66)
        self.stdout.write(
            f"Recall (correct document retrieved and above floor): "
            f"{recall_hits}/{on_topic_total}"
        )
        off_topic_total = len(off_topic_scores) + gated_off_topic
        self.stdout.write(
            f"Off-topic leakage (wrongly answered): "
            f"{len(leaked)}/{off_topic_total}"
        )
        self.stdout.write(
            f"  of which stopped by the vocabulary gate: {gated_off_topic}"
        )
        self.stdout.write("")
        self.stdout.write(f"Worst on-topic score : {worst_on_topic:.4f}")
        self.stdout.write(
            f"Best off-topic score : {best_off_topic:.4f}  "
            f"(scored queries only; gated ones never reach the floor)"
        )
        self.stdout.write(f"Separation margin    : {margin:+.4f}")

        if margin > 0:
            suggested = round((worst_on_topic + best_off_topic) / 2, 3)
            verdict = self.style.SUCCESS(
                f"Clean separation. Suggested threshold: {suggested} "
                f"(currently {threshold})"
            )
        else:
            suggested = None
            verdict = self.style.WARNING(
                "On-topic and off-topic scores overlap — no single threshold "
                "separates them. Improve the corpus, the chunking, or move to "
                "a semantic embedding backend."
            )
        self.stdout.write("")
        self.stdout.write(verdict)

        if rejected_on_topic:
            self.stdout.write(
                self.style.ERROR(
                    f"\nFalse negatives ({len(rejected_on_topic)}) — answerable "
                    f"questions the floor rejected:"
                )
            )
            for line in rejected_on_topic:
                self.stdout.write(f"  - {line}")

        if wrong_document:
            self.stdout.write(
                self.style.WARNING(
                    f"\nRetrieved the wrong document ({len(wrong_document)}):"
                )
            )
            for line in wrong_document:
                self.stdout.write(f"  - {line}")

        if leaked:
            self.stdout.write(
                self.style.ERROR(
                    f"\nOff-topic leakage ({len(leaked)}) — should have been "
                    f"rejected:"
                )
            )
            for line in leaked:
                self.stdout.write(f"  - {line}")

    # ------------------------------------------------------------------
    def _report_oov_separation(self) -> None:
        """
        Calibrate the vocabulary gate, the second of the two thresholds.

        Reported alongside the cosine margin because on a lexical backend the
        gate is what actually rejects adversarial questions — cosine alone
        cannot, since "policy on flying drones over the hub" shares real
        vocabulary with a real policy.
        """
        from django.conf import settings

        from apps.knowledge.vocabulary import out_of_vocabulary_ratio

        current = settings.KNOWLEDGE["MAX_OOV_RATIO"]
        on_topic: list[float] = []
        off_topic: list[float] = []

        for question, expected in LABELLED_QUERIES:
            ratio, _ = out_of_vocabulary_ratio(question)
            (on_topic if expected else off_topic).append(ratio)

        worst_on = max(on_topic) if on_topic else 0.0
        best_off = min(off_topic) if off_topic else 1.0
        gap = best_off - worst_on

        self.stdout.write("")
        self.stdout.write("Vocabulary gate (lexical backends only)")
        self.stdout.write(f"  active MAX_OOV_RATIO   : {current}")
        self.stdout.write(f"  highest on-topic OOV   : {worst_on:.3f}")
        self.stdout.write(f"  lowest off-topic OOV   : {best_off:.3f}")
        self.stdout.write(f"  separation             : {gap:+.3f}")
        if gap > 0:
            self.stdout.write(
                f"  suggested MAX_OOV_RATIO: {round((worst_on + best_off) / 2, 3)}"
            )
        else:
            self.stdout.write(
                self.style.WARNING(
                    "  no separation — expand STOPWORDS, improve stemming, or "
                    "add the missing SOP to the corpus"
                )
            )
