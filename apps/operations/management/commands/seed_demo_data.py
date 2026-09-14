"""
Seed realistic Cars24-like operational data.

    python manage.py seed_demo_data --flush

Design notes
------------
1. **Deterministic.** A fixed RNG seed means the same command always produces
   the same database. Demos are reproducible and tests can assert on specific
   order ids.

2. **The timeline is derived, never authored.** Every `order_events` row is
   generated *from* the payment / document / finance / delivery rows that the
   scenario created. It is therefore impossible for the timeline to claim a
   payment succeeded when the payments table disagrees. That property is the
   whole point: the assistant reads both, and inconsistent seed data would
   produce confidently contradictory answers that look like model failures.

3. **Explicit primary keys.** Order ids are assigned from fixed bands so that
   #1243 and #2325 — the ids in the product brief — always exist. The
   sequence is reset afterwards so ordinary inserts continue cleanly.
"""

from __future__ import annotations

import datetime as dt
import random
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.db import connection, transaction
from django.utils import timezone

from apps.accounts.permissions import Role
from apps.operations.formatting import indian_currency
from apps.operations.models import (
    Customer,
    Delivery,
    DeliveryStatus,
    DocumentStatus,
    FinanceStatus,
    Order,
    OrderDocument,
    OrderEvent,
    OrderStatus,
    OrderType,
    Payment,
    PaymentPurpose,
    PaymentStatus,
    RCTransferStatus,
    Vehicle,
)
from apps.operations.seed.reference import (
    AGENTS,
    CHANNEL_WEIGHTS,
    COLOURS,
    DELIVERY_SLOTS,
    EVENT_TEMPLATES,
    FINANCE_PARTNERS,
    FIRST_NAMES,
    GATEWAYS,
    LAST_NAMES,
    LOCATIONS,
    LOGISTICS_PARTNERS,
    VEHICLE_CATALOGUE,
)
from apps.operations.seed.scenarios import (
    PINNED_SCENARIOS,
    SCENARIOS,
    SCENARIOS_BY_NAME,
    Scenario,
)

#: Minute offsets keep same-day events in a sensible order without needing
#: hand-written timestamps: an order is created before a document is
#: verified, which happens before that day's payment, and so on.
CATEGORY_OFFSET_MINUTES = {
    "ORDER": 0,
    "STATUS": 30,
    "DOCUMENT": 120,
    "PAYMENT": 240,
    "FINANCE": 300,
    "RC": 360,
    "DELIVERY": 420,
    "NOTE": 480,
}

DEMO_PASSWORD = "opsai12345"

DEMO_USERS = [
    ("viewer", Role.VIEWER, "Vani Rathore", "Support"),
    ("agent", Role.OPS_AGENT, "Arun Sethi", "Order Operations"),
    ("manager", Role.OPS_MANAGER, "Manisha Gill", "Order Operations"),
    ("admin", Role.ADMIN, "Adi Narang", "Platform"),
]


