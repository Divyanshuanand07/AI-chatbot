"""
Tests for the tool layer: registry, scope filtering, and the executor.

The security property being asserted here is that the assistant can never be
a way around the permission model. Two independent mechanisms enforce it —
tools are withheld from the tool list, *and* the executor re-checks on every
call — and both are tested, because defence in depth is only depth if the
second layer actually works when the first is bypassed.
"""

from __future__ import annotations

import pytest
from django.test import override_settings

from apps.accounts.permissions import Role
from apps.assistant.tools import ToolContext, ToolExecutor, ToolResult, registry
from apps.operations.models import OrderStatus

pytestmark = pytest.mark.django_db


@pytest.fixture
def ctx(agent):
    return ToolContext(user=agent, trace_id="trace-test")


@pytest.fixture
def executor(ctx):
    return ToolExecutor(ctx)


# ---------------------------------------------------------------------------
class TestRegistry:
    def test_every_expected_tool_is_registered(self):
        names = set(registry.names())
        # The six tools named in the brief, plus the composite, the rule
        # engine, entity resolution, documents and the knowledge base.
        assert {
            "get_order",
            "get_payment_status",
            "get_customer",
            "get_vehicle",
            "get_order_timeline",
            "get_delivery_status",
            "get_order_summary",
            "diagnose_order_blockers",
            "find_orders",
            "get_order_documents",
            "get_finance_status",
            "search_knowledge_base",
        } <= names

    def test_definitions_are_sorted_for_prompt_cache_stability(self, agent):
        """
        Anthropic caching is a prefix match over tools -> system -> messages,
        so an unstable tool order silently destroys the cache hit rate.
        """
        names = [d["name"] for d in registry.definitions_for(agent.scopes)]
        assert names == sorted(names)

    def test_definitions_are_byte_identical_across_calls(self, agent):
        first = registry.definitions_for(agent.scopes)
        second = registry.definitions_for(agent.scopes)
        assert first == second

    def test_every_tool_uses_strict_schemas(self, agent):
        """strict + additionalProperties:false guarantees valid arguments."""
        for definition in registry.definitions_for(agent.scopes):
            assert definition["strict"] is True, definition["name"]
            schema = definition["input_schema"]
            assert schema["additionalProperties"] is False, definition["name"]
            # Under strict mode every property must be declared required;
            # optional arguments are expressed as nullable types instead.
            assert set(schema["required"]) == set(schema["properties"]), (
                definition["name"]
            )

    def test_every_tool_has_a_substantive_description(self, agent):
        """The description is the model's only tool-selection signal."""
        for definition in registry.definitions_for(agent.scopes):
            assert len(definition["description"]) > 80, definition["name"]


# ---------------------------------------------------------------------------
class TestScopeFiltering:
    def test_viewer_cannot_see_privileged_tools(self, viewer, agent):
        viewer_tools = {t.name for t in registry.available_to(viewer.scopes)}
        agent_tools = {t.name for t in registry.available_to(agent.scopes)}

        assert "diagnose_order_blockers" not in viewer_tools
        assert "get_finance_status" not in viewer_tools
        assert "diagnose_order_blockers" in agent_tools
        assert viewer_tools < agent_tools

    def test_admin_sees_every_tool(self, make_user):
        admin = make_user(Role.ADMIN, username="v_admin")
        assert len(registry.available_to(admin.scopes)) == len(registry.all())

    def test_executor_rejects_a_tool_the_role_cannot_use(self, viewer, stuck_order):
        """
        Defence in depth.

        The viewer's model is never offered this tool, so reaching here means
        a replayed conversation or a client bug. It must still be refused.
        """
        executor = ToolExecutor(ToolContext(user=viewer, trace_id="t"))
        invocation = executor.execute(
            "diagnose_order_blockers", {"order_id": stuck_order.pk}
        )
        assert invocation.ok is False
        assert invocation.result.error_code == "FORBIDDEN"
        assert "diagnostics:read" in invocation.result.error_details["missing_scopes"]

    def test_pii_masking_follows_the_caller_not_the_request(
        self, viewer, agent, healthy_order
    ):
        """The same tool and arguments must return less to a lesser role."""
        full = ToolExecutor(ToolContext(user=agent, trace_id="t")).execute(
            "get_customer", {"order_id": healthy_order.pk}
        )
        masked = ToolExecutor(ToolContext(user=viewer, trace_id="t")).execute(
            "get_customer", {"order_id": healthy_order.pk}
        )

        assert full.result.data["pii_visible"] is True
        assert masked.result.data["pii_visible"] is False
        assert "*" in masked.result.data["phone"]
        assert masked.result.data["phone"] != full.result.data["phone"]
        # The masked payload tells the model *why*, so it can explain itself.
        assert "pii_note" in masked.result.data


