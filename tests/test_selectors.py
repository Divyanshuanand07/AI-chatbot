"""
Tests for the read layer: entity parsing, money formatting, masking and search.

Formatting gets real attention here because the assistant quotes these strings
verbatim. Indian digit grouping is the specific risk: models reliably render
₹8,52,500 as ₹852,500, so the grouping is computed in Python and asserted
here rather than trusted to a prompt instruction.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from apps.common.exceptions import NotFoundError, ValidationError
from apps.operations import selectors
from apps.operations.formatting import (
    indian_currency,
    mask_email,
    mask_phone,
    money,
)
from apps.operations.models import DocumentStatus, DocumentType, OrderStatus


# ---------------------------------------------------------------------------
class TestIndianCurrency:
    """2-2-3 grouping: 1,00,000 not 100,000."""

    @pytest.mark.parametrize(
        "value,expected",
        [
            (0, "₹0.00"),
            (999, "₹999.00"),
            (1000, "₹1,000.00"),
            (99999, "₹99,999.00"),
            (100000, "₹1,00,000.00"),
            (852500, "₹8,52,500.00"),
            (1128000, "₹11,28,000.00"),
            (10000000, "₹1,00,00,000.00"),
            (Decimal("647900.50"), "₹6,47,900.50"),
        ],
    )
    def test_grouping(self, value, expected):
        assert indian_currency(value) == expected

    def test_negative_amounts(self):
        assert indian_currency(Decimal("-5000")) == "-₹5,000.00"

    def test_none_renders_as_a_dash_not_zero(self):
        """Unknown and zero are different facts; conflating them misleads."""
        assert indian_currency(None) == "—"

    def test_money_carries_both_forms(self):
        result = money(Decimal("852500"))
        assert result == {"value": 852500.0, "display": "₹8,52,500.00"}


# ---------------------------------------------------------------------------
class TestMasking:
    def test_phone_keeps_only_the_last_four_digits(self):
        assert mask_phone("9815089056") == "******9056"

    def test_short_input_is_fully_masked(self):
        assert mask_phone("12") == "****"

    def test_email_keeps_the_domain_for_recognisability(self):
        masked = mask_email("pranav.pillai@example.com")
        assert masked.endswith("@example.com")
        assert "pranav" not in masked


# ---------------------------------------------------------------------------
class TestOrderIdParsing:
    @pytest.mark.parametrize("raw,expected", [(1243, 1243), ("1243", 1243), ("#1243", 1243), (" #1243 ", 1243)])
    def test_accepts_the_forms_operators_and_models_produce(self, raw, expected):
        assert selectors.parse_order_id(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "", None, "12.5", "-4", 0, True])
    def test_rejects_everything_else(self, raw):
        with pytest.raises(ValidationError):
            selectors.parse_order_id(raw)

    def test_error_message_is_usable_by_the_model(self):
        with pytest.raises(ValidationError) as exc:
            selectors.parse_order_id("ORD-99")
        assert "1243" in exc.value.message  # shows the expected shape


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestOrderLookup:
    def test_missing_order_raises_with_the_id_in_the_message(self):
        with pytest.raises(NotFoundError) as exc:
            selectors.get_order(987654)
        assert "987654" in exc.value.message
        assert exc.value.details["order_id"] == 987654

    def test_amount_due_never_goes_negative(self, make_order, make_payment):
        """An overpayment is a finance issue, not a negative balance."""
        order = make_order(total_amount=Decimal("100000.00"))
        make_payment(order, amount=Decimal("150000.00"))
        reloaded = selectors.get_order(order.pk)
        assert reloaded.amount_due == Decimal("0.00")

    def test_only_settled_payments_count_towards_paid(
        self, make_order, make_payment
    ):
        from apps.operations.models import PaymentStatus

        order = make_order(total_amount=Decimal("100000.00"))
        make_payment(order, amount=Decimal("40000.00"), status=PaymentStatus.SUCCESS)
        make_payment(order, amount=Decimal("60000.00"), status=PaymentStatus.PENDING)
        make_payment(order, amount=Decimal("60000.00"), status=PaymentStatus.FAILED)

        data = selectors.payment_status(selectors.get_order(order.pk))
        assert data["amount_paid"]["value"] == 40000.0
        assert data["amount_due"]["value"] == 60000.0

    def test_refunds_are_excluded_from_amount_paid(self, make_order, make_payment):
        from apps.operations.models import PaymentPurpose

        order = make_order(total_amount=Decimal("100000.00"))
        make_payment(order, amount=Decimal("50000.00"))
        make_payment(
            order, amount=Decimal("50000.00"), purpose=PaymentPurpose.REFUND
        )
        data = selectors.payment_status(selectors.get_order(order.pk))
        assert data["amount_paid"]["value"] == 50000.0
        assert data["amount_refunded"]["value"] == 50000.0

    def test_timeline_limit_is_clamped(self, make_order, make_event):
        order = make_order()
        for i in range(5):
            make_event(order, occurred_days_ago=5 - i)
        data = selectors.timeline(selectors.get_order(order.pk), limit=9999)
        assert data["event_count_returned"] <= 50

    def test_documents_flag_outstanding_items(
        self, make_order, make_document
    ):
        order = make_order()
        make_document(order, doc_type=DocumentType.AADHAAR, status=DocumentStatus.VERIFIED)
        make_document(
            order, doc_type=DocumentType.PAN, status=DocumentStatus.NOT_UPLOADED
        )
        data = selectors.documents(selectors.get_order(order.pk))
        assert data["all_mandatory_verified"] is False
        assert data["pending_count"] == 1


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestFindOrders:
    def test_requires_at_least_one_filter(self):
        with pytest.raises(ValidationError):
            selectors.find_orders()

    def test_phone_matches_with_formatting_stripped(self, make_order, make_customer):
        customer = make_customer(phone="9876543210")
        order = make_order(customer=customer)
        result = selectors.find_orders(phone="+91 98765-43210")
        assert result["orders"][0]["order_id"] == order.pk

    @pytest.mark.parametrize("partial", ["12", "543210", "76543210"])
    def test_partial_phone_numbers_are_rejected(self, partial):
        """
        A suffix can match several customers, and showing an operator the
        wrong person's order is worse than asking for the full number.
        """
        with pytest.raises(ValidationError, match="10 digits"):
            selectors.find_orders(phone=partial)

    def test_registration_is_normalised(self, make_order, make_vehicle):
        vehicle = make_vehicle(registration_number="HR26AB1234")
        order = make_order(vehicle=vehicle)
        result = selectors.find_orders(registration_number="hr 26 ab 1234")
        assert result["orders"][0]["order_id"] == order.pk

    def test_unknown_status_lists_the_valid_ones(self):
        with pytest.raises(ValidationError) as exc:
            selectors.find_orders(status="NOT_A_STATUS")
        assert OrderStatus.DELIVERED in exc.value.details["valid_statuses"]

    def test_results_are_capped(self, make_order, make_customer):
        customer = make_customer(phone="9111111111")
        for _ in range(6):
            make_order(customer=customer)
        result = selectors.find_orders(phone="9111111111", limit=3)
        assert result["returned"] == 3
        assert result["match_count"] == 6
        assert result["truncated"] is True


# ---------------------------------------------------------------------------
@pytest.mark.django_db
class TestPiiBoundary:
    def test_include_pii_false_masks_contact_fields(self, healthy_order):
        order = selectors.get_order(healthy_order.pk)
        masked = selectors.customer_info(order, include_pii=False)
        unmasked = selectors.customer_info(order, include_pii=True)

        assert masked["phone"] != unmasked["phone"]
        assert "*" in masked["phone"]
        # The payload explains the masking so the model can too.
        assert "pii_note" in masked
        assert "pii_note" not in unmasked
