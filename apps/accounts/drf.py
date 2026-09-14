"""DRF permission classes backed by scopes, and a scope-aware JWT serializer."""

from __future__ import annotations

from rest_framework.permissions import BasePermission
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer


class HasScope(BasePermission):
    """
    Require every scope listed in the view's `required_scopes`.

    Usage:
        class OrderDetail(APIView):
            permission_classes = [IsAuthenticated, HasScope]
            required_scopes = (Scope.ORDERS_READ,)
    """

    message = "Your role does not grant access to this resource."

    def has_permission(self, request, view) -> bool:
        required = tuple(getattr(view, "required_scopes", ()))
        if not required:
            return True
        user = request.user
        if not user or not user.is_authenticated:
            return False
        return user.has_all_scopes(required)


class ScopedTokenObtainPairSerializer(TokenObtainPairSerializer):
    """Embeds role and scopes in the access token for downstream services."""

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)
        token["role"] = user.role
        token["scopes"] = sorted(str(s) for s in user.scopes)
        token["employee_id"] = user.employee_id
        return token
