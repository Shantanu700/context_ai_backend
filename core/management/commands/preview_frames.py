import shutil
import time
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError

from core import media


def _timecode(value):
    """PySceneDetect reads a bare int as a frame number, so make plain numbers mean seconds."""
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return value  # HH:MM:SS, passed through


class Command(BaseCommand):
    help = "Detect scenes in a local video and write the keyframes to a folder. No DB, no Gemini."

    def add_arguments(self, parser):
        parser.add_argument("path")
        parser.add_argument("--out", default="frames_preview", help="output folder (recreated)")
        parser.add_argument("--threshold", type=float, help="ContentDetector threshold; lower cuts more")
        parser.add_argument("--start", help="start at, in seconds or HH:MM:SS")
        parser.add_argument("--end", help="stop at, in seconds or HH:MM:SS — samples a long video")

    def handle(self, *args, **opts):
        path = Path(opts["path"]).expanduser()
        if not path.is_file():
            raise CommandError(f"{path} is not a file")

        out = Path(opts["out"])
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(parents=True)

        info = media.probe(path)
        self.stdout.write(
            f"{path.name}: {info['duration']:.0f}s {info['width']}x{info['height']} "
            f"audio={info['has_audio']}"
        )

        began = time.monotonic()
        scenes = media.detect_scenes(
            path, opts["threshold"], _timecode(opts["start"]), _timecode(opts["end"])
        )
        detect_secs = time.monotonic() - began
        self.stdout.write(f"detected {len(scenes)} scenes in {detect_secs:.0f}s")

        began = time.monotonic()
        total = 0
        for i, (start, end) in enumerate(scenes):
            times = media.keyframe_times(start, end)
            for j, at in enumerate(times):
                media.extract_keyframe(path, at, out / f"{i:04d}_{j}_{at:.1f}s.jpg")
                total += 1
            self.stdout.write(f"  scene {i:>4}  {start:8.2f} -> {end:8.2f}s  ({end - start:5.2f}s)  {len(times)} frame(s)")

        self.stdout.write(
            self.style.SUCCESS(
                f"wrote {total} keyframes to {out}/ in {time.monotonic() - began:.0f}s — open {out}/"
            )
        )
