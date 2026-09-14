"""
Operational domain model.

Entities: customers, vehicles, orders, payments, order_events, plus two
supporting tables (deliveries, order_documents) that the "why is this order
stuck?" diagnosis depends on — you cannot explain a blocked order without
knowing which document was rejected or how many delivery attempts failed.

All money is Decimal. All timestamps are timezone-aware.
"""

from __future__ import annotations

from decimal import Decimal

from django.db import models
from django.db.models import Q, Sum
from django.utils import timezone


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------
class OrderStatus(models.TextChoices):
    CREATED = "CREATED", "Created"
    DOCS_PENDING = "DOCS_PENDING", "Documents pending"
    PAYMENT_PENDING = "PAYMENT_PENDING", "Payment pending"
    PAYMENT_PARTIAL = "PAYMENT_PARTIAL", "Partially paid"
    PAYMENT_COMPLETE = "PAYMENT_COMPLETE", "Payment complete"
    FINANCE_IN_PROGRESS = "FINANCE_IN_PROGRESS", "Finance in progress"
    RC_TRANSFER_PENDING = "RC_TRANSFER_PENDING", "RC transfer pending"
    READY_FOR_DELIVERY = "READY_FOR_DELIVERY", "Ready for delivery"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY", "Out for delivery"
    DELIVERED = "DELIVERED", "Delivered"
    ON_HOLD = "ON_HOLD", "On hold"
    CANCELLED = "CANCELLED", "Cancelled"
    REFUND_PENDING = "REFUND_PENDING", "Refund pending"
    REFUNDED = "REFUNDED", "Refunded"


#: Statuses that represent a finished order — no further progress expected.
TERMINAL_STATUSES = frozenset(
    {OrderStatus.DELIVERED, OrderStatus.REFUNDED, OrderStatus.CANCELLED}
)

#: Ordered lifecycle used to answer "how far along is this order?".
LIFECYCLE_SEQUENCE = (
    OrderStatus.CREATED,
    OrderStatus.DOCS_PENDING,
    OrderStatus.PAYMENT_PENDING,
    OrderStatus.PAYMENT_PARTIAL,
    OrderStatus.PAYMENT_COMPLETE,
    OrderStatus.FINANCE_IN_PROGRESS,
    OrderStatus.RC_TRANSFER_PENDING,
    OrderStatus.READY_FOR_DELIVERY,
    OrderStatus.OUT_FOR_DELIVERY,
    OrderStatus.DELIVERED,
)


class OrderType(models.TextChoices):
    BUY = "BUY", "Customer buying a car"
    SELL = "SELL", "Customer selling a car"


class Channel(models.TextChoices):
    APP = "APP", "Mobile app"
    WEB = "WEB", "Website"
    HUB_WALKIN = "HUB_WALKIN", "Hub walk-in"
    PARTNER = "PARTNER", "Partner dealer"
    TELESALES = "TELESALES", "Telesales"


class FinanceStatus(models.TextChoices):
    NOT_APPLICABLE = "NOT_APPLICABLE", "Not applicable (self funded)"
    APPLIED = "APPLIED", "Application submitted"
    UNDER_REVIEW = "UNDER_REVIEW", "Under review"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"
    DISBURSED = "DISBURSED", "Disbursed"


class PaymentStatus(models.TextChoices):
    INITIATED = "INITIATED", "Initiated"
    PENDING = "PENDING", "Pending at bank"
    AUTHORIZED = "AUTHORIZED", "Authorized, not captured"
    SUCCESS = "SUCCESS", "Success"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"
    REFUNDED = "REFUNDED", "Refunded"

SETTLED_PAYMENT_STATUSES = frozenset({PaymentStatus.SUCCESS})

class PaymentMethod(models.TextChoices):
    UPI = "UPI", "UPI"
    NETBANKING = "NETBANKING", "Net banking"
    CARD = "CARD", "Debit/credit card"
    CASH = "CASH", "Cash at hub"
    CHEQUE = "CHEQUE", "Cheque"
    LOAN_DISBURSEMENT = "LOAN_DISBURSEMENT", "Loan disbursement"
    WALLET = "WALLET", "Wallet"


