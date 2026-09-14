"""
Tests for the demo data seeder.

The property worth protecting is **internal consistency**. The seeder derives
every timeline event from the payment / document / finance / delivery rows it
created, so the timeline can never claim a payment succeeded when the payments
table disagrees. If that breaks, the assistant starts producing confidently
contradictory answers that look like model failures but are bad fixtures.
"""

from __future__ import annotations

import pytest
from django.core.management import call_command

from apps.operations.diagnostics import diagnose
from apps.operations.models import (
    Order,
    OrderEvent,
    Payment,
    PaymentPurpose,
    PaymentStatus,
)
from apps.operations.seed.scenarios import PINNED_SCENARIOS, SCENARIOS

pytestmark = [pytest.mark.django_db, pytest.mark.slow]


@pytest.fixture(scope="class")
def seeded(django_db_setup, django_db_blocker):
    """Seed once for the whole class — the full run is not cheap."""
    with django_db_blocker.unblock():
        call_command("seed_demo_data", "--flush", "--orders", 200, verbosity=0)
        yield
        call_command("seed_demo_data", "--flush", "--orders", 1, verbosity=0)


@pytest.mark.usefixtures("seeded")
class TestSeededData:
    def test_pinned_demo_orders_exist(self):
        """The ids quoted in the product brief must always be present."""
        for order_id in PINNED_SCENARIOS:
            assert Order.objects.filter(pk=order_id).exists(), order_id

    def test_pinned_payment_order_has_a_failed_retry(self):
        order = Order.objects.with_payment_totals().get(pk=1243)
        payments = list(order.payments.all())
        assert any(p.status == PaymentStatus.FAILED for p in payments)
        assert any(p.status == PaymentStatus.SUCCESS for p in payments)
        assert order.amount_due > 0

    def test_pinned_stuck_order_is_genuinely_stuck(self):
        order = Order.objects.with_related().with_payment_totals().get(pk=2325)
        result = diagnose(order)
        assert result["is_stuck"] is True
        # Several interacting blockers, not just one.
        assert result["blocker_count"] >= 4
        assert result["primary_blocker"]["is_symptom"] is False

    def test_every_order_has_a_timeline(self):
        without_events = (
            Order.objects.filter(events__isnull=True).values_list("id", flat=True)
        )
        assert list(without_events) == []

    def test_event_sequences_are_contiguous_per_order(self):
        for order_id in Order.objects.values_list("id", flat=True)[:25]:
            sequences = list(
                OrderEvent.objects.filter(order_id=order_id)
                .order_by("sequence")
                .values_list("sequence", flat=True)
            )
            assert sequences == list(range(1, len(sequences) + 1)), order_id

    def test_events_are_chronological_in_sequence_order(self):
        for order_id in Order.objects.values_list("id", flat=True)[:25]:
            timestamps = list(
                OrderEvent.objects.filter(order_id=order_id)
                .order_by("sequence")
                .values_list("occurred_at", flat=True)
            )
            assert timestamps == sorted(timestamps), order_id

    def test_every_settled_payment_has_a_matching_event(self):
        """
        The consistency property. A payment row without its event would let
        the timeline and the ledger tell different stories.
        """
        settled = Payment.objects.filter(status=PaymentStatus.SUCCESS).exclude(
            purpose=PaymentPurpose.REFUND
        )[:60]
        for payment in settled:
            assert OrderEvent.objects.filter(
                order_id=payment.order_id,
                event_type="PAYMENT_SUCCESS",
                metadata__reference=payment.reference,
            ).exists(), payment.reference

    def test_failed_payments_record_a_reason(self):
        for payment in Payment.objects.filter(status=PaymentStatus.FAILED)[:20]:
            assert payment.failure_reason
            assert payment.failure_code

    def test_amount_paid_never_exceeds_the_order_total_by_accident(self):
        for order in Order.objects.with_payment_totals()[:50]:
            assert order.settled_amount <= order.total_amount * 2

    def test_scenario_coverage_is_broad(self):
        """Every status the rule engine handles should appear in the data."""
        statuses = set(Order.objects.values_list("status", flat=True))
        assert len(statuses) >= 10

    def test_demo_users_are_created(self, django_user_model):
        usernames = set(
            django_user_model.objects.values_list("username", flat=True)
        )
        assert {"viewer", "agent", "manager", "admin"} <= usernames

    def test_order_id_sequence_is_reset_after_explicit_ids(
        self, make_customer, make_vehicle
    ):
        """
        The seeder inserts explicit primary keys, which leaves Postgres'
        sequence behind. Without `setval`, the next *ordinary* insert — one
        that lets the database allocate the id — collides with a seeded row.

        This has to bypass the make_order fixture, which assigns ids itself.
        """
        from django.utils import timezone

        highest = Order.objects.order_by("-id").first().pk
        now = timezone.now()

        order = Order.objects.create(
            customer=make_customer(),
            vehicle=make_vehicle(),
            status="CREATED",
            channel="APP",
            hub="Test Hub",
            city="Gurugram",
            total_amount="100000.00",
            booked_at=now,
            created_at=now,
            status_changed_at=now,
        )
        assert order.pk > highest


class TestSeedDeterminism:
    def test_same_seed_produces_the_same_orders(self):
        """Reproducible demos: the same command yields the same database."""

        def fingerprint():
            return [
                (o.pk, o.status, str(o.total_amount))
                for o in Order.objects.order_by("pk")
            ]

        call_command("seed_demo_data", "--flush", "--orders", 40, "--seed", 7, verbosity=0)
        first = fingerprint()

        call_command("seed_demo_data", "--flush", "--orders", 40, "--seed", 7, verbosity=0)
        second = fingerprint()

        assert first == second
        assert len(first) == 40

    def test_different_seeds_produce_different_data(self):
        call_command("seed_demo_data", "--flush", "--orders", 40, "--seed", 1, verbosity=0)
        first = list(Order.objects.order_by("pk").values_list("status", flat=True))

        call_command("seed_demo_data", "--flush", "--orders", 40, "--seed", 2, verbosity=0)
        second = list(Order.objects.order_by("pk").values_list("status", flat=True))

        assert first != second


class TestScenarioCatalogue:
    def test_every_scenario_documents_its_intent(self):
        for scenario in SCENARIOS:
            assert scenario.intent, scenario.name

    def test_status_path_ends_at_the_declared_status(self):
        for scenario in SCENARIOS:
            assert scenario.status_path[-1][1] == scenario.status, scenario.name

    def test_status_path_runs_backwards_in_time(self):
        """days_ago must decrease: earlier transitions happened longer ago."""
        for scenario in SCENARIOS:
            days = [entry[0] for entry in scenario.status_path]
            assert days == sorted(days, reverse=True), scenario.name

    def test_pinned_scenarios_exist_in_the_catalogue(self):
        names = {s.name for s in SCENARIOS}
        for scenario_name in PINNED_SCENARIOS.values():
            assert scenario_name in names
