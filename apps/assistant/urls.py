from django.urls import path

from . import views

urlpatterns = [
    # The endpoint operators actually use.
    path("query/", views.AskView.as_view(), name="ai-query"),
    # Transparency surfaces.
    path("tools/", views.ToolCatalogueView.as_view(), name="ai-tools"),
    path(
        "conversations/",
        views.ConversationListView.as_view(),
        name="ai-conversations",
    ),
    path(
        "conversations/<uuid:conversation_id>/",
        views.ConversationDetailView.as_view(),
        name="ai-conversation-detail",
    ),
    path("audit/", views.AuditLogView.as_view(), name="ai-audit"),
]
