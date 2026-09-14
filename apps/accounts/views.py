from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.views import TokenObtainPairView

from .drf import ScopedTokenObtainPairSerializer
from .serializers import UserSerializer


class LoginView(TokenObtainPairView):
    """POST username/password -> access + refresh tokens carrying role/scopes."""

    serializer_class = ScopedTokenObtainPairSerializer


@extend_schema(
    summary="Current user, role and granted scopes",
    # APIView gives drf-spectacular nothing to introspect, so the response
    # shape has to be declared for the schema to be useful to clients.
    responses={200: UserSerializer},
)
class MeView(APIView):
    """Who am I, and what am I allowed to do?"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(UserSerializer(request.user).data)
