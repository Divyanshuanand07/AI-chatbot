"""
Tests for the blocker rule engine.

This is the highest-value test file in the project. The rule engine is what
lets the assistant answer "why is this order stuck?" without the model
inventing a cause, so every rule needs a test that fails when the rule breaks.

Roughly half the tests here assert that an order is **not** stuck. That is
intentional: the dangerous failure mode of a rule engine is not a missed
blocker, it is flagging healthy orders until operators stop reading the output.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from django.utils import timezone

from apps.operations.diagnostics import diagnose
from apps.operations.models import (
    DeliveryStatus,
    DocumentStatus,
    DocumentType,
    FinanceStatus,
    KycStatus,
    Order,
    OrderStatus,
    PaymentMethod,
    PaymentPurpose,
    PaymentStatus,
    RCTransferStatus,
)

pytestmark = pytest.mark.django_db


def reload_order(order: Order) -> Order:
    """Re-fetch with the annotations the diagnosis expects."""
    return Order.objects.with_related().with_payment_totals().get(pk=order.pk)


def codes(result: dict) -> set[str]:
    return {b["code"] for b in result["blockers"]}


# ---------------------------------------------------------------------------
# Must NOT be flagged
# ---------------------------------------------------------------------------
class TestNoFalsePositives:
    def test_healthy_order_is_not_stuck(self, healthy_order):
        result = diagnose(reload_order(healthy_order))
        assert result["is_stuck"] is False
        assert result["primary_blocker"] is None

    def test_brand_new_order_with_no_documents_is_not_stuck(
        self, make_order, make_payment, make_document
    ):
        """A one-day-old order with nothing uploaded is normal, not blocked."""
        order = make_order(
            status=OrderStatus.CREATED, age_days=1, status_age_days=1
        )
        make_payment(order, initiated_days_ago=1)
        for doc_type in (DocumentType.AADHAAR, DocumentType.PAN):
            make_document(order, doc_type=doc_type, status=DocumentStatus.NOT_UPLOADED)

        result = diagnose(reload_order(order))
        assert result["is_stuck"] is False
        # The finding is still reported, just not as a blocker.
        assert "DOCUMENTS_MISSING" in codes(result)
        missing = next(
            b for b in result["blockers"] if b["code"] == "DOCUMENTS_MISSING"
        )
        assert missing["severity"] == "low"
        assert missing["sla_breached"] is False
        assert missing["evidence"]["within_sla"] is True

    def test_delivered_order_is_never_stuck(
        self, make_order, make_payment, make_delivery
    ):
        order = make_order(
            status=OrderStatus.DELIVERED,
            age_days=40,
            status_age_days=20,
            expected_delivery_in_days=-21,
        )
        make_payment(
            order,
            amount=order.total_amount,
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=30,
        )
        make_delivery(order, status=DeliveryStatus.DELIVERED)

        result = diagnose(reload_order(order))
        assert result["is_stuck"] is False
        assert "delivered" in result["verdict"].lower()

    def test_refunded_order_is_never_stuck(self, make_order, make_payment):
        order = make_order(
            status=OrderStatus.REFUNDED, age_days=50, status_age_days=30
        )
        make_payment(order, initiated_days_ago=45)
        make_payment(
            order,
            purpose=PaymentPurpose.REFUND,
            status=PaymentStatus.SUCCESS,
            initiated_days_ago=30,
        )
        result = diagnose(reload_order(order))
        assert result["is_stuck"] is False

    def test_failed_payment_followed_by_success_is_not_a_blocker(
        self, make_order, make_payment, make_document, make_event
    ):
        """A retried failure is resolved history, not an open problem."""
        order = make_order(
            status=OrderStatus.PAYMENT_COMPLETE,
            total_amount=Decimal("500000.00"),
            age_days=10,
            status_age_days=1,
        )
        make_payment(
            order,
            amount=Decimal("500000.00"),
            status=PaymentStatus.FAILED,
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=5,
            failure_reason="Insufficient balance",
        )
        make_payment(
            order,
            amount=Decimal("500000.00"),
            status=PaymentStatus.SUCCESS,
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=2,
        )
        make_document(order, status=DocumentStatus.VERIFIED)
        make_event(order, occurred_days_ago=1)

        result = diagnose(reload_order(order))
        assert "PAYMENT_FAILED_NOT_RETRIED" not in codes(result)
        assert result["is_stuck"] is False


# ---------------------------------------------------------------------------
# Must be flagged
# ---------------------------------------------------------------------------
class TestBlockerDetection:
    def test_multi_blocker_order_leads_with_the_cause_not_the_symptom(
        self, stuck_order
    ):
        """
        The whole point of the cause/symptom ranking.

        This order is also overdue for delivery and silent for 10 days, but
        the actionable root cause is the rejected PAN — and it routes to
        Documentation, not Logistics.
        """
        result = diagnose(reload_order(stuck_order))

        assert result["is_stuck"] is True
        assert result["primary_blocker"]["code"] == "DOCUMENTS_REJECTED"
        assert result["primary_blocker"]["owner_team"] == "Documentation"
        assert result["primary_blocker"]["is_symptom"] is False

        found = codes(result)
        assert {"DOCUMENTS_REJECTED", "FINANCE_REVIEW_OVERDUE", "DELIVERY_OVERDUE"} <= found

        # Symptoms are reported separately so they can be phrased as effects.
        consequence_codes = {c["code"] for c in result["consequences"]}
        assert "DELIVERY_OVERDUE" in consequence_codes
        assert "DOCUMENTS_REJECTED" not in consequence_codes

    def test_rejected_document_carries_the_reason_as_evidence(self, stuck_order):
        result = diagnose(reload_order(stuck_order))
        blocker = next(
            b for b in result["blockers"] if b["code"] == "DOCUMENTS_REJECTED"
        )
        rejected = blocker["evidence"]["rejected_documents"]
        assert rejected[0]["doc_type"] == DocumentType.PAN
        assert "does not match" in rejected[0]["rejection_reason"]

    def test_order_on_hold_is_critical_and_ranks_first(
        self, make_order, make_payment, make_document
    ):
        order = make_order(
            status=OrderStatus.ON_HOLD,
            age_days=30,
            status_age_days=12,
            hold_reason="Customer raised an inspection dispute",
        )
        make_payment(order, initiated_days_ago=28)
        make_document(order, status=DocumentStatus.VERIFIED)

        result = diagnose(reload_order(order))
        assert result["is_stuck"] is True
        assert result["primary_blocker"]["code"] == "ORDER_ON_HOLD"
        assert result["primary_blocker"]["severity"] == "critical"
        assert "inspection dispute" in result["primary_blocker"]["detail"]

    def test_finance_rejection_is_critical(self, make_order, make_payment):
        order = make_order(
            status=OrderStatus.FINANCE_IN_PROGRESS,
            age_days=16,
            status_age_days=12,
            finance_status=FinanceStatus.REJECTED,
            finance_partner="HDFC Bank",
            loan_amount=Decimal("400000.00"),
            finance_updated_at=timezone.now() - dt.timedelta(days=4),
        )
        make_payment(order, initiated_days_ago=15)

        result = diagnose(reload_order(order))
        blocker = next(b for b in result["blockers"] if b["code"] == "FINANCE_REJECTED")
        assert blocker["severity"] == "critical"
        assert blocker["owner_team"] == "Finance"
        assert result["primary_blocker"]["code"] == "FINANCE_REJECTED"

    def test_finance_review_past_sla_is_flagged(self, make_order, make_payment):
        order = make_order(
            status=OrderStatus.FINANCE_IN_PROGRESS,
            age_days=12,
            status_age_days=8,
            finance_status=FinanceStatus.UNDER_REVIEW,
            finance_partner="ICICI Bank",
            finance_updated_at=timezone.now() - dt.timedelta(days=6),
        )
        make_payment(order, initiated_days_ago=11)

        result = diagnose(reload_order(order))
        blocker = next(
            b for b in result["blockers"] if b["code"] == "FINANCE_REVIEW_OVERDUE"
        )
        assert blocker["evidence"]["days_under_review"] >= 6
        assert blocker["evidence"]["sla_days"] == 3

    def test_finance_review_within_sla_is_not_flagged(self, make_order, make_payment):
        order = make_order(
            status=OrderStatus.FINANCE_IN_PROGRESS,
            age_days=3,
            status_age_days=1,
            finance_status=FinanceStatus.UNDER_REVIEW,
            finance_updated_at=timezone.now() - dt.timedelta(days=1),
        )
        make_payment(order, initiated_days_ago=2)
        result = diagnose(reload_order(order))
        assert "FINANCE_REVIEW_OVERDUE" not in codes(result)

    def test_payment_shortfall_against_paid_status_is_critical(
        self, make_order, make_payment, make_document, make_event
    ):
        """Status says paid, ledger disagrees — never release the vehicle."""
        order = make_order(
            status=OrderStatus.READY_FOR_DELIVERY,
            total_amount=Decimal("600000.00"),
            age_days=20,
            status_age_days=1,
        )
        make_payment(order, amount=Decimal("100000.00"), initiated_days_ago=10)
        make_document(order, status=DocumentStatus.VERIFIED)
        make_event(order, occurred_days_ago=1)

        result = diagnose(reload_order(order))
        blocker = next(
            b for b in result["blockers"] if b["code"] == "PAYMENT_SHORTFALL_VS_STATUS"
        )
        assert blocker["severity"] == "critical"
        assert blocker["evidence"]["amount_due"]["value"] == 500000.0
        assert "do not release" in blocker["suggested_action"].lower()

    def test_failed_payment_without_retry_is_flagged_with_reason(
        self, make_order, make_payment, make_document
    ):
        order = make_order(
            status=OrderStatus.PAYMENT_PARTIAL,
            total_amount=Decimal("500000.00"),
            age_days=9,
            status_age_days=6,
        )
        make_payment(order, amount=Decimal("20000.00"), initiated_days_ago=8)
        make_payment(
            order,
            amount=Decimal("480000.00"),
            status=PaymentStatus.FAILED,
            purpose=PaymentPurpose.FULL_PAYMENT,
            method=PaymentMethod.NETBANKING,
            initiated_days_ago=3,
            failure_code="INSUFFICIENT_FUNDS",
            failure_reason="Insufficient balance in the debit account",
        )
        make_document(order, status=DocumentStatus.VERIFIED)

        result = diagnose(reload_order(order))
        blocker = next(
            b for b in result["blockers"] if b["code"] == "PAYMENT_FAILED_NOT_RETRIED"
        )
        assert blocker["evidence"]["failure_code"] == "INSUFFICIENT_FUNDS"
        assert blocker["owner_team"] == "Payments"

    def test_payment_stuck_in_flight_warns_against_double_collection(
        self, make_order, make_payment
    ):
        order = make_order(
            status=OrderStatus.PAYMENT_PENDING,
            total_amount=Decimal("500000.00"),
            age_days=6,
            status_age_days=4,
        )
        make_payment(
            order,
            amount=Decimal("480000.00"),
            status=PaymentStatus.PENDING,
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=3,
        )
        result = diagnose(reload_order(order))
        blocker = next(
            b for b in result["blockers"] if b["code"] == "PAYMENT_STUCK_IN_FLIGHT"
        )
        # The money may already be debited — this wording protects the customer.
        assert "already be debited" in blocker["suggested_action"]

    def test_rc_transfer_past_sla_is_flagged(
        self, make_order, make_payment, make_document, make_event
    ):
        order = make_order(
            status=OrderStatus.RC_TRANSFER_PENDING,
            total_amount=Decimal("500000.00"),
            age_days=44,
            status_age_days=38,
        )
        order.vehicle.rc_transfer_status = RCTransferStatus.IN_PROGRESS
        order.vehicle.rc_transfer_applied_on = timezone.localdate() - dt.timedelta(
            days=37
        )
        order.vehicle.save()
        make_payment(
            order,
            amount=Decimal("500000.00"),
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=40,
        )
        make_document(order, status=DocumentStatus.VERIFIED)
        make_event(order, occurred_days_ago=1)

        result = diagnose(reload_order(order))
        blocker = next(
            b for b in result["blockers"] if b["code"] == "RC_TRANSFER_OVERDUE"
        )
        assert blocker["evidence"]["days_elapsed"] >= 21
        assert blocker["owner_team"] == "RTO / Documentation"

    def test_rc_transfer_never_filed_is_an_internal_miss(
        self, make_order, make_payment, make_document, make_event
    ):
        order = make_order(
            status=OrderStatus.RC_TRANSFER_PENDING,
            total_amount=Decimal("500000.00"),
            age_days=25,
            status_age_days=10,
        )
        make_payment(
            order,
            amount=Decimal("500000.00"),
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=20,
        )
        make_document(order, status=DocumentStatus.VERIFIED)
        make_event(order, occurred_days_ago=1)

        result = diagnose(reload_order(order))
        assert "RC_TRANSFER_NOT_INITIATED" in codes(result)

    def test_exhausted_delivery_attempts_are_flagged(
        self, make_order, make_payment, make_document, make_delivery, make_event
    ):
        order = make_order(
            status=OrderStatus.OUT_FOR_DELIVERY,
            total_amount=Decimal("500000.00"),
            age_days=25,
            status_age_days=6,
            expected_delivery_in_days=-5,
        )
        make_payment(
            order,
            amount=Decimal("500000.00"),
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=20,
        )
        make_document(order, status=DocumentStatus.VERIFIED)
        make_delivery(
            order,
            status=DeliveryStatus.ATTEMPT_FAILED,
            attempts=3,
            last_attempt_at=timezone.now() - dt.timedelta(days=2),
            failure_reason="Customer unreachable at the delivery address",
        )
        make_event(order, occurred_days_ago=2)

        result = diagnose(reload_order(order))
        blocker = next(
            b
            for b in result["blockers"]
            if b["code"] == "DELIVERY_ATTEMPTS_EXHAUSTED"
        )
        assert blocker["evidence"]["attempts"] == 3
        assert blocker["owner_team"] == "Logistics"

    def test_ready_order_with_no_slot_booked_is_a_process_gap(
        self, make_order, make_payment, make_document, make_delivery, make_event
    ):
        order = make_order(
            status=OrderStatus.READY_FOR_DELIVERY,
            total_amount=Decimal("500000.00"),
            age_days=20,
            status_age_days=6,
            expected_delivery_in_days=4,
        )
        order.vehicle.rc_transfer_status = RCTransferStatus.COMPLETED
        order.vehicle.save()
        make_payment(
            order,
            amount=Decimal("500000.00"),
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=16,
        )
        make_document(order, status=DocumentStatus.VERIFIED)
        make_delivery(order, status=DeliveryStatus.NOT_SCHEDULED)
        make_event(order, occurred_days_ago=1)

        result = diagnose(reload_order(order))
        assert "DELIVERY_NOT_SCHEDULED" in codes(result)

    def test_refund_past_sla_is_flagged(self, make_order, make_payment):
        order = make_order(
            status=OrderStatus.REFUND_PENDING,
            age_days=28,
            status_age_days=13,
            expected_delivery_in_days=None,
        )
        make_payment(order, amount=Decimal("50000.00"), initiated_days_ago=25)

        result = diagnose(reload_order(order))
        blocker = next(b for b in result["blockers"] if b["code"] == "REFUND_PENDING")
        assert blocker["evidence"]["days_pending"] >= 7
        assert blocker["owner_team"] == "Finance"
        assert blocker["severity"] == "high"

    def test_rejected_kyc_blocks_the_order(
        self, make_order, make_customer, make_payment
    ):
        customer = make_customer(kyc_status=KycStatus.REJECTED)
        order = make_order(
            customer=customer,
            status=OrderStatus.DOCS_PENDING,
            age_days=12,
            status_age_days=11,
        )
        make_payment(order, initiated_days_ago=11)

        result = diagnose(reload_order(order))
        assert "KYC_REJECTED" in codes(result)
        assert result["is_stuck"] is True

    def test_silent_order_is_flagged_as_a_symptom(
        self, make_order, make_payment, make_document, make_event
    ):
        order = make_order(
            status=OrderStatus.RC_TRANSFER_PENDING,
            total_amount=Decimal("500000.00"),
            age_days=30,
            status_age_days=9,
        )
        order.vehicle.rc_transfer_status = RCTransferStatus.IN_PROGRESS
        order.vehicle.rc_transfer_applied_on = timezone.localdate() - dt.timedelta(days=5)
        order.vehicle.save()
        make_payment(
            order,
            amount=Decimal("500000.00"),
            purpose=PaymentPurpose.FULL_PAYMENT,
            initiated_days_ago=25,
        )
        make_document(order, status=DocumentStatus.VERIFIED)
        make_event(order, occurred_days_ago=9)

        result = diagnose(reload_order(order))
        blocker = next(
            b for b in result["blockers"] if b["code"] == "NO_RECENT_ACTIVITY"
        )
        assert blocker["is_symptom"] is True
        assert blocker["evidence"]["days_since_activity"] >= 3


# ---------------------------------------------------------------------------
# Output contract
# ---------------------------------------------------------------------------
class TestDiagnosisShape:
    def test_healthy_signals_tell_the_operator_what_is_fine(self, stuck_order):
        result = diagnose(reload_order(stuck_order))
        assert any("KYC" in signal for signal in result["healthy_signals"])

    def test_every_blocker_is_actionable(self, stuck_order):
        """A finding with no owner or no next action is not useful to anyone."""
        result = diagnose(reload_order(stuck_order))
        assert result["blockers"]
        for blocker in result["blockers"]:
            assert blocker["owner_team"], blocker["code"]
            assert blocker["suggested_action"], blocker["code"]
            assert blocker["severity"] in {
                "critical",
                "high",
                "medium",
                "low",
                "info",
            }
            assert blocker["detail"]

    def test_blockers_are_ordered_by_severity_then_cause(self, stuck_order):
        from apps.operations.diagnostics import CAUSE_RANK, SEVERITY_ORDER

        result = diagnose(reload_order(stuck_order))
        keys = [
            (
                SEVERITY_ORDER[b["severity"]],
                CAUSE_RANK.get(b["code"], 50),
            )
            for b in result["blockers"]
        ]
        assert keys == sorted(keys)

    def test_diagnosis_is_deterministic(self, stuck_order):
        """Same order, same verdict — reproducibility is the selling point."""
        first = diagnose(reload_order(stuck_order))
        second = diagnose(reload_order(stuck_order))
        assert codes(first) == codes(second)
        assert first["primary_blocker"]["code"] == second["primary_blocker"]["code"]
        assert first["verdict"] == second["verdict"]
