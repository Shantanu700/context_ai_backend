import time

from celery import shared_task
from django.db.models import F

from .models import Video
from .storage import pull_to_tmp

DUMMY_SCENES = 5


@shared_task
def process_video(video_id: int) -> str:
    """Phase 2 stub: proves upload -> queue -> progress -> status without touching media.

    Replaced by the real ffprobe/scenedetect/whisper orchestration in Phase 3.
    """
    video = Video.objects.get(pk=video_id)
    try:
        if video.file_key:
            local = pull_to_tmp(video.file_key)
            size = local.stat().st_size
            local.unlink()
        else:
            size = 0

        Video.objects.filter(pk=video_id).update(
            status=Video.Status.PROCESSING, scenes_total=DUMMY_SCENES, scenes_done=0, error=""
        )
        for _ in range(DUMMY_SCENES):
            time.sleep(1)
            # F() so concurrent per-scene tasks can't lose an increment
            Video.objects.filter(pk=video_id).update(scenes_done=F("scenes_done") + 1)

        Video.objects.filter(pk=video_id).update(status=Video.Status.DONE)
        return f"stub processed {video.uuid} ({size} bytes)"
    except Exception as exc:
        Video.objects.filter(pk=video_id).update(status=Video.Status.FAILED, error=str(exc))
        raise
