from __future__ import annotations

import asyncio
import httpx
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

import yt_dlp
from yt_dlp.version import __version__ as ytdlp_version

from bot.config import settings
from bot.errors import DownloadError, FileTooLargeError
from bot.system_dependencies import require_ffmpeg

logger = logging.getLogger(__name__)
ProgressCallback = Callable[[dict[str, Any]], None]
CancelCallback = Callable[[], bool]


@dataclass(slots=True)
class FormatOption:
    key: str
    label: str
    kind: str
    format_id: str
    height: int | None
    abr: float | None
    ext: str
    size: int | None
    fastest: bool = False
    has_audio: bool = False


@dataclass(slots=True)
class MediaInfo:
    title: str
    webpage_url: str
    thumbnail: str | None
    duration: int | None
    uploader: str | None
    view_count: int | None
    upload_date: str | None
    formats: list[FormatOption]


@dataclass(slots=True)
class PlaylistItem:
    title: str
    webpage_url: str
    duration: int | None
    uploader: str | None
    thumbnail: str | None


@dataclass(slots=True)
class PlaylistInfo:
    title: str
    webpage_url: str
    thumbnail: str | None
    uploader: str | None
    items: list[PlaylistItem]


@dataclass(slots=True)
class DownloadResult:
    path: Path
    title: str
    kind: str
    thumbnail: Path | None = None