class PaymentPurpose(models.TextChoices):
    TOKEN = "TOKEN", "Token / booking amount"
    DOWN_PAYMENT = "DOWN_PAYMENT", "Down payment"
    FULL_PAYMENT = "FULL_PAYMENT", "Full vehicle payment"
    LOAN_DISBURSEMENT = "LOAN_DISBURSEMENT", "Loan disbursement"
    CHARGES = "CHARGES", "RC transfer / service charges"
    REFUND = "REFUND", "Refund to customer"


class DeliveryStatus(models.TextChoices):
    NOT_SCHEDULED = "NOT_SCHEDULED", "Not scheduled"
    SCHEDULED = "SCHEDULED", "Scheduled"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY", "Out for delivery"
    ATTEMPT_FAILED = "ATTEMPT_FAILED", "Delivery attempt failed"
    DELIVERED = "DELIVERED", "Delivered"
    CANCELLED = "CANCELLED", "Cancelled"


class DocumentType(models.TextChoices):
    AADHAAR = "AADHAAR", "Aadhaar"
    PAN = "PAN", "PAN card"
    ADDRESS_PROOF = "ADDRESS_PROOF", "Address proof"
    DRIVING_LICENCE = "DRIVING_LICENCE", "Driving licence"
    RC = "RC", "Registration certificate"
    INSURANCE = "INSURANCE", "Insurance policy"
    NOC = "NOC", "Loan NOC / hypothecation removal"
    FORM_29_30 = "FORM_29_30", "Form 29 / 30"
    BANK_STATEMENT = "BANK_STATEMENT", "Bank statement"
    SALARY_SLIP = "SALARY_SLIP", "Salary slip"


class DocumentStatus(models.TextChoices):
    NOT_UPLOADED = "NOT_UPLOADED", "Not uploaded"
    UPLOADED = "UPLOADED", "Uploaded, awaiting verification"
    VERIFIED = "VERIFIED", "Verified"
    REJECTED = "REJECTED", "Rejected"


class RCTransferStatus(models.TextChoices):
    NOT_STARTED = "NOT_STARTED", "Not started"
    APPLIED = "APPLIED", "Applied at RTO"
    IN_PROGRESS = "IN_PROGRESS", "In progress at RTO"
    COMPLETED = "COMPLETED", "Completed"
    REJECTED = "REJECTED", "Rejected by RTO"


class EventActor(models.TextChoices):
    SYSTEM = "SYSTEM", "System"
    AGENT = "AGENT", "Operations agent"
    CUSTOMER = "CUSTOMER", "Customer"
    PARTNER = "PARTNER", "Partner / vendor"


class KycStatus(models.TextChoices):
    PENDING = "PENDING", "Pending"
    VERIFIED = "VERIFIED", "Verified"
    REJECTED = "REJECTED", "Rejected"


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class Customer(models.Model):
    code = models.CharField(max_length=24, unique=True)
    full_name = models.CharField(max_length=120)
    phone = models.CharField(max_length=20, db_index=True)
    email = models.EmailField(blank=True, db_index=True)
    city = models.CharField(max_length=64)
    state = models.CharField(max_length=64)
    kyc_status = models.CharField(
        max_length=16, choices=KycStatus.choices, default=KycStatus.PENDING
    )
    customer_since = models.DateField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "customers"
        indexes = [models.Index(fields=["full_name"])]

    def __str__(self) -> str:
        return f"{self.code} {self.full_name}"


class Vehicle(models.Model):
    registration_number = models.CharField(max_length=20, unique=True)
    make = models.CharField(max_length=40)
    model = models.CharField(max_length=60)
    variant = models.CharField(max_length=60, blank=True)
    year = models.PositiveSmallIntegerField()
    fuel_type = models.CharField(max_length=20)
    transmission = models.CharField(max_length=20)
    km_driven = models.PositiveIntegerField()
    colour = models.CharField(max_length=30)
    owner_count = models.PositiveSmallIntegerField(default=1)
    inspection_score = models.DecimalField(max_digits=3, decimal_places=1, null=True)
    listing_price = models.DecimalField(max_digits=12, decimal_places=2)
    hub = models.CharField(max_length=64)
    rc_transfer_status = models.CharField(
        max_length=20,
        choices=RCTransferStatus.choices,
        default=RCTransferStatus.NOT_STARTED,
    )
    rc_transfer_applied_on = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "vehicles"
        indexes = [models.Index(fields=["make", "model"])]

    def __str__(self) -> str:
        return f"{self.registration_number} {self.make} {self.model}"

    @property
    def display_name(self) -> str:
        parts = [str(self.year), self.make, self.model, self.variant]
        return " ".join(p for p in parts if p)


