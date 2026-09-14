"""Request middleware: trace id propagation and access logging."""

from __future__ import annotations

import logging
import time

from .tracing import clear_trace_id, get_trace_id, set_trace_id

logger = logging.getLogger("apps.access")

TRACE_HEADER = "HTTP_X_TRACE_ID"
RESPONSE_HEADER = "X-Trace-Id"


class RequestTraceMiddleware:
    """
    Assigns (or adopts) a trace id per request and emits one access log line.

    An upstream gateway may already have issued a trace id; we honour it so
    traces stitch together across services.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        incoming = request.META.get(TRACE_HEADER, "").strip()
        trace_id = set_trace_id(incoming[:64] or None)
        request.trace_id = trace_id

        started = time.perf_counter()
        try:
            response = self.get_response(request)
        except Exception:
            logger.exception(
                "request_failed",
                extra={
                    "method": request.method,
                    "path": request.path,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            clear_trace_id()
            raise

        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        response[RESPONSE_HEADER] = trace_id

        # Probes would otherwise dominate the log volume.
        if request.path not in ("/healthz", "/readyz"):
            user = getattr(request, "user", None)
            logger.info(
                "request",
                extra={
                    "method": request.method,
                    "path": request.path,
                    "status": response.status_code,
                    "duration_ms": duration_ms,
                    "user_id": getattr(user, "id", None),
                    "role": getattr(user, "role", None),
                },
            )

        clear_trace_id()
        return response


__all__ = ["RequestTraceMiddleware", "get_trace_id"]
