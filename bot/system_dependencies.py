from __future__ import annotations

import shutil

from bot.errors import DownloadError


FFMPEG_INSTALL_HINT = (
    "FFmpeg is not installed. Rebuild the bot image with: "
    "docker compose build --no-cache bot"
)


def require_ffmpeg() -> None:
    if shutil.which("ffmpeg") is None:
        raise DownloadError(FFMPEG_INSTALL_HINT)
