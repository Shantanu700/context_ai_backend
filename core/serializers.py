from django.core.files.storage import default_storage
from rest_framework import serializers

from .models import Ad, Scene, Video


class AdSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ad
        exclude = ("embedding",)


class VideoStatusSerializer(serializers.ModelSerializer):
    progress = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = (
            "uuid", "status", "error", "duration", "width", "height", "has_audio",
            "scenes_total", "scenes_done", "progress", "created_at", "updated_at",
        )

    def get_progress(self, obj) -> str:
        return f"{obj.scenes_done}/{obj.scenes_total} scenes analyzed"


class VideoCreateSerializer(serializers.Serializer):
    file = serializers.FileField(required=False)
    source_url = serializers.URLField(required=False)

    def validate(self, attrs):
        if bool(attrs.get("file")) == bool(attrs.get("source_url")):
            raise serializers.ValidationError("Provide exactly one of `file` or `source_url`.")
        return attrs


class SceneSerializer(serializers.ModelSerializer):
    keyframe_urls = serializers.SerializerMethodField()
    recommended_ad = AdSerializer(read_only=True)

    class Meta:
        model = Scene
        fields = (
            "index", "start", "end", "keyframe_urls", "transcript_text",
            "description", "objects_seen", "tone", "iab_categories",
            "recommended_ad", "match_score", "rationale", "brand_safety_flag",
        )

    def get_keyframe_urls(self, obj) -> list[str]:
        return [default_storage.url(key) for key in obj.keyframe_keys]
