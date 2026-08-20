import base64

from django.contrib.auth import authenticate, login, logout
from django.core.files.storage import default_storage
from django.db import connection
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import (
    OpenApiExample,
    OpenApiParameter,
    OpenApiResponse,
    extend_schema,
    inline_serializer,
)
from rest_framework import mixins, serializers, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.response import Response
from rest_framework.views import APIView

from .models import Ad, Video
from .serializers import (
    AdSerializer,
    SceneSerializer,
    UserSerializer,
    VideoCreateSerializer,
    VideoJobSerializer,
    VideoStatusSerializer,
)
from .tasks import embed_ad, process_video

# Everything is authenticated by DEFAULT_PERMISSION_CLASSES; views opt out with
# `authentication = False`, or per method with `authentication = {"post": False}`.

_health_schema = inline_serializer(
    name="Health",
    fields={"status": serializers.CharField(), "db": serializers.CharField()},
)


@extend_schema(tags=["ops"])
class HealthAV(APIView):
    """Liveness probe. Public — a probe that needs credentials is not a probe."""

    authentication = False

    @extend_schema(
        summary="Liveness probe",
        description="Returns 503 with the connection error if Postgres is unreachable.",
        responses={200: _health_schema, 503: _health_schema},
    )
    def get(self, request):
        try:
            connection.ensure_connection()
        except Exception as exc:  # noqa: BLE001 - report, don't crash the probe
            return Response(
                {"status": "error", "db": str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE
            )
        return Response({"status": "ok", "db": "ok"})


@extend_schema(tags=["auth"])
class LoginAV(APIView):
    """Session login/logout. POST is the only public method — it is how you get a session."""

    authentication = {"post": False}

    def decrypt_auth(self, meta_info) -> dict[str, str]:
        if not meta_info:
            return {}
        header, _, data = meta_info.partition(" ")
        if header != "Basic":
            return {}
        username, _, password = base64.b64decode(data).decode("utf-8").partition(":")
        return {"username": username, "password": password}

    @extend_schema(summary="Who am I", responses={200: UserSerializer})
    def get(self, request):
        return Response(UserSerializer(request.user).data)

    @extend_schema(
        summary="Log in and get a session cookie",
        request=None,
        parameters=[
            OpenApiParameter(
                name="Authorization",
                type=OpenApiTypes.STR,
                location=OpenApiParameter.HEADER,
                required=True,
                examples=[
                    OpenApiExample(
                        name="User Authentication",
                        value="Basic YWRtaW46cGFzc3dvcmQ=",
                        summary="base64 encoded username:password",
                    )
                ],
            )
        ],
        responses={200: UserSerializer, 401: OpenApiResponse(description="Bad credentials")},
    )
    def post(self, request):
        credentials = self.decrypt_auth(request.META.get("HTTP_AUTHORIZATION"))
        user = authenticate(request, **credentials) if credentials else None
        if user is None:
            raise AuthenticationFailed("Invalid credentials")
        login(request, user)
        return Response(UserSerializer(user).data)

    @extend_schema(summary="Log out", request=None, responses={204: None})
    def delete(self, request):
        logout(request)
        return Response(status=status.HTTP_204_NO_CONTENT)


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

    def perform_create(self, serializer):
        ad = serializer.save()
        embed_ad.delay(ad.pk)  # embedding in a worker: the model is 90 MB
