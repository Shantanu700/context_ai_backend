from django.core.files.storage import default_storage
from rest_framework import serializers

from .models import Ad, Scene, Video


class AdSerializer(serializers.ModelSerializer):
    class Meta:
        model = Ad
        exclude = ("embedding",)


class VideoStatusSerializer(serializers.ModelSerializer):
    progress = serializers.SerializerMethodField()
    scenes_failed = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = (
            "uuid", "status", "error", "duration", "width", "height", "has_audio",
            "scenes_total", "scenes_done", "scenes_failed", "progress", "created_at", "updated_at",
        )

    def get_progress(self, obj) -> str:
        return f"{obj.scenes_done}/{obj.scenes_total} scenes analyzed"

    def get_scenes_failed(self, obj) -> int:
        """scenes_done counts attempts, so compare it against what actually got tagged —
        counting untagged scenes alone would report every pending scene as a failure."""
        return max(obj.scenes_done - obj.scenes.exclude(description="").count(), 0)


class VideoJobSerializer(serializers.Serializer):
    """What POST /videos hands back: the video's public id and the queued Celery job."""

    uuid = serializers.UUIDField(read_only=True)
    job_id = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)


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
