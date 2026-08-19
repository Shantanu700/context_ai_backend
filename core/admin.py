from django.contrib import admin

from .models import Ad, Scene, Video


class SceneInline(admin.TabularInline):
    model = Scene
    extra = 0
    fields = ("index", "start", "end", "tone", "recommended_ad", "brand_safety_flag")
    readonly_fields = fields
    show_change_link = True
    can_delete = False


@admin.register(Video)
class VideoAdmin(admin.ModelAdmin):
    list_display = ("uuid", "status", "duration", "progress", "created_at")
    list_filter = ("status",)
    readonly_fields = ("uuid", "created_at", "updated_at")
    inlines = [SceneInline]

    @admin.display(description="progress")
    def progress(self, obj):
        return f"{obj.scenes_done}/{obj.scenes_total}"


@admin.register(Ad)
class AdAdmin(admin.ModelAdmin):
    list_display = ("brand", "title", "target_tone", "has_embedding")
    search_fields = ("brand", "title", "description")
    exclude = ("embedding",)

    @admin.display(boolean=True, description="embedded")
    def has_embedding(self, obj):
        return obj.embedding is not None


@admin.register(Scene)
class SceneAdmin(admin.ModelAdmin):
    list_display = ("video", "index", "start", "end", "tone", "recommended_ad", "brand_safety_flag")
    list_filter = ("brand_safety_flag", "tone")
    search_fields = ("description", "transcript_text")
    raw_id_fields = ("video", "recommended_ad")
    exclude = ("embedding",)