# ---------------------------------------------------------------------------
class TestExecutorErrorHandling:
    def test_missing_order_returns_a_structured_failure(self, executor):
        invocation = executor.execute("get_order", {"order_id": 999999})
        assert invocation.ok is False
        assert invocation.result.error_code == "ORDER_NOT_FOUND"
        # The message must be usable verbatim by the model.
        assert "999999" in invocation.result.error_message

    def test_malformed_order_id_is_distinguished_from_a_missing_one(self, executor):
        """
        Different codes because they need different responses: one means
        "say it doesn't exist", the other means "ask me to confirm the id".
        """
        invocation = executor.execute("get_order", {"order_id": "not-a-number"})
        assert invocation.result.error_code == "INVALID_ORDER_ID"

    def test_unknown_tool_lists_the_real_ones(self, executor):
        invocation = executor.execute("get_horoscope", {"order_id": 1})
        assert invocation.result.error_code == "UNKNOWN_TOOL"
        assert "get_order" in invocation.result.error_details["available_tools"]

    def test_bad_arguments_do_not_raise(self, executor, healthy_order):
        invocation = executor.execute(
            "get_order", {"order_id": healthy_order.pk, "nonsense": True}
        )
        assert invocation.ok is False
        assert invocation.result.error_code == "BAD_ARGUMENTS"

    def test_internal_errors_are_contained(self, executor, monkeypatch):
        """An unexpected exception must reach the model as a failure, not a 500."""

        def boom(*args, **kwargs):
            raise RuntimeError("database on fire")

        monkeypatch.setattr(
            "apps.assistant.tools.catalog.selectors.order_core", boom
        )
        invocation = executor.execute("get_order", {"order_id": 1})
        assert invocation.ok is False
        assert invocation.result.error_code in ("INTERNAL_ERROR", "ORDER_NOT_FOUND")

    def test_find_orders_requires_at_least_one_filter(self, executor):
        """An unfiltered dump of the order table is never a useful answer."""
        invocation = executor.execute(
            "find_orders",
            {
                "phone": None,
                "registration_number": None,
                "customer_name": None,
                "status": None,
                "hub": None,
                "limit": None,
            },
        )
        assert invocation.result.error_code == "INVALID_SEARCH"

    def test_error_payload_marks_failure_explicitly(self, executor):
        invocation = executor.execute("get_order", {"order_id": 999999})
        payload = invocation.result.to_model_payload()
        assert payload["ok"] is False
        assert payload["error"]["code"] == "ORDER_NOT_FOUND"


# ---------------------------------------------------------------------------
class TestExecutorBehaviour:
    def test_order_id_accepts_the_hash_prefix_operators_type(
        self, executor, healthy_order
    ):
        invocation = executor.execute("get_order", {"order_id": f"#{healthy_order.pk}"})
        assert invocation.ok is True
        assert invocation.result.data["order_id"] == healthy_order.pk

    def test_parallel_execution_preserves_input_order(self, executor, healthy_order):
        """
        tool_result blocks must line up with the tool_use blocks that asked
        for them, so ordering is part of the contract, not a nicety.
        """
        calls = [
            {"name": n, "arguments": {"order_id": healthy_order.pk}, "tool_use_id": f"t{i}"}
            for i, n in enumerate(
                ["get_order", "get_payment_status", "get_vehicle", "get_delivery_status"]
            )
        ]
        results = executor.execute_many(calls)
        assert [r.tool_name for r in results] == [c["name"] for c in calls]
        assert [r.tool_use_id for r in results] == ["t0", "t1", "t2", "t3"]
        assert all(r.ok for r in results)

    def test_composite_summary_matches_the_individual_tools(
        self, executor, stuck_order
    ):
        """
        get_order_summary exists to save round trips, so it must return the
        same values the separate tools would — otherwise the model learns two
        different vocabularies for the same facts.
        """
        summary = executor.execute(
            "get_order_summary", {"order_id": stuck_order.pk}
        ).result.data
        payment = executor.execute(
            "get_payment_status", {"order_id": stuck_order.pk}
        ).result.data

        assert summary["payment"]["amount_due"] == payment["amount_due"]
        assert summary["order"]["status"] == stuck_order.status
        assert set(summary) == {
            "order",
            "customer",
            "vehicle",
            "payment",
            "delivery",
            "documents",
            "finance",
            "timeline",
        }

    # Caching on, but still inline (TIMEOUT_SECONDS 0) — see the note in
    # config/settings/test.py about worker threads and test transactions.
    @override_settings(
        TOOLS={"TIMEOUT_SECONDS": 0, "MAX_PARALLEL": 6, "CACHE_ENABLED": True}
    )
    def test_cache_key_is_scoped_to_the_caller(self, viewer, agent, healthy_order):
        """
        A shared cache entry would leak unmasked PII to a lesser role. The
        cache key includes the caller's scopes, so the two roles cannot
        collide on one entry.
        """
        from django.core.cache import cache

        cache.clear()
        agent_result = ToolExecutor(ToolContext(user=agent, trace_id="t")).execute(
            "get_customer", {"order_id": healthy_order.pk}
        )
        viewer_result = ToolExecutor(ToolContext(user=viewer, trace_id="t")).execute(
            "get_customer", {"order_id": healthy_order.pk}
        )
        assert agent_result.result.data["pii_visible"] is True
        assert viewer_result.result.data["pii_visible"] is False
        cache.clear()

    def test_find_orders_resolves_by_phone(self, executor, healthy_order):
        invocation = executor.execute(
            "find_orders",
            {
                "phone": healthy_order.customer.phone,
                "registration_number": None,
                "customer_name": None,
                "status": None,
                "hub": None,
                "limit": 5,
            },
        )
        assert invocation.ok is True
        assert healthy_order.pk in [
            o["order_id"] for o in invocation.result.data["orders"]
        ]

    def test_find_orders_rejects_an_unknown_status(self, executor):
        invocation = executor.execute(
            "find_orders",
            {
                "phone": None,
                "registration_number": None,
                "customer_name": None,
                "status": "TOTALLY_MADE_UP",
                "hub": None,
                "limit": None,
            },
        )
        assert invocation.ok is False
        assert OrderStatus.DELIVERED in invocation.result.error_details["valid_statuses"]


