from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from shazamio import Shazam

from bot.config import settings
from bot.errors import DownloadError
from bot.system_dependencies import require_ffmpeg


@dataclass(slots=True)
class RecognizedTrack:
    title: str
    artist: str
    spotify_url: str | None
    query: str
    cover_url: str | None = None


class MusicFinderService:
    def __init__(self) -> None:
        self._shazam = Shazam()

    async def prepare_audio(self, source: Path) -> Path:
        require_ffmpeg()
        job_dir = settings.download_dir / f"recognize-{uuid4().hex}"
        job_dir.mkdir(parents=True, exist_ok=True)
        output = job_dir / "sample.mp3"

        process = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-t",
            "30",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "44100",
            "-b:a",
            "128k",
            str(output),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        if process.returncode != 0 or not output.exists():
            shutil.rmtree(job_dir, ignore_errors=True)
            raise DownloadError(
                "FFmpeg could not extract an audio sample: "
                + stderr.decode(errors="replace")[-500:]
            )
        return output

    async def recognize(self, audio_path: Path) -> RecognizedTrack:
        try:
            result = await self._shazam.recognize(str(audio_path))
        except Exception as exc:
            raise DownloadError(f"Music recognition failed: {exc}") from exc

        track = result.get("track") or {}
        title = track.get("title")
        artist = track.get("subtitle")
        if not title or not artist:
            raise DownloadError("No matching song was found.")

        spotify_url = None
        images = track.get("images") or {}
        cover_url = images.get("coverart") or images.get("background")
        for section in track.get("sections") or []:
            for action in section.get("actions") or []:
                uri = action.get("uri") or action.get("url")
                if isinstance(uri, str) and "spotify" in uri.lower():
                    spotify_url = uri
                    break
            if spotify_url:
                break

        return RecognizedTrack(
            title=title,
            artist=artist,
            spotify_url=spotify_url,
            query=f"{artist} - {title}",
            cover_url=cover_url,
        )

    @staticmethod
    def cleanup(path: Path) -> None:
        shutil.rmtree(path.parent, ignore_errors=True)
