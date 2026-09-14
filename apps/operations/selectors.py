"""
Read layer — the single source of operational truth.

Both the plain REST API and every AI tool call these functions. There is no
second query path for the assistant, which means:

  * the model cannot see anything a human with the same role could not see,
  * PII masking is applied once, here, not per-caller,
  * changing a business definition (e.g. what "paid" means) changes it
    everywhere at once.

Every function returns plain JSON-serialisable dicts.
"""

from __future__ import annotations

from django.db.models import Q

from apps.common.exceptions import NotFoundError, ValidationError

from .formatting import (
    humanize_age,
    iso,
    label,
    mask_email,
    mask_phone,
    money,
)
from .models import (
    Delivery,
    DeliveryStatus,
    DocumentStatus,
    DocumentType,
    FinanceStatus,
    Order,
    OrderStatus,
    OrderType,
    PaymentMethod,
    PaymentPurpose,
    PaymentStatus,
    RCTransferStatus,
)

MAX_TIMELINE_EVENTS = 50
MAX_SEARCH_RESULTS = 25


# ---------------------------------------------------------------------------
# Order lookup
# ---------------------------------------------------------------------------
def parse_order_id(raw) -> int:
    """
    Coerce whatever the model extracted ('#1243', '1243', 1243) to an int.

    Raises ValidationError on anything that is not a positive integer, so a
    malformed id produces an explicit tool error rather than a silent miss.
    """
    if isinstance(raw, bool):
        raise ValidationError("Order id must be a number.")
    if isinstance(raw, int):
        value = raw
    else:
        text = str(raw or "").strip().lstrip("#").replace(" ", "")
        if not text.isdigit():
            raise ValidationError(
                f"'{raw}' is not a valid order id. Order ids are numeric, e.g. 1243."
            )
        value = int(text)
    if value <= 0:
        raise ValidationError("Order id must be a positive number.")
    return value


def get_order(order_id) -> Order:
    """Fetch an order with related rows preloaded, or raise NotFoundError."""
    pk = parse_order_id(order_id)
    order = (
        Order.objects.with_related().with_payment_totals().filter(pk=pk).first()
    )
    if order is None:
        raise NotFoundError(
            f"Order #{pk} does not exist in the operations database.",
            details={"order_id": pk},
        )
    return order


# ---------------------------------------------------------------------------
# Per-entity projections
# ---------------------------------------------------------------------------
def order_core(order: Order) -> dict:
    step, total_steps = order.lifecycle_position
    return {
        "order_id": order.pk,
        "status": order.status,
        "status_label": label(OrderStatus, order.status),
        "order_type": order.order_type,
        "order_type_label": label(OrderType, order.order_type),
        "channel": order.channel,
        "hub": order.hub,
        "city": order.city,
        "assigned_agent": order.assigned_agent or None,
        "total_amount": money(order.total_amount),
        "booking_amount": money(order.booking_amount),
        "amount_paid": money(order.settled_amount),
        "amount_due": money(order.amount_due),
        "is_fully_paid": order.is_fully_paid,
        "lifecycle_step": f"{step} of {total_steps}" if step else "off standard flow",
        "booked_at": iso(order.booked_at),
        "created_at": iso(order.created_at),
        "status_changed_at": iso(order.status_changed_at),
        "days_in_current_status": order.days_in_current_status,
        "status_last_changed": humanize_age(order.status_changed_at),
        "order_age_days": order.age_days,
        "expected_delivery_date": iso(order.expected_delivery_date),
        "is_terminal": order.is_terminal,
        "hold_reason": order.hold_reason or None,
        "cancellation_reason": order.cancellation_reason or None,
    }


