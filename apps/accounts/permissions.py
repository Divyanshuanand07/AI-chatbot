"""
Permission scopes and the role -> scope mapping.

Scopes are the single authorization vocabulary in the system. They gate:
  * plain REST endpoints (via `HasScope`),
  * AI tools (each tool declares `required_scopes`; the orchestrator hides
    tools the caller may not use, so the model cannot even attempt them),
  * field-level PII masking on customer data.

Keeping one vocabulary for humans and for the model is what stops the
assistant from becoming a privilege-escalation path around the REST API.
"""

from __future__ import annotations

from django.db import models


class Scope(models.TextChoices):
    ORDERS_READ = "orders:read", "Read orders"
    PAYMENTS_READ = "payments:read", "Read payments"
    CUSTOMERS_READ = "customers:read", "Read customers (non-identifying fields)"
    CUSTOMERS_PII = "customers:pii", "Read customer contact details"
    VEHICLES_READ = "vehicles:read", "Read vehicles"
    EVENTS_READ = "events:read", "Read order timeline"
    DELIVERY_READ = "delivery:read", "Read delivery status"
    FINANCE_READ = "finance:read", "Read finance/loan status"
    DIAGNOSTICS_READ = "diagnostics:read", "Run order blocker diagnosis"
    KNOWLEDGE_READ = "knowledge:read", "Search SOPs and policies"
    AUDIT_READ = "audit:read", "Read audit logs"


class Role(models.TextChoices):
    VIEWER = "viewer", "Viewer"
    OPS_AGENT = "ops_agent", "Operations Agent"
    OPS_MANAGER = "ops_manager", "Operations Manager"
    ADMIN = "admin", "Administrator"


# Read-only, least-privilege first. Each role is a superset of the one above.
_VIEWER_SCOPES = frozenset(
    {
        Scope.ORDERS_READ,
        Scope.PAYMENTS_READ,
        Scope.CUSTOMERS_READ,
        Scope.VEHICLES_READ,
        Scope.EVENTS_READ,
        Scope.DELIVERY_READ,
        Scope.KNOWLEDGE_READ,
    }
)

_OPS_AGENT_SCOPES = _VIEWER_SCOPES | {
    Scope.CUSTOMERS_PII,
    Scope.FINANCE_READ,
    Scope.DIAGNOSTICS_READ,
}

_OPS_MANAGER_SCOPES = _OPS_AGENT_SCOPES | {Scope.AUDIT_READ}

_ADMIN_SCOPES = frozenset(Scope)

ROLE_SCOPES: dict[str, frozenset[str]] = {
    Role.VIEWER: _VIEWER_SCOPES,
    Role.OPS_AGENT: frozenset(_OPS_AGENT_SCOPES),
    Role.OPS_MANAGER: frozenset(_OPS_MANAGER_SCOPES),
    Role.ADMIN: _ADMIN_SCOPES,
}


def scopes_for_role(role: str) -> frozenset[str]:
    return ROLE_SCOPES.get(role, frozenset())
