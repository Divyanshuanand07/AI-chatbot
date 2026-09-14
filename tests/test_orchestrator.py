"""
Tests for the AI orchestrator.

These are the tests that actually assert the product requirements: simple
lookups, multi-tool summaries, reasoning queries, follow-up context, missing
order ids handled without hallucination, and a complete audit trail.

All of it runs against the deterministic offline driver, so the suite is free
and repeatable. A handful of tests inject a purpose-built fake provider to
reach paths the offline driver would never produce — an exhausted iteration
budget, a provider outage, an ungrounded answer.
"""

from __future__ import annotations

import pytest

from apps.assistant.llm.base import LLMError, LLMProvider, LLMResponse, ToolCall, Usage
from apps.assistant.models import (
    AuditAction,
    AuditLog,
    Conversation,
    Message,
    MessageRole,
    ToolInvocationLog,
)
from apps.assistant.orchestrator import Orchestrator

pytestmark = pytest.mark.django_db


def tool_names(answer) -> list[str]:
    return [call["tool"] for call in answer.tool_calls]


# ---------------------------------------------------------------------------
# Fake providers for paths the offline driver cannot reach
# ---------------------------------------------------------------------------
class RecordingProvider(LLMProvider):
    """Captures what the orchestrator sent, then answers with fixed text."""

    name = "recording"

    def __init__(self, text="done"):
        self.calls: list[dict] = []
        self.text = text

    def complete(self, *, system, messages, tools):
        self.calls.append({"system": system, "messages": messages, "tools": tools})
        return LLMResponse(
            text=self.text,
            stop_reason="end_turn",
            raw_content=[{"type": "text", "text": self.text}],
            usage=Usage(input_tokens=10, output_tokens=5),
            model="fake",
        )


class AlwaysToolsProvider(LLMProvider):
    """Never stops calling tools — exercises the iteration budget."""

    name = "always_tools"

    def __init__(self, order_id):
        self.order_id = order_id
        self.turns = 0

    def complete(self, *, system, messages, tools):
        self.turns += 1
        if not tools:
            # The forced closing call: no tools offered, must answer.
            text = "Based on what I could gather, the order is in progress."
            return LLMResponse(
                text=text,
                stop_reason="end_turn",
                raw_content=[{"type": "text", "text": text}],
                usage=Usage(output_tokens=5),
                model="fake",
            )
        return LLMResponse(
            text="",
            tool_calls=[
                ToolCall(
                    id=f"call{self.turns}",
                    name="get_order",
                    arguments={"order_id": self.order_id},
                )
            ],
            stop_reason="tool_use",
            raw_content=[{"type": "tool_use", "id": f"call{self.turns}"}],
            usage=Usage(output_tokens=5),
            model="fake",
        )


class BrokenProvider(LLMProvider):
    name = "broken"

    def complete(self, *, system, messages, tools):
        raise LLMError("upstream exploded", retryable=True, code="llm_unreachable")


class UngroundedProvider(LLMProvider):
    """Quotes money without calling any tool — the grounding guard's target."""

    name = "ungrounded"

    def complete(self, *, system, messages, tools):
        text = "Order #1243 has ₹5,00,000.00 outstanding."
        return LLMResponse(
            text=text,
            stop_reason="end_turn",
            raw_content=[{"type": "text", "text": text}],
            usage=Usage(output_tokens=9),
            model="fake",
        )


class RefusingProvider(LLMProvider):
    name = "refusing"

    def complete(self, *, system, messages, tools):
        return LLMResponse(
            text="",
            stop_reason="refusal",
            raw_content=[],
            usage=Usage(),
            model="fake",
            refusal_category="cyber",
        )