def payment_status(order: Order) -> dict:
    """
    Payment posture for an order: the aggregate plus every transaction.

    The aggregate is computed here so the model never sums a list itself.
    """
    payments = list(order.payments.all())
    payments.sort(key=lambda p: p.initiated_at, reverse=True)

    settled = [p for p in payments if p.is_settled and p.purpose != PaymentPurpose.REFUND]
    failed = [p for p in payments if p.status == PaymentStatus.FAILED]
    in_flight = [
        p
        for p in payments
        if p.status in (PaymentStatus.INITIATED, PaymentStatus.PENDING, PaymentStatus.AUTHORIZED)
    ]
    refunds = [p for p in payments if p.purpose == PaymentPurpose.REFUND]

    latest = payments[0] if payments else None

    if not payments:
        headline = "No payment has been initiated on this order yet."
    elif order.is_fully_paid:
        headline = "Fully paid."
    elif settled:
        headline = f"Partially paid — {money(order.amount_due)['display']} still due."
    elif in_flight:
        headline = "Payment initiated but not yet confirmed by the bank."
    elif failed:
        headline = "All payment attempts so far have failed."
    else:
        headline = "No settled payment on this order."

    return {
        "order_id": order.pk,
        "order_status": order.status,
        "summary": headline,
        "total_amount": money(order.total_amount),
        "amount_paid": money(order.settled_amount),
        "amount_due": money(order.amount_due),
        "amount_refunded": money(order.refunded_amount),
        "is_fully_paid": order.is_fully_paid,
        "counts": {
            "total": len(payments),
            "settled": len(settled),
            "failed": len(failed),
            "in_flight": len(in_flight),
            "refunds": len(refunds),
        },
        "latest_payment": _payment_row(latest) if latest else None,
        "payments": [_payment_row(p) for p in payments],
    }


def _payment_row(payment) -> dict:
    return {
        "reference": payment.reference,
        "amount": money(payment.amount),
        "status": payment.status,
        "status_label": label(PaymentStatus, payment.status),
        "method": payment.method,
        "method_label": label(PaymentMethod, payment.method),
        "purpose": payment.purpose,
        "purpose_label": label(PaymentPurpose, payment.purpose),
        "gateway": payment.gateway or None,
        "gateway_txn_id": payment.gateway_txn_id or None,
        "failure_code": payment.failure_code or None,
        "failure_reason": payment.failure_reason or None,
        "initiated_at": iso(payment.initiated_at),
        "completed_at": iso(payment.completed_at),
        "initiated": humanize_age(payment.initiated_at),
    }


def customer_info(order: Order, *, include_pii: bool) -> dict:
    """
    Customer attached to an order.

    `include_pii` is decided by the caller's scopes, not by the model. A
    viewer-role operator (or an assistant acting for one) gets masked
    contact details and the response says so explicitly, so the model can
    tell the user why it cannot read out a phone number.
    """
    customer = order.customer
    data = {
        "order_id": order.pk,
        "customer_code": customer.code,
        "full_name": customer.full_name,
        "city": customer.city,
        "state": customer.state,
        "kyc_status": customer.kyc_status,
        "customer_since": iso(customer.customer_since),
        "total_orders": customer.orders.count(),
        "pii_visible": include_pii,
    }
    if include_pii:
        data["phone"] = customer.phone
        data["email"] = customer.email or None
    else:
        data["phone"] = mask_phone(customer.phone)
        data["email"] = mask_email(customer.email) if customer.email else None
        data["pii_note"] = (
            "Contact details are masked because the requesting role lacks the "
            "'customers:pii' scope. Do not guess the hidden digits."
        )
    return data


def vehicle_info(order: Order) -> dict:
    vehicle = order.vehicle
    return {
        "order_id": order.pk,
        "registration_number": vehicle.registration_number,
        "display_name": vehicle.display_name,
        "make": vehicle.make,
        "model": vehicle.model,
        "variant": vehicle.variant or None,
        "year": vehicle.year,
        "fuel_type": vehicle.fuel_type,
        "transmission": vehicle.transmission,
        "km_driven": vehicle.km_driven,
        "colour": vehicle.colour,
        "owner_count": vehicle.owner_count,
        "inspection_score": (
            float(vehicle.inspection_score) if vehicle.inspection_score else None
        ),
        "listing_price": money(vehicle.listing_price),
        "hub": vehicle.hub,
        "rc_transfer_status": vehicle.rc_transfer_status,
        "rc_transfer_status_label": label(
            RCTransferStatus, vehicle.rc_transfer_status
        ),
        "rc_transfer_applied_on": iso(vehicle.rc_transfer_applied_on),
    }


