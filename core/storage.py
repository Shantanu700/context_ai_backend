"""Storage helpers. The backend itself (local FS vs R2) is chosen in settings.STORAGES."""

import logging
import shutil
import tempfile
import time
import urllib.request
from pathlib import Path

from django.conf import settings
from django.core.files import File
from django.core.files.storage import default_storage

logger = logging.getLogger(__name__)

TMP_ROOT = Path(tempfile.gettempdir()) / "ctxai"


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
    dest = TMP_ROOT / key
    if dest.exists() and dest.stat().st_size == default_storage.size(key):
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    with default_storage.open(key, "rb") as src, dest.open("wb") as out:
        shutil.copyfileobj(src, out)
    return dest


def purge_tmp(key: str) -> None:
    """Drop a cached pull once the tasks sharing it are done. Videos are large."""
    if not key:
        return
    path = TMP_ROOT / key
    path.unlink(missing_ok=True)
    for parent in path.parents:  # tidy the per-video directory, stop at TMP_ROOT
        if parent == TMP_ROOT or not parent.is_relative_to(TMP_ROOT):
            break
        try:
            parent.rmdir()
        except OSError:
            break  # not empty, leave it


def sweep_tmp(max_age_hours: float | None = None) -> int:
    """Delete cached pulls older than the cutoff.

    purge_tmp handles the happy path; this catches what a crashed or failed run left
    behind, so the disk cannot fill up over time. Called at the start of each pipeline
    run, which avoids needing celery beat just for this.
    """
    cutoff = time.time() - (max_age_hours or settings.TMP_CACHE_HOURS) * 3600
    removed = 0
    for path in TMP_ROOT.rglob("*"):
        if path.is_file() and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    for path in sorted(TMP_ROOT.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()  # only succeeds when empty
            except OSError:
                pass
    if removed:
        logger.info("swept %s stale cached video(s) from %s", removed, TMP_ROOT)
    return removed


def delete(key: str) -> None:
    """Remove a stored object. Used when a losing scene's keyframes are pruned."""
    if key:
        default_storage.delete(key)


def download(url: str, dest: Path) -> Path:
    """Fetch a direct media URL to disk. Not a YouTube adapter — that lands in Phase 3."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url) as resp, dest.open("wb") as out:  # noqa: S310 - operator-supplied URL
        shutil.copyfileobj(resp, out)
    return dest
