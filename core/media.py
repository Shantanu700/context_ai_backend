"""Media inspection and extraction. Pure functions over local paths — no ORM, no Celery."""

import json
import subprocess
import wave
from pathlib import Path

import numpy as np
from django.conf import settings

KEYFRAME_WIDTH = 768
_whisper = None


def _run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, check=True).stdout


def probe(path: Path) -> dict:
    """duration / resolution / audio-stream presence via ffprobe."""
    data = json.loads(
        _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)])
    )
    video = next((s for s in data["streams"] if s["codec_type"] == "video"), None)
    if video is None:
        raise ValueError("file has no video stream")
    return {
        "duration": float(data["format"].get("duration") or video.get("duration") or 0),
        "width": int(video["width"]),
        "height": int(video["height"]),
        "has_audio": any(s["codec_type"] == "audio" for s in data["streams"]),
    }


def detect_scenes(path: Path) -> list[tuple[float, float]]:
    """PySceneDetect ContentDetector -> [(start_seconds, end_seconds)]."""
    from scenedetect import ContentDetector, detect

    scenes = detect(str(path), ContentDetector(threshold=settings.SCENE_THRESHOLD))
    if not scenes:  # single unbroken shot
        return [(0.0, probe(path)["duration"])]
    return [(s.get_seconds(), e.get_seconds()) for s, e in scenes]


def keyframe_times(start: float, end: float) -> list[float]:
    """1 frame for a short scene, else 2-3 evenly spaced away from the cut points."""
    length = end - start
    n = 1 if length < 5 else 2 if length < 15 else 3
    return [start + length * (i + 1) / (n + 1) for i in range(n)]


def extract_keyframe(path: Path, at: float, dest: Path) -> Path:
    """Downscaled JPEG. -ss before -i so ffmpeg seeks instead of decoding from 0."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run([
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{at:.3f}", "-i", str(path),
        # min() so a small source is never upscaled into a bigger, pricier frame
        "-frames:v", "1", "-vf", f"scale='min({KEYFRAME_WIDTH},iw)':-2", "-y", str(dest),
    ])
    return dest


def extract_audio(path: Path, dest: Path) -> Path:
    """Mono 16 kHz wav — what whisper wants."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", "16000", "-y", str(dest)])
    return dest


def transcribe(wav: Path) -> list[dict]:
    """faster-whisper with segment timestamps."""
    global _whisper
    if _whisper is None:
        from faster_whisper import WhisperModel

        _whisper = WhisperModel(settings.WHISPER_MODEL, device="cpu", compute_type="int8")

    # hand whisper the samples directly: ffmpeg already produced 16 kHz mono pcm_s16le,
    # so this skips a second decode (and pyav, whose bundled dylibs clash with cv2's)
    with wave.open(str(wav)) as w:
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0

    segments, _info = _whisper.transcribe(samples, vad_filter=True)
    return [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]


def align(bounds: list[tuple[float, float]], segments: list[dict]) -> list[str]:
    """Assign each transcript segment to the scene containing its midpoint."""
    texts: list[list[str]] = [[] for _ in bounds]
    for seg in segments:
        mid = (seg["start"] + seg["end"]) / 2
        for i, (start, end) in enumerate(bounds):
            if start <= mid < end:
                texts[i].append(seg["text"])
                break
        else:  # past the last cut (whisper can overrun the video by a hair)
            if bounds:
                texts[-1].append(seg["text"])
    return [" ".join(t).strip() for t in texts]
