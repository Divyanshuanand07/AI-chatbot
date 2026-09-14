from __future__ import annotations

from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    scopes = serializers.SerializerMethodField()

    class Meta:
        model = User
        fields = (
            "id",
            "username",
            "email",
            "first_name",
            "last_name",
            "role",
            "employee_id",
            "team",
            "scopes",
        )
        read_only_fields = fields

    def get_scopes(self, obj: User) -> list[str]:
        return sorted(str(s) for s in obj.scopes)
