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
    """Copy a stored object to a local temp file — the pipeline only ever works on local paths."""
    dest = Path(tempfile.mkdtemp(prefix="ctxai-")) / Path(key).name
    with default_storage.open(key, "rb") as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out)
    return dest


def download(url: str, dest: Path) -> Path:
    """Fetch a direct media URL to disk. Not a YouTube adapter — that lands in Phase 3."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, dest.open("wb") as out:  # noqa: S310 - operator-supplied URL
        shutil.copyfileobj(resp, out)
    return dest