# ---------------------------------------------------------------------------
# Use case 1: simple lookups
# ---------------------------------------------------------------------------
class TestSimpleLookups:
    def test_payment_status_question_calls_the_payment_tool(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(
            f"What is the payment status of order #{stuck_order.pk}?"
        )
        assert tool_names(answer) == ["get_payment_status"]
        assert answer.order_ids == [stuck_order.pk]
        # Figures come from the tool, pre-formatted.
        assert "₹" in answer.answer

    def test_status_question_falls_back_to_the_order_tool(self, agent, healthy_order):
        answer = Orchestrator(agent).ask(
            f"what is the status of order {healthy_order.pk}"
        )
        assert tool_names(answer) == ["get_order"]

    def test_timeline_question_calls_the_timeline_tool(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(
            f"What happened with order {stuck_order.pk}?"
        )
        assert tool_names(answer) == ["get_order_timeline"]

    def test_delivery_question_calls_the_delivery_tool(self, agent, healthy_order):
        answer = Orchestrator(agent).ask(
            f"when will order {healthy_order.pk} be delivered?"
        )
        assert "get_delivery_status" in tool_names(answer)


# ---------------------------------------------------------------------------
# Use case 2: multi-tool
# ---------------------------------------------------------------------------
class TestMultiTool:
    def test_summary_uses_one_composite_call_not_six(self, agent, stuck_order):
        """
        The efficiency requirement. A complete summary must cost one tool
        round trip, not one per entity.
        """
        answer = Orchestrator(agent).ask(
            f"Give me a complete summary of order #{stuck_order.pk}"
        )
        assert tool_names(answer) == ["get_order_summary"]
        assert answer.iterations == 2  # one tool turn, one answer turn

    def test_summary_answer_covers_every_entity(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(
            f"give me an overview of order {stuck_order.pk}"
        )
        text = answer.answer.lower()
        for expected in ("order #", "customer", "vehicle", "payment", "delivery"):
            assert expected in text

    def test_two_intents_in_one_question_run_together(self, agent, healthy_order):
        answer = Orchestrator(agent).ask(
            f"show me the delivery status and the documents for order {healthy_order.pk}"
        )
        called = set(tool_names(answer))
        assert {"get_delivery_status", "get_order_documents"} <= called
        # Still a single tool-calling round trip.
        assert answer.iterations == 2


# ---------------------------------------------------------------------------
# Use case 3: reasoning
# ---------------------------------------------------------------------------
class TestReasoningQueries:
    def test_why_stuck_calls_the_rule_engine(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(f"Why is order {stuck_order.pk} stuck?")
        assert tool_names(answer) == ["diagnose_order_blockers"]

    def test_stuck_answer_leads_with_the_root_cause_and_owner(
        self, agent, stuck_order
    ):
        answer = Orchestrator(agent).ask(f"why is order {stuck_order.pk} stuck?")
        assert "Documentation" in answer.answer
        assert "rejected" in answer.answer.lower()
        # The overdue delivery is present but framed as an effect.
        assert "knock-on" in answer.answer.lower() or "overdue" in answer.answer.lower()

    def test_healthy_order_is_reported_as_not_stuck(self, agent, healthy_order):
        answer = Orchestrator(agent).ask(f"is order {healthy_order.pk} stuck?")
        assert "not stuck" in answer.answer.lower()


# ---------------------------------------------------------------------------
# Use case 4: conversation context
# ---------------------------------------------------------------------------
class TestFollowUpContext:
    def test_follow_up_resolves_the_order_without_repeating_it(
        self, agent, stuck_order
    ):
        orchestrator = Orchestrator(agent)
        first = orchestrator.ask(f"What happened with order {stuck_order.pk}?")
        conversation = Conversation.objects.get(pk=first.conversation_id)
        assert conversation.last_order_id == stuck_order.pk

        second = orchestrator.ask(
            "and what about its payment?", conversation=conversation
        )
        assert tool_names(second) == ["get_payment_status"]
        assert second.order_ids == [stuck_order.pk]

    def test_follow_up_can_switch_intent_on_the_same_order(self, agent, stuck_order):
        orchestrator = Orchestrator(agent)
        first = orchestrator.ask(f"payment status of order {stuck_order.pk}")
        conversation = Conversation.objects.get(pk=first.conversation_id)

        second = orchestrator.ask("why is it stuck?", conversation=conversation)
        assert tool_names(second) == ["diagnose_order_blockers"]
        assert second.order_ids == [stuck_order.pk]

    def test_context_tracks_multiple_orders_without_mixing_them(
        self, agent, stuck_order, healthy_order
    ):
        orchestrator = Orchestrator(agent)
        first = orchestrator.ask(f"status of order {stuck_order.pk}")
        conversation = Conversation.objects.get(pk=first.conversation_id)
        orchestrator.ask(f"status of order {healthy_order.pk}", conversation=conversation)
        conversation.refresh_from_db()

        assert conversation.last_order_id == healthy_order.pk
        assert set(conversation.context["mentioned_order_ids"]) == {
            stuck_order.pk,
            healthy_order.pk,
        }

    def test_history_is_replayed_as_text_without_tool_blocks(
        self, agent, stuck_order
    ):
        """
        Tool results are deliberately not replayed into later turns — an
        answer built from a payment status fetched three turns ago would be a
        correctness bug. Follow-ups re-read instead.
        """
        orchestrator = Orchestrator(agent)
        first = orchestrator.ask(f"payment status of order {stuck_order.pk}")
        conversation = Conversation.objects.get(pk=first.conversation_id)

        recorder = RecordingProvider()
        Orchestrator(agent, provider=recorder).ask(
            "anything else?", conversation=conversation
        )

        replayed = recorder.calls[0]["messages"]
        assert all(isinstance(m["content"], str) for m in replayed)
        assert replayed[0]["role"] == "user"
        serialised = str(replayed)
        assert "tool_use" not in serialised
        assert "tool_result" not in serialised


# ---------------------------------------------------------------------------
# Use case 6: missing / invalid ids, no hallucination
# ---------------------------------------------------------------------------
class TestNoHallucination:
    def test_missing_order_is_reported_as_missing(self, agent):
        answer = Orchestrator(agent).ask("what is the payment status of order 999999?")
        assert answer.tool_calls[0]["error_code"] == "ORDER_NOT_FOUND"
        assert "999999" in answer.answer
        assert "does not exist" in answer.answer.lower()
        # Crucially: no invented figures.
        assert "₹" not in answer.answer

    def test_question_without_an_order_id_asks_instead_of_guessing(self, agent):
        answer = Orchestrator(agent).ask("what is the payment status?")
        assert answer.tool_calls == []
        assert "will not guess" in answer.answer.lower()

    def test_grounding_guard_flags_money_with_no_successful_tool(self, agent):
        """
        A last-resort detector. The answer is not rewritten — silently editing
        model output hides the failure — but the warning surfaces in the
        response, the logs and the audit row.
        """
        answer = Orchestrator(agent, provider=UngroundedProvider()).ask(
            "what is outstanding on order 1243?"
        )
        assert any(w.startswith("ungrounded_answer") for w in answer.warnings)

    def test_grounding_guard_stays_quiet_when_tools_succeeded(
        self, agent, stuck_order
    ):
        answer = Orchestrator(agent).ask(f"payment status of order {stuck_order.pk}")
        assert not any(w.startswith("ungrounded_answer") for w in answer.warnings)


# ---------------------------------------------------------------------------
# Use case 7: authorization
# ---------------------------------------------------------------------------
class TestAuthorization:
    def test_viewer_is_not_offered_the_diagnosis_tool(self, viewer, stuck_order):
        answer = Orchestrator(viewer).ask(f"why is order {stuck_order.pk} stuck?")
        assert tool_names(answer) == []
        assert "permission" in answer.answer.lower()

    def test_viewer_does_not_get_a_substituted_answer(self, viewer, stuck_order):
        """
        Quietly answering an easier question is worse than refusing, because
        the operator cannot tell they got something else.
        """
        answer = Orchestrator(viewer).ask(f"why is order {stuck_order.pk} stuck?")
        assert "get_order" not in tool_names(answer)

    def test_agent_gets_the_diagnosis(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(f"why is order {stuck_order.pk} stuck?")
        assert tool_names(answer) == ["diagnose_order_blockers"]

    def test_viewer_sees_masked_contact_details(self, viewer, healthy_order):
        answer = Orchestrator(viewer).ask(
            f"who is the customer on order {healthy_order.pk}?"
        )
        assert "*" in answer.answer
        assert "masked" in answer.answer.lower()


# ---------------------------------------------------------------------------
# Use case 8: audit, observability, resilience
# ---------------------------------------------------------------------------
class TestPersistenceAndAudit:
    def test_a_turn_persists_both_messages(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(f"status of order {stuck_order.pk}")
        messages = Message.objects.filter(
            conversation_id=answer.conversation_id
        ).order_by("sequence")

        assert [m.role for m in messages] == [
            MessageRole.USER,
            MessageRole.ASSISTANT,
        ]
        assert messages[1].content == answer.answer
        assert messages[1].trace_id == answer.trace_id

    def test_tool_invocations_are_logged(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(f"why is order {stuck_order.pk} stuck?")
        logs = ToolInvocationLog.objects.filter(trace_id=answer.trace_id)
        assert logs.count() == 1
        log = logs.first()
        assert log.tool_name == "diagnose_order_blockers"
        assert log.ok is True
        assert log.user_id == agent.id
        assert log.duration_ms >= 0

    def test_failed_tool_calls_are_logged_too(self, agent):
        answer = Orchestrator(agent).ask("status of order 999999")
        log = ToolInvocationLog.objects.get(trace_id=answer.trace_id)
        assert log.ok is False
        assert log.error_code == "ORDER_NOT_FOUND"

    def test_audit_row_captures_who_asked_what(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(
            f"why is order {stuck_order.pk} stuck?",
            request_meta={"ip": "10.0.0.9", "user_agent": "pytest"},
        )
        entry = AuditLog.objects.get(trace_id=answer.trace_id)

        assert entry.action == AuditAction.AI_QUERY
        assert entry.username == agent.username
        # Role is denormalised: the authority held at the time, not now.
        assert entry.role == agent.role
        assert str(stuck_order.pk) in entry.query_text
        assert entry.order_ids_touched == [stuck_order.pk]
        assert entry.tools_called[0]["tool_name"] == "diagnose_order_blockers"
        assert entry.ip_address == "10.0.0.9"
        assert entry.succeeded is True

    def test_citations_come_from_execution_not_from_the_model(
        self, agent, stuck_order
    ):
        """
        The reported tools are built from execution records, so the model
        cannot claim to have checked something it never called.
        """
        answer = Orchestrator(agent, provider=RecordingProvider(
            "I checked the payment ledger and the delivery system."
        )).ask(f"tell me about order {stuck_order.pk}")

        assert answer.tool_calls == []  # it called nothing, whatever it says

    def test_provider_failure_is_audited_and_raised(self, agent):
        orchestrator = Orchestrator(agent, provider=BrokenProvider())
        with pytest.raises(LLMError):
            orchestrator.ask("status of order 1")

        entry = AuditLog.objects.get(action=AuditAction.AI_QUERY_FAILED)
        assert entry.succeeded is False
        assert entry.error_code == "llm_unreachable"
        assert entry.metadata["retryable"] is True

    def test_usage_is_accumulated_across_turns(self, agent, stuck_order):
        answer = Orchestrator(agent).ask(f"status of order {stuck_order.pk}")
        assert set(answer.usage) == {
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        }

    def test_conversation_is_titled_from_the_first_question(self, agent, stuck_order):
        question = f"Why is order {stuck_order.pk} stuck and who owns it?"
        answer = Orchestrator(agent).ask(question)
        conversation = Conversation.objects.get(pk=answer.conversation_id)
        assert conversation.title == question


# ---------------------------------------------------------------------------
class TestResilience:
    def test_iteration_budget_is_enforced_and_still_answers(self, agent, healthy_order):
        provider = AlwaysToolsProvider(healthy_order.pk)
        answer = Orchestrator(agent, provider=provider).ask("tell me everything")

        assert "tool_iteration_budget_exhausted" in answer.warnings
        assert answer.iterations == 6  # LLM_MAX_TOOL_ITERATIONS in test settings
        # The operator still gets prose rather than an empty response.
        assert answer.answer

    def test_refusal_is_handled_gracefully(self, agent):
        answer = Orchestrator(agent, provider=RefusingProvider()).ask("do something odd")
        assert any(w.startswith("model_refusal") for w in answer.warnings)
        assert "can't answer" in answer.answer.lower()

    def test_empty_model_answer_does_not_produce_an_empty_response(self, agent):
        answer = Orchestrator(agent, provider=RecordingProvider("")).ask("hello?")
        assert "empty_model_answer" in answer.warnings
        assert answer.answer

    def test_trace_id_links_every_record(self, agent, stuck_order):
        answer = Orchestrator(agent, trace_id="fixed-trace-1").ask(
            f"why is order {stuck_order.pk} stuck?"
        )
        assert answer.trace_id == "fixed-trace-1"
        assert Message.objects.filter(trace_id="fixed-trace-1").count() == 2
        assert ToolInvocationLog.objects.filter(trace_id="fixed-trace-1").count() == 1
        assert AuditLog.objects.filter(trace_id="fixed-trace-1").count() == 1


# ---------------------------------------------------------------------------
class TestPromptConstruction:
    def test_system_prompt_is_split_for_prompt_caching(self, agent):
        recorder = RecordingProvider()
        Orchestrator(agent, provider=recorder).ask("hello")
        system = recorder.calls[0]["system"]

        assert len(system) == 2
        # Stable block carries the cache breakpoint...
        assert system[0]["cache_control"] == {"type": "ephemeral"}
        # ...and the volatile block must not, or nothing would ever hit.
        assert "cache_control" not in system[1]

    def test_stable_block_has_no_per_request_content(self, agent, make_user):
        """
        A date or username in the cached prefix invalidates it on every
        request — the most common way a team pays full price for caching.
        """
        other = make_user(username="someone_else")
        first = RecordingProvider()
        second = RecordingProvider()
        Orchestrator(agent, provider=first).ask("hello")
        Orchestrator(other, provider=second).ask("hello")

        assert first.calls[0]["system"][0]["text"] == second.calls[0]["system"][0]["text"]

    def test_volatile_block_carries_role_and_context(self, agent, stuck_order):
        orchestrator = Orchestrator(agent)
        first = orchestrator.ask(f"status of order {stuck_order.pk}")
        conversation = Conversation.objects.get(pk=first.conversation_id)

        recorder = RecordingProvider()
        Orchestrator(agent, provider=recorder).ask(
            "and the payment?", conversation=conversation
        )
        volatile = recorder.calls[0]["system"][1]["text"]

        assert agent.role in volatile
        assert f"#{stuck_order.pk}" in volatile
        assert "orders:read" in volatile

    def test_tools_offered_match_the_caller_role(self, viewer, agent):
        viewer_recorder = RecordingProvider()
        agent_recorder = RecordingProvider()
        Orchestrator(viewer, provider=viewer_recorder).ask("hello")
        Orchestrator(agent, provider=agent_recorder).ask("hello")

        viewer_tools = {t["name"] for t in viewer_recorder.calls[0]["tools"]}
        agent_tools = {t["name"] for t in agent_recorder.calls[0]["tools"]}
        assert "diagnose_order_blockers" not in viewer_tools
        assert "diagnose_order_blockers" in agent_tools
