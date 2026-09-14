"""
Seed scenario catalogue.

Each scenario describes a *realistic operational situation* — not random field
values. That distinction matters: an assistant tested against randomly
generated rows looks fine, then falls apart on real data where a failed
payment, a rejected document and an overdue loan review all interact on the
same order.

A scenario is a declarative plan. The materialiser (see the `seed_demo_data`
management command) turns it into rows and, crucially, *derives the order
timeline from the plan's own facts* — so a PAN rejection on day 9 always has
a matching `DOCUMENT_REJECTED` event on day 9. The timeline can never
contradict the payment and document tables.

Two order ids are pinned to specific scenarios because they appear in the
product examples:
    #1243 -> partially paid with a failed retry ("what is the payment status?")
    #2325 -> genuinely stuck, several interacting blockers ("why is it stuck?")
"""

from __future__ import annotations

from dataclasses import dataclass, field

from apps.operations.models import (
    DeliveryStatus,
    DocumentStatus,
    DocumentType,
    FinanceStatus,
    KycStatus,
    OrderStatus,
    PaymentMethod,
    PaymentPurpose,
    PaymentStatus,
    RCTransferStatus,
)

# Order ids quoted in the product brief, wired to hand-built situations so the
# demo queries always have something meaningful to return.
PINNED_SCENARIOS = {
    1243: "payment_partial_with_failure",
    2325: "stuck_multi_blocker",
}


@dataclass
class DocPlan:
    doc_type: str
    status: str
    days_ago: int | None = None
    rejection_reason: str = ""
    is_mandatory: bool = True


@dataclass
class PaymentPlan:
    purpose: str
    fraction: float          # of the order total
    status: str
    method: str
    days_ago: int
    failure_code: str = ""
    failure_reason: str = ""


@dataclass
class DeliveryPlan:
    status: str
    scheduled_in_days: int | None = None   # negative = in the past
    attempts: int = 0
    last_attempt_days_ago: int | None = None
    failure_reason: str = ""
    delivered_days_ago: int | None = None


@dataclass
class FinancePlan:
    status: str
    days_ago: int
    fraction: float = 0.75


@dataclass
class RCPlan:
    status: str
    applied_days_ago: int | None = None


@dataclass
class ExtraEvent:
    days_ago: float
    event_type: str
    description: str
    actor_type: str = "AGENT"


@dataclass
class Scenario:
    """A complete, internally consistent operational situation."""

    name: str
    weight: int
    age_days: int
    status: str
    #: (days_ago, status) transitions in chronological order. The last entry
    #: must match `status`; its timestamp becomes `status_changed_at`.
    status_path: list[tuple[int, str]]
    docs: list[DocPlan] = field(default_factory=list)
    payments: list[PaymentPlan] = field(default_factory=list)
    delivery: DeliveryPlan | None = None
    finance: FinancePlan | None = None
    rc: RCPlan = field(default_factory=lambda: RCPlan(RCTransferStatus.NOT_STARTED))
    expected_delivery_in_days: int | None = None
    hold_reason: str = ""
    cancellation_reason: str = ""
    kyc_status: str = KycStatus.VERIFIED
    extra_events: list[ExtraEvent] = field(default_factory=list)
    #: Human note describing what this scenario is for — printed by the
    #: seeder so you know which order ids exercise which use case.
    intent: str = ""


# ---------------------------------------------------------------------------
# Reusable document bundles
# ---------------------------------------------------------------------------
def _docs_all_verified(days_ago: int) -> list[DocPlan]:
    return [
        DocPlan(DocumentType.AADHAAR, DocumentStatus.VERIFIED, days_ago),
        DocPlan(DocumentType.PAN, DocumentStatus.VERIFIED, days_ago),
        DocPlan(DocumentType.ADDRESS_PROOF, DocumentStatus.VERIFIED, days_ago),
        DocPlan(DocumentType.DRIVING_LICENCE, DocumentStatus.VERIFIED, days_ago),
    ]


def _docs_finance_bundle(days_ago: int) -> list[DocPlan]:
    return [
        *_docs_all_verified(days_ago),
        DocPlan(DocumentType.BANK_STATEMENT, DocumentStatus.VERIFIED, days_ago),
        DocPlan(DocumentType.SALARY_SLIP, DocumentStatus.VERIFIED, days_ago),
    ]


