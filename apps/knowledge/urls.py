from django.urls import path

from . import views

urlpatterns = [
    path("search/", views.KnowledgeSearchView.as_view(), name="knowledge-search"),
    path(
        "documents/",
        views.KnowledgeDocumentListView.as_view(),
        name="knowledge-documents",
    ),
]
