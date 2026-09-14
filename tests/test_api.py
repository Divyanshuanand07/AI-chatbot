"""
HTTP-level tests: authentication, role-based authorization, error envelopes
and the assistant endpoint.

The authorization tests matter most. Every one of them has a counterpart in
`test_tools.py` asserting the same rule at the tool layer — the point being
that a caller gets the same answer whether they go through the REST API or
through the assistant. If those ever diverge, the assistant has become a way
to read data the API would have refused.
"""

from __future__ import annotations

import pytest
from django.urls import reverse

from apps.assistant.models import AuditLog, Conversation

pytestmark = pytest.mark.django_db


# ---------------------------------------------------------------------------
class TestProbes:
    def test_healthz_is_open_and_checks_nothing(self, api_client):
        response = api_client.get("/healthz")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_readyz_reports_dependencies(self, api_client):
        response = api_client.get("/readyz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ready"
        assert body["checks"]["database"] == "ok"


# ---------------------------------------------------------------------------
class TestAuthentication:
    def test_anonymous_access_is_rejected(self, api_client, healthy_order):
        response = api_client.get(f"/api/orders/{healthy_order.pk}/")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthenticated"

    def test_login_returns_tokens_carrying_role_and_scopes(self, api_client, agent):
        response = api_client.post(
            reverse("login"),
            {"username": agent.username, "password": "testpass123"},
            format="json",
        )
        assert response.status_code == 200
        body = response.json()
        assert "access" in body and "refresh" in body

        import jwt

        claims = jwt.decode(body["access"], options={"verify_signature": False})
        assert claims["role"] == agent.role
        assert "orders:read" in claims["scopes"]

    def test_me_endpoint_reports_effective_permissions(self, auth_client, viewer):
        response = auth_client(viewer).get(reverse("me"))
        assert response.status_code == 200
        body = response.json()
        assert body["role"] == "viewer"
        assert "diagnostics:read" not in body["scopes"]


# ---------------------------------------------------------------------------
class TestOrderEndpoints:
    def test_order_detail(self, auth_client, agent, healthy_order):
        response = auth_client(agent).get(f"/api/orders/{healthy_order.pk}/")
        assert response.status_code == 200
        body = response.json()
        assert body["order_id"] == healthy_order.pk
        assert body["total_amount"]["display"].startswith("₹")

    def test_payment_status_totals_are_precomputed(
        self, auth_client, agent, stuck_order
    ):
        response = auth_client(agent).get(f"/api/orders/{stuck_order.pk}/payments/")
        body = response.json()
        assert body["amount_due"]["value"] == 768000.0
        assert body["counts"]["settled"] == 1

    def test_summary_returns_every_entity(self, auth_client, agent, stuck_order):
        response = auth_client(agent).get(f"/api/orders/{stuck_order.pk}/summary/")
        assert set(response.json()) == {
            "order",
            "customer",
            "vehicle",
            "payment",
            "delivery",
            "documents",
            "finance",
            "timeline",
        }

    def test_timeline_is_chronological(self, auth_client, agent, stuck_order):
        response = auth_client(agent).get(f"/api/orders/{stuck_order.pk}/timeline/")
        events = response.json()["events"]
        assert events == sorted(events, key=lambda e: e["occurred_at"])

    def test_diagnosis_endpoint(self, auth_client, agent, stuck_order):
        response = auth_client(agent).get(f"/api/orders/{stuck_order.pk}/diagnosis/")
        body = response.json()
        assert body["is_stuck"] is True
        assert body["primary_blocker"]["code"] == "DOCUMENTS_REJECTED"

    def test_order_search_by_registration(self, auth_client, agent, healthy_order):
        response = auth_client(agent).get(
            "/api/orders/",
            {"registration_number": healthy_order.vehicle.registration_number},
        )
        body = response.json()
        assert body["match_count"] == 1
        assert body["orders"][0]["order_id"] == healthy_order.pk

    def test_order_search_requires_a_filter(self, auth_client, agent):
        response = auth_client(agent).get("/api/orders/")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"


# ---------------------------------------------------------------------------
class TestErrorEnvelope:
    def test_missing_order_returns_a_structured_404(
        self, auth_client, agent
    ):
        response = auth_client(agent).get("/api/orders/999999/")
        assert response.status_code == 404
        body = response.json()
        assert body["error"]["code"] == "not_found"
        assert body["error"]["details"]["order_id"] == 999999
        assert "trace_id" in body

    def test_malformed_order_id_returns_400(self, auth_client, agent):
        response = auth_client(agent).get("/api/orders/not-a-number/")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "validation_error"

    def test_trace_id_is_returned_as_a_header(self, auth_client, agent, healthy_order):
        response = auth_client(agent).get(f"/api/orders/{healthy_order.pk}/")
        assert response["X-Trace-Id"]

    def test_inbound_trace_id_is_honoured(self, auth_client, agent, healthy_order):
        """An upstream gateway's trace id must survive, so traces stitch."""
        response = auth_client(agent).get(
            f"/api/orders/{healthy_order.pk}/", HTTP_X_TRACE_ID="gateway-trace-42"
        )
        assert response["X-Trace-Id"] == "gateway-trace-42"


# ---------------------------------------------------------------------------
class TestRoleBasedAccess:
    def test_viewer_cannot_run_diagnostics(self, auth_client, viewer, stuck_order):
        response = auth_client(viewer).get(f"/api/orders/{stuck_order.pk}/diagnosis/")
        assert response.status_code == 403
        assert response.json()["error"]["code"] == "forbidden"

    def test_viewer_cannot_read_finance(self, auth_client, viewer, stuck_order):
        response = auth_client(viewer).get(f"/api/orders/{stuck_order.pk}/finance/")
        assert response.status_code == 403

    def test_agent_can_run_diagnostics(self, auth_client, agent, stuck_order):
        response = auth_client(agent).get(f"/api/orders/{stuck_order.pk}/diagnosis/")
        assert response.status_code == 200

    def test_pii_is_masked_for_roles_without_the_scope(
        self, auth_client, viewer, agent, healthy_order
    ):
        full = auth_client(agent).get(
            f"/api/orders/{healthy_order.pk}/customer/"
        ).json()
        api_client_masked = auth_client(viewer)
        masked = api_client_masked.get(
            f"/api/orders/{healthy_order.pk}/customer/"
        ).json()

        assert full["pii_visible"] is True
        assert masked["pii_visible"] is False
        assert "*" in masked["phone"]
        # Non-identifying fields stay visible — masking is field-level.
        assert masked["full_name"] == full["full_name"]
        assert masked["city"] == full["city"]

    def test_audit_log_requires_the_audit_scope(self, auth_client, agent, manager):
        assert auth_client(agent).get("/api/ai/audit/").status_code == 403
        assert auth_client(manager).get("/api/ai/audit/").status_code == 200


# ---------------------------------------------------------------------------
class TestAssistantEndpoint:
    def test_ask_returns_answer_with_citations(self, auth_client, agent, stuck_order):
        response = auth_client(agent).post(
            "/api/ai/query/",
            {"question": f"why is order {stuck_order.pk} stuck?"},
            format="json",
        )
        assert response.status_code == 200
        body = response.json()

        assert body["answer"]
        assert body["tools_used"][0]["tool"] == "diagnose_order_blockers"
        assert body["order_ids"] == [stuck_order.pk]
        assert body["trace_id"]
        assert body["conversation_id"]
        assert body["provider"]["provider"] == "stub"

    def test_question_is_validated(self, auth_client, agent):
        response = auth_client(agent).post(
            "/api/ai/query/", {"question": ""}, format="json"
        )
        assert response.status_code == 400

    def test_question_length_is_bounded(self, auth_client, agent):
        response = auth_client(agent).post(
            "/api/ai/query/", {"question": "x" * 5000}, format="json"
        )
        assert response.status_code == 400

    def test_conversation_continues_across_requests(
        self, auth_client, agent, stuck_order
    ):
        client = auth_client(agent)
        first = client.post(
            "/api/ai/query/",
            {"question": f"what happened with order {stuck_order.pk}?"},
            format="json",
        ).json()

        second = client.post(
            "/api/ai/query/",
            {
                "question": "and its payment?",
                "conversation_id": first["conversation_id"],
            },
            format="json",
        ).json()

        assert second["conversation_id"] == first["conversation_id"]
        assert second["order_ids"] == [stuck_order.pk]
        assert second["tools_used"][0]["tool"] == "get_payment_status"

    def test_cannot_continue_another_users_conversation(
        self, auth_client, agent, manager, stuck_order
    ):
        """
        Authorization must not rest on a UUID being hard to guess.
        """
        owned = auth_client(agent).post(
            "/api/ai/query/",
            {"question": f"status of order {stuck_order.pk}"},
            format="json",
        ).json()

        response = auth_client(manager).post(
            "/api/ai/query/",
            {"question": "and the payment?", "conversation_id": owned["conversation_id"]},
            format="json",
        )
        assert response.status_code == 404

    def test_unknown_conversation_is_rejected(self, auth_client, agent):
        response = auth_client(agent).post(
            "/api/ai/query/",
            {
                "question": "hello",
                "conversation_id": "11111111-1111-1111-1111-111111111111",
            },
            format="json",
        )
        assert response.status_code == 404

    def test_query_writes_an_audit_row(self, auth_client, agent, stuck_order):
        auth_client(agent).post(
            "/api/ai/query/",
            {"question": f"status of order {stuck_order.pk}"},
            format="json",
        )
        entry = AuditLog.objects.latest("created_at")
        assert entry.username == agent.username
        assert entry.ip_address is not None


# ---------------------------------------------------------------------------
class TestConversationEndpoints:
    def test_listing_only_shows_my_conversations(
        self, auth_client, agent, manager, stuck_order
    ):
        auth_client(agent).post(
            "/api/ai/query/",
            {"question": f"status of order {stuck_order.pk}"},
            format="json",
        )
        response = auth_client(manager).get("/api/ai/conversations/")
        assert response.json()["count"] == 0

    def test_transcript_is_readable_by_the_owner(
        self, auth_client, agent, stuck_order
    ):
        client = auth_client(agent)
        created = client.post(
            "/api/ai/query/",
            {"question": f"status of order {stuck_order.pk}"},
            format="json",
        ).json()

        response = client.get(f"/api/ai/conversations/{created['conversation_id']}/")
        assert response.status_code == 200
        messages = response.json()["messages"]
        assert [m["role"] for m in messages] == ["user", "assistant"]

    def test_closing_a_conversation_retains_history(
        self, auth_client, agent, stuck_order
    ):
        """Closed, not deleted — the transcript is an audit record."""
        client = auth_client(agent)
        created = client.post(
            "/api/ai/query/",
            {"question": f"status of order {stuck_order.pk}"},
            format="json",
        ).json()

        assert client.delete(
            f"/api/ai/conversations/{created['conversation_id']}/"
        ).status_code == 204

        conversation = Conversation.objects.get(pk=created["conversation_id"])
        assert conversation.is_active is False
        assert conversation.messages.count() == 2


# ---------------------------------------------------------------------------
class TestToolCatalogueEndpoint:
    def test_shows_what_the_role_can_and_cannot_use(self, auth_client, viewer):
        body = auth_client(viewer).get("/api/ai/tools/").json()
        available = {t["name"] for t in body["available"]}
        withheld = {t["name"] for t in body["withheld"]}

        assert "get_order" in available
        assert "diagnose_order_blockers" in withheld
        entry = next(
            t for t in body["withheld"] if t["name"] == "diagnose_order_blockers"
        )
        assert entry["missing_scopes"] == ["diagnostics:read"]

    def test_admin_has_nothing_withheld(self, auth_client, make_user):
        from apps.accounts.permissions import Role

        admin = make_user(Role.ADMIN, username="api_admin")
        body = auth_client(admin).get("/api/ai/tools/").json()
        assert body["withheld"] == []


# ---------------------------------------------------------------------------
class TestKnowledgeEndpoints:
    @pytest.fixture(autouse=True)
    def _corpus(self, db):
        from django.core.cache import cache
        from django.core.management import call_command

        cache.clear()
        call_command("ingest_knowledge", verbosity=0)
        yield
        cache.clear()

    def test_search_requires_a_query(self, auth_client, agent):
        assert auth_client(agent).get("/api/knowledge/search/").status_code == 400

    def test_search_returns_scored_passages(self, auth_client, agent):
        body = auth_client(agent).get(
            "/api/knowledge/search/", {"q": "how long does a refund take"}
        ).json()
        assert body["result_count"] > 0
        assert body["results"][0]["similarity"] >= body["threshold"]

    def test_documents_are_listed(self, auth_client, agent):
        body = auth_client(agent).get("/api/knowledge/documents/").json()
        assert body["count"] == 7
