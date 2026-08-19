from rest_framework import serializers

from .models import Ad, Video


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