class Command(BaseCommand):
    help = "Seed realistic Cars24-like orders, payments, events and users."

    def add_arguments(self, parser):
        parser.add_argument(
            "--orders", type=int, default=200, help="How many orders to create."
        )
        parser.add_argument(
            "--seed", type=int, default=24, help="RNG seed for reproducibility."
        )
        parser.add_argument(
            "--flush",
            action="store_true",
            help="Delete existing operational data first.",
        )
        parser.add_argument(
            "--skip-users",
            action="store_true",
            help="Do not create the demo user accounts.",
        )

    # ------------------------------------------------------------------
    @transaction.atomic
    def handle(self, *args, **options):
        self.rng = random.Random(options["seed"])
        self.now = timezone.now()
        self.today = timezone.localdate()

        if options["flush"]:
            self._flush()

        order_ids = self._allocate_order_ids(options["orders"])
        assignments = self._assign_scenarios(order_ids)

        self.stdout.write(f"Seeding {len(order_ids)} orders...")

        customers, vehicles = self._build_customers_and_vehicles(assignments)
        Customer.objects.bulk_create(customers)
        Vehicle.objects.bulk_create(vehicles)

        orders = self._build_orders(assignments, customers, vehicles)
        Order.objects.bulk_create(orders)

        self._build_children(assignments, orders)
        self._reset_order_sequence()

        if not options["skip_users"]:
            self._create_users()

        self._report(assignments)

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------
    def _flush(self):
        self.stdout.write(self.style.WARNING("Flushing operational tables..."))
        OrderEvent.objects.all().delete()
        Payment.objects.all().delete()
        OrderDocument.objects.all().delete()
        Delivery.objects.all().delete()
        Order.objects.all().delete()
        Vehicle.objects.all().delete()
        Customer.objects.all().delete()

    def _allocate_order_ids(self, count: int) -> list[int]:
        """
        Order ids come from two fixed bands so the brief's ids always exist.

        Band 1 starts at 1200 and band 2 at 2300, which covers #1243 and
        #2325 as long as at least ~50 orders are requested per band.
        """
        half = max(count // 2, 1)
        band_one = list(range(1200, 1200 + half))
        band_two = list(range(2300, 2300 + (count - half)))
        ids = band_one + band_two

        for pinned in PINNED_SCENARIOS:
            if pinned not in ids:
                self.stdout.write(
                    self.style.WARNING(
                        f"Order #{pinned} is outside the generated range; "
                        f"raise --orders to include it."
                    )
                )
        return ids

    def _assign_scenarios(self, order_ids: list[int]) -> list[tuple[int, Scenario]]:
        """Weighted scenario assignment, with the pinned ids forced."""
        population = [s.name for s in SCENARIOS]
        weights = [s.weight for s in SCENARIOS]

        assignments: list[tuple[int, Scenario]] = []
        for oid in order_ids:
            if oid in PINNED_SCENARIOS:
                scenario = SCENARIOS_BY_NAME[PINNED_SCENARIOS[oid]]
            else:
                name = self.rng.choices(population, weights=weights, k=1)[0]
                scenario = SCENARIOS_BY_NAME[name]
            assignments.append((oid, scenario))
        return assignments

    # ------------------------------------------------------------------
    # Time helpers
    # ------------------------------------------------------------------
    def _ts(self, days_ago: float, category: str) -> dt.datetime:
        return (
            self.now
            - dt.timedelta(days=days_ago)
            + dt.timedelta(minutes=CATEGORY_OFFSET_MINUTES[category])
        )

    # ------------------------------------------------------------------
    # Row builders
    # ------------------------------------------------------------------
    def _build_customers_and_vehicles(self, assignments):
        customers: list[Customer] = []
        vehicles: list[Vehicle] = []

        for index, (_oid, scenario) in enumerate(assignments):
            city, state, _hub, series = LOCATIONS[index % len(LOCATIONS)]

            first = self.rng.choice(FIRST_NAMES)
            last = self.rng.choice(LAST_NAMES)
            customers.append(
                Customer(
                    code=f"CUS{100000 + index}",
                    full_name=f"{first} {last}",
                    phone=f"9{self.rng.randint(100000000, 999999999)}",
                    email=f"{first.lower()}.{last.lower()}{self.rng.randint(1, 99)}@example.com",
                    city=city,
                    state=state,
                    kyc_status=scenario.kyc_status,
                    customer_since=self.today
                    - dt.timedelta(days=self.rng.randint(30, 1500)),
                )
            )

            make, model, variant, fuel, transmission, low, high = self.rng.choice(
                VEHICLE_CATALOGUE
            )
            price = Decimal(self.rng.randrange(low, high, 5000))
            year = self.rng.randint(2015, 2023)
            vehicles.append(
                Vehicle(
                    registration_number=(
                        f"{series}{self.rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
                        f"{self.rng.choice('ABCDEFGHJKLMNPQRSTUVWXYZ')}"
                        f"{self.rng.randint(1000, 9999)}"
                    ),
                    make=make,
                    model=model,
                    variant=variant,
                    year=year,
                    fuel_type=fuel,
                    transmission=transmission,
                    km_driven=self.rng.randrange(8000, 110000, 500),
                    colour=self.rng.choice(COLOURS),
                    owner_count=self.rng.choices([1, 2, 3], weights=[70, 25, 5])[0],
                    inspection_score=Decimal(
                        str(round(self.rng.uniform(6.5, 9.8), 1))
                    ),
                    listing_price=price,
                    hub=_hub,
                    rc_transfer_status=scenario.rc.status,
                    rc_transfer_applied_on=(
                        self.today - dt.timedelta(days=scenario.rc.applied_days_ago)
                        if scenario.rc.applied_days_ago is not None
                        else None
                    ),
                )
            )

        # Registration numbers must be unique; regenerate any collisions.
        self._dedupe_registrations(vehicles)
        return customers, vehicles

    def _dedupe_registrations(self, vehicles: list[Vehicle]):
        seen: set[str] = set()
        for vehicle in vehicles:
            while vehicle.registration_number in seen:
                vehicle.registration_number = (
                    f"{vehicle.registration_number[:6]}"
                    f"{self.rng.randint(1000, 9999)}"
                )
            seen.add(vehicle.registration_number)

    def _build_orders(self, assignments, customers, vehicles) -> list[Order]:
        orders: list[Order] = []

        for index, (oid, scenario) in enumerate(assignments):
            customer = customers[index]
            vehicle = vehicles[index]

            charges = Decimal(self.rng.choice([9000, 12500, 15000, 18000]))
            total = Decimal(vehicle.listing_price) + charges
            booking = Decimal(self.rng.choice([11000, 21000, 25000]))

            created_at = self._ts(scenario.age_days, "ORDER")
            final_days_ago = scenario.status_path[-1][0]

            finance_status = FinanceStatus.NOT_APPLICABLE
            finance_partner = ""
            loan_amount = None
            finance_updated_at = None
            if scenario.finance:
                finance_status = scenario.finance.status
                finance_partner = self.rng.choice(FINANCE_PARTNERS)
                loan_amount = (total * Decimal(str(scenario.finance.fraction))).quantize(
                    Decimal("1")
                )
                finance_updated_at = self._ts(scenario.finance.days_ago, "FINANCE")

            channel = self.rng.choices(
                [c for c, _ in CHANNEL_WEIGHTS],
                weights=[w for _, w in CHANNEL_WEIGHTS],
            )[0]

            orders.append(
                Order(
                    id=oid,
                    customer_id=customer.pk,
                    vehicle_id=vehicle.pk,
                    order_type=OrderType.BUY,
                    status=scenario.status,
                    channel=channel,
                    hub=vehicle.hub,
                    city=customer.city,
                    total_amount=total,
                    booking_amount=booking,
                    finance_status=finance_status,
                    finance_partner=finance_partner,
                    loan_amount=loan_amount,
                    finance_updated_at=finance_updated_at,
                    assigned_agent=self.rng.choice(AGENTS),
                    hold_reason=scenario.hold_reason,
                    cancellation_reason=scenario.cancellation_reason,
                    expected_delivery_date=(
                        self.today
                        + dt.timedelta(days=scenario.expected_delivery_in_days)
                        if scenario.expected_delivery_in_days is not None
                        else None
                    ),
                    booked_at=created_at + dt.timedelta(minutes=12),
                    status_changed_at=self._ts(final_days_ago, "STATUS"),
                    created_at=created_at,
                )
            )
        return orders

    # ------------------------------------------------------------------
    def _build_children(self, assignments, orders):
        """
        Create payments, documents, delivery and the derived timeline.

        Events are collected from the rows actually created, then sorted and
        numbered, which is what guarantees timeline/ledger consistency.
        """
        documents: list[OrderDocument] = []
        payments: list[Payment] = []
        deliveries: list[Delivery] = []
        events: list[OrderEvent] = []

        order_by_id = {o.pk: o for o in orders}

        for oid, scenario in assignments:
            order = order_by_id[oid]
            collected: list[dict] = []

            self._add_lifecycle_events(order, scenario, collected)
            self._add_documents(order, scenario, documents, collected)
            self._add_payments(order, scenario, payments, collected)
            self._add_finance_events(order, scenario, collected)
            self._add_rc_events(order, scenario, collected)
            self._add_delivery(order, scenario, deliveries, collected)

            for extra in scenario.extra_events:
                collected.append(
                    {
                        "occurred_at": self._ts(extra.days_ago, "NOTE"),
                        "event_type": extra.event_type,
                        "description": extra.description,
                        "actor_type": extra.actor_type,
                        "actor_name": order.assigned_agent
                        if extra.actor_type == "AGENT"
                        else "",
                        "metadata": {},
                    }
                )

            collected.sort(key=lambda e: e["occurred_at"])
            for sequence, item in enumerate(collected, start=1):
                events.append(
                    OrderEvent(
                        order_id=oid,
                        sequence=sequence,
                        event_type=item["event_type"],
                        description=item["description"][:300],
                        actor_type=item.get("actor_type", "SYSTEM"),
                        actor_name=item.get("actor_name", "")[:80],
                        from_status=item.get("from_status", ""),
                        to_status=item.get("to_status", ""),
                        metadata=item.get("metadata", {}),
                        occurred_at=item["occurred_at"],
                    )
                )

        OrderDocument.objects.bulk_create(documents, batch_size=500)
        Payment.objects.bulk_create(payments, batch_size=500)
        Delivery.objects.bulk_create(deliveries, batch_size=500)
        OrderEvent.objects.bulk_create(events, batch_size=1000)

    # -- event producers ------------------------------------------------
    def _add_lifecycle_events(self, order: Order, scenario: Scenario, out: list[dict]):
        path = scenario.status_path
        first_days, first_status = path[0]

        out.append(
            {
                "occurred_at": self._ts(first_days, "ORDER"),
                "event_type": "ORDER_CREATED",
                "description": EVENT_TEMPLATES["ORDER_CREATED"].format(
                    channel=order.channel, vehicle=f"vehicle #{order.vehicle_id}"
                ),
                "actor_type": "CUSTOMER",
                "to_status": first_status,
                "metadata": {"channel": order.channel, "hub": order.hub},
            }
        )

        for i in range(1, len(path)):
            prev_days, prev_status = path[i - 1]
            days, status = path[i]

            if status == OrderStatus.ON_HOLD:
                event_type = "HOLD_APPLIED"
                description = EVENT_TEMPLATES["HOLD_APPLIED"].format(
                    reason=scenario.hold_reason or "no reason recorded"
                )
            elif status == OrderStatus.CANCELLED:
                event_type = "ORDER_CANCELLED"
                description = EVENT_TEMPLATES["ORDER_CANCELLED"].format(
                    reason=scenario.cancellation_reason or "no reason recorded"
                )
            else:
                event_type = "STATUS_CHANGED"
                description = EVENT_TEMPLATES["STATUS_CHANGED"].format(
                    from_status=prev_status, to_status=status
                )

            out.append(
                {
                    "occurred_at": self._ts(days, "STATUS"),
                    "event_type": event_type,
                    "description": description,
                    "actor_type": "SYSTEM",
                    "from_status": prev_status,
                    "to_status": status,
                    "metadata": {},
                }
            )

    def _add_documents(
        self, order: Order, scenario: Scenario, documents: list, out: list[dict]
    ):
        for plan in scenario.docs:
            uploaded_at = verified_at = None

            if plan.status == DocumentStatus.UPLOADED and plan.days_ago is not None:
                uploaded_at = self._ts(plan.days_ago, "DOCUMENT")
            elif plan.status in (DocumentStatus.VERIFIED, DocumentStatus.REJECTED):
                uploaded_at = self._ts((plan.days_ago or 0) + 1, "DOCUMENT")
                if plan.status == DocumentStatus.VERIFIED:
                    verified_at = self._ts(plan.days_ago or 0, "DOCUMENT")

            documents.append(
                OrderDocument(
                    order_id=order.pk,
                    doc_type=plan.doc_type,
                    status=plan.status,
                    is_mandatory=plan.is_mandatory,
                    uploaded_at=uploaded_at,
                    verified_at=verified_at,
                    rejection_reason=plan.rejection_reason,
                )
            )

            if uploaded_at:
                out.append(
                    {
                        "occurred_at": uploaded_at,
                        "event_type": "DOCUMENT_UPLOADED",
                        "description": EVENT_TEMPLATES["DOCUMENT_UPLOADED"].format(
                            doc_type=plan.doc_type
                        ),
                        "actor_type": "CUSTOMER",
                        "metadata": {"doc_type": plan.doc_type},
                    }
                )
            if plan.status == DocumentStatus.VERIFIED and verified_at:
                out.append(
                    {
                        "occurred_at": verified_at,
                        "event_type": "DOCUMENT_VERIFIED",
                        "description": EVENT_TEMPLATES["DOCUMENT_VERIFIED"].format(
                            doc_type=plan.doc_type
                        ),
                        "actor_type": "AGENT",
                        "actor_name": order.assigned_agent,
                        "metadata": {"doc_type": plan.doc_type},
                    }
                )
            if plan.status == DocumentStatus.REJECTED:
                out.append(
                    {
                        "occurred_at": self._ts(plan.days_ago or 0, "DOCUMENT"),
                        "event_type": "DOCUMENT_REJECTED",
                        "description": EVENT_TEMPLATES["DOCUMENT_REJECTED"].format(
                            doc_type=plan.doc_type,
                            reason=plan.rejection_reason or "no reason recorded",
                        ),
                        "actor_type": "AGENT",
                        "actor_name": order.assigned_agent,
                        "metadata": {
                            "doc_type": plan.doc_type,
                            "rejection_reason": plan.rejection_reason,
                        },
                    }
                )

    def _add_payments(
        self, order: Order, scenario: Scenario, payments: list, out: list[dict]
    ):
        for index, plan in enumerate(scenario.payments, start=1):
            amount = (
                Decimal(order.total_amount) * Decimal(str(plan.fraction))
            ).quantize(Decimal("0.01"))
            initiated_at = self._ts(plan.days_ago, "PAYMENT")
            completed_at = None
            if plan.status in (PaymentStatus.SUCCESS, PaymentStatus.FAILED):
                completed_at = initiated_at + dt.timedelta(
                    minutes=self.rng.randint(1, 25)
                )

            gateway = (
                ""
                if plan.method == "LOAN_DISBURSEMENT"
                else self.rng.choice(GATEWAYS)
            )
            payments.append(
                Payment(
                    order_id=order.pk,
                    reference=f"PAY-{order.pk:06d}-{index:02d}",
                    amount=amount,
                    status=plan.status,
                    method=plan.method,
                    purpose=plan.purpose,
                    gateway=gateway,
                    gateway_txn_id=(
                        f"{gateway[:4].lower()}_{self.rng.randrange(10**11, 10**12)}"
                        if gateway
                        else ""
                    ),
                    failure_code=plan.failure_code,
                    failure_reason=plan.failure_reason,
                    initiated_at=initiated_at,
                    completed_at=completed_at,
                )
            )

            common = {
                "purpose": plan.purpose,
                "amount": indian_currency(amount),
                "method": plan.method,
            }
            out.append(
                {
                    "occurred_at": initiated_at,
                    "event_type": "PAYMENT_INITIATED",
                    "description": EVENT_TEMPLATES["PAYMENT_INITIATED"].format(**common),
                    "actor_type": "CUSTOMER",
                    "metadata": {"reference": f"PAY-{order.pk:06d}-{index:02d}"},
                }
            )

            if plan.status == PaymentStatus.SUCCESS:
                key = (
                    "REFUND_PROCESSED"
                    if plan.purpose == PaymentPurpose.REFUND
                    else "PAYMENT_SUCCESS"
                )
                out.append(
                    {
                        "occurred_at": completed_at,
                        "event_type": key,
                        "description": EVENT_TEMPLATES[key].format(**common),
                        "actor_type": "SYSTEM",
                        "metadata": {"reference": f"PAY-{order.pk:06d}-{index:02d}"},
                    }
                )
            elif plan.status == PaymentStatus.FAILED:
                out.append(
                    {
                        "occurred_at": completed_at,
                        "event_type": "PAYMENT_FAILED",
                        "description": EVENT_TEMPLATES["PAYMENT_FAILED"].format(
                            **common, reason=plan.failure_reason or "unknown reason"
                        ),
                        "actor_type": "SYSTEM",
                        "metadata": {
                            "reference": f"PAY-{order.pk:06d}-{index:02d}",
                            "failure_code": plan.failure_code,
                        },
                    }
                )
            else:
                out.append(
                    {
                        "occurred_at": initiated_at + dt.timedelta(minutes=2),
                        "event_type": "PAYMENT_PENDING",
                        "description": EVENT_TEMPLATES["PAYMENT_PENDING"].format(**common),
                        "actor_type": "SYSTEM",
                        "metadata": {"reference": f"PAY-{order.pk:06d}-{index:02d}"},
                    }
                )

    def _add_finance_events(self, order: Order, scenario: Scenario, out: list[dict]):
        if not scenario.finance:
            return
        plan = scenario.finance
        partner = order.finance_partner
        amount = indian_currency(order.loan_amount)

        out.append(
            {
                "occurred_at": self._ts(plan.days_ago + 3, "FINANCE"),
                "event_type": "FINANCE_APPLIED",
                "description": EVENT_TEMPLATES["FINANCE_APPLIED"].format(partner=partner),
                "actor_type": "AGENT",
                "actor_name": order.assigned_agent,
                "metadata": {"partner": partner},
            }
        )

        if plan.status == FinanceStatus.DISBURSED:
            out.append(
                {
                    "occurred_at": self._ts(plan.days_ago + 1, "FINANCE"),
                    "event_type": "FINANCE_APPROVED",
                    "description": EVENT_TEMPLATES["FINANCE_APPROVED"].format(
                        partner=partner, amount=amount
                    ),
                    "actor_type": "PARTNER",
                    "metadata": {"partner": partner},
                }
            )

        terminal = {
            FinanceStatus.UNDER_REVIEW: "FINANCE_UNDER_REVIEW",
            FinanceStatus.APPROVED: "FINANCE_APPROVED",
            FinanceStatus.REJECTED: "FINANCE_REJECTED",
            FinanceStatus.DISBURSED: "FINANCE_DISBURSED",
        }.get(plan.status)
        if terminal:
            out.append(
                {
                    "occurred_at": self._ts(plan.days_ago, "FINANCE"),
                    "event_type": terminal,
                    "description": EVENT_TEMPLATES[terminal].format(
                        partner=partner, amount=amount
                    ),
                    "actor_type": "PARTNER",
                    "metadata": {"partner": partner},
                }
            )

    def _add_rc_events(self, order: Order, scenario: Scenario, out: list[dict]):
        plan = scenario.rc
        if plan.applied_days_ago is None:
            return

        out.append(
            {
                "occurred_at": self._ts(plan.applied_days_ago, "RC"),
                "event_type": "RC_TRANSFER_APPLIED",
                "description": EVENT_TEMPLATES["RC_TRANSFER_APPLIED"],
                "actor_type": "PARTNER",
                "metadata": {},
            }
        )

        if plan.status == RCTransferStatus.COMPLETED:
            out.append(
                {
                    "occurred_at": self._ts(
                        max(plan.applied_days_ago - 9, 0), "RC"
                    ),
                    "event_type": "RC_TRANSFER_COMPLETED",
                    "description": EVENT_TEMPLATES["RC_TRANSFER_COMPLETED"],
                    "actor_type": "PARTNER",
                    "metadata": {},
                }
            )
        elif plan.status == RCTransferStatus.REJECTED:
            out.append(
                {
                    "occurred_at": self._ts(
                        max(plan.applied_days_ago - 20, 0), "RC"
                    ),
                    "event_type": "RC_TRANSFER_REJECTED",
                    "description": EVENT_TEMPLATES["RC_TRANSFER_REJECTED"],
                    "actor_type": "PARTNER",
                    "metadata": {},
                }
            )

    def _add_delivery(
        self, order: Order, scenario: Scenario, deliveries: list, out: list[dict]
    ):
        plan = scenario.delivery
        if plan is None:
            return

        scheduled_for = (
            self.now + dt.timedelta(days=plan.scheduled_in_days)
            if plan.scheduled_in_days is not None
            else None
        )
        last_attempt_at = (
            self._ts(plan.last_attempt_days_ago, "DELIVERY")
            if plan.last_attempt_days_ago is not None
            else None
        )
        delivered_at = (
            self._ts(plan.delivered_days_ago, "DELIVERY")
            if plan.delivered_days_ago is not None
            else None
        )
        slot = self.rng.choice(DELIVERY_SLOTS) if scheduled_for else ""

        deliveries.append(
            Delivery(
                order_id=order.pk,
                status=plan.status,
                scheduled_for=scheduled_for,
                slot=slot,
                address_line=f"{self.rng.randint(1, 240)}, "
                f"{self.rng.choice(['Sector', 'Block', 'Phase'])} "
                f"{self.rng.randint(1, 90)}",
                city=order.city,
                pincode=f"{self.rng.randint(110001, 700099)}",
                logistics_partner=self.rng.choice(LOGISTICS_PARTNERS)
                if plan.status != DeliveryStatus.NOT_SCHEDULED
                else "",
                tracking_reference=f"TRK{self.rng.randrange(10**7, 10**8)}"
                if plan.status != DeliveryStatus.NOT_SCHEDULED
                else "",
                attempts=plan.attempts,
                last_attempt_at=last_attempt_at,
                failure_reason=plan.failure_reason,
                delivered_at=delivered_at,
            )
        )

        if scheduled_for:
            out.append(
                {
                    "occurred_at": scheduled_for - dt.timedelta(days=1),
                    "event_type": "DELIVERY_SCHEDULED",
                    "description": EVENT_TEMPLATES["DELIVERY_SCHEDULED"].format(
                        when=scheduled_for.date().isoformat(), slot=slot
                    ),
                    "actor_type": "AGENT",
                    "actor_name": order.assigned_agent,
                    "metadata": {"slot": slot},
                }
            )
        if last_attempt_at and plan.status == DeliveryStatus.ATTEMPT_FAILED:
            out.append(
                {
                    "occurred_at": last_attempt_at,
                    "event_type": "DELIVERY_ATTEMPT_FAILED",
                    "description": EVENT_TEMPLATES["DELIVERY_ATTEMPT_FAILED"].format(
                        reason=plan.failure_reason or "reason not recorded"
                    ),
                    "actor_type": "PARTNER",
                    "metadata": {"attempts": plan.attempts},
                }
            )
        if delivered_at:
            out.append(
                {
                    "occurred_at": delivered_at,
                    "event_type": "DELIVERED",
                    "description": EVENT_TEMPLATES["DELIVERED"],
                    "actor_type": "PARTNER",
                    "metadata": {},
                }
            )
        if plan.status == DeliveryStatus.CANCELLED:
            out.append(
                {
                    "occurred_at": self._ts(
                        scenario.status_path[-1][0], "DELIVERY"
                    ),
                    "event_type": "DELIVERY_CANCELLED",
                    "description": EVENT_TEMPLATES["DELIVERY_CANCELLED"],
                    "actor_type": "SYSTEM",
                    "metadata": {},
                }
            )

    # ------------------------------------------------------------------
    def _reset_order_sequence(self):
        """Explicit pks leave the sequence behind; move it past our ids."""
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT setval(pg_get_serial_sequence('orders', 'id'), "
                "COALESCE((SELECT MAX(id) FROM orders), 1))"
            )

    def _create_users(self):
        User = get_user_model()
        created = []
        for username, role, full_name, team in DEMO_USERS:
            first, _, last = full_name.partition(" ")
            user, was_created = User.objects.get_or_create(
                username=username,
                defaults={
                    "email": f"{username}@cars24.example.com",
                    "first_name": first,
                    "last_name": last,
                    "role": role,
                    "team": team,
                    "employee_id": f"C24{self.rng.randint(1000, 9999)}",
                    "is_staff": role == Role.ADMIN,
                    "is_superuser": role == Role.ADMIN,
                },
            )
            if was_created:
                user.set_password(DEMO_PASSWORD)
                user.save(update_fields=["password"])
                created.append(f"{username} ({role})")

        if created:
            self.stdout.write(
                self.style.SUCCESS(
                    f"Created users: {', '.join(created)} — password '{DEMO_PASSWORD}'"
                )
            )

    def _report(self, assignments):
        counts: dict[str, int] = {}
        for _oid, scenario in assignments:
            counts[scenario.name] = counts.get(scenario.name, 0) + 1

        self.stdout.write(self.style.SUCCESS("\nSeed complete."))
        self.stdout.write(
            f"  customers={Customer.objects.count()} "
            f"vehicles={Vehicle.objects.count()} "
            f"orders={Order.objects.count()} "
            f"payments={Payment.objects.count()} "
            f"documents={OrderDocument.objects.count()} "
            f"deliveries={Delivery.objects.count()} "
            f"events={OrderEvent.objects.count()}"
        )

        self.stdout.write("\nScenario distribution:")
        for name in sorted(counts, key=lambda n: -counts[n]):
            self.stdout.write(f"  {counts[name]:>4}  {name}")

        self.stdout.write("\nPinned demo orders:")
        for oid, name in PINNED_SCENARIOS.items():
            scenario = SCENARIOS_BY_NAME[name]
            exists = Order.objects.filter(pk=oid).exists()
            marker = "" if exists else "  [MISSING]"
            self.stdout.write(f"  #{oid} -> {name}{marker}")
            self.stdout.write(f"        {scenario.intent}")
