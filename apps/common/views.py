"""Liveness and readiness probes."""

from __future__ import annotations

from django.core.cache import cache
from django.db import connection
from django.http import JsonResponse


def health(_request):
    """Liveness: the process is up. Deliberately checks no dependency."""
    return JsonResponse({"status": "ok"})


def readiness(_request):
    """
    Readiness: can we actually serve traffic?

    Database is required. Cache is reported but not fatal — the tool layer
    degrades to uncached reads rather than failing.
    """
    checks: dict[str, str] = {}
    ready = True

    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover - depends on infra state
        checks["database"] = f"error: {exc.__class__.__name__}"
        ready = False

    try:
        cache.set("_readyz", "1", 5)
        checks["cache"] = "ok" if cache.get("_readyz") == "1" else "degraded"
    except Exception as exc:  # pragma: no cover
        checks["cache"] = f"degraded: {exc.__class__.__name__}"

    return JsonResponse(
        {"status": "ready" if ready else "not_ready", "checks": checks},
        status=200 if ready else 503,
    )
