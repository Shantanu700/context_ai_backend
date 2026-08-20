import shutil
import subprocess
import tempfile
from pathlib import Path

from celery import chord, shared_task
from django.conf import settings

from . import media
from .models import Scene, Video
from .storage import download, pull_to_tmp, store


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
        Video.objects.filter(pk=video_id).update(
            scenes_total=len(scenes), scenes_done=0, status=Video.Status.DONE
        )
        return f"built {len(scenes)} scenes, {len(segments)} transcript segments"
    except Exception as exc:
        _fail(video_id, exc)
        raise
