import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from celery import chord, group, shared_task
from django.conf import settings
from django.core.files.storage import default_storage
from django.db.models import F

from . import gemini, media
from .embeddings import embed
from .matching import ad_text, rank_ads, scene_text
from .models import Ad, Scene, Video
from .storage import download, pull_to_tmp, store

logger = logging.getLogger(__name__)


def _fail(video_id: int, exc: Exception) -> None:
    Video.objects.filter(pk=video_id).update(status=Video.Status.FAILED, error=str(exc)[:2000])


def _fetch_source(video: Video) -> str:
    """Get the source into storage and return its key. Uploads already have one."""
    if video.file_key:
        return video.file_key

    tmp = Path(tempfile.mkdtemp(prefix="ctxai-src-"))
    host = video.source_url.split("/")[2] if "//" in video.source_url else ""
    if "youtube" in host or "youtu.be" in host:
        local = _youtube(video.source_url, tmp)
    else:
        local = download(video.source_url, tmp / (Path(video.source_url).name or "source.mp4"))

    key = store(local, f"videos/{video.uuid}/{local.name}")
    Video.objects.filter(pk=video.pk).update(file_key=key)
    shutil.rmtree(tmp, ignore_errors=True)
    return key


def _youtube(url: str, into: Path) -> Path:
    """Thin, best-effort adapter: shell out to yt-dlp if it happens to be installed."""
    if shutil.which("yt-dlp") is None:
        raise RuntimeError("yt-dlp is not installed — upload a file or run `manage.py fetch_sample` instead")
    try:
        subprocess.run(
            ["yt-dlp", "-f", "mp4", "-o", str(into / "%(id)s.%(ext)s"), url],
            capture_output=True, text=True, check=True, timeout=600,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"could not download {url} — upload a file or pick the sample video") from exc
    files = sorted(into.iterdir())
    if not files:
        raise RuntimeError(f"yt-dlp produced no file for {url}")
    return files[0]


@shared_task
def process_video(video_id: int) -> str:
    """Steps 1-2 of the pipeline, then fan out scene detection and audio in parallel."""
    video = Video.objects.get(pk=video_id)
    try:
        Video.objects.filter(pk=video_id).update(
            status=Video.Status.PROCESSING, error="", scenes_total=0, scenes_done=0
        )
        video.file_key = _fetch_source(video)
        local = pull_to_tmp(video.file_key)

        info = media.probe(local)
        if info["duration"] > settings.MAX_VIDEO_SECONDS:
            raise ValueError(
                f"video is {info['duration']:.0f}s, over the {settings.MAX_VIDEO_SECONDS}s limit "
                "(raise MAX_VIDEO_SECONDS)"
            )
        Video.objects.filter(pk=video_id).update(**info)

        chord([detect_scenes.s(video_id), transcribe_audio.s(video_id)])(build_scenes.s(video_id))
        return f"probed {video.uuid}: {info['duration']:.0f}s {info['width']}x{info['height']}"
    except Exception as exc:
        _fail(video_id, exc)
        raise


@shared_task
def detect_scenes(video_id: int) -> list[dict]:
    """Steps 2-3: cut points, then keyframes uploaded to storage."""
    video = Video.objects.get(pk=video_id)
    try:
        local = pull_to_tmp(video.file_key)
        tmp = Path(tempfile.mkdtemp(prefix="ctxai-frames-"))
        scenes = []
        for i, (start, end) in enumerate(media.detect_scenes(local)):
            keys = []
            for j, at in enumerate(media.keyframe_times(start, end)):
                frame = media.extract_keyframe(local, at, tmp / f"{i:04d}_{j}.jpg")
                keys.append(store(frame, f"videos/{video.uuid}/frames/{i:04d}_{j}.jpg"))
            scenes.append({"start": start, "end": end, "keyframe_keys": keys})
        shutil.rmtree(tmp, ignore_errors=True)
        return scenes
    except Exception as exc:
        _fail(video_id, exc)
        raise


@shared_task
def transcribe_audio(video_id: int) -> list[dict]:
    """Step 4, in parallel with scene detection. No audio stream is not an error."""
    video = Video.objects.get(pk=video_id)
    try:
        if not video.has_audio:
            return []
        local = pull_to_tmp(video.file_key)
        tmp = Path(tempfile.mkdtemp(prefix="ctxai-audio-"))
        segments = media.transcribe(media.extract_audio(local, tmp / "audio.wav"))
        shutil.rmtree(tmp, ignore_errors=True)
        return segments
    except Exception as exc:
        _fail(video_id, exc)
        raise


