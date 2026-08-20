import subprocess
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import Video
from core.storage import download, store
from core.tasks import process_video


class Command(BaseCommand):
    help = "Register a sample video. Defaults to Meridian (Netflix Open Content, CC BY 4.0)."

    def add_arguments(self, parser):
        parser.add_argument("--url", default=settings.SAMPLE_VIDEO_URL, help="direct media URL to download")
        parser.add_argument("--path", help="use an already-downloaded local file instead of downloading")
        parser.add_argument("--process", action="store_true", help="dispatch the pipeline once registered")
        parser.add_argument(
            "--trim", type=float,
            help="register only the opening N seconds (stream copy). Meridian is 12 min of 4K, "
                 "which is a few hundred Gemini calls — an excerpt keeps a demo run affordable.",
        )

    def handle(self, *args, **opts):
        if opts["path"]:
            local = Path(opts["path"]).expanduser()
            if not local.is_file():
                raise CommandError(f"{local} is not a file")
        else:
            url = opts["url"]
            local = Path(settings.SAMPLE_DIR) / Path(url).name
            if local.exists():
                self.stdout.write(f"cached {local} ({local.stat().st_size / 1e6:.0f} MB)")
            else:
                self.stdout.write(f"downloading {url}\n  -> {local} (large file, no resume — be patient)")
                download(url, local)

        if opts["trim"]:
            excerpt = local.with_name(f"{local.stem}_first{int(opts['trim'])}s.mp4")
            if not excerpt.exists():
                self.stdout.write(f"trimming to {opts['trim']:.0f}s -> {excerpt.name}")
                subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-t", str(opts["trim"]),
                     "-i", str(local), "-c", "copy", "-y", str(excerpt)],
                    check=True,
                )
            local = excerpt

        video = Video.objects.create()
        video.file_key = store(local, f"videos/{video.uuid}/{local.name}")
        video.save(update_fields=["file_key"])
        self.stdout.write(self.style.SUCCESS(f"registered {video.uuid} at {video.file_key}"))

        if opts["process"]:
            job = process_video.delay(video.pk)
            self.stdout.write(f"dispatched job {job.id}")
        self.stdout.write(f"status: curl localhost:8000/videos/{video.uuid}/status")
