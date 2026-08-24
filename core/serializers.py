from django.core.files.storage import default_storage
from rest_framework import serializers

from django.contrib.auth import get_user_model

from .models import Ad, AdSlot, Scene, Tone, Video


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = get_user_model()
        fields = ("id", "username", "email", "is_staff")


class ToneRelatedField(serializers.SlugRelatedField):
    """Writes accept any tone name and create the Tone row if it doesn't exist yet."""

    def to_internal_value(self, data):
        tone, _ = Tone.objects.get_or_create(name=str(data).strip().lower())
        return tone


class ToneSerializer(serializers.ModelSerializer):
    class Meta:
        model = Tone
        fields = ("id", "name")


class AdSerializer(serializers.ModelSerializer):
    target_tone = ToneRelatedField(slug_field="name", queryset=Tone.objects.all(), required=False, allow_null=True)
    asset = serializers.FileField(write_only=True, required=False)
    asset_url = serializers.SerializerMethodField()
    is_embedded = serializers.SerializerMethodField()

    class Meta:
        model = Ad
        exclude = ("embedding", "asset_key")

    def get_is_embedded(self, obj) -> bool:
        return obj.embedding is not None

    def get_asset_url(self, obj) -> str | None:
        return default_storage.url(obj.asset_key) if obj.asset_key else None

    def create(self, validated_data):
        asset = validated_data.pop("asset", None)
        ad = super().create(validated_data)
        if asset:
            ad.asset_key = default_storage.save(f"ads/{ad.pk}/{asset.name}", asset)
            ad.save(update_fields=["asset_key"])
        return ad


class AdAssetSerializer(serializers.Serializer):
    """Backs the dedicated "replace the creative" action — never exposes the rest of `Ad`."""

    asset = serializers.FileField()

    def update(self, instance, validated_data):
        instance.asset_key = default_storage.save(
            f"ads/{instance.pk}/{validated_data['asset'].name}", validated_data["asset"]
        )
        instance.save(update_fields=["asset_key"])
        return instance


class VideoStatusSerializer(serializers.ModelSerializer):
    progress = serializers.SerializerMethodField()
    scenes_failed = serializers.SerializerMethodField()
    thumbnail_url = serializers.SerializerMethodField()

    class Meta:
        model = Video
        fields = (
            "uuid", "status", "error", "duration", "width", "height", "has_audio",
            "thumbnail_url", "scenes_total", "scenes_done", "scenes_failed", "progress",
            "detect_scenes_enabled", "transcribe_enabled", "analyze_enabled",
            "created_at", "updated_at",
        )

    def get_progress(self, obj) -> str:
        return f"{obj.scenes_done}/{obj.scenes_total} scenes analyzed"

    def get_thumbnail_url(self, obj) -> str | None:
        return default_storage.url(obj.thumbnail_key) if obj.thumbnail_key else None

    def get_scenes_failed(self, obj) -> int:
        """scenes_done counts attempts, so compare it against what actually got tagged —
        counting untagged scenes alone would report every pending scene as a failure."""
        return max(obj.scenes_done - obj.scenes.exclude(description="").count(), 0)


class VideoListSerializer(VideoStatusSerializer):
    """Same shape as the detail/status view, plus a direct link to the uploaded source file."""

    file_url = serializers.SerializerMethodField()

    class Meta(VideoStatusSerializer.Meta):
        fields = VideoStatusSerializer.Meta.fields + ("file_url",)

    def get_file_url(self, obj) -> str | None:
        return default_storage.url(obj.file_key) if obj.file_key else None


class VideoJobSerializer(serializers.Serializer):
    """What POST /videos hands back: the video's public id and the queued Celery job."""

    uuid = serializers.UUIDField(read_only=True)
    job_id = serializers.CharField(read_only=True)
    status = serializers.CharField(read_only=True)


class VideoCreateSerializer(serializers.Serializer):
    file = serializers.FileField(required=False)
    source_url = serializers.URLField(required=False)
    detect_scenes = serializers.BooleanField(required=False, default=True)
    transcribe = serializers.BooleanField(required=False, default=True)
    analyze = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        if bool(attrs.get("file")) == bool(attrs.get("source_url")):
            raise serializers.ValidationError("Provide exactly one of `file` or `source_url`.")
        return attrs


class VideoReprocessSerializer(serializers.Serializer):
    """Explicit opt-in only — nothing reruns unless asked for."""

    detect_scenes = serializers.BooleanField(required=False, default=False)
    transcribe = serializers.BooleanField(required=False, default=False)
    analyze = serializers.BooleanField(required=False, default=False)

    def validate(self, attrs):
        if not any(attrs.values()):
            raise serializers.ValidationError("Request at least one of detect_scenes, transcribe, analyze.")
        return attrs


class SceneSerializer(serializers.ModelSerializer):
    keyframe_urls = serializers.SerializerMethodField()
    recommended_ad = AdSerializer(read_only=True)
    tone = serializers.SlugRelatedField(slug_field="name", read_only=True)

    class Meta:
        model = Scene
        fields = (
            "index", "start", "end", "keyframe_urls", "transcript_text",
            "description", "objects_seen", "tone", "iab_categories",
            "recommended_ad", "match_score", "rationale", "brand_safety_flag",
        )

    def get_keyframe_urls(self, obj) -> list[str]:
        return [default_storage.url(key) for key in obj.keyframe_keys]


class AdSlotSerializer(serializers.ModelSerializer):
    """One ad placement. `ad` is written as a pk; `ad_detail` is what the editor renders."""

    ad_detail = AdSerializer(source="ad", read_only=True)

    class Meta:
        model = AdSlot
        fields = (
            "id", "scene", "ad", "ad_detail", "at_seconds", "duration",
            "placement", "is_overlay", "state", "score",
        )
        # ids come and go as the operator adds and deletes slots, so a PUT assigns fresh
        # ones rather than trying to preserve them
        read_only_fields = ("id",)

    def validate_duration(self, value):
        if value <= 0:
            raise serializers.ValidationError("Duration must be positive.")
        return value

    def validate_at_seconds(self, value):
        if value < 0:
            raise serializers.ValidationError("A slot cannot start before the video does.")
        return value

    def validate_scene(self, value):
        """A slot may only point at a scene of its own video."""
        video = self.context.get("video")
        if value is not None and video is not None and value.video_id != video.pk:
            raise serializers.ValidationError("That scene belongs to a different video.")
        return value
