from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models

from .permissions import Role, Scope, scopes_for_role


class User(AbstractUser):
    """
    Operations user.

    Authorization is role-based: a role expands to a fixed set of scopes
    (see `permissions.py`). Roles are coarse on purpose — ops teams change
    people often, and per-user ACLs rot.
    """

    role = models.CharField(
        max_length=32,
        choices=Role.choices,
        default=Role.VIEWER,
        db_index=True,
    )
    employee_id = models.CharField(max_length=32, blank=True, db_index=True)
    team = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "accounts_user"

    def __str__(self) -> str:
        return f"{self.username} ({self.role})"

    @property
    def scopes(self) -> frozenset[str]:
        if self.is_superuser:
            return frozenset(Scope)
        return scopes_for_role(self.role)

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes

    def has_all_scopes(self, required: tuple[str, ...]) -> bool:
        return set(required).issubset(self.scopes)