class YTDLPService:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)
        self._cookie_work_file = settings.cookie_work_file

    @staticmethod
    def _js_runtimes() -> dict[str, dict[str, str]] | None:
        runtimes: dict[str, dict[str, str]] = {}
        for item in settings.ytdlp_js_runtimes:
            name, _, path = item.partition(":")
            name = name.strip().lower()
            path = path.strip()
            if not name:
                continue
            runtimes[name] = {"path": path} if path else {}
        return runtimes or None

    def _base(self, output_dir: Path) -> dict[str, Any]:
        options: dict[str, Any] = {
            "outtmpl": str(output_dir / "%(title).150B [%(id)s].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "windowsfilenames": True,
            "retries": 3,
            "fragment_retries": 3,
            "file_access_retries": 3,
            "socket_timeout": 30,
            "continuedl": True,
            "part": True,
        }
        if js_runtimes := self._js_runtimes():
            options["js_runtimes"] = js_runtimes
        if cookie_file := self._validated_cookie_file():
            options["cookiefile"] = str(cookie_file)
        return options

    def _validated_cookie_file(self) -> Path | None:
        configured = settings.ytdlp_cookie_file
        if configured is None:
            logger.debug("No yt-dlp cookie file configured; continuing without authentication.")
            return None

        path = configured.expanduser()
        if not path.exists():
            logger.info(
                "Configured yt-dlp cookie file is absent; continuing without cookies: %s",
                path,
            )
            return None
        if not path.is_file():
            raise DownloadError(f"YTDLP_COOKIES_FILE is not a regular file: {path}")
        if not os.access(path, os.R_OK):
            raise DownloadError(f"YouTube cookie file is not readable: {path}")
        try:
            if path.stat().st_size == 0:
                raise DownloadError(f"YouTube cookie file is empty: {path}")
            if path.stat().st_mode & 0o077:
                raise DownloadError(
                    "YouTube cookie file permissions are too open. "
                    f"Run: chmod 600 {path}"
                )
            with path.open("r", encoding="utf-8", errors="replace") as cookie_stream:
                lines = [line.rstrip("\r\n") for line in cookie_stream if line.strip()]
        except OSError as exc:
            raise DownloadError(f"Could not read YouTube cookie file: {path}") from exc

        header_ok = any(
            line.startswith(("# Netscape HTTP Cookie File", "# HTTP Cookie File"))
            for line in lines[:5]
        )
        cookie_row_ok = any(
            not line.startswith("#") and len(line.split("\t")) >= 7 for line in lines
        )
        if not header_ok or not cookie_row_ok:
            raise DownloadError(
                "YouTube cookie file is not valid Netscape cookies.txt format. "
                "Export it again from your local browser."
            )

        try:
            self._cookie_work_file.parent.mkdir(parents=True, exist_ok=True)
            valid_lines = [
                line for line in lines
                if line.startswith("#") or len(line.split("\t")) >= 7
            ]
            self._cookie_work_file.write_text(
                "\n".join(valid_lines) + "\n",
                encoding="utf-8",
            )
            self._cookie_work_file.chmod(0o600)
        except OSError as exc:
            raise DownloadError(
                "Could not create the private writable yt-dlp cookie copy."
            ) from exc

        logger.info(
            "Using validated YouTube cookies through a private writable copy."
        )
        return self._cookie_work_file

    @staticmethod
    def _download_error(exc: Exception) -> DownloadError:
        message = str(exc)
        lowered = message.lower()
        cookies_configured = bool(
            settings.ytdlp_cookie_file
            and settings.ytdlp_cookie_file.expanduser().is_file()
        )
        if "sign in to confirm you" in lowered and "not a bot" in lowered:
            if cookies_configured:
                detail = (
                    "YouTube rejected the authenticated request. The exported cookies may "
                    "be expired or invalid, or YouTube may still be challenging the hosting "
                    "provider/datacenter IP. Export fresh Netscape cookies and run the "
                    "diagnostic; cookies cannot guarantee that a cloud IP will be accepted."
                )
            else:
                detail = (
                    "YouTube challenged the hosting provider/datacenter IP. Configure a "
                    "valid Netscape cookie file with YTDLP_COOKIES_FILE and try again."
                )
            logger.warning("YouTube bot-check challenge (yt-dlp %s): %s", ytdlp_version, detail)
            return DownloadError(detail)
        if "cookies" in lowered and any(
            marker in lowered for marker in ("expired", "invalid", "login", "authentication")
        ):
            detail = (
                "YouTube authentication cookies appear invalid or expired. Export a fresh "
                "Netscape cookies.txt file from your local authenticated browser session."
            )
            logger.warning("YouTube cookie authentication failed (yt-dlp %s).", ytdlp_version)
            return DownloadError(detail)
        if "unsupported url" in lowered:
            return DownloadError("This URL is not supported by the installed yt-dlp version.")
        if "universal data for rehydration" in lowered:
            detail = (
                "TikTok did not return usable video data. Rebuild the Docker image to load "
                "the current yt-dlp TikTok extractor. If the image is current, TikTok is "
                "blocking or challenging the Google Cloud Shell IP; try again later or use "
                "a different network location."
            )
            logger.warning("TikTok webpage challenge failed (yt-dlp %s).", ytdlp_version)
            return DownloadError(detail)
        logger.warning("yt-dlp %s failed: %s", ytdlp_version, message)
        return DownloadError(message)

    @staticmethod
    def _thumbnail(info: dict[str, Any]) -> str | None:
        thumbnail = info.get("thumbnail")
        if thumbnail:
            return str(thumbnail)

        thumbnails = [
            item for item in (info.get("thumbnails") or [])
            if item and item.get("url")
        ]
        if thumbnails:
            best = max(
                thumbnails,
                key=lambda item: (
                    item.get("preference") or 0,
                    item.get("width") or 0,
                    item.get("height") or 0,
                ),
            )
            return str(best["url"])

        video_id = info.get("id")
        ie_key = str(
            info.get("ie_key")
            or info.get("extractor_key")
            or info.get("extractor")
            or ""
        ).lower()
        if video_id and len(str(video_id)) == 11 and "youtube" in ie_key:
            return f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg"

        return None

    @staticmethod
    def _download_remote_thumbnail(url: str, target: Path) -> bool:
        try:
            response = httpx.get(url, timeout=20.0)
            if response.status_code == 200 and response.content:
                target.write_bytes(response.content)
                return True
        except Exception as exc:
            logger.debug("Could not download remote thumbnail: %s", exc)
        return False

    @staticmethod
    def _create_thumbnail_from_video(source: Path, target: Path) -> bool:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    "00:00:03",
                    "-i",
                    str(source),
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(target),
                ],
                check=True,
            )
            return target.is_file()
        except Exception as exc:
            logger.debug("Could not generate video thumbnail from %s: %s", source, exc)
            return False

    async def inspect(self, url: str) -> MediaInfo:
        async with self._semaphore:
            return await asyncio.wait_for(
                asyncio.to_thread(self._inspect_sync, url),
                timeout=settings.download_timeout_seconds,
            )

    async def inspect_playlist(self, url: str) -> PlaylistInfo | None:
        async with self._semaphore:
            return await asyncio.wait_for(
                asyncio.to_thread(self._inspect_playlist_sync, url),
                timeout=settings.download_timeout_seconds,
            )

    def _entry_url(self, entry: dict[str, Any]) -> str | None:
        url = entry.get("webpage_url") or entry.get("url")
        if not url:
            return None
        if str(url).startswith(("http://", "https://")):
            return str(url)
        ie_key = str(entry.get("ie_key") or entry.get("extractor_key") or "").lower()
        if "youtube" in ie_key:
            return f"https://www.youtube.com/watch?v={url}"
        return None

    def _inspect_playlist_sync(self, url: str) -> PlaylistInfo | None:
        opts = self._base(settings.download_dir)
        opts.update({
            "skip_download": True,
            "extract_flat": "in_playlist",
            "noplaylist": False,
            "playlistend": settings.max_playlist_items,
        })
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError:
            raise
        except Exception as exc:
            error = self._download_error(exc)
            logger.info("URL is not a yt-dlp playlist or could not be inspected: %s", error)
            if "YouTube" in str(error):
                raise error from exc
            return None

        entries = info.get("entries") or []
        if info.get("_type") != "playlist" or not entries:
            return None

        items: list[PlaylistItem] = []
        for entry in entries:
            if not entry:
                continue
            entry_url = self._entry_url(entry)
            if not entry_url:
                continue
            items.append(PlaylistItem(
                title=entry.get("title") or "Untitled",
                webpage_url=entry_url,
                duration=entry.get("duration"),
                uploader=entry.get("uploader") or entry.get("channel"),
                thumbnail=self._thumbnail(entry),
            ))

        if not items:
            return None

        return PlaylistInfo(
            title=info.get("title") or "Untitled playlist",
            webpage_url=info.get("webpage_url") or url,
            thumbnail=self._thumbnail(info) or items[0].thumbnail,
            uploader=info.get("uploader") or info.get("channel"),
            items=items,
        )

    def _inspect_sync(self, url: str) -> MediaInfo:
        opts = self._base(settings.download_dir)
        opts["skip_download"] = True
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except DownloadError:
            raise
        except Exception as exc:
            raise self._download_error(exc) from exc

        raw_formats = info.get("formats") or []
        formats: list[FormatOption] = []

        # One representative format per resolution, preferring progressive MP4.
        by_height: dict[int, dict[str, Any]] = {}
        for fmt in raw_formats:
            h = fmt.get("height")
            if not h or fmt.get("vcodec") == "none":
                continue
            current = by_height.get(int(h))
            progressive = fmt.get("acodec") != "none"
            score = (
                progressive,
                fmt.get("ext") == "mp4",
                fmt.get("filesize") is not None,
                fmt.get("tbr") or 0,
            )
            if current is None:
                by_height[int(h)] = fmt
            else:
                old_score = (
                    current.get("acodec") != "none",
                    current.get("ext") == "mp4",
                    current.get("filesize") is not None,
                    current.get("tbr") or 0,
                )
                if score > old_score:
                    by_height[int(h)] = fmt

        fastest_height = None
        progressive_heights = [
            h for h, f in by_height.items() if f.get("acodec") != "none"
        ]
        if progressive_heights:
            # Progressive streams avoid separate audio/video merging.
            fastest_height = max(progressive_heights)

        for h in sorted(by_height, reverse=True):
            fmt = by_height[h]
            size = fmt.get("filesize") or fmt.get("filesize_approx")
            formats.append(FormatOption(
                key=f"v{h}",
                label=f"{h}p",
                kind="video",
                format_id=str(fmt["format_id"]),
                height=h,
                abr=None,
                ext=str(fmt.get("ext") or ""),
                size=size,
                fastest=h == fastest_height,
                has_audio=fmt.get("acodec") != "none",
            ))

        # Stable audio choices. yt-dlp selects the source; FFmpeg always creates MP3.
        for kbps in (320, 192, 128, 64):
            formats.append(FormatOption(
                key=f"a{kbps}",
                label=f"{kbps} kbps",
                kind="audio",
                format_id="bestaudio/best",
                height=None,
                abr=float(kbps),
                ext="mp3",
                size=None,
                fastest=kbps == 128,
            ))

        return MediaInfo(
            title=info.get("title") or "Untitled",
            webpage_url=info.get("webpage_url") or url,
            thumbnail=self._thumbnail(info),
            duration=info.get("duration"),
            uploader=info.get("uploader") or info.get("channel"),
            view_count=info.get("view_count"),
            upload_date=info.get("upload_date"),
            formats=formats[:14],
        )

    async def download(
        self,
        url: str,
        option: FormatOption,
        progress_callback: ProgressCallback,
        cancel_callback: CancelCallback | None = None,
    ) -> DownloadResult:
        async with self._semaphore:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                asyncio.to_thread(
                    self._download_sync,
                    url,
                    option,
                    lambda data: loop.call_soon_threadsafe(progress_callback, data),
                    cancel_callback,
                ),
                timeout=settings.download_timeout_seconds,
            )

    def _download_sync(
        self,
        url: str,
        option: FormatOption,
        progress_callback: ProgressCallback,
        cancel_callback: CancelCallback | None = None,
    ) -> DownloadResult:
        require_ffmpeg()
        job_dir = settings.download_dir / uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Created visible media job directory: %s (host: downloads/%s)",
            job_dir,
            job_dir.name,
        )
        opts = self._base(job_dir)
        def checked_progress(data: dict[str, Any]) -> None:
            if cancel_callback and cancel_callback():
                raise DownloadError("Download cancelled.")
            progress_callback(data)

        opts["progress_hooks"] = [checked_progress]

        if option.kind == "audio":
            opts.update({
                "format": "bestaudio/best",
                "postprocessors": [{
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": str(int(option.abr or 192)),
                }],
            })
        else:
            # Progressive formats already contain audio and must be selected
            # exactly. Adding "+bestaudio/best" can make the combination
            # unavailable and silently fall back to the site's best video.
            opts.update({
                "format": self._video_format_selector(option),
                "merge_output_format": "mp4",
                "postprocessors": [{
                    "key": "FFmpegVideoConvertor",
                    "preferedformat": "mp4",
                }],
            })

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)

            wanted = {".mp3"} if option.kind == "audio" else {".mp4"}
            outputs = [
                p for p in job_dir.rglob("*")
                if p.is_file() and p.suffix.lower() in wanted
            ]
            if not outputs:
                raise DownloadError("No final MP4/MP3 output was created.")

            path = max(outputs, key=lambda p: p.stat().st_size)
            if path.stat().st_size > settings.max_upload_bytes:
                raise FileTooLargeError(
                    f"Output is {path.stat().st_size / 1_000_000:.1f} MB; "
                    f"bot limit is {settings.max_upload_bytes / 1_000_000:.1f} MB."
                )

            thumbnail_path = None
            if option.kind == "video":
                remote_thumb = self._thumbnail(info)
                candidate = job_dir / "thumbnail.jpg"
                if remote_thumb and self._download_remote_thumbnail(remote_thumb, candidate):
                    thumbnail_path = candidate
                elif self._create_thumbnail_from_video(path, candidate):
                    thumbnail_path = candidate

            return DownloadResult(
                path=path,
                title=info.get("title") or path.stem,
                kind=option.kind,
                thumbnail=thumbnail_path,
            )
        except (DownloadError, FileTooLargeError):
            logger.warning("Media download failed; cleaning job directory: %s", job_dir)
            shutil.rmtree(job_dir, ignore_errors=True)
            raise
        except Exception as exc:
            logger.warning("Media download interrupted; cleaning job directory: %s", job_dir)
            shutil.rmtree(job_dir, ignore_errors=True)
            raise self._download_error(exc) from exc

    @staticmethod
    def cleanup(result: DownloadResult) -> None:
        shutil.rmtree(result.path.parent, ignore_errors=True)

    @staticmethod
    def _video_format_selector(option: FormatOption) -> str:
        if option.has_audio:
            return option.format_id
        # If no separate audio stream exists, retain the exact selected video
        # as a silent fallback instead of changing resolution via "/best".
        return f"{option.format_id}+bestaudio/{option.format_id}"
