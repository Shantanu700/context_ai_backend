"""Storage helpers. The backend itself (local FS vs R2) is chosen in settings.STORAGES."""

import shutil
import tempfile
import urllib.request
from pathlib import Path

from django.core.files import File
from django.core.files.storage import default_storage


def store(local_path: Path, key: str) -> str:
    """Put a local file into storage. Returns the key actually used."""
    with open(local_path, "rb") as f:
        return default_storage.save(key, File(f))


def pull_to_tmp(key: str) -> Path:
    """Copy a stored object to a local temp file — the pipeline only ever works on local paths.

    Cached by key so the scene-detection and audio tasks don't each re-download the
    same (possibly 850 MB) file.
    ponytail: cache is per-host /tmp; with workers on separate machines each host
    pulls once, which is the intended behaviour anyway.
    """
    dest = Path(tempfile.gettempdir()) / "ctxai" / key
    if dest.exists() and dest.stat().st_size == default_storage.size(key):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with default_storage.open(key, "rb") as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out)
    return dest


def download(url: str, dest: Path) -> Path:
    """Fetch a direct media URL to disk. Not a YouTube adapter — that lands in Phase 3."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, dest.open("wb") as out:  # noqa: S310 - operator-supplied URL
        shutil.copyfileobj(resp, out)
    return dest