# ---------------------------------------------------------------------------
# The catalogue
# ---------------------------------------------------------------------------
SCENARIOS: list[Scenario] = [
    # -- healthy / completed -------------------------------------------------
    Scenario(
        name="delivered_self_funded",
        weight=16,
        age_days=42,
        status=OrderStatus.DELIVERED,
        status_path=[
            (42, OrderStatus.CREATED),
            (41, OrderStatus.DOCS_PENDING),
            (38, OrderStatus.PAYMENT_PENDING),
            (35, OrderStatus.PAYMENT_COMPLETE),
            (33, OrderStatus.RC_TRANSFER_PENDING),
            (24, OrderStatus.READY_FOR_DELIVERY),
            (22, OrderStatus.OUT_FOR_DELIVERY),
            (21, OrderStatus.DELIVERED),
        ],
        docs=_docs_all_verified(39),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 40),
            PaymentPlan(PaymentPurpose.FULL_PAYMENT, 0.96, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 35),
        ],
        delivery=DeliveryPlan(
            DeliveryStatus.DELIVERED,
            scheduled_in_days=-22,
            attempts=1,
            last_attempt_days_ago=21,
            delivered_days_ago=21,
        ),
        rc=RCPlan(RCTransferStatus.COMPLETED, applied_days_ago=33),
        expected_delivery_in_days=-22,
        intent="Happy path, self funded. Baseline for 'complete summary'.",
    ),
    Scenario(
        name="delivered_financed",
        weight=10,
        age_days=48,
        status=OrderStatus.DELIVERED,
        status_path=[
            (48, OrderStatus.CREATED),
            (47, OrderStatus.DOCS_PENDING),
            (44, OrderStatus.FINANCE_IN_PROGRESS),
            (38, OrderStatus.PAYMENT_COMPLETE),
            (36, OrderStatus.RC_TRANSFER_PENDING),
            (27, OrderStatus.READY_FOR_DELIVERY),
            (25, OrderStatus.OUT_FOR_DELIVERY),
            (24, OrderStatus.DELIVERED),
        ],
        docs=_docs_finance_bundle(45),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 46),
            PaymentPlan(PaymentPurpose.DOWN_PAYMENT, 0.21, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 42),
            PaymentPlan(PaymentPurpose.LOAN_DISBURSEMENT, 0.75, PaymentStatus.SUCCESS, PaymentMethod.LOAN_DISBURSEMENT, 38),
        ],
        finance=FinancePlan(FinanceStatus.DISBURSED, days_ago=38),
        delivery=DeliveryPlan(
            DeliveryStatus.DELIVERED,
            scheduled_in_days=-25,
            attempts=1,
            last_attempt_days_ago=24,
            delivered_days_ago=24,
        ),
        rc=RCPlan(RCTransferStatus.COMPLETED, applied_days_ago=36),
        expected_delivery_in_days=-25,
        intent="Happy path with a loan. Exercises finance fields in summaries.",
    ),
    Scenario(
        name="created_fresh",
        weight=6,
        age_days=1,
        status=OrderStatus.CREATED,
        status_path=[(1, OrderStatus.CREATED)],
        docs=[
            DocPlan(DocumentType.AADHAAR, DocumentStatus.NOT_UPLOADED),
            DocPlan(DocumentType.PAN, DocumentStatus.NOT_UPLOADED),
            DocPlan(DocumentType.ADDRESS_PROOF, DocumentStatus.NOT_UPLOADED),
        ],
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 1),
        ],
        kyc_status=KycStatus.PENDING,
        expected_delivery_in_days=12,
        intent="Brand new order — must NOT be reported as stuck.",
    ),
    Scenario(
        name="docs_pending_fresh",
        weight=8,
        age_days=2,
        status=OrderStatus.DOCS_PENDING,
        status_path=[(2, OrderStatus.CREATED), (2, OrderStatus.DOCS_PENDING)],
        docs=[
            DocPlan(DocumentType.AADHAAR, DocumentStatus.VERIFIED, 1),
            DocPlan(DocumentType.PAN, DocumentStatus.UPLOADED, 1),
            DocPlan(DocumentType.ADDRESS_PROOF, DocumentStatus.NOT_UPLOADED),
        ],
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 2),
        ],
        expected_delivery_in_days=11,
        intent="Within SLA — tests that the rule engine does not cry wolf.",
    ),
    Scenario(
        name="out_for_delivery",
        weight=6,
        age_days=18,
        status=OrderStatus.OUT_FOR_DELIVERY,
        status_path=[
            (18, OrderStatus.CREATED),
            (17, OrderStatus.DOCS_PENDING),
            (15, OrderStatus.PAYMENT_COMPLETE),
            (13, OrderStatus.RC_TRANSFER_PENDING),
            (2, OrderStatus.READY_FOR_DELIVERY),
            (0, OrderStatus.OUT_FOR_DELIVERY),
        ],
        docs=_docs_all_verified(16),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 17),
            PaymentPlan(PaymentPurpose.FULL_PAYMENT, 0.96, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 15),
        ],
        delivery=DeliveryPlan(
            DeliveryStatus.OUT_FOR_DELIVERY, scheduled_in_days=0, attempts=0
        ),
        rc=RCPlan(RCTransferStatus.COMPLETED, applied_days_ago=13),
        expected_delivery_in_days=0,
        intent="In flight today — 'where is my car' style query.",
    ),

    # -- payment problems ----------------------------------------------------
    Scenario(
        name="payment_partial_with_failure",
        weight=6,
        age_days=9,
        status=OrderStatus.PAYMENT_PARTIAL,
        status_path=[
            (9, OrderStatus.CREATED),
            (8, OrderStatus.DOCS_PENDING),
            (7, OrderStatus.PAYMENT_PENDING),
            (6, OrderStatus.PAYMENT_PARTIAL),
        ],
        docs=_docs_all_verified(7),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 8),
            PaymentPlan(PaymentPurpose.DOWN_PAYMENT, 0.20, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 6),
            PaymentPlan(
                PaymentPurpose.FULL_PAYMENT, 0.76, PaymentStatus.FAILED,
                PaymentMethod.NETBANKING, 3,
                failure_code="INSUFFICIENT_FUNDS",
                failure_reason="Insufficient balance in the debit account",
            ),
        ],
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        expected_delivery_in_days=5,
        extra_events=[
            ExtraEvent(2, "CUSTOMER_CONTACTED", "Called customer about the failed payment; asked to retry after salary credit."),
        ],
        intent="PINNED to #1243 — 'what is the payment status of order #1243?'",
    ),
    Scenario(
        name="payment_in_flight_stuck",
        weight=4,
        age_days=6,
        status=OrderStatus.PAYMENT_PENDING,
        status_path=[
            (6, OrderStatus.CREATED),
            (5, OrderStatus.DOCS_PENDING),
            (4, OrderStatus.PAYMENT_PENDING),
        ],
        docs=_docs_all_verified(5),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 5),
            PaymentPlan(PaymentPurpose.FULL_PAYMENT, 0.96, PaymentStatus.PENDING, PaymentMethod.UPI, 3),
        ],
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        expected_delivery_in_days=6,
        intent="Gateway never returned a final status — money may be debited.",
    ),
    Scenario(
        name="payment_pending_no_attempt",
        weight=6,
        age_days=7,
        status=OrderStatus.PAYMENT_PENDING,
        status_path=[
            (7, OrderStatus.CREATED),
            (6, OrderStatus.DOCS_PENDING),
            (5, OrderStatus.PAYMENT_PENDING),
        ],
        docs=_docs_all_verified(6),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 7),
        ],
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        expected_delivery_in_days=4,
        extra_events=[
            ExtraEvent(4, "PAYMENT_LINK_SENT", "Balance payment link shared on WhatsApp."),
        ],
        intent="Balance never attempted — SLA breach on payment follow-up.",
    ),

    # -- the flagship 'stuck' case ------------------------------------------
    Scenario(
        name="stuck_multi_blocker",
        weight=4,
        age_days=26,
        status=OrderStatus.FINANCE_IN_PROGRESS,
        status_path=[
            (26, OrderStatus.CREATED),
            (25, OrderStatus.DOCS_PENDING),
            (22, OrderStatus.FINANCE_IN_PROGRESS),
        ],
        docs=[
            DocPlan(DocumentType.AADHAAR, DocumentStatus.VERIFIED, 23),
            DocPlan(
                DocumentType.PAN, DocumentStatus.REJECTED, 9,
                rejection_reason="Name on PAN does not match Aadhaar records",
            ),
            DocPlan(DocumentType.ADDRESS_PROOF, DocumentStatus.VERIFIED, 23),
            DocPlan(DocumentType.BANK_STATEMENT, DocumentStatus.UPLOADED, 9),
            DocPlan(DocumentType.SALARY_SLIP, DocumentStatus.NOT_UPLOADED),
        ],
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 24),
        ],
        finance=FinancePlan(FinanceStatus.UNDER_REVIEW, days_ago=11),
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        expected_delivery_in_days=-6,
        extra_events=[
            ExtraEvent(11, "FINANCE_FOLLOWUP", "Chased lender for a credit decision; no response yet."),
        ],
        intent=(
            "PINNED to #2325 — 'why is order #2325 stuck?'. Deliberately has "
            "several interacting blockers: rejected PAN, a missing salary slip, "
            "verification past SLA, a loan review past SLA, an outstanding "
            "balance, an overdue promised date, and no activity for 9 days."
        ),
    ),

    # -- finance problems ----------------------------------------------------
    Scenario(
        name="finance_rejected",
        weight=5,
        age_days=16,
        status=OrderStatus.FINANCE_IN_PROGRESS,
        status_path=[
            (16, OrderStatus.CREATED),
            (15, OrderStatus.DOCS_PENDING),
            (12, OrderStatus.FINANCE_IN_PROGRESS),
        ],
        docs=_docs_finance_bundle(13),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 15),
        ],
        finance=FinancePlan(FinanceStatus.REJECTED, days_ago=4),
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        expected_delivery_in_days=2,
        extra_events=[
            ExtraEvent(3, "CUSTOMER_CONTACTED", "Informed customer of loan rejection; discussing alternate lender."),
        ],
        intent="Loan rejected — critical blocker, order cannot proceed as funded.",
    ),

    # -- RC transfer problems ------------------------------------------------
    Scenario(
        name="rc_transfer_overdue",
        weight=5,
        age_days=44,
        status=OrderStatus.RC_TRANSFER_PENDING,
        status_path=[
            (44, OrderStatus.CREATED),
            (43, OrderStatus.DOCS_PENDING),
            (40, OrderStatus.PAYMENT_COMPLETE),
            (38, OrderStatus.RC_TRANSFER_PENDING),
        ],
        docs=_docs_all_verified(41),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 43),
            PaymentPlan(PaymentPurpose.FULL_PAYMENT, 0.96, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 40),
        ],
        rc=RCPlan(RCTransferStatus.IN_PROGRESS, applied_days_ago=37),
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        expected_delivery_in_days=-9,
        intent="Paid in full but RTO is sitting on the transfer past SLA.",
    ),
    Scenario(
        name="rc_transfer_rejected",
        weight=3,
        age_days=35,
        status=OrderStatus.RC_TRANSFER_PENDING,
        status_path=[
            (35, OrderStatus.CREATED),
            (34, OrderStatus.DOCS_PENDING),
            (31, OrderStatus.PAYMENT_COMPLETE),
            (29, OrderStatus.RC_TRANSFER_PENDING),
        ],
        docs=_docs_all_verified(32),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 34),
            PaymentPlan(PaymentPurpose.FULL_PAYMENT, 0.96, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 31),
        ],
        rc=RCPlan(RCTransferStatus.REJECTED, applied_days_ago=27),
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        expected_delivery_in_days=-5,
        extra_events=[
            ExtraEvent(6, "RTO_REJECTION", "RTO rejected Form 30 — seller signature mismatch.", "PARTNER"),
        ],
        intent="RTO rejection — needs a re-filing, not a customer follow-up.",
    ),

    # -- delivery problems ---------------------------------------------------
    Scenario(
        name="delivery_attempts_failed",
        weight=5,
        age_days=25,
        status=OrderStatus.OUT_FOR_DELIVERY,
        status_path=[
            (25, OrderStatus.CREATED),
            (24, OrderStatus.DOCS_PENDING),
            (21, OrderStatus.PAYMENT_COMPLETE),
            (19, OrderStatus.RC_TRANSFER_PENDING),
            (9, OrderStatus.READY_FOR_DELIVERY),
            (6, OrderStatus.OUT_FOR_DELIVERY),
        ],
        docs=_docs_all_verified(22),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 24),
            PaymentPlan(PaymentPurpose.FULL_PAYMENT, 0.96, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 21),
        ],
        rc=RCPlan(RCTransferStatus.COMPLETED, applied_days_ago=19),
        delivery=DeliveryPlan(
            DeliveryStatus.ATTEMPT_FAILED,
            scheduled_in_days=-6,
            attempts=3,
            last_attempt_days_ago=2,
            failure_reason="Customer unreachable at the delivery address",
        ),
        expected_delivery_in_days=-5,
        intent="Three failed runs — costs money, needs a call not another dispatch.",
    ),
    Scenario(
        name="ready_no_slot_booked",
        weight=5,
        age_days=20,
        status=OrderStatus.READY_FOR_DELIVERY,
        status_path=[
            (20, OrderStatus.CREATED),
            (19, OrderStatus.DOCS_PENDING),
            (16, OrderStatus.PAYMENT_COMPLETE),
            (14, OrderStatus.RC_TRANSFER_PENDING),
            (6, OrderStatus.READY_FOR_DELIVERY),
        ],
        docs=_docs_all_verified(17),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 19),
            PaymentPlan(PaymentPurpose.FULL_PAYMENT, 0.96, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 16),
        ],
        rc=RCPlan(RCTransferStatus.COMPLETED, applied_days_ago=14),
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        expected_delivery_in_days=-1,
        intent="Everything done, nobody booked the slot. Pure process gap.",
    ),

    # -- holds, cancellations, refunds --------------------------------------
    Scenario(
        name="on_hold_dispute",
        weight=4,
        age_days=30,
        status=OrderStatus.ON_HOLD,
        status_path=[
            (30, OrderStatus.CREATED),
            (29, OrderStatus.DOCS_PENDING),
            (26, OrderStatus.PAYMENT_PARTIAL),
            (12, OrderStatus.ON_HOLD),
        ],
        docs=_docs_all_verified(27),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 29),
            PaymentPlan(PaymentPurpose.DOWN_PAYMENT, 0.30, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 26),
        ],
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        hold_reason="Customer raised an inspection dispute — rear bumper repaint not disclosed",
        expected_delivery_in_days=-8,
        extra_events=[
            ExtraEvent(12, "HOLD_APPLIED", "Order held pending re-inspection of reported repaint."),
            ExtraEvent(10, "REINSPECTION_REQUESTED", "Re-inspection requested from the hub QC team."),
        ],
        intent="Explicit hold with a reason — highest severity blocker.",
    ),
    Scenario(
        name="cancelled_refund_pending",
        weight=4,
        age_days=28,
        status=OrderStatus.REFUND_PENDING,
        status_path=[
            (28, OrderStatus.CREATED),
            (27, OrderStatus.DOCS_PENDING),
            (24, OrderStatus.PAYMENT_PARTIAL),
            (14, OrderStatus.CANCELLED),
            (13, OrderStatus.REFUND_PENDING),
        ],
        docs=_docs_all_verified(25),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 27),
            PaymentPlan(PaymentPurpose.DOWN_PAYMENT, 0.15, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 24),
        ],
        delivery=DeliveryPlan(DeliveryStatus.CANCELLED),
        cancellation_reason="Customer chose a different vehicle",
        expected_delivery_in_days=-10,
        intent="Refund past the 7-day SLA — money owed to the customer.",
    ),
    Scenario(
        name="refunded_complete",
        weight=3,
        age_days=50,
        status=OrderStatus.REFUNDED,
        status_path=[
            (50, OrderStatus.CREATED),
            (49, OrderStatus.DOCS_PENDING),
            (46, OrderStatus.PAYMENT_PARTIAL),
            (40, OrderStatus.CANCELLED),
            (39, OrderStatus.REFUND_PENDING),
            (35, OrderStatus.REFUNDED),
        ],
        docs=_docs_all_verified(47),
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 49),
            PaymentPlan(PaymentPurpose.REFUND, 0.04, PaymentStatus.SUCCESS, PaymentMethod.NETBANKING, 35),
        ],
        delivery=DeliveryPlan(DeliveryStatus.CANCELLED),
        cancellation_reason="Loan rejected, customer opted out",
        expected_delivery_in_days=-38,
        intent="Closed cleanly — must NOT be reported as stuck.",
    ),
    Scenario(
        name="kyc_rejected",
        weight=3,
        age_days=12,
        status=OrderStatus.DOCS_PENDING,
        status_path=[
            (12, OrderStatus.CREATED),
            (11, OrderStatus.DOCS_PENDING),
        ],
        docs=[
            DocPlan(DocumentType.AADHAAR, DocumentStatus.REJECTED, 6,
                    rejection_reason="Aadhaar image blurred, number not readable"),
            DocPlan(DocumentType.PAN, DocumentStatus.VERIFIED, 9),
            DocPlan(DocumentType.ADDRESS_PROOF, DocumentStatus.UPLOADED, 6),
        ],
        payments=[
            PaymentPlan(PaymentPurpose.TOKEN, 0.04, PaymentStatus.SUCCESS, PaymentMethod.UPI, 11),
        ],
        delivery=DeliveryPlan(DeliveryStatus.NOT_SCHEDULED),
        kyc_status=KycStatus.REJECTED,
        expected_delivery_in_days=1,
        intent="Compliance blocker — KYC rejected, delivery impossible.",
    ),
]

SCENARIOS_BY_NAME = {s.name: s for s in SCENARIOS}
