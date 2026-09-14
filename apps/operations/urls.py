from django.urls import path

from . import views

urlpatterns = [
    path("orders/", views.OrderSearchView.as_view(), name="order-search"),
    path("orders/<str:order_id>/", views.OrderDetailView.as_view(), name="order-detail"),
    path(
        "orders/<str:order_id>/summary/",
        views.OrderSummaryView.as_view(),
        name="order-summary",
    ),
    path(
        "orders/<str:order_id>/payments/",
        views.OrderPaymentsView.as_view(),
        name="order-payments",
    ),
    path(
        "orders/<str:order_id>/customer/",
        views.OrderCustomerView.as_view(),
        name="order-customer",
    ),
    path(
        "orders/<str:order_id>/vehicle/",
        views.OrderVehicleView.as_view(),
        name="order-vehicle",
    ),
    path(
        "orders/<str:order_id>/timeline/",
        views.OrderTimelineView.as_view(),
        name="order-timeline",
    ),
    path(
        "orders/<str:order_id>/delivery/",
        views.OrderDeliveryView.as_view(),
        name="order-delivery",
    ),
    path(
        "orders/<str:order_id>/documents/",
        views.OrderDocumentsView.as_view(),
        name="order-documents",
    ),
    path(
        "orders/<str:order_id>/finance/",
        views.OrderFinanceView.as_view(),
        name="order-finance",
    ),
    path(
        "orders/<str:order_id>/diagnosis/",
        views.OrderDiagnosisView.as_view(),
        name="order-diagnosis",
    ),
]