def timeline(order: Order, *, limit: int = 20) -> dict:
    limit = max(1, min(int(limit), MAX_TIMELINE_EVENTS))
    events = list(order.events.order_by("-occurred_at", "-sequence")[:limit])
    events.reverse()  # chronological reads better for narration

    total = order.events.count()
    return {
        "order_id": order.pk,
        "current_status": order.status,
        "event_count_total": total,
        "event_count_returned": len(events),
        "truncated": total > len(events),
        "last_activity_at": iso(events[-1].occurred_at) if events else None,
        "last_activity": humanize_age(events[-1].occurred_at) if events else "never",
        "events": [
            {
                "sequence": e.sequence,
                "event_type": e.event_type,
                "description": e.description,
                "actor": e.actor_name or e.actor_type,
                "actor_type": e.actor_type,
                "from_status": e.from_status or None,
                "to_status": e.to_status or None,
                "occurred_at": iso(e.occurred_at),
                "occurred": humanize_age(e.occurred_at),
                "metadata": e.metadata or {},
            }
            for e in events
        ],
    }


def delivery_status(order: Order) -> dict:
    delivery: Delivery | None = getattr(order, "delivery", None)
    if delivery is None:
        return {
            "order_id": order.pk,
            "has_delivery_record": False,
            "status": DeliveryStatus.NOT_SCHEDULED,
            "status_label": label(DeliveryStatus, DeliveryStatus.NOT_SCHEDULED),
            "summary": "No delivery has been scheduled for this order yet.",
            "expected_delivery_date": iso(order.expected_delivery_date),
        }

    from django.utils import timezone

    is_overdue = bool(
        order.expected_delivery_date
        and delivery.status != DeliveryStatus.DELIVERED
        and order.expected_delivery_date < timezone.localdate()
    )
    days_overdue = (
        (timezone.localdate() - order.expected_delivery_date).days if is_overdue else 0
    )

    return {
        "order_id": order.pk,
        "has_delivery_record": True,
        "status": delivery.status,
        "status_label": label(DeliveryStatus, delivery.status),
        "scheduled_for": iso(delivery.scheduled_for),
        "slot": delivery.slot or None,
        "city": delivery.city or None,
        "pincode": delivery.pincode or None,
        "logistics_partner": delivery.logistics_partner or None,
        "tracking_reference": delivery.tracking_reference or None,
        "attempts": delivery.attempts,
        "last_attempt_at": iso(delivery.last_attempt_at),
        "failure_reason": delivery.failure_reason or None,
        "delivered_at": iso(delivery.delivered_at),
        "expected_delivery_date": iso(order.expected_delivery_date),
        "is_overdue": is_overdue,
        "days_overdue": days_overdue,
    }


def documents(order: Order) -> dict:
    docs = list(order.documents.all())
    by_status: dict[str, list[str]] = {}
    for doc in docs:
        by_status.setdefault(doc.status, []).append(doc.doc_type)

    pending = [
        d
        for d in docs
        if d.is_mandatory
        and d.status in (DocumentStatus.NOT_UPLOADED, DocumentStatus.REJECTED, DocumentStatus.UPLOADED)
    ]

    return {
        "order_id": order.pk,
        "total": len(docs),
        "all_mandatory_verified": not pending,
        "pending_count": len(pending),
        "by_status": by_status,
        "documents": [
            {
                "doc_type": d.doc_type,
                "doc_type_label": label(DocumentType, d.doc_type),
                "status": d.status,
                "status_label": label(DocumentStatus, d.status),
                "is_mandatory": d.is_mandatory,
                "uploaded_at": iso(d.uploaded_at),
                "verified_at": iso(d.verified_at),
                "rejection_reason": d.rejection_reason or None,
            }
            for d in sorted(docs, key=lambda x: x.doc_type)
        ],
    }


