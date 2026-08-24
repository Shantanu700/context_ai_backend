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
from .models import Ad, Scene, Tone, Video
from .storage import download, pull_to_tmp, purge_tmp, store, sweep_tmp

logger = logging.getLogger(__name__)


def _fail(video_id: int, exc: Exception) -> None:
    Video.objects.filter(pk=video_id).update(status=Video.Status.FAILED, error=str(exc)[:2000])


def _fetch_source(video: Video) -> str:
    if video.file_key:
        return video.file_key

    tmp = Path(tempfile.mkdtemp(prefix="ctxai-src-"))
    host = video.source_url.split("/")[2] if "//" in video.source_url else ""
    if "youtube" in host or "youtu.be" in host:
        local = _youtube(video.source_url, tmp)
    else:
        local = download(video.source_url, tmp / (Path(video.source_url).name or "source.mp4"))

    try:
        key = store(local, f"videos/{video.uuid}/{local.name}")
        Video.objects.filter(pk=video.pk).update(file_key=key)
        return key
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _youtube(url: str, into: Path) -> Path:
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
    video = Video.objects.get(pk=video_id)
    try:
        Video.objects.filter(pk=video_id).update(
            status=Video.Status.PROCESSING, error="", scenes_total=0, scenes_done=0
        )
        sweep_tmp()  # clear anything a previous failed run stranded
        video.file_key = _fetch_source(video)
        local = pull_to_tmp(video.file_key)

        info = media.probe(local)
        if info["duration"] > settings.MAX_VIDEO_SECONDS:
            raise ValueError(
                f"video is {info['duration']:.0f}s, over the {settings.MAX_VIDEO_SECONDS}s limit "
                "(raise MAX_VIDEO_SECONDS)"
            )
        Video.objects.filter(pk=video_id).update(**info)

        tmp = Path(tempfile.mkdtemp(prefix="ctxai-thumb-"))
        try:
            thumb = media.extract_keyframe(local, min(1.0, info["duration"]), tmp / "thumbnail.jpg")
            thumbnail_key = store(thumb, f"videos/{video.uuid}/thumbnail.jpg")
            Video.objects.filter(pk=video_id).update(thumbnail_key=thumbnail_key)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

        dispatch_detect_and_transcribe(video_id, video.transcribe_enabled)
        return f"probed {video.uuid}: {info['duration']:.0f}s {info['width']}x{info['height']}"
    except Exception as exc:
        _fail(video_id, exc)
        raise


@shared_task
def detect_scenes(video_id: int) -> list[dict]:
    video = Video.objects.get(pk=video_id)
    try:
        local = pull_to_tmp(video.file_key)
        tmp = Path(tempfile.mkdtemp(prefix="ctxai-frames-"))
        try:
            bounds = media.detect_scenes(local) if video.detect_scenes_enabled else [(0.0, video.duration)]
            scenes = []
            for i, (start, end) in enumerate(bounds):
                keys = []
                for j, at in enumerate(media.keyframe_times(start, end)):
                    frame = media.extract_keyframe(local, at, tmp / f"{i:04d}_{j}.jpg")
                    keys.append(store(frame, f"videos/{video.uuid}/frames/{i:04d}_{j}.jpg"))
                scenes.append({"start": start, "end": end, "keyframe_keys": keys})
            return scenes
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:
        _fail(video_id, exc)
        raise


@shared_task
def transcribe_audio(video_id: int) -> list[dict]:
    video = Video.objects.get(pk=video_id)
    try:
        if not video.transcribe_enabled or not video.has_audio:
            return []
        local = pull_to_tmp(video.file_key)
        tmp = Path(tempfile.mkdtemp(prefix="ctxai-audio-"))
        try:
            segments = media.transcribe(media.extract_audio(local, tmp / "audio.wav"))
            Video.objects.filter(pk=video_id).update(transcript_segments=segments)
            return segments
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:
        _fail(video_id, exc)
        raise


def queue_analysis_or_finish(video_id: int, scene_ids: list[int]):
    if scene_ids and Video.objects.values_list("analyze_enabled", flat=True).get(pk=video_id):
        Video.objects.filter(pk=video_id).update(scenes_done=0)
        return chord(
            group(analyze_scene.s(pk) for pk in scene_ids),
            finish_video.s(video_id).on_error(fail_video.s(video_id=video_id)),
        ).apply_async()
    Video.objects.filter(pk=video_id).update(status=Video.Status.DONE)
    return None


@shared_task
def build_scenes(results: list, video_id: int) -> str:
    scenes, segments = results
    try:
        purge_tmp(Video.objects.values_list("file_key", flat=True).get(pk=video_id))
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
        queue_analysis_or_finish(video_id, scene_ids)
        return f"built {len(scenes)} scenes, {len(segments)} transcript segments"
    except Exception as exc:
        _fail(video_id, exc)
        raise


@shared_task
def realign_scenes(segments: list[dict], video_id: int) -> str:
    """Re-align a fresh transcript onto EXISTING scenes, in place — unlike build_scenes,
    this never touches keyframe_keys or any prior Gemini analysis on those rows."""
    try:
        scenes = list(Scene.objects.filter(video_id=video_id).order_by("index"))
        texts = media.align([(s.start, s.end) for s in scenes], segments)
        for scene, text in zip(scenes, texts):
            scene.transcript_text = text
        Scene.objects.bulk_update(scenes, ["transcript_text"])
        queue_analysis_or_finish(video_id, [s.pk for s in scenes])
        return f"realigned {len(scenes)} scenes, {len(segments)} transcript segments"
    except Exception as exc:
        _fail(video_id, exc)
        raise


@shared_task
def frozen_transcript(video_id: int) -> list[dict]:
    """Stand-in for transcribe_audio when a reprocess reuses the transcript from a prior
    run instead of re-transcribing — same signature/shape, so build_scenes can't tell."""
    return Video.objects.values_list("transcript_segments", flat=True).get(pk=video_id)


def dispatch_detect_and_transcribe(video_id: int, transcribe_enabled: bool):
    """Full scene rebuild: used both for the first run and a detect_scenes reprocess."""
    transcribe_sig = transcribe_audio.s(video_id) if transcribe_enabled else frozen_transcript.s(video_id)
    return chord([detect_scenes.s(video_id), transcribe_sig])(build_scenes.s(video_id))


def dispatch_transcribe_only(video_id: int):
    """Re-transcribe and re-align onto existing scenes without touching scene boundaries."""
    return (transcribe_audio.s(video_id) | realign_scenes.s(video_id)).apply_async()


TRANSIENT_STATUS = {429, 500, 502, 503, 504}  # rate limit / overloaded, worth re-queuing


@shared_task(bind=True, rate_limit=settings.SCENE_ANALYSIS_RATE, max_retries=settings.SCENE_RETRIES)
def analyze_scene(self, scene_id: int) -> int:
    scene = Scene.objects.get(pk=scene_id)
    try:
        frames = []
        for key in scene.keyframe_keys:
            with default_storage.open(key, "rb") as f:
                frames.append(f.read())

        analysis = gemini.analyze_scene(frames, scene.transcript_text)
        scene.description = analysis.description
        scene.objects_seen = analysis.objects
        if analysis.tone:
            tone, _ = Tone.objects.get_or_create(name=analysis.tone.strip().lower())
            scene.tone = tone
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
            raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))
        logger.exception("scene %s analysis failed", scene_id)

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
