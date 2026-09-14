from django.contrib import admin

from .models import AuditLog, Conversation, Message, ToolInvocationLog


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    fields = ("sequence", "role", "content", "tool_iterations", "latency_ms")
    readonly_fields = fields
    can_delete = False


@admin.register(Conversation)
class ConversationAdmin(admin.ModelAdmin):
    list_display = ("id", "user", "title", "is_active", "last_active_at")
    list_filter = ("is_active", "user__role")
    search_fields = ("id", "title", "user__username")
    inlines = [MessageInline]
    readonly_fields = ("context",)


@admin.register(ToolInvocationLog)
class ToolInvocationLogAdmin(admin.ModelAdmin):
    list_display = (
        "tool_name",
        "user",
        "ok",
        "error_code",
        "duration_ms",
        "cache_hit",
        "created_at",
    )
    list_filter = ("tool_name", "ok", "cache_hit", "error_code")
    search_fields = ("trace_id", "tool_name")
    date_hierarchy = "created_at"


@admin.register(AuditLog)
class AuditLogAdmin(admin.ModelAdmin):
    """Audit rows are immutable — readable in admin, never editable."""

    list_display = (
        "created_at",
        "username",
        "role",
        "action",
        "succeeded",
        "duration_ms",
        "trace_id",
    )
    list_filter = ("action", "succeeded", "role")
    search_fields = ("username", "trace_id", "query_text")
    date_hierarchy = "created_at"

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
