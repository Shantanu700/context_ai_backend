from django.core.files.storage import default_storage
from django.db import connection
from drf_spectacular.utils import OpenApiExample, OpenApiResponse, extend_schema, inline_serializer
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action, api_view
from rest_framework.response import Response

from .models import Ad, Video
from .serializers import (
    AdSerializer,
    SceneSerializer,
    VideoCreateSerializer,
    VideoJobSerializer,
    VideoStatusSerializer,
)
from .tasks import process_video

_health_schema = inline_serializer(
    name="Health",
    fields={"status": serializers.CharField(), "db": serializers.CharField()},
)


@extend_schema(
    summary="Liveness probe",
    description="Returns 503 with the connection error if Postgres is unreachable.",
    responses={200: _health_schema, 503: _health_schema},
    tags=["ops"],
)
@api_view(["GET"])
def health(request):
    try:
        connection.ensure_connection()
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
        return Response({"status": "error", "db": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
    return Response({"status": "ok", "db": "ok"})


@extend_schema(tags=["videos"])
class VideoViewSet(viewsets.GenericViewSet):
    queryset = Video.objects.all()
    serializer_class = VideoStatusSerializer
    lookup_field = "uuid"

    @extend_schema(
        summary="Upload a video and queue the pipeline",
        description=(
            "Send exactly one of `file` (multipart) or `source_url` (JSON). Returns immediately — "
            "all heavy work happens in a Celery worker. Poll `/videos/{uuid}/status` for progress."
        ),
        request=VideoCreateSerializer,
        responses={202: VideoJobSerializer},
        examples=[
            OpenApiExample("multipart upload", value={"file": "<binary>"}, request_only=True),
            OpenApiExample(
                "by URL", value={"source_url": "https://example.com/clip.mp4"}, request_only=True
            ),
        ],
    )
    def create(self, request):
        payload = VideoCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        upload = payload.validated_data.get("file")

        video = Video.objects.create(source_url=payload.validated_data.get("source_url", ""))
        if upload:
            video.file_key = default_storage.save(f"videos/{video.uuid}/{upload.name}", upload)
            video.save(update_fields=["file_key"])

        result = process_video.delay(video.pk)
        body = VideoJobSerializer({"uuid": video.uuid, "job_id": result.id, "status": video.status}).data
        return Response(body, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="Job progress",
        responses={200: VideoStatusSerializer, 404: OpenApiResponse(description="Unknown video")},
    )
    @action(detail=True, methods=["get"])
    def status(self, request, uuid=None):
        return Response(VideoStatusSerializer(self.get_object()).data)

    @extend_schema(
        summary="Scenes with tags, keyframes and the recommended ad",
        description=(
            "Ordered by scene index. `description`, `tone`, `iab_categories` and the "
            "recommendation are filled in by the per-scene analysis tasks."
        ),
        responses={200: SceneSerializer(many=True), 404: OpenApiResponse(description="Unknown video")},
    )
    # pagination_class=None: this action returns a bare array, and without it the
    # generated schema advertises a paginated envelope the view never sends
    @action(detail=True, methods=["get"], pagination_class=None)
    def scenes(self, request, uuid=None):
        qs = self.get_object().scenes.select_related("recommended_ad")
        return Response(SceneSerializer(qs, many=True).data)


@extend_schema(tags=["ads"])
class AdViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    """The ad catalog matched against each scene."""

    queryset = Ad.objects.all()
    serializer_class = AdSerializer
