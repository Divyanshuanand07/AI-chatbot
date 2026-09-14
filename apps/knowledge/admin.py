from django.contrib import admin

from .models import Chunk, Document


class ChunkInline(admin.TabularInline):
    model = Chunk
    extra = 0
    # The embedding itself is 512 floats — useless to a human and enormous in
    # a form, so it is excluded rather than shown readonly.
    fields = ("sequence", "heading", "char_count", "embedding_provider")
    readonly_fields = fields
    can_delete = False


@admin.register(Document)
class DocumentAdmin(admin.ModelAdmin):
    list_display = (
        "title",
        "category",
        "version",
        "owner_team",
        "effective_from",
        "is_active",
        "updated_at",
    )
    list_filter = ("category", "is_active", "owner_team")
    search_fields = ("title", "slug", "content")
    readonly_fields = ("checksum", "source_path")
    inlines = [ChunkInline]


@admin.register(Chunk)
class ChunkAdmin(admin.ModelAdmin):
    list_display = (
        "document",
        "sequence",
        "heading",
        "char_count",
        "embedding_provider",
    )
    list_filter = ("embedding_provider", "document__category")
    search_fields = ("heading", "content")
    exclude = ("embedding",)