# ---------------------------------------------------------------------------
class TestThreadedExecution:
    """
    Covers the real production path, which inline mode bypasses.

    `transaction=True` commits fixture data so a worker thread's own database
    connection can see it. These tests are slower, which is exactly why the
    rest of the suite runs inline — but skipping them entirely would mean the
    code that actually runs in production is never executed.
    """

    @pytest.mark.django_db(transaction=True)
    @override_settings(
        TOOLS={"TIMEOUT_SECONDS": 8.0, "MAX_PARALLEL": 6, "CACHE_ENABLED": False}
    )
    def test_parallel_tools_run_in_threads_and_keep_order(self, agent, healthy_order):
        executor = ToolExecutor(ToolContext(user=agent, trace_id="t"))
        assert executor.inline is False

        names = ["get_order", "get_payment_status", "get_vehicle", "get_delivery_status"]
        results = executor.execute_many(
            [
                {
                    "name": name,
                    "arguments": {"order_id": healthy_order.pk},
                    "tool_use_id": f"t{i}",
                }
                for i, name in enumerate(names)
            ]
        )
        assert [r.tool_name for r in results] == names
        assert all(r.ok for r in results), [
            (r.tool_name, r.result.error_code) for r in results
        ]

    @override_settings(
        TOOLS={"TIMEOUT_SECONDS": 0.15, "MAX_PARALLEL": 6, "CACHE_ENABLED": False}
    )
    def test_a_slow_tool_times_out_instead_of_hanging_the_request(
        self, agent, monkeypatch
    ):
        import time as time_module

        def crawl(*args, **kwargs):
            time_module.sleep(2)
            raise AssertionError("should have timed out before this")

        monkeypatch.setattr(
            "apps.assistant.tools.catalog.selectors.get_order", crawl
        )
        executor = ToolExecutor(ToolContext(user=agent, trace_id="t"))
        invocation = executor.execute("get_order", {"order_id": 1})

        assert invocation.ok is False
        assert invocation.result.error_code == "TIMEOUT"
        # The model must be told not to fabricate the missing data.
        assert "do not invent" in invocation.result.error_message.lower()


class TestToolResultContract:
    def test_success_payload_shape(self):
        result = ToolResult.success({"a": 1})
        assert result.to_model_payload() == {"ok": True, "data": {"a": 1}}

    def test_failure_payload_shape(self):
        result = ToolResult.failure("X", "boom", details={"k": "v"})
        payload = result.to_model_payload()
        assert payload["ok"] is False
        assert payload["error"] == {"code": "X", "message": "boom", "details": {"k": "v"}}

    def test_money_is_preformatted_so_the_model_never_calculates(
        self, executor, stuck_order
    ):
        data = executor.execute(
            "get_payment_status", {"order_id": stuck_order.pk}
        ).result.data
        for key in ("total_amount", "amount_paid", "amount_due"):
            assert set(data[key]) == {"value", "display"}
            assert data[key]["display"].startswith("₹")
        # Indian digit grouping (2-2-3), which models reliably get wrong.
        assert data["total_amount"]["display"] == "₹8,00,000.00"
