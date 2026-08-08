from __future__ import annotations

import logging
import shutil
from pathlib import Path

from bot.config import settings
from bot.errors import DownloadError

logger = logging.getLogger(__name__)


def local_file_path(path: Path) -> str | Path:
    """Prepare a file for local-path delivery in Docker or multipart locally."""
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError as exc:
        raise DownloadError(f"Upload file does not exist: {path}") from exc
    if not resolved.is_file():
        raise DownloadError(f"Upload file does not exist: {resolved}")
    return str(resolved) if settings.is_docker else resolved


def ensure_disk_space(required_bytes: int = 0) -> None:
    """Fail early while retaining a configurable safety margin."""
    target = settings.download_dir.resolve()
    target.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target).free
    required = max(0, required_bytes) + settings.min_free_disk_bytes
    if free < required:
        raise DownloadError(
            "Insufficient disk space: "
            f"{free / 1_000_000:.1f} MB free, "
            f"{required / 1_000_000:.1f} MB required including safety margin."
        )
    logger.debug("Disk preflight passed: %d bytes free", free)
