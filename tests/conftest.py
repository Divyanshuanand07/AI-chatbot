"""
Shared fixtures.

Tests build the exact order state each assertion needs rather than running the
demo seeder. That is deliberate: a diagnostics test should fail when the *rule*
breaks, not when someone retunes a seed scenario. The seeder is exercised by
its own test instead.

No test touches a real model provider — `config.settings.test` pins
`LLM_PROVIDER=stub`, so the whole suite runs offline and free.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.permissions import Role
from apps.operations.models import (
    Customer,
    Delivery,
    DeliveryStatus,
    DocumentStatus,
    DocumentType,
    FinanceStatus,
    KycStatus,
    Order,
    OrderDocument,
    OrderEvent,
    OrderStatus,
    OrderType,
    Payment,
    PaymentMethod,
    PaymentPurpose,
    PaymentStatus,
    RCTransferStatus,
    Vehicle,
)


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
@pytest.fixture
def now():
    return timezone.now()


def days_ago(n: float):
    return timezone.now() - dt.timedelta(days=n)


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
@pytest.fixture
def make_user(db, django_user_model):
    counter = {"n": 0}

    def _make(role=Role.OPS_AGENT, **kwargs):
        counter["n"] += 1
        defaults = {
            "username": kwargs.pop("username", f"user{counter['n']}"),
            "role": role,
            "team": "Order Operations",
            "employee_id": f"C24{1000 + counter['n']}",
        }
        defaults.update(kwargs)
        user = django_user_model.objects.create(**defaults)
        user.set_password("testpass123")
        user.save()
        return user

    return _make


@pytest.fixture
def viewer(make_user):
    return make_user(Role.VIEWER, username="v_viewer")


@pytest.fixture
def agent(make_user):
    return make_user(Role.OPS_AGENT, username="v_agent")


@pytest.fixture
def manager(make_user):
    return make_user(Role.OPS_MANAGER, username="v_manager")


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def auth_client(api_client):
    """Client authenticated as a given user, bypassing the login round trip."""

    def _auth(user):
        api_client.force_authenticate(user=user)
        return api_client

    return _auth


# ---------------------------------------------------------------------------
# Operational data
# ---------------------------------------------------------------------------
@pytest.fixture
def make_customer(db):
    counter = {"n": 0}

    def _make(**kwargs):
        counter["n"] += 1
        defaults = {
            "code": f"CUS{9000 + counter['n']}",
            "full_name": "Test Customer",
            "phone": f"98000000{counter['n']:02d}",
            "email": f"test{counter['n']}@example.com",
            "city": "Gurugram",
            "state": "Haryana",
            "kyc_status": KycStatus.VERIFIED,
            "customer_since": timezone.localdate() - dt.timedelta(days=400),
        }
        defaults.update(kwargs)
        return Customer.objects.create(**defaults)

    return _make


@pytest.fixture
def make_vehicle(db):
    counter = {"n": 0}

    def _make(**kwargs):
        counter["n"] += 1
        defaults = {
            "registration_number": f"HR26TT{1000 + counter['n']}",
            "make": "Maruti Suzuki",
            "model": "Swift",
            "variant": "VXI",
            "year": 2021,
            "fuel_type": "Petrol",
            "transmission": "Manual",
            "km_driven": 32000,
            "colour": "White",
            "owner_count": 1,
            "inspection_score": Decimal("8.4"),
            "listing_price": Decimal("500000.00"),
            "hub": "Gurugram Sector 48 Hub",
            "rc_transfer_status": RCTransferStatus.NOT_STARTED,
        }
        defaults.update(kwargs)
        return Vehicle.objects.create(**defaults)

    return _make


@pytest.fixture
def make_order(make_customer, make_vehicle):
    """
    Create an order with sensible defaults.

    `status_age_days` and `age_days` are separate arguments because most
    blocker rules key off how long the order has sat in its *current* status,
    which is not the same as how old the order is.

    Ids are assigned explicitly from 1200 up, mirroring real Cars24 order
    numbers. This is not cosmetic: entity extraction requires at least three
    digits (so a "30" in "last 30 days" is not mistaken for an order), and a
    default sequence PK of 7 would silently fail to be recognised in a
    question — a test artefact that has nothing to do with production.
    """
    counter = {"n": 1200}

    def _make(
        *,
        order_id=None,
        status=OrderStatus.PAYMENT_PENDING,
        total_amount=Decimal("512500.00"),
        age_days=10,
        status_age_days=2,
        customer=None,
        vehicle=None,
        expected_delivery_in_days=5,
        **kwargs,
    ):
        customer = customer or make_customer()
        vehicle = vehicle or make_vehicle()
        created = days_ago(age_days)
        defaults = {
            "customer": customer,
            "vehicle": vehicle,
            "order_type": OrderType.BUY,
            "status": status,
            "channel": "APP",
            "hub": vehicle.hub,
            "city": customer.city,
            "total_amount": total_amount,
            "booking_amount": Decimal("21000.00"),
            "finance_status": FinanceStatus.NOT_APPLICABLE,
            "assigned_agent": "Test Agent",
            "booked_at": created,
            "created_at": created,
            "status_changed_at": days_ago(status_age_days),
            "expected_delivery_date": (
                timezone.localdate() + dt.timedelta(days=expected_delivery_in_days)
                if expected_delivery_in_days is not None
                else None
            ),
        }
        defaults.update(kwargs)
        if order_id is None:
            counter["n"] += 1
            order_id = counter["n"]
        defaults["id"] = order_id
        return Order.objects.create(**defaults)

    return _make


@pytest.fixture
def make_payment(db):
    counter = {"n": 0}

    def _make(
        order,
        *,
        amount=Decimal("21000.00"),
        status=PaymentStatus.SUCCESS,
        purpose=PaymentPurpose.TOKEN,
        method=PaymentMethod.UPI,
        initiated_days_ago=5,
        **kwargs,
    ):
        counter["n"] += 1
        initiated = days_ago(initiated_days_ago)
        defaults = {
            "order": order,
            "reference": f"PAY-TEST-{counter['n']:04d}",
            "amount": amount,
            "status": status,
            "purpose": purpose,
            "method": method,
            "gateway": "Razorpay",
            "initiated_at": initiated,
            "completed_at": (
                initiated + dt.timedelta(minutes=5)
                if status in (PaymentStatus.SUCCESS, PaymentStatus.FAILED)
                else None
            ),
        }
        defaults.update(kwargs)
        return Payment.objects.create(**defaults)

    return _make


@pytest.fixture
def make_document(db):
    def _make(
        order,
        *,
        doc_type=DocumentType.AADHAAR,
        status=DocumentStatus.VERIFIED,
        uploaded_days_ago=6,
        **kwargs,
    ):
        defaults = {
            "order": order,
            "doc_type": doc_type,
            "status": status,
            "is_mandatory": True,
            "uploaded_at": (
                days_ago(uploaded_days_ago)
                if status != DocumentStatus.NOT_UPLOADED
                else None
            ),
            "verified_at": (
                days_ago(uploaded_days_ago - 1)
                if status == DocumentStatus.VERIFIED
                else None
            ),
        }
        defaults.update(kwargs)
        return OrderDocument.objects.create(**defaults)

    return _make


@pytest.fixture
def make_delivery(db):
    def _make(order, *, status=DeliveryStatus.NOT_SCHEDULED, **kwargs):
        defaults = {
            "order": order,
            "status": status,
            "city": order.city,
            "pincode": "122018",
            "attempts": 0,
        }
        defaults.update(kwargs)
        return Delivery.objects.create(**defaults)

    return _make


@pytest.fixture
def make_event(db):
    counters: dict[int, int] = {}

    def _make(order, *, event_type="STATUS_CHANGED", occurred_days_ago=1, **kwargs):
        counters[order.pk] = counters.get(order.pk, 0) + 1
        defaults = {
            "order": order,
            "sequence": counters[order.pk],
            "event_type": event_type,
            "description": kwargs.pop("description", f"{event_type} happened"),
            "actor_type": "SYSTEM",
            "occurred_at": days_ago(occurred_days_ago),
        }
        defaults.update(kwargs)
        return OrderEvent.objects.create(**defaults)

    return _make


# ---------------------------------------------------------------------------
# Composite fixtures for the common scenarios
# ---------------------------------------------------------------------------
@pytest.fixture
def healthy_order(make_order, make_payment, make_document, make_delivery, make_event):
    """Fully paid, documents verified, delivery scheduled — must never be 'stuck'."""
    order = make_order(
        status=OrderStatus.READY_FOR_DELIVERY,
        total_amount=Decimal("500000.00"),
        age_days=12,
        status_age_days=1,
        expected_delivery_in_days=3,
    )
    order.vehicle.rc_transfer_status = RCTransferStatus.COMPLETED
    order.vehicle.rc_transfer_applied_on = timezone.localdate() - dt.timedelta(days=10)
    order.vehicle.save()

    make_payment(order, amount=Decimal("500000.00"), purpose=PaymentPurpose.FULL_PAYMENT)
    for doc_type in (DocumentType.AADHAAR, DocumentType.PAN, DocumentType.ADDRESS_PROOF):
        make_document(order, doc_type=doc_type, status=DocumentStatus.VERIFIED)
    make_delivery(
        order,
        status=DeliveryStatus.SCHEDULED,
        scheduled_for=timezone.now() + dt.timedelta(days=2),
        slot="10:00 - 12:00",
        logistics_partner="Cars24 Logistics",
    )
    make_event(order, event_type="STATUS_CHANGED", occurred_days_ago=1)
    return order


@pytest.fixture
def stuck_order(make_order, make_payment, make_document, make_delivery, make_event):
    """Rejected document plus a stalled loan review — a multi-blocker order."""
    order = make_order(
        status=OrderStatus.FINANCE_IN_PROGRESS,
        total_amount=Decimal("800000.00"),
        age_days=26,
        status_age_days=22,
        expected_delivery_in_days=-6,
        finance_status=FinanceStatus.UNDER_REVIEW,
        finance_partner="Axis Bank",
        loan_amount=Decimal("600000.00"),
        finance_updated_at=days_ago(11),
    )
    make_payment(order, amount=Decimal("32000.00"), initiated_days_ago=24)
    make_document(order, doc_type=DocumentType.AADHAAR, status=DocumentStatus.VERIFIED)
    make_document(
        order,
        doc_type=DocumentType.PAN,
        status=DocumentStatus.REJECTED,
        uploaded_days_ago=10,
        rejection_reason="Name on PAN does not match Aadhaar records",
    )
    make_document(
        order,
        doc_type=DocumentType.SALARY_SLIP,
        status=DocumentStatus.NOT_UPLOADED,
    )
    make_delivery(order, status=DeliveryStatus.NOT_SCHEDULED)
    make_event(order, event_type="DOCUMENT_REJECTED", occurred_days_ago=10)
    return order
