"""
Plain operational read APIs.

These exist independently of the assistant — dashboards and humans use them
directly — and they are the same selectors the AI tools call. Building them
first, then wrapping them in tools, is what keeps the LLM out of the data path.
"""

from __future__ import annotations

from drf_spectacular.utils import OpenApiParameter, extend_schema, extend_schema_view
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.drf import HasScope
from apps.accounts.permissions import Scope

from . import selectors
from .diagnostics import diagnose


class _BaseOrderView(APIView):
    """Shared plumbing: auth, scope check, throttle scope, order loading."""

    permission_classes = [IsAuthenticated, HasScope]
    throttle_scope = "read_api"
    required_scopes: tuple[str, ...] = ()

    def get_order(self, order_id):
        return selectors.get_order(order_id)


# `operation_id` must be set per-method, not on the class: a class-level id
# would be applied to every verb. Without an explicit id this endpoint
# collides with OrderDetailView and drf-spectacular silently disambiguates
# with a numeral suffix, which makes generated clients rename themselves on
# unrelated changes.
@extend_schema_view(
    get=extend_schema(
        summary="Order search",
        operation_id="orders_search",
        parameters=[
            OpenApiParameter("phone", str),
            OpenApiParameter("registration_number", str),
            OpenApiParameter("customer_name", str),
            OpenApiParameter("status", str),
            OpenApiParameter("hub", str),
            OpenApiParameter("limit", int),
        ],
        responses={200: dict},
    )
)
class OrderSearchView(_BaseOrderView):
    """Find orders by phone, plate, customer name, status or hub."""

    required_scopes = (Scope.ORDERS_READ,)

    def get(self, request):
        params = request.query_params
        data = selectors.find_orders(
            phone=params.get("phone"),
            registration_number=params.get("registration_number"),
            customer_name=params.get("customer_name"),
            status=params.get("status"),
            hub=params.get("hub"),
            limit=params.get("limit", 10),
        )
        return Response(data)


@extend_schema(summary="Order core details", responses={200: dict})
class OrderDetailView(_BaseOrderView):
    required_scopes = (Scope.ORDERS_READ,)

    def get(self, request, order_id):
        return Response(selectors.order_core(self.get_order(order_id)))


@extend_schema(summary="Complete order summary", responses={200: dict})
class OrderSummaryView(_BaseOrderView):
    required_scopes = (Scope.ORDERS_READ, Scope.PAYMENTS_READ, Scope.CUSTOMERS_READ)

    def get(self, request, order_id):
        order = self.get_order(order_id)
        return Response(
            selectors.order_summary(
                order,
                include_pii=request.user.has_scope(Scope.CUSTOMERS_PII),
            )
        )


@extend_schema(summary="Payment status for an order", responses={200: dict})
class OrderPaymentsView(_BaseOrderView):
    required_scopes = (Scope.PAYMENTS_READ,)

    def get(self, request, order_id):
        return Response(selectors.payment_status(self.get_order(order_id)))


@extend_schema(summary="Customer attached to an order", responses={200: dict})
class OrderCustomerView(_BaseOrderView):
    required_scopes = (Scope.CUSTOMERS_READ,)

    def get(self, request, order_id):
        order = self.get_order(order_id)
        return Response(
            selectors.customer_info(
                order, include_pii=request.user.has_scope(Scope.CUSTOMERS_PII)
            )
        )


@extend_schema(summary="Vehicle attached to an order", responses={200: dict})
class OrderVehicleView(_BaseOrderView):
    required_scopes = (Scope.VEHICLES_READ,)

    def get(self, request, order_id):
        return Response(selectors.vehicle_info(self.get_order(order_id)))


@extend_schema(
    summary="Order timeline",
    parameters=[OpenApiParameter("limit", int)],
    responses={200: dict},
)
class OrderTimelineView(_BaseOrderView):
    required_scopes = (Scope.EVENTS_READ,)

    def get(self, request, order_id):
        order = self.get_order(order_id)
        return Response(
            selectors.timeline(order, limit=request.query_params.get("limit", 20))
        )


@extend_schema(summary="Delivery status", responses={200: dict})
class OrderDeliveryView(_BaseOrderView):
    required_scopes = (Scope.DELIVERY_READ,)

    def get(self, request, order_id):
        return Response(selectors.delivery_status(self.get_order(order_id)))


@extend_schema(summary="Order documents", responses={200: dict})
class OrderDocumentsView(_BaseOrderView):
    required_scopes = (Scope.ORDERS_READ,)

    def get(self, request, order_id):
        return Response(selectors.documents(self.get_order(order_id)))


@extend_schema(summary="Finance / loan status", responses={200: dict})
class OrderFinanceView(_BaseOrderView):
    required_scopes = (Scope.FINANCE_READ,)

    def get(self, request, order_id):
        return Response(selectors.finance_info(self.get_order(order_id)))


@extend_schema(
    summary="Deterministic blocker diagnosis",
    description=(
        "Runs the rule engine that explains why an order is not progressing. "
        "Results are computed in Python, not inferred by a model."
    ),
    responses={200: dict},
)
class OrderDiagnosisView(_BaseOrderView):
    required_scopes = (Scope.DIAGNOSTICS_READ,)

    def get(self, request, order_id):
        return Response(diagnose(self.get_order(order_id)))