def finance_info(order: Order) -> dict:
    return {
        "order_id": order.pk,
        "finance_status": order.finance_status,
        "finance_status_label": label(FinanceStatus, order.finance_status),
        "is_financed": order.finance_status != FinanceStatus.NOT_APPLICABLE,
        "finance_partner": order.finance_partner or None,
        "loan_amount": money(order.loan_amount),
        "updated_at": iso(order.finance_updated_at),
        "updated": humanize_age(order.finance_updated_at),
    }


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------
def order_summary(order: Order, *, include_pii: bool, timeline_limit: int = 10) -> dict:
    """
    Everything about one order in a single call.

    Exists so that "give me a complete summary of order #2325" costs one tool
    round trip instead of six. The orchestrator still records it as one
    invocation, and the response carries the same shapes as the individual
    tools so the model sees a consistent vocabulary.
    """
    return {
        "order": order_core(order),
        "customer": customer_info(order, include_pii=include_pii),
        "vehicle": vehicle_info(order),
        "payment": payment_status(order),
        "delivery": delivery_status(order),
        "documents": documents(order),
        "finance": finance_info(order),
        "timeline": timeline(order, limit=timeline_limit),
    }


# ---------------------------------------------------------------------------
# Search / entity resolution
# ---------------------------------------------------------------------------
def find_orders(
    *,
    phone: str | None = None,
    registration_number: str | None = None,
    customer_name: str | None = None,
    status: str | None = None,
    hub: str | None = None,
    limit: int = 10,
) -> dict:
    """
    Resolve orders from something other than an order id.

    Operators often have a phone number or a registration plate instead of an
    order number; without this the assistant would have to ask them to go
    look it up. At least one filter is required — an unfiltered dump of the
    order table is never a useful answer.
    """
    limit = max(1, min(int(limit), MAX_SEARCH_RESULTS))

    filters = Q()
    applied: dict[str, str] = {}

    if phone:
        digits = "".join(ch for ch in str(phone) if ch.isdigit())[-10:]
        if len(digits) < 10:
            raise ValidationError(
                "Provide at least the last 10 digits of the phone number."
            )
        filters &= Q(customer__phone__endswith=digits)
        applied["phone"] = digits
    if registration_number:
        normalised = str(registration_number).replace(" ", "").replace("-", "").upper()
        filters &= Q(vehicle__registration_number__icontains=normalised)
        applied["registration_number"] = normalised
    if customer_name:
        filters &= Q(customer__full_name__icontains=str(customer_name).strip())
        applied["customer_name"] = str(customer_name).strip()
    if status:
        value = str(status).strip().upper()
        valid = {s.value for s in OrderStatus}
        if value not in valid:
            raise ValidationError(
                f"'{status}' is not a known order status.",
                details={"valid_statuses": sorted(valid)},
            )
        filters &= Q(status=value)
        applied["status"] = value
    if hub:
        filters &= Q(hub__icontains=str(hub).strip())
        applied["hub"] = str(hub).strip()

    if not applied:
        raise ValidationError(
            "At least one search filter is required "
            "(phone, registration_number, customer_name, status or hub)."
        )

    queryset = (
        Order.objects.filter(filters)
        .select_related("customer", "vehicle")
        .with_payment_totals()
        .order_by("-created_at")
    )
    total = queryset.count()
    rows = list(queryset[:limit])

    return {
        "filters_applied": applied,
        "match_count": total,
        "returned": len(rows),
        "truncated": total > len(rows),
        "orders": [
            {
                "order_id": o.pk,
                "status": o.status,
                "status_label": label(OrderStatus, o.status),
                "customer_name": o.customer.full_name,
                "vehicle": o.vehicle.display_name,
                "registration_number": o.vehicle.registration_number,
                "hub": o.hub,
                "total_amount": money(o.total_amount),
                "amount_due": money(o.amount_due),
                "created_at": iso(o.created_at),
                "expected_delivery_date": iso(o.expected_delivery_date),
            }
            for o in rows
        ],
    }