class OrderQuerySet(models.QuerySet):
    def with_related(self) -> OrderQuerySet:
        """Everything the AI tools and summary endpoint need, in few queries."""
        return self.select_related("customer", "vehicle", "delivery").prefetch_related(
            "payments", "documents"
        )

    def with_payment_totals(self) -> OrderQuerySet:
        """Annotate settled inflow so amount_due never needs a Python loop."""
        return self.annotate(
            _settled_amount=Sum(
                "payments__amount",
                filter=Q(payments__status=PaymentStatus.SUCCESS)
                & ~Q(payments__purpose=PaymentPurpose.REFUND),
            ),
            _refunded_amount=Sum(
                "payments__amount",
                filter=Q(payments__status=PaymentStatus.SUCCESS)
                & Q(payments__purpose=PaymentPurpose.REFUND),
            ),
        )

    def active(self) -> OrderQuerySet:
        return self.exclude(status__in=TERMINAL_STATUSES)


class Order(models.Model):
    """
    An order is the unit operators reason about.

    `id` is the number operators quote ("order #1243"), so it is exposed
    directly rather than hidden behind a surrogate reference.
    """

    id = models.BigAutoField(primary_key=True)
    customer = models.ForeignKey(
        Customer, on_delete=models.PROTECT, related_name="orders"
    )
    vehicle = models.ForeignKey(
        Vehicle, on_delete=models.PROTECT, related_name="orders"
    )
    order_type = models.CharField(
        max_length=8, choices=OrderType.choices, default=OrderType.BUY
    )
    status = models.CharField(
        max_length=32,
        choices=OrderStatus.choices,
        default=OrderStatus.CREATED,
        db_index=True,
    )
    channel = models.CharField(max_length=16, choices=Channel.choices)
    hub = models.CharField(max_length=64)
    city = models.CharField(max_length=64)

    total_amount = models.DecimalField(max_digits=12, decimal_places=2)
    booking_amount = models.DecimalField(
        max_digits=12, decimal_places=2, default=Decimal("0")
    )

    finance_status = models.CharField(
        max_length=20,
        choices=FinanceStatus.choices,
        default=FinanceStatus.NOT_APPLICABLE,
    )
    finance_partner = models.CharField(max_length=64, blank=True)
    loan_amount = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True
    )
    finance_updated_at = models.DateTimeField(null=True, blank=True)

    assigned_agent = models.CharField(max_length=80, blank=True)
    hold_reason = models.CharField(max_length=200, blank=True)
    cancellation_reason = models.CharField(max_length=200, blank=True)

    expected_delivery_date = models.DateField(null=True, blank=True)
    booked_at = models.DateTimeField()
    status_changed_at = models.DateTimeField()
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField(auto_now=True)

    objects = OrderQuerySet.as_manager()

    class Meta:
        db_table = "orders"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["status", "-created_at"]),
            models.Index(fields=["customer", "-created_at"]),
            models.Index(fields=["hub", "status"]),
            models.Index(fields=["expected_delivery_date"]),
        ]

    def __str__(self) -> str:
        return f"Order #{self.pk} ({self.status})"

    # -- derived values -----------------------------------------------------
    @property
    def settled_amount(self) -> Decimal:
        """Money received and settled, excluding refunds."""
        cached = getattr(self, "_settled_amount", None)
        if cached is not None:
            return Decimal(cached)
        total = self.payments.filter(status=PaymentStatus.SUCCESS).exclude(
            purpose=PaymentPurpose.REFUND
        ).aggregate(total=Sum("amount"))["total"]
        return Decimal(total or 0)

    @property
    def refunded_amount(self) -> Decimal:
        cached = getattr(self, "_refunded_amount", None)
        if cached is not None:
            return Decimal(cached)
        total = self.payments.filter(
            status=PaymentStatus.SUCCESS, purpose=PaymentPurpose.REFUND
        ).aggregate(total=Sum("amount"))["total"]
        return Decimal(total or 0)

    @property
    def amount_due(self) -> Decimal:
        due = Decimal(self.total_amount) - self.settled_amount
        return due if due > 0 else Decimal("0.00")

    @property
    def is_fully_paid(self) -> bool:
        return self.amount_due <= Decimal("0.00")

    @property
    def days_in_current_status(self) -> int:
        return (timezone.now() - self.status_changed_at).days

    @property
    def age_days(self) -> int:
        return (timezone.now() - self.created_at).days

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES

    @property
    def lifecycle_position(self) -> tuple[int, int]:
        """(current step, total steps) — 0 when the status is off the happy path."""
        try:
            return LIFECYCLE_SEQUENCE.index(self.status) + 1, len(LIFECYCLE_SEQUENCE)
        except ValueError:
            return 0, len(LIFECYCLE_SEQUENCE)


