from django.core.files.storage import default_storage
from django.db import connection
from django.http import JsonResponse
from rest_framework import mixins, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import Ad, Video
from .serializers import AdSerializer, VideoCreateSerializer, VideoStatusSerializer
from .tasks import process_video


def health(request):
    try:
        connection.ensure_connection()
    except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
        return JsonResponse({"status": "error", "db": str(exc)}, status=503)
    return JsonResponse({"status": "ok", "db": "ok"})


class VideoViewSet(viewsets.GenericViewSet):
    queryset = Video.objects.all()
    serializer_class = VideoStatusSerializer
    lookup_field = "uuid"

    def create(self, request):
        payload = VideoCreateSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        upload = payload.validated_data.get("file")

        video = Video.objects.create(source_url=payload.validated_data.get("source_url", ""))
        if upload:
            video.file_key = default_storage.save(f"videos/{video.uuid}/{upload.name}", upload)
            video.save(update_fields=["file_key"])

        result = process_video.delay(video.pk)
        return Response(
            {"uuid": str(video.uuid), "job_id": result.id, "status": video.status},
            status=status.HTTP_202_ACCEPTED,
        )

    @action(detail=True, methods=["get"])
    def status(self, request, uuid=None):
        return Response(VideoStatusSerializer(self.get_object()).data)


class AdViewSet(mixins.ListModelMixin, mixins.CreateModelMixin, viewsets.GenericViewSet):
    queryset = Ad.objects.all()
    serializer_class = AdSerializer
