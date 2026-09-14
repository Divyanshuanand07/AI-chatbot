from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin

from .models import User


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    list_display = ("username", "email", "role", "employee_id", "team", "is_active")
    list_filter = ("role", "team", "is_active", "is_staff")
    _OPERATIONS_FIELDSET = ("Operations", {"fields": ("role", "employee_id", "team")})

    fieldsets = (*BaseUserAdmin.fieldsets, _OPERATIONS_FIELDSET)
    add_fieldsets = (*BaseUserAdmin.add_fieldsets, _OPERATIONS_FIELDSET)
