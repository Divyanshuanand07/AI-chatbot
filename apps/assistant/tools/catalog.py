"""
The tool catalogue.

Tool *descriptions* are prompt engineering, not documentation. They are the
only thing the model uses to choose between eleven similar-sounding options,
so each one says what the tool returns, when to prefer it, and — where it
matters — when **not** to use it. The "prefer get_order_summary over six
separate calls" hint is what turns a complete-summary question into one round
trip instead of six.

Every handler is thin: validate, delegate to `apps.operations.selectors`,
wrap the outcome. No business logic lives here, because this layer must stay
a faithful mirror of what the REST API would return.
"""

from __future__ import annotations

from apps.accounts.permissions import Scope
from apps.common.exceptions import NotFoundError, ValidationError
from apps.operations import selectors
from apps.operations.diagnostics import diagnose
from apps.operations.models import OrderStatus

from .base import ToolContext, ToolResult, order_id_schema, registry


# ---------------------------------------------------------------------------
# Shared order loading
# ---------------------------------------------------------------------------
def _load_order(order_id):
    """
    Resolve an order id to an Order, or to an explicit tool failure.

    Returns `(order, None)` or `(None, ToolResult)`. The two failure codes are
    distinct on purpose: ORDER_NOT_FOUND means "this id is valid but no such
    order exists" (the model should say so), while INVALID_ORDER_ID means the
    model mis-extracted the entity (it should ask the user to confirm).
    """
    try:
        return selectors.get_order(order_id), None
    except NotFoundError as exc:
        return None, ToolResult.failure(
            "ORDER_NOT_FOUND",
            exc.message,
            details=exc.details,
        )
    except ValidationError as exc:
        return None, ToolResult.failure(
            "INVALID_ORDER_ID",
            exc.message,
            details=exc.details,
        )


