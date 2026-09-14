from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from apps.common.views import health, readiness

urlpatterns = [
    path("admin/", admin.site.urls),
    # Operational probes
    path("healthz", health, name="health"),
    path("readyz", readiness, name="readiness"),
    # Auth
    path("api/auth/", include("apps.accounts.urls")),
    # Plain operational read APIs (used by humans, dashboards and the AI tools)
    path("api/", include("apps.operations.urls")),
    # Natural-language assistant
    path("api/ai/", include("apps.assistant.urls")),
    # Knowledge base / SOPs
    path("api/knowledge/", include("apps.knowledge.urls")),
    # API docs
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "api/docs/",
        SpectacularSwaggerView.as_view(url_name="schema"),
        name="swagger-ui",
    ),
]
