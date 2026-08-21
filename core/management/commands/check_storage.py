import urllib.request
import uuid

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Round-trip a small object through the configured storage backend."

    def handle(self, *args, **opts):
        backend = type(default_storage).__name__
        self.stdout.write(f"STORAGE_BACKEND={settings.STORAGE_BACKEND} ({backend})")
        if settings.STORAGE_BACKEND == "r2":
            self.stdout.write(f"  bucket   {settings.STORAGES['default']['OPTIONS']['bucket_name']}")
            self.stdout.write(f"  endpoint {settings.STORAGES['default']['OPTIONS']['endpoint_url']}")

        key = f"healthcheck/{uuid.uuid4()}.txt"
        payload = b"context-ai storage check"
        try:
            saved = default_storage.save(key, ContentFile(payload))
            self.stdout.write(f"  write    ok -> {saved}")

            with default_storage.open(saved, "rb") as f:
                if f.read() != payload:
                    raise CommandError("read back different bytes than were written")
            self.stdout.write("  read     ok")

            url = default_storage.url(saved)
            self.stdout.write(f"  url      {url[:96]}{'...' if len(url) > 96 else ''}")
            if url.startswith("http"):
                # a presigned URL must work for an anonymous client — that is how the
                # frontend will fetch keyframes
                with urllib.request.urlopen(url, timeout=15) as resp:  # noqa: S310
                    if resp.read() != payload:
                        raise CommandError("presigned URL served different bytes")
                self.stdout.write("  fetch    ok (presigned URL is publicly retrievable)")
        finally:
            default_storage.delete(key)
            self.stdout.write("  delete   ok")

        self.stdout.write(self.style.SUCCESS("storage round-trip passed"))