class Payment(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="payments")
    reference = models.CharField(max_length=40, unique=True)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    status = models.CharField(
        max_length=16, choices=PaymentStatus.choices, db_index=True
    )
    method = models.CharField(max_length=24, choices=PaymentMethod.choices)
    purpose = models.CharField(max_length=24, choices=PaymentPurpose.choices)
    gateway = models.CharField(max_length=40, blank=True)
    gateway_txn_id = models.CharField(max_length=64, blank=True)
    failure_code = models.CharField(max_length=40, blank=True)
    failure_reason = models.CharField(max_length=200, blank=True)
    initiated_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "payments"
        ordering = ["-initiated_at"]
        indexes = [
            models.Index(fields=["order", "-initiated_at"]),
            models.Index(fields=["status", "-initiated_at"]),
        ]

    def __str__(self) -> str:
        return f"{self.reference} {self.amount} {self.status}"

    @property
    def is_settled(self) -> bool:
        return self.status in SETTLED_PAYMENT_STATUSES


class Delivery(models.Model):
    order = models.OneToOneField(
        Order, on_delete=models.CASCADE, related_name="delivery"
    )
    status = models.CharField(
        max_length=24,
        choices=DeliveryStatus.choices,
        default=DeliveryStatus.NOT_SCHEDULED,
        db_index=True,
    )
    scheduled_for = models.DateTimeField(null=True, blank=True)
    slot = models.CharField(max_length=40, blank=True)
    address_line = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=64, blank=True)
    pincode = models.CharField(max_length=10, blank=True)
    logistics_partner = models.CharField(max_length=64, blank=True)
    tracking_reference = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    failure_reason = models.CharField(max_length=200, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "deliveries"
        verbose_name_plural = "deliveries"

    def __str__(self) -> str:
        return f"Delivery for order #{self.order_id}: {self.status}"


class OrderDocument(models.Model):
    order = models.ForeignKey(
        Order, on_delete=models.CASCADE, related_name="documents"
    )
    doc_type = models.CharField(max_length=24, choices=DocumentType.choices)
    status = models.CharField(
        max_length=20,
        choices=DocumentStatus.choices,
        default=DocumentStatus.NOT_UPLOADED,
    )
    is_mandatory = models.BooleanField(default=True)
    uploaded_at = models.DateTimeField(null=True, blank=True)
    verified_at = models.DateTimeField(null=True, blank=True)
    rejection_reason = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "order_documents"
        constraints = [
            models.UniqueConstraint(
                fields=["order", "doc_type"], name="uniq_order_doc_type"
            )
        ]
        indexes = [models.Index(fields=["order", "status"])]

    def __str__(self) -> str:
        return f"{self.doc_type}={self.status} (order #{self.order_id})"


class OrderEvent(models.Model):
    """
    Append-only audit trail of what happened to an order.

    This is what makes "what happened with order #2325?" answerable from
    data rather than from the model's imagination.
    """

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name="events")
    sequence = models.PositiveIntegerField()
    event_type = models.CharField(max_length=48, db_index=True)
    description = models.CharField(max_length=300)
    actor_type = models.CharField(
        max_length=16, choices=EventActor.choices, default=EventActor.SYSTEM
    )
    actor_name = models.CharField(max_length=80, blank=True)
    from_status = models.CharField(max_length=32, blank=True)
    to_status = models.CharField(max_length=32, blank=True)
    metadata = models.JSONField(default=dict, blank=True)
    occurred_at = models.DateTimeField(db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "order_events"
        ordering = ["order_id", "sequence"]
        constraints = [
            models.UniqueConstraint(
                fields=["order", "sequence"], name="uniq_order_event_sequence"
            )
        ]
        indexes = [models.Index(fields=["order", "-occurred_at"])]

    def __str__(self) -> str:
        return f"#{self.order_id}.{self.sequence} {self.event_type}"
