"""
Deterministic order-blocker diagnosis.

This module answers "why is order #2325 stuck?" **in Python, not in the LLM**.

Why it matters: if you hand a model raw order rows and ask it to infer the
blocker, it will produce a plausible narrative that is sometimes wrong — and
wrong root-cause analysis is worse than no answer, because an operator will
act on it. So every conclusion here is a rule with named evidence, a severity,
an owning team and a suggested next action. The model's only job is to render
these findings as readable prose.

Adding a new failure mode means adding a rule here, which is also where it
becomes testable.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from django.conf import settings
from django.utils import timezone

from .formatting import humanize_age, iso, money
from .models import (
    DeliveryStatus,
    DocumentStatus,
    FinanceStatus,
    Order,
    OrderStatus,
    PaymentPurpose,
    PaymentStatus,
    RCTransferStatus,
)

SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}

#: Causal ordering, most upstream first.
#:
#: Severity alone is not enough to pick a *primary* blocker. An order with a
#: rejected PAN will also be overdue for delivery — but "delivery is overdue"
#: is a consequence, and leading an operator with it sends them to the wrong
#: team. Within the same severity band, the more upstream cause wins.
CAUSE_RANK = {
    "ORDER_ON_HOLD": 0,
    "KYC_REJECTED": 1,
    "KYC_PENDING": 2,
    "DOCUMENTS_REJECTED": 3,
    "DOCUMENTS_MISSING": 4,
    "FINANCE_REJECTED": 5,
    "FINANCE_REVIEW_OVERDUE": 6,
    "DOCUMENT_VERIFICATION_OVERDUE": 7,
    "PAYMENT_FAILED_NOT_RETRIED": 8,
    "PAYMENT_STUCK_IN_FLIGHT": 9,
    "PAYMENT_SHORTFALL_VS_STATUS": 10,
    "PAYMENT_OUTSTANDING": 11,
    "RC_TRANSFER_REJECTED": 12,
    "RC_TRANSFER_NOT_INITIATED": 13,
    "RC_TRANSFER_OVERDUE": 14,
    "DELIVERY_ATTEMPTS_EXHAUSTED": 15,
    "DELIVERY_NOT_SCHEDULED": 16,
    "REFUND_PENDING": 17,
    # Symptoms — real findings, but never the thing to act on first.
    "DELIVERY_OVERDUE": 90,
    "NO_RECENT_ACTIVITY": 91,
}

#: Findings that describe an effect rather than a cause. Reported, but never
#: chosen as the primary blocker when a genuine cause is also present.
SYMPTOM_CODES = frozenset({"DELIVERY_OVERDUE", "NO_RECENT_ACTIVITY"})

#: Statuses at or beyond which the vehicle amount is expected to be settled.
_POST_PAYMENT_STATUSES = frozenset(
    {
        OrderStatus.PAYMENT_COMPLETE,
        OrderStatus.RC_TRANSFER_PENDING,
        OrderStatus.READY_FOR_DELIVERY,
        OrderStatus.OUT_FOR_DELIVERY,
        OrderStatus.DELIVERED,
    }
)


@dataclass
class Blocker:
    code: str
    severity: str
    title: str
    detail: str
    owner_team: str
    suggested_action: str
    evidence: dict = field(default_factory=dict)
    sla_breached: bool = False

    @property
    def is_symptom(self) -> bool:
        return self.code in SYMPTOM_CODES

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (
            SEVERITY_ORDER.get(self.severity, 9),
            CAUSE_RANK.get(self.code, 50),
            self.code,
        )

    def to_dict(self) -> dict:
        data = asdict(self)
        data["is_symptom"] = self.is_symptom
        return data


def _sla(key: str) -> int:
    return settings.OPS_SLA[key]


# ---------------------------------------------------------------------------
# Individual rules. Each returns a Blocker or None.
# ---------------------------------------------------------------------------
def _rule_on_hold(order: Order) -> Blocker | None:
    if order.status != OrderStatus.ON_HOLD:
        return None
    return Blocker(
        code="ORDER_ON_HOLD",
        severity="critical",
        title="Order is explicitly on hold",
        detail=(
            f"The order was put on hold {humanize_age(order.status_changed_at)}"
            + (f": {order.hold_reason}" if order.hold_reason else " with no reason recorded.")
        ),
        owner_team="Operations",
        suggested_action=(
            "Review the hold reason and release the hold, or confirm with the "
            "customer that the order should be cancelled."
        ),
        evidence={
            "hold_reason": order.hold_reason or None,
            "on_hold_since": iso(order.status_changed_at),
            "days_on_hold": order.days_in_current_status,
        },
        sla_breached=order.days_in_current_status > _sla("STALE_EVENT_DAYS"),
    )


def _rule_refund_pending(order: Order) -> Blocker | None:
    if order.status != OrderStatus.REFUND_PENDING:
        return None
    days = order.days_in_current_status
    breached = days > _sla("REFUND_DAYS")
    return Blocker(
        code="REFUND_PENDING",
        severity="high" if breached else "medium",
        title="Refund is pending",
        detail=(
            f"The order was cancelled and a refund of "
            f"{money(order.settled_amount - order.refunded_amount)['display']} "
            f"has been pending for {days} day(s)."
        ),
        owner_team="Finance",
        suggested_action=(
            "Confirm the refund has been raised with the payment gateway and "
            "share the expected credit date with the customer."
        ),
        evidence={
            "pending_since": iso(order.status_changed_at),
            "days_pending": days,
            "sla_days": _sla("REFUND_DAYS"),
            "amount_collected": money(order.settled_amount),
            "amount_refunded": money(order.refunded_amount),
        },
        sla_breached=breached,
    )


def _rule_documents(order: Order) -> list[Blocker]:
    blockers: list[Blocker] = []
    docs = list(order.documents.all())

    rejected = [d for d in docs if d.status == DocumentStatus.REJECTED]
    if rejected:
        blockers.append(
            Blocker(
                code="DOCUMENTS_REJECTED",
                severity="high",
                title=f"{len(rejected)} document(s) were rejected",
                detail=(
                    "Rejected: "
                    + "; ".join(
                        f"{d.doc_type} ({d.rejection_reason or 'no reason recorded'})"
                        for d in rejected
                    )
                ),
                owner_team="Documentation",
                suggested_action=(
                    "Contact the customer to re-upload the rejected documents. "
                    "The order cannot progress until they are verified."
                ),
                evidence={
                    "rejected_documents": [
                        {
                            "doc_type": d.doc_type,
                            "rejection_reason": d.rejection_reason or None,
                            "updated_at": iso(d.updated_at),
                        }
                        for d in rejected
                    ]
                },
                sla_breached=True,
            )
        )

    missing = [
        d
        for d in docs
        if d.is_mandatory and d.status == DocumentStatus.NOT_UPLOADED
    ]
    if missing:
        # Age-graded. A one-day-old order with nothing uploaded is the normal
        # state of the world, not a blocked order — reporting it as "high"
        # trains operators to ignore the diagnosis entirely.
        waiting_days = order.age_days
        collection_sla = _sla("DOCS_COLLECTION_DAYS")
        overdue = waiting_days > collection_sla
        names = ", ".join(sorted(d.doc_type for d in missing))
        blockers.append(
            Blocker(
                code="DOCUMENTS_MISSING",
                severity="high" if overdue else "low",
                title=(
                    f"{len(missing)} mandatory document(s) not uploaded"
                    if overdue
                    else f"{len(missing)} document(s) still awaited from the customer"
                ),
                detail=(
                    f"Awaiting: {names}. Outstanding for {waiting_days} day(s), "
                    f"beyond the {collection_sla}-day collection window."
                    if overdue
                    else (
                        f"Awaiting: {names}. The order is {waiting_days} day(s) old, "
                        f"still inside the {collection_sla}-day collection window — "
                        f"normal at this stage."
                    )
                ),
                owner_team="Documentation",
                suggested_action=(
                    "Send the customer the pending-document list and a re-upload link."
                    if overdue
                    else "No action needed yet; follow up if not uploaded soon."
                ),
                evidence={
                    "missing_documents": sorted(d.doc_type for d in missing),
                    "order_age_days": waiting_days,
                    "collection_sla_days": collection_sla,
                    "within_sla": not overdue,
                },
                sla_breached=overdue,
            )
        )

    stale_verification = [
        d
        for d in docs
        if d.status == DocumentStatus.UPLOADED
        and d.uploaded_at
        and (timezone.now() - d.uploaded_at).days > _sla("DOCS_VERIFICATION_DAYS")
    ]
    if stale_verification:
        blockers.append(
            Blocker(
                code="DOCUMENT_VERIFICATION_OVERDUE",
                severity="medium",
                title="Uploaded documents are awaiting verification beyond SLA",
                detail=(
                    f"{len(stale_verification)} document(s) have been waiting for "
                    f"internal verification longer than the "
                    f"{_sla('DOCS_VERIFICATION_DAYS')}-day SLA."
                ),
                owner_team="Documentation",
                suggested_action="Escalate to the document verification queue owner.",
                evidence={
                    "documents": [
                        {
                            "doc_type": d.doc_type,
                            "uploaded_at": iso(d.uploaded_at),
                            "waiting_days": (timezone.now() - d.uploaded_at).days,
                        }
                        for d in stale_verification
                    ],
                    "sla_days": _sla("DOCS_VERIFICATION_DAYS"),
                },
                sla_breached=True,
            )
        )

    return blockers


def _rule_payments(order: Order) -> list[Blocker]:
    blockers: list[Blocker] = []
    payments = sorted(order.payments.all(), key=lambda p: p.initiated_at)

    settled = [
        p
        for p in payments
        if p.status == PaymentStatus.SUCCESS and p.purpose != PaymentPurpose.REFUND
    ]
    failed = [p for p in payments if p.status == PaymentStatus.FAILED]
    in_flight = [
        p
        for p in payments
        if p.status
        in (PaymentStatus.INITIATED, PaymentStatus.PENDING, PaymentStatus.AUTHORIZED)
    ]

    # A failed attempt only matters if nothing succeeded afterwards.
    if failed:
        last_failure = failed[-1]
        later_success = any(
            p.initiated_at > last_failure.initiated_at for p in settled
        )
        if not later_success:
            blockers.append(
                Blocker(
                    code="PAYMENT_FAILED_NOT_RETRIED",
                    severity="high",
                    title="Latest payment attempt failed and was not retried",
                    detail=(
                        f"Payment {last_failure.reference} for "
                        f"{money(last_failure.amount)['display']} failed "
                        f"{humanize_age(last_failure.initiated_at)}"
                        + (
                            f" — {last_failure.failure_reason}."
                            if last_failure.failure_reason
                            else " with no reason recorded."
                        )
                    ),
                    owner_team="Payments",
                    suggested_action=(
                        "Share a fresh payment link with the customer and confirm "
                        "the failure reason has been resolved (e.g. limit, mandate)."
                    ),
                    evidence={
                        "reference": last_failure.reference,
                        "amount": money(last_failure.amount),
                        "method": last_failure.method,
                        "failure_code": last_failure.failure_code or None,
                        "failure_reason": last_failure.failure_reason or None,
                        "failed_at": iso(last_failure.initiated_at),
                        "failed_attempt_count": len(failed),
                    },
                    sla_breached=True,
                )
            )

    if in_flight:
        oldest = in_flight[0]
        hours = (timezone.now() - oldest.initiated_at).total_seconds() / 3600
        if hours > 24:
            blockers.append(
                Blocker(
                    code="PAYMENT_STUCK_IN_FLIGHT",
                    severity="medium",
                    title="Payment is stuck awaiting bank confirmation",
                    detail=(
                        f"Payment {oldest.reference} has been in "
                        f"'{oldest.status}' for {int(hours)} hours without a "
                        f"final status from the gateway."
                    ),
                    owner_team="Payments",
                    suggested_action=(
                        "Run a gateway status reconciliation for this reference "
                        "before asking the customer to pay again — the money may "
                        "already be debited."
                    ),
                    evidence={
                        "reference": oldest.reference,
                        "status": oldest.status,
                        "gateway": oldest.gateway or None,
                        "gateway_txn_id": oldest.gateway_txn_id or None,
                        "initiated_at": iso(oldest.initiated_at),
                        "hours_in_flight": int(hours),
                    },
                    sla_breached=True,
                )
            )

    due = order.amount_due
    if due > Decimal("0") and order.status in _POST_PAYMENT_STATUSES:
        # Status says paid, ledger disagrees — a genuine data/process conflict.
        blockers.append(
            Blocker(
                code="PAYMENT_SHORTFALL_VS_STATUS",
                severity="critical",
                title="Order status assumes full payment but money is still due",
                detail=(
                    f"Status is '{order.status}' yet {money(due)['display']} of "
                    f"{money(order.total_amount)['display']} remains unsettled."
                ),
                owner_team="Finance",
                suggested_action=(
                    "Reconcile the payment ledger against the order status. Do not "
                    "release the vehicle until the shortfall is explained."
                ),
                evidence={
                    "order_status": order.status,
                    "total_amount": money(order.total_amount),
                    "amount_paid": money(order.settled_amount),
                    "amount_due": money(due),
                },
                sla_breached=True,
            )
        )
    elif due > Decimal("0") and order.status in (
        OrderStatus.PAYMENT_PENDING,
        OrderStatus.PAYMENT_PARTIAL,
        OrderStatus.DOCS_PENDING,
        OrderStatus.CREATED,
    ):
        days = order.days_in_current_status
        breached = days > _sla("PAYMENT_PENDING_DAYS")
        blockers.append(
            Blocker(
                code="PAYMENT_OUTSTANDING",
                severity="high" if breached else "medium",
                title="Payment is outstanding",
                detail=(
                    f"{money(due)['display']} of "
                    f"{money(order.total_amount)['display']} is still to be "
                    f"collected; the order has been in '{order.status}' for "
                    f"{days} day(s)."
                ),
                owner_team="Payments",
                suggested_action=(
                    "Follow up with the customer on the outstanding amount, or "
                    "check whether finance disbursement is expected to cover it."
                ),
                evidence={
                    "amount_due": money(due),
                    "amount_paid": money(order.settled_amount),
                    "days_in_status": days,
                    "sla_days": _sla("PAYMENT_PENDING_DAYS"),
                    "settled_payment_count": len(settled),
                },
                sla_breached=breached,
            )
        )

    return blockers


def _rule_finance(order: Order) -> Blocker | None:
    if order.finance_status == FinanceStatus.REJECTED:
        return Blocker(
            code="FINANCE_REJECTED",
            severity="critical",
            title="Loan application was rejected",
            detail=(
                f"{order.finance_partner or 'The finance partner'} rejected the "
                f"loan application"
                f"{' ' + humanize_age(order.finance_updated_at) if order.finance_updated_at else ''}."
                " The order cannot proceed on the original funding plan."
            ),
            owner_team="Finance",
            suggested_action=(
                "Offer the customer an alternative lender or a self-funded plan, "
                "or begin cancellation and refund of the token amount."
            ),
            evidence={
                "finance_partner": order.finance_partner or None,
                "loan_amount": money(order.loan_amount),
                "updated_at": iso(order.finance_updated_at),
            },
            sla_breached=True,
        )

    if order.finance_status == FinanceStatus.UNDER_REVIEW and order.finance_updated_at:
        days = (timezone.now() - order.finance_updated_at).days
        if days > _sla("FINANCE_REVIEW_DAYS"):
            return Blocker(
                code="FINANCE_REVIEW_OVERDUE",
                severity="high",
                title="Loan application stuck in review",
                detail=(
                    f"The application has been under review with "
                    f"{order.finance_partner or 'the finance partner'} for "
                    f"{days} day(s), beyond the "
                    f"{_sla('FINANCE_REVIEW_DAYS')}-day SLA."
                ),
                owner_team="Finance",
                suggested_action=(
                    "Escalate to the finance partner's relationship manager for a decision."
                ),
                evidence={
                    "finance_partner": order.finance_partner or None,
                    "days_under_review": days,
                    "sla_days": _sla("FINANCE_REVIEW_DAYS"),
                    "updated_at": iso(order.finance_updated_at),
                },
                sla_breached=True,
            )
    return None


def _rule_rc_transfer(order: Order) -> Blocker | None:
    vehicle = order.vehicle

    if vehicle.rc_transfer_status == RCTransferStatus.REJECTED:
        return Blocker(
            code="RC_TRANSFER_REJECTED",
            severity="high",
            title="RC transfer was rejected by the RTO",
            detail=(
                f"The RTO rejected the ownership transfer for "
                f"{vehicle.registration_number}."
            ),
            owner_team="RTO / Documentation",
            suggested_action=(
                "Pull the RTO rejection memo, correct the filing and re-apply."
            ),
            evidence={
                "registration_number": vehicle.registration_number,
                "applied_on": iso(vehicle.rc_transfer_applied_on),
            },
            sla_breached=True,
        )

    if (
        vehicle.rc_transfer_status in (RCTransferStatus.APPLIED, RCTransferStatus.IN_PROGRESS)
        and vehicle.rc_transfer_applied_on
    ):
        days = (timezone.localdate() - vehicle.rc_transfer_applied_on).days
        if days > _sla("RC_TRANSFER_DAYS"):
            return Blocker(
                code="RC_TRANSFER_OVERDUE",
                severity="medium",
                title="RC transfer is taking longer than SLA",
                detail=(
                    f"RC transfer for {vehicle.registration_number} was applied "
                    f"{days} days ago, beyond the {_sla('RC_TRANSFER_DAYS')}-day SLA."
                ),
                owner_team="RTO / Documentation",
                suggested_action=(
                    "Follow up with the RTO agent and give the customer a revised date."
                ),
                evidence={
                    "registration_number": vehicle.registration_number,
                    "rc_transfer_status": vehicle.rc_transfer_status,
                    "applied_on": iso(vehicle.rc_transfer_applied_on),
                    "days_elapsed": days,
                    "sla_days": _sla("RC_TRANSFER_DAYS"),
                },
                sla_breached=True,
            )

    if (
        order.status in (OrderStatus.RC_TRANSFER_PENDING,)
        and vehicle.rc_transfer_status == RCTransferStatus.NOT_STARTED
    ):
        return Blocker(
            code="RC_TRANSFER_NOT_INITIATED",
            severity="high",
            title="Order is waiting on an RC transfer that was never started",
            detail=(
                f"The order status is 'RC transfer pending' but no transfer has "
                f"been filed for {vehicle.registration_number}."
            ),
            owner_team="RTO / Documentation",
            suggested_action="File the RC transfer application with the RTO.",
            evidence={
                "registration_number": vehicle.registration_number,
                "rc_transfer_status": vehicle.rc_transfer_status,
                "days_in_status": order.days_in_current_status,
            },
            sla_breached=order.days_in_current_status > _sla("STALE_EVENT_DAYS"),
        )

    return None


def _rule_delivery(order: Order) -> list[Blocker]:
    blockers: list[Blocker] = []
    delivery = getattr(order, "delivery", None)
    if delivery is None:
        return blockers

    limit = _sla("DELIVERY_ATTEMPT_LIMIT")
    if delivery.attempts >= limit and delivery.status != DeliveryStatus.DELIVERED:
        blockers.append(
            Blocker(
                code="DELIVERY_ATTEMPTS_EXHAUSTED",
                severity="high",
                title=f"{delivery.attempts} delivery attempts have failed",
                detail=(
                    f"The last attempt was {humanize_age(delivery.last_attempt_at)}"
                    + (
                        f" and failed: {delivery.failure_reason}."
                        if delivery.failure_reason
                        else "."
                    )
                ),
                owner_team="Logistics",
                suggested_action=(
                    "Call the customer to confirm a new slot and address before "
                    "dispatching again; repeated failed runs carry a cost."
                ),
                evidence={
                    "attempts": delivery.attempts,
                    "attempt_limit": limit,
                    "last_attempt_at": iso(delivery.last_attempt_at),
                    "failure_reason": delivery.failure_reason or None,
                    "logistics_partner": delivery.logistics_partner or None,
                },
                sla_breached=True,
            )
        )

    if (
        order.expected_delivery_date
        and delivery.status != DeliveryStatus.DELIVERED
        and order.expected_delivery_date < timezone.localdate()
    ):
        overdue_days = (timezone.localdate() - order.expected_delivery_date).days
        blockers.append(
            Blocker(
                code="DELIVERY_OVERDUE",
                severity="high" if overdue_days > 3 else "medium",
                title=f"Delivery is {overdue_days} day(s) overdue",
                detail=(
                    f"Promised delivery date was "
                    f"{order.expected_delivery_date.isoformat()}; current delivery "
                    f"status is '{delivery.status}'."
                ),
                owner_team="Logistics",
                suggested_action=(
                    "Give the customer a firm revised date and log the reason for "
                    "the delay against the order."
                ),
                evidence={
                    "expected_delivery_date": iso(order.expected_delivery_date),
                    "days_overdue": overdue_days,
                    "delivery_status": delivery.status,
                },
                sla_breached=True,
            )
        )

    if (
        order.status == OrderStatus.READY_FOR_DELIVERY
        and delivery.status == DeliveryStatus.NOT_SCHEDULED
    ):
        blockers.append(
            Blocker(
                code="DELIVERY_NOT_SCHEDULED",
                severity="medium",
                title="Order is ready but no delivery slot is booked",
                detail=(
                    f"The order has been ready for delivery for "
                    f"{order.days_in_current_status} day(s) with no slot scheduled."
                ),
                owner_team="Logistics",
                suggested_action="Book a delivery slot with the customer.",
                evidence={"days_ready": order.days_in_current_status},
                sla_breached=order.days_in_current_status > _sla("STALE_EVENT_DAYS"),
            )
        )

    return blockers


def _rule_staleness(order: Order, last_event_at) -> Blocker | None:
    """Nothing has happened on this order for longer than the SLA."""
    threshold = _sla("STALE_EVENT_DAYS")
    reference = last_event_at or order.status_changed_at
    days = (timezone.now() - reference).days
    if days <= threshold:
        return None
    return Blocker(
        code="NO_RECENT_ACTIVITY",
        severity="medium",
        title=f"No activity recorded for {days} days",
        detail=(
            f"The last event on this order was {humanize_age(reference)}, while "
            f"the order sits in '{order.status}'. Nobody appears to be working it."
        ),
        owner_team="Operations",
        suggested_action=(
            "Assign an owner and record the next action against the order."
        ),
        evidence={
            "last_activity_at": iso(reference),
            "days_since_activity": days,
            "sla_days": threshold,
            "current_status": order.status,
            "assigned_agent": order.assigned_agent or None,
        },
        sla_breached=True,
    )


def _rule_kyc(order: Order) -> Blocker | None:
    customer = order.customer
    if customer.kyc_status == "VERIFIED":
        return None
    if order.status in (OrderStatus.DELIVERED, OrderStatus.CANCELLED, OrderStatus.REFUNDED):
        return None
    severity = "high" if customer.kyc_status == "REJECTED" else "medium"
    return Blocker(
        code=f"KYC_{customer.kyc_status}",
        severity=severity,
        title=f"Customer KYC is {customer.kyc_status.lower()}",
        detail=(
            f"KYC for customer {customer.code} is '{customer.kyc_status}'. "
            "Delivery cannot be completed without verified KYC."
        ),
        owner_team="Compliance",
        suggested_action="Re-run the KYC check with the customer's documents.",
        evidence={"customer_code": customer.code, "kyc_status": customer.kyc_status},
        sla_breached=customer.kyc_status == "REJECTED",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def diagnose(order: Order) -> dict:
    """
    Run every rule against an order and return a structured verdict.

    The caller (a tool, or the REST endpoint) passes this straight to the
    model. Nothing here is probabilistic — the same order always yields the
    same findings, which is what makes the assistant's answers reproducible
    and reviewable.
    """
    last_event = order.events.order_by("-occurred_at").first()
    last_event_at = last_event.occurred_at if last_event else None

    blockers: list[Blocker] = []

    for rule in (_rule_on_hold, _rule_refund_pending, _rule_finance, _rule_rc_transfer, _rule_kyc):
        found = rule(order)
        if found:
            blockers.append(found)

    blockers.extend(_rule_documents(order))
    blockers.extend(_rule_payments(order))
    blockers.extend(_rule_delivery(order))

    stale = _rule_staleness(order, last_event_at)
    if stale:
        blockers.append(stale)

    blockers.sort(key=lambda b: b.sort_key)

    primary: Blocker | None = None

    # A finished order is not "stuck", even if it has historical findings.
    if order.status == OrderStatus.DELIVERED:
        is_stuck = False
        verdict = "Order is delivered and complete. No active blockers."
    elif order.status == OrderStatus.REFUNDED:
        is_stuck = False
        verdict = "Order was cancelled and the refund is complete."
    elif order.status == OrderStatus.CANCELLED and not blockers:
        is_stuck = False
        verdict = "Order is cancelled with nothing outstanding."
    else:
        blocking = [
            b
            for b in blockers
            if b.severity in ("critical", "high") or b.sla_breached
        ]
        is_stuck = bool(blocking)
        if is_stuck:
            # Lead with a cause, not a consequence. "Delivery is overdue" is
            # true but useless as a first instruction; "PAN was rejected" is
            # what someone can actually act on.
            causes = [b for b in blocking if not b.is_symptom]
            primary = (causes or blocking)[0]
            verdict = f"Order is stuck. Primary blocker: {primary.title}."
        else:
            verdict = (
                "No blocker found — the order is progressing normally through "
                f"'{order.status}'."
            )

    healthy = _healthy_signals(order, blockers)

    return {
        "order_id": order.pk,
        "status": order.status,
        "is_stuck": is_stuck,
        "verdict": verdict,
        "primary_blocker": primary.to_dict() if primary else None,
        "blocker_count": len(blockers),
        "blockers": [b.to_dict() for b in blockers],
        # Split out so the model can phrase downstream effects as effects
        # ("as a result, delivery is 6 days overdue") rather than as causes.
        "consequences": [b.to_dict() for b in blockers if b.is_symptom],
        "healthy_signals": healthy,
        "days_in_current_status": order.days_in_current_status,
        "last_activity_at": iso(last_event_at),
        "last_activity": humanize_age(last_event_at) if last_event_at else "never",
        "assigned_agent": order.assigned_agent or None,
        "rules_evaluated": [
            "ORDER_ON_HOLD",
            "REFUND_PENDING",
            "FINANCE",
            "RC_TRANSFER",
            "KYC",
            "DOCUMENTS",
            "PAYMENTS",
            "DELIVERY",
            "NO_RECENT_ACTIVITY",
        ],
    }


def _healthy_signals(order: Order, blockers: list[Blocker]) -> list[str]:
    """
    What is *not* a problem.

    Without this the assistant tends to imply everything is broken. Telling
    the operator "payments and documents are clear, the hold-up is logistics"
    is most of the value of a diagnosis.
    """
    codes = {b.code for b in blockers}
    signals: list[str] = []

    if order.is_fully_paid:
        signals.append(
            f"Payment complete — {money(order.settled_amount)['display']} settled."
        )
    docs = list(order.documents.all())
    if docs and all(
        d.status == DocumentStatus.VERIFIED for d in docs if d.is_mandatory
    ):
        signals.append("All mandatory documents are verified.")
    if order.finance_status in (FinanceStatus.APPROVED, FinanceStatus.DISBURSED):
        signals.append(f"Finance is {order.finance_status.lower()}.")
    if order.finance_status == FinanceStatus.NOT_APPLICABLE:
        signals.append("Self-funded order — no finance dependency.")
    if order.vehicle.rc_transfer_status == RCTransferStatus.COMPLETED:
        signals.append("RC transfer is complete.")
    if order.customer.kyc_status == "VERIFIED":
        signals.append("Customer KYC is verified.")
    delivery = getattr(order, "delivery", None)
    if (
        delivery
        and delivery.status == DeliveryStatus.SCHEDULED
        and "DELIVERY_OVERDUE" not in codes
    ):
        signals.append(f"Delivery is scheduled for {iso(delivery.scheduled_for)}.")

    return signals