# ---------------------------------------------------------------------------
# Core single-entity lookups
# ---------------------------------------------------------------------------
@registry.register(
    name="get_order",
    description=(
        "Get the core details of one order: current status, order type, hub, "
        "city, assigned agent, total amount, amount paid, amount still due, "
        "how long it has been in its current status, and the promised "
        "delivery date. Use this for questions about an order's status or "
        "progress. For a full picture across customer, vehicle, payments and "
        "timeline, use get_order_summary instead of calling several tools."
    ),
    input_schema=order_id_schema(),
    required_scopes=(Scope.ORDERS_READ,),
    cache_ttl=20,
)
def get_order(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(selectors.order_core(order))


@registry.register(
    name="get_payment_status",
    description=(
        "Get the payment position of one order: a plain-language summary, the "
        "total amount, how much is settled, how much is still due, how much "
        "was refunded, counts of settled/failed/in-flight payments, and every "
        "individual transaction with its method, gateway reference and "
        "failure reason where applicable. All totals are pre-calculated — "
        "never add up the transaction list yourself."
    ),
    input_schema=order_id_schema(),
    required_scopes=(Scope.PAYMENTS_READ,),
    cache_ttl=15,
)
def get_payment_status(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(selectors.payment_status(order))


@registry.register(
    name="get_customer",
    description=(
        "Get the customer attached to one order: name, city, state, KYC "
        "status, how long they have been a customer, their total order count, "
        "and contact details. Contact details are masked when the requesting "
        "operator's role does not permit reading them; when the response says "
        "pii_visible is false, report that the details are restricted and "
        "never guess the hidden characters."
    ),
    input_schema=order_id_schema(),
    required_scopes=(Scope.CUSTOMERS_READ,),
    cache_ttl=30,
    returns_pii=True,
)
def get_customer(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(
        selectors.customer_info(
            order, include_pii=ctx.has_scope(Scope.CUSTOMERS_PII)
        )
    )


@registry.register(
    name="get_vehicle",
    description=(
        "Get the vehicle on one order: registration number, make, model, "
        "variant, year, fuel type, transmission, kilometres driven, colour, "
        "number of previous owners, inspection score, listing price, the hub "
        "holding it, and the RC (ownership transfer) status."
    ),
    input_schema=order_id_schema(),
    required_scopes=(Scope.VEHICLES_READ,),
    cache_ttl=60,
)
def get_vehicle(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(selectors.vehicle_info(order))


@registry.register(
    name="get_order_timeline",
    description=(
        "Get the chronological event history of one order — what actually "
        "happened and when, including status changes, document uploads and "
        "verifications, payment attempts, finance decisions, RC transfer "
        "filings and delivery attempts. Use this for 'what happened with this "
        "order?' questions. Use diagnose_order_blockers instead when the "
        "question is why an order is stuck or delayed."
    ),
    input_schema=order_id_schema(
        {
            "limit": {
                "type": ["integer", "null"],
                "description": (
                    "How many of the most recent events to return "
                    "(1-50, default 20). Pass null for the default."
                ),
            }
        }
    ),
    required_scopes=(Scope.EVENTS_READ,),
    cache_ttl=15,
)
def get_order_timeline(ctx: ToolContext, order_id, limit=None) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(selectors.timeline(order, limit=limit or 20))


@registry.register(
    name="get_delivery_status",
    description=(
        "Get the delivery position of one order: delivery status, scheduled "
        "date and time slot, logistics partner, tracking reference, how many "
        "delivery attempts have been made, the reason the last attempt "
        "failed, whether the promised date has passed, and by how many days."
    ),
    input_schema=order_id_schema(),
    required_scopes=(Scope.DELIVERY_READ,),
    cache_ttl=15,
)
def get_delivery_status(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(selectors.delivery_status(order))


@registry.register(
    name="get_order_documents",
    description=(
        "Get the document checklist for one order: every required document "
        "with its status (not uploaded, uploaded and awaiting verification, "
        "verified, or rejected), the rejection reason where one exists, and "
        "whether all mandatory documents are verified."
    ),
    input_schema=order_id_schema(),
    required_scopes=(Scope.ORDERS_READ,),
    cache_ttl=20,
)
def get_order_documents(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(selectors.documents(order))


@registry.register(
    name="get_finance_status",
    description=(
        "Get the loan/finance position of one order: whether the order is "
        "financed at all, the lending partner, the loan amount, the "
        "application status (applied, under review, approved, rejected or "
        "disbursed), and when it last changed."
    ),
    input_schema=order_id_schema(),
    required_scopes=(Scope.FINANCE_READ,),
    cache_ttl=30,
)
def get_finance_status(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(selectors.finance_info(order))


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------
@registry.register(
    name="get_order_summary",
    description=(
        "Get EVERYTHING about one order in a single call: core order details, "
        "customer, vehicle, full payment position, delivery, document "
        "checklist, finance status and recent timeline. Strongly prefer this "
        "over calling get_order, get_customer, get_vehicle, "
        "get_payment_status, get_delivery_status and get_order_timeline "
        "separately — it returns the same data in one round trip. Use it "
        "whenever the operator asks for a summary, an overview, or 'everything' "
        "about an order."
    ),
    input_schema=order_id_schema(),
    required_scopes=(
        Scope.ORDERS_READ,
        Scope.PAYMENTS_READ,
        Scope.CUSTOMERS_READ,
    ),
    cache_ttl=15,
    returns_pii=True,
)
def get_order_summary(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(
        selectors.order_summary(
            order, include_pii=ctx.has_scope(Scope.CUSTOMERS_PII)
        )
    )


@registry.register(
    name="diagnose_order_blockers",
    description=(
        "Explain why an order is stuck, delayed or not progressing. Returns a "
        "deterministic rule-engine verdict: whether the order is stuck, the "
        "primary blocker (the root cause, already distinguished from "
        "downstream symptoms), every individual finding with its severity, "
        "supporting evidence, owning team and suggested next action, plus a "
        "list of things that are healthy and a separate list of consequences. "
        "Use this for any 'why is it stuck / delayed / not moving' question. "
        "Report the findings as given — do not infer additional causes of "
        "your own."
    ),
    input_schema=order_id_schema(),
    required_scopes=(Scope.DIAGNOSTICS_READ,),
    cache_ttl=10,
)
def diagnose_order_blockers(ctx: ToolContext, order_id) -> ToolResult:
    order, failure = _load_order(order_id)
    if failure:
        return failure
    return ToolResult.success(diagnose(order))


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------
@registry.register(
    name="find_orders",
    description=(
        "Find orders when you do not have an order id — search by customer "
        "phone number, vehicle registration number, customer name, order "
        "status or hub. Returns matching orders with their id, status, "
        "customer, vehicle and amount due. At least one filter is required. "
        "Use this to resolve an order id before calling the other tools, or "
        "to answer questions about a set of orders (for example all orders "
        "stuck at a given hub). Pass null for filters you are not using."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "phone": {
                "type": ["string", "null"],
                "description": (
                    "Customer's full 10-digit phone number. Partial numbers "
                    "are rejected, because a suffix can match several "
                    "customers and returning the wrong person's order is "
                    "worse than asking for the full number."
                ),
            },
            "registration_number": {
                "type": ["string", "null"],
                "description": "Vehicle registration number, e.g. HR26AB1234.",
            },
            "customer_name": {
                "type": ["string", "null"],
                "description": "Full or partial customer name.",
            },
            "status": {
                "type": ["string", "null"],
                "description": "Exact order status.",
                "enum": [None] + [s.value for s in OrderStatus],
            },
            "hub": {
                "type": ["string", "null"],
                "description": "Full or partial hub name, e.g. 'Gurugram'.",
            },
            "limit": {
                "type": ["integer", "null"],
                "description": "Maximum orders to return (1-25, default 10).",
            },
        },
        "required": [
            "phone",
            "registration_number",
            "customer_name",
            "status",
            "hub",
            "limit",
        ],
        "additionalProperties": False,
    },
    required_scopes=(Scope.ORDERS_READ,),
    cache_ttl=15,
)
def find_orders(
    ctx: ToolContext,
    phone=None,
    registration_number=None,
    customer_name=None,
    status=None,
    hub=None,
    limit=None,
) -> ToolResult:
    try:
        return ToolResult.success(
            selectors.find_orders(
                phone=phone,
                registration_number=registration_number,
                customer_name=customer_name,
                status=status,
                hub=hub,
                limit=limit or 10,
            )
        )
    except ValidationError as exc:
        return ToolResult.failure(
            "INVALID_SEARCH", exc.message, details=exc.details
        )
