"""
Uniform API error envelope.

Every failure — expected or not — comes back as:

    {"error": {"code": "...", "message": "...", "details": {...}},
     "trace_id": "..."}

Unexpected exceptions are logged with the stack trace but never leak internals
to the client.
"""

from __future__ import annotations

import logging

from django.core.exceptions import ObjectDoesNotExist, PermissionDenied
from django.http import Http404
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

from .tracing import get_trace_id

logger = logging.getLogger(__name__)


class DomainError(Exception):
    """Base class for expected, client-reportable domain failures."""

    code = "domain_error"
    status_code = status.HTTP_400_BAD_REQUEST

    def __init__(self, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    code = "not_found"
    status_code = status.HTTP_404_NOT_FOUND


class ForbiddenError(DomainError):
    code = "forbidden"
    status_code = status.HTTP_403_FORBIDDEN


class ValidationError(DomainError):
    code = "validation_error"
    status_code = status.HTTP_400_BAD_REQUEST


class UpstreamError(DomainError):
    """A dependency we do not control failed (model provider, cache, ...)."""

    code = "upstream_error"
    status_code = status.HTTP_502_BAD_GATEWAY


def _envelope(code: str, message: str, details: dict | None = None) -> dict:
    body = {"error": {"code": code, "message": message}}
    if details:
        body["error"]["details"] = details
    trace_id = get_trace_id()
    if trace_id:
        body["trace_id"] = trace_id
    return body


def api_exception_handler(exc, context):
    if isinstance(exc, DomainError):
        return Response(
            _envelope(exc.code, exc.message, exc.details),
            status=exc.status_code,
        )

    if isinstance(exc, Http404 | ObjectDoesNotExist):
        return Response(
            _envelope("not_found", "The requested resource does not exist."),
            status=status.HTTP_404_NOT_FOUND,
        )

    if isinstance(exc, PermissionDenied):
        return Response(
            _envelope("forbidden", "You do not have access to this resource."),
            status=status.HTTP_403_FORBIDDEN,
        )

    response = drf_exception_handler(exc, context)

    if response is not None:
        detail = response.data
        message = "Request could not be processed."
        details = None
        if isinstance(detail, dict) and "detail" in detail:
            message = str(detail["detail"])
        elif isinstance(detail, dict):
            message = "One or more fields are invalid."
            details = detail
        elif isinstance(detail, list):
            message = "; ".join(str(item) for item in detail)

        code = {
            400: "bad_request",
            401: "unauthenticated",
            403: "forbidden",
            404: "not_found",
            405: "method_not_allowed",
            429: "rate_limited",
        }.get(response.status_code, "error")

        response.data = _envelope(code, message, details)
        return response

    # Unhandled — log loudly, respond opaquely.
    view = context.get("view")
    logger.exception(
        "unhandled_exception",
        extra={"view": type(view).__name__ if view else None},
    )
    return Response(
        _envelope(
            "internal_error",
            "An unexpected error occurred. Quote the trace id when reporting it.",
        ),
        status=status.HTTP_500_INTERNAL_SERVER_ERROR,
    )
