import base64

from django.contrib.auth import authenticate, login, logout
from django.core.files.storage import default_storage
from django.db import connection, transaction
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

from .models import Ad, AdSlot, Tone, Video
from .serializers import (
    AdAssetSerializer,
    AdSerializer,
    AdSlotSerializer,
    SceneSerializer,
    ToneSerializer,
    UserSerializer,
    VideoCreateSerializer,
    VideoJobSerializer,
    VideoListSerializer,
    VideoReprocessSerializer,
    VideoStatusSerializer,
)
from .slots import seed_slots
from .tasks import (
    dispatch_detect_and_transcribe,
    dispatch_transcribe_only,
    embed_ad,
    process_video,
    queue_analysis_or_finish,
)

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
class VideoViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.DestroyModelMixin, viewsets.GenericViewSet
):
    queryset = Video.objects.all()
    serializer_class = VideoStatusSerializer
    lookup_field = "uuid"

    def get_queryset(self):
        return self.queryset.filter(user=self.request.user)

    def get_serializer_class(self):
        # both carry file_url, which the editor needs to play anything
        if self.action in ("list", "retrieve"):
            return VideoListSerializer
        return super().get_serializer_class()

    @extend_schema(
        summary="List videos uploaded by the current user",
        responses={200: VideoListSerializer(many=True)},
    )
    def list(self, request, *args, **kwargs):
        return super().list(request, *args, **kwargs)

    @extend_schema(
        summary="One video, with a link to its source file",
        description="What the editor loads on a cold open. `/status` is the same minus `file_url`.",
        responses={200: VideoListSerializer, 404: OpenApiResponse(description="Unknown video")},
    )
    def retrieve(self, request, *args, **kwargs):
        return super().retrieve(request, *args, **kwargs)

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

        video = Video.objects.create(
            user=request.user,
            source_url=payload.validated_data.get("source_url", ""),
            detect_scenes_enabled=payload.validated_data["detect_scenes"],
            transcribe_enabled=payload.validated_data["transcribe"],
            analyze_enabled=payload.validated_data["analyze"],
        )
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
        summary="Re-run one or more pipeline stages",
        description=(
            "Re-trigger detect_scenes, transcribe and/or analyze for an existing video, reusing "
            "the stored source file, keyframes and scene boundaries instead of restarting the "
            "whole pipeline. Explicit opt-in only — at least one stage must be requested."
        ),
        request=VideoReprocessSerializer,
        responses={
            202: VideoJobSerializer,
            400: OpenApiResponse(description="No stage requested, or detect_scenes was never run"),
            404: OpenApiResponse(description="Unknown video"),
            409: OpenApiResponse(description="Video is already processing"),
        },
    )
    @action(detail=True, methods=["post"])
    def reprocess(self, request, uuid=None):
        video = self.get_object()
        payload = VideoReprocessSerializer(data=request.data)
        payload.is_valid(raise_exception=True)
        stages = payload.validated_data

        if video.status == Video.Status.PROCESSING:
            return Response(
                {"detail": "video is already processing"}, status=status.HTTP_409_CONFLICT
            )

        scene_ids = list(video.scenes.order_by("index").values_list("pk", flat=True))
        if not stages["detect_scenes"] and not scene_ids:
            return Response(
                {"detail": "run detect_scenes at least once before reprocessing other stages"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        video.detect_scenes_enabled = stages["detect_scenes"]
        video.transcribe_enabled = stages["transcribe"]
        video.analyze_enabled = stages["analyze"]
        video.status = Video.Status.PROCESSING
        video.error = ""
        video.save(update_fields=[
            "detect_scenes_enabled", "transcribe_enabled", "analyze_enabled", "status", "error",
        ])

        if stages["detect_scenes"]:
            async_result = dispatch_detect_and_transcribe(video.pk, stages["transcribe"])
        elif stages["transcribe"]:
            async_result = dispatch_transcribe_only(video.pk)
        else:  # analyze only
            async_result = queue_analysis_or_finish(video.pk, scene_ids)

        job_id = async_result.id if async_result else ""
        body = VideoJobSerializer({"uuid": video.uuid, "job_id": job_id, "status": video.status}).data
        return Response(body, status=status.HTTP_202_ACCEPTED)

    @extend_schema(
        summary="Scenes with tags, keyframes and the recommended ad",
        description=(
            "Ordered by scene index. `description`, `tone`, `iab_categories` and the "
            "recommendation are filled in by the per-scene analysis tasks. Once analysis "
            "finishes, scenes are pruned to at most one per uniquely recommended ad — "
            "scenes that matched no ad, or lost to a better-fit scene for the same ad, "
            "are deleted, so this list is often shorter than `scenes_total`."
        ),
        responses={200: SceneSerializer(many=True), 404: OpenApiResponse(description="Unknown video")},
    )
    @action(detail=True, methods=["get"], pagination_class=None)
    def scenes(self, request, uuid=None):
        qs = self.get_object().scenes.select_related("recommended_ad")
        return Response(SceneSerializer(qs, many=True).data)

    @extend_schema(
        methods=["GET"],
        summary="The ad plan for this video",
        description=(
            "Ordered by timecode. On the first read the list is drafted from the scenes that "
            "have a recommended ad; from then on it is whatever was last PUT here."
        ),
        responses={200: AdSlotSerializer(many=True), 404: OpenApiResponse(description="Unknown video")},
    )
    @extend_schema(
        methods=["PUT"],
        summary="Replace the ad plan",
        description=(
            "Send the whole slot list. Anything omitted is deleted. One call therefore covers "
            "adding, moving, editing and removing slots, as well as undo."
        ),
        request=AdSlotSerializer(many=True),
        responses={
            200: AdSlotSerializer(many=True),
            400: OpenApiResponse(description="A slot in the list is invalid"),
            404: OpenApiResponse(description="Unknown video"),
        },
    )
    @action(detail=True, methods=["get", "put"], pagination_class=None)
    def slots(self, request, uuid=None):
        video = self.get_object()

        if request.method == "PUT":
            payload = AdSlotSerializer(data=request.data, many=True, context={"video": video})
            payload.is_valid(raise_exception=True)
            # ponytail: last-write-wins whole-list replace. It is one endpoint instead of
            # four and makes undo a plain PUT of an earlier snapshot; swap in per-slot
            # PATCH if two people ever edit one video at once.
            with transaction.atomic():
                video.slots.all().delete()
                AdSlot.objects.bulk_create(
                    AdSlot(video=video, **fields) for fields in payload.validated_data
                )
                # an explicit plan, even an empty one, is never overwritten by a draft
                if not video.slots_seeded:
                    video.slots_seeded = True
                    video.save(update_fields=["slots_seeded"])
        else:
            seed_slots(video)

        qs = video.slots.select_related("ad").order_by("at_seconds")
        return Response(AdSlotSerializer(qs, many=True).data)

    @extend_schema(
        summary="Delete a video",
        description="Removes the video and its scenes. Does not remove the stored file/keyframes.",
        responses={204: None, 404: OpenApiResponse(description="Unknown video")},
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


@extend_schema(tags=["ads"])
class AdViewSet(
    mixins.ListModelMixin, mixins.RetrieveModelMixin, mixins.CreateModelMixin,
    mixins.DestroyModelMixin, viewsets.GenericViewSet,
):
    """The ad catalog matched against each scene."""

    queryset = Ad.objects.all()
    serializer_class = AdSerializer

    def perform_create(self, serializer):
        ad = serializer.save()
        embed_ad.delay(ad.pk)  # embedding in a worker: the model is 90 MB

    @extend_schema(
        summary="Replace an ad's creative asset",
        description="Uploads (or replaces) the video/overlay file previewed in the editor.",
        request=AdAssetSerializer,
        responses={200: AdSerializer},
    )
    @action(detail=True, methods=["put"])
    def asset(self, request, pk=None):
        ad = self.get_object()
        serializer = AdAssetSerializer(ad, data=request.data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(AdSerializer(ad).data)

    @extend_schema(
        summary="Delete an ad",
        description="Removes the ad from the catalog. Does not remove the stored asset file.",
        responses={204: None, 404: OpenApiResponse(description="Unknown ad")},
    )
    def destroy(self, request, *args, **kwargs):
        return super().destroy(request, *args, **kwargs)


@extend_schema(tags=["tones"])
class ToneViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
    """Existing tones to pick from when tagging a scene or an ad's target_tone."""

    queryset = Tone.objects.all()
    serializer_class = ToneSerializer
    pagination_class = None