@shared_task
def build_scenes(results: list, video_id: int) -> str:
    """Steps 5-6: align transcript to cuts and persist. Phase 4 fans out analysis from here."""
    scenes, segments = results
    try:
        texts = media.align([(s["start"], s["end"]) for s in scenes], segments)
        Scene.objects.filter(video_id=video_id).delete()  # idempotent on reprocess
        Scene.objects.bulk_create([
            Scene(
                video_id=video_id,
                index=i,
                start=s["start"],
                end=s["end"],
                keyframe_keys=s["keyframe_keys"],
                transcript_text=text,
            )
            for i, (s, text) in enumerate(zip(scenes, texts))
        ])
        Video.objects.filter(pk=video_id).update(scenes_total=len(scenes), scenes_done=0)

        scene_ids = list(
            Scene.objects.filter(video_id=video_id).order_by("index").values_list("pk", flat=True)
        )
        if scene_ids:
            # the errback belongs on the chord body; celery rejects links on a group header
            chord(
                group(analyze_scene.s(pk) for pk in scene_ids),
                finish_video.s(video_id).on_error(fail_video.s(video_id=video_id)),
            ).apply_async()
        else:
            Video.objects.filter(pk=video_id).update(status=Video.Status.DONE)
        return f"built {len(scenes)} scenes, {len(segments)} transcript segments"
    except Exception as exc:
        _fail(video_id, exc)
        raise


TRANSIENT_STATUS = {429, 500, 502, 503, 504}  # rate limit / overloaded, worth re-queuing


@shared_task(bind=True, rate_limit=settings.SCENE_ANALYSIS_RATE, max_retries=settings.SCENE_RETRIES)
def analyze_scene(self, scene_id: int) -> int:
    """Steps 7-9 for one scene: Gemini tags, embedding, ad match, rationale.

    Rate-limited quota exhaustion is re-queued a minute later; anything else is logged
    and the scene left untagged. Either way the chord must not be stranded, so a scene
    only ever fails quietly — never by raising past the retry budget.
    """
    scene = Scene.objects.get(pk=scene_id)
    try:
        frames = []
        for key in scene.keyframe_keys:
            with default_storage.open(key, "rb") as f:
                frames.append(f.read())

        analysis = gemini.analyze_scene(frames, scene.transcript_text)
        scene.description = analysis.description
        scene.objects_seen = analysis.objects
        scene.tone = analysis.tone
        scene.iab_categories = analysis.iab_categories
        scene.embedding = embed([scene_text(scene)])[0]

        matches = rank_ads(scene)
        scene.top_matches = [{"ad_id": ad.pk, "score": round(score, 4)} for ad, score in matches]
        if matches:
            best, score = matches[0]
            fit = gemini.write_rationale(scene, best)
            scene.recommended_ad = best
            scene.match_score = score
            scene.rationale = fit.rationale
            scene.brand_safety_flag = fit.brand_safety_flag
        scene.save()
    except Exception as exc:
        if getattr(exc, "code", None) in TRANSIENT_STATUS and self.request.retries < self.max_retries:
            # raises Retry, so the increment below is skipped and the scene is not
            # double-counted. A minute per attempt: the free-tier window is per minute.
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        logger.exception("scene %s analysis failed", scene_id)

    # F() because these tasks land concurrently
    Video.objects.filter(pk=scene.video_id).update(scenes_done=F("scenes_done") + 1)
    return scene_id


@shared_task
def finish_video(scene_ids: list, video_id: int) -> str:
    Video.objects.filter(pk=video_id).update(status=Video.Status.DONE)
    return f"analyzed {len(scene_ids)} scenes"


@shared_task
def fail_video(*args, video_id: int | None = None) -> None:
    """Chord errback. analyze_scene swallows its own errors, so reaching this means the
    worker itself died (OOM, kill, segfault) — without it the video sits in `processing`.

    *args because celery passes errbacks a different shape depending on how they are
    attached; video_id comes through as a kwarg so it can never be positionally confused.
    """
    _fail(video_id, Exception(f"scene analysis did not complete: {args[-1] if args else 'worker lost'}"))


@shared_task
def embed_ad(ad_id: int) -> int:
    """Ads created through the API need an embedding too, or they can never be matched."""
    ad = Ad.objects.get(pk=ad_id)
    ad.embedding = embed([ad_text(ad)])[0]
    ad.save(update_fields=["embedding"])
    return ad_id
