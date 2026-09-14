"""
Django admin for the operational tables.

Value here is diagnostic, not data-entry: when the assistant gives an answer
you doubt, admin is where you verify the underlying rows in two clicks.
Everything is registered read-mostly (inlines are readonly) because this
service is a read path — orders are written by upstream Cars24 systems.
"""

from django.contrib import admin

from .models import (
    Customer,
    Delivery,
    Order,
    OrderDocument,
    OrderEvent,
    Payment,
    Vehicle,
)


class PaymentInline(admin.TabularInline):
    model = Payment
    extra = 0
    fields = ("reference", "amount", "status", "method", "purpose", "initiated_at")
    readonly_fields = fields
    can_delete = False


class OrderDocumentInline(admin.TabularInline):
    model = OrderDocument
    extra = 0
    fields = ("doc_type", "status", "is_mandatory", "rejection_reason")
    readonly_fields = fields
    can_delete = False


class OrderEventInline(admin.TabularInline):
    model = OrderEvent
    extra = 0
    fields = ("sequence", "event_type", "description", "actor_name", "occurred_at")
    readonly_fields = fields
    can_delete = False


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """One screen that shows an order's full operational picture."""

    list_display = (
        "id",
        "status",
        "customer",
        "vehicle",
        "hub",
        "total_amount",
        "finance_status",
        "expected_delivery_date",
        "created_at",
    )
    list_filter = ("status", "order_type", "finance_status", "channel", "hub")
    search_fields = (
        "id",
        "customer__full_name",
        "customer__phone",
        "vehicle__registration_number",
    )
    date_hierarchy = "created_at"
    inlines = [PaymentInline, OrderDocumentInline, OrderEventInline]
    autocomplete_fields = ("customer", "vehicle")
    list_select_related = ("customer", "vehicle")


@admin.register(Customer)
class CustomerAdmin(admin.ModelAdmin):
    list_display = ("code", "full_name", "phone", "city", "kyc_status")
    list_filter = ("kyc_status", "state")
    # Required by Order.autocomplete_fields.
    search_fields = ("code", "full_name", "phone", "email")


@admin.register(Vehicle)
class VehicleAdmin(admin.ModelAdmin):
    list_display = (
        "registration_number",
        "make",
        "model",
        "year",
        "hub",
        "rc_transfer_status",
    )
    list_filter = ("make", "fuel_type", "transmission", "rc_transfer_status")
    search_fields = ("registration_number", "make", "model")


@admin.register(Payment)
class PaymentAdmin(admin.ModelAdmin):
    list_display = (
        "reference",
        "order",
        "amount",
        "status",
        "method",
        "purpose",
        "initiated_at",
    )
    list_filter = ("status", "method", "purpose")
    search_fields = ("reference", "gateway_txn_id", "order__id")


@admin.register(Delivery)
class DeliveryAdmin(admin.ModelAdmin):
    list_display = ("order", "status", "scheduled_for", "attempts", "delivered_at")
    list_filter = ("status", "logistics_partner")
    search_fields = ("order__id", "tracking_reference")


@admin.register(OrderEvent)
class OrderEventAdmin(admin.ModelAdmin):
    list_display = ("order", "sequence", "event_type", "actor_name", "occurred_at")
    list_filter = ("event_type", "actor_type")
    search_fields = ("order__id", "event_type", "description")
    date_hierarchy = "occurred_at"


@admin.register(OrderDocument)
class OrderDocumentAdmin(admin.ModelAdmin):
    list_display = ("order", "doc_type", "status", "is_mandatory", "updated_at")
    list_filter = ("doc_type", "status")
    search_fields = ("order__id",)
