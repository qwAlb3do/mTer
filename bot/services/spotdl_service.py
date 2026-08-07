from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from bot.config import settings
from bot.errors import DownloadError, FileTooLargeError
from bot.services.ytdlp_service import PlaylistInfo, PlaylistItem

logger = logging.getLogger(__name__)
SPOTIFY_URL_RE = re.compile(
    r"(?:https?://open\.spotify\.com/(?:intl-[a-z]{2}/)?|spotify:)"
    r"(?P<kind>track|album|playlist)[:/](?P<id>[A-Za-z0-9]{22})",
    re.IGNORECASE,
)


@dataclass(slots=True)
class SpotifyResult:
    path: Path
    title: str
    kind: str = "audio"


class SpotDLService:
    def __init__(self) -> None:
        self._semaphore = asyncio.Semaphore(settings.max_concurrent_downloads)

    def _credential_pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if settings.spotify_client_id and settings.spotify_client_secret:
            pairs.append((settings.spotify_client_id, settings.spotify_client_secret))

        if settings.spotify_apps_list:
            try:
                raw_pairs = json.loads(settings.spotify_apps_list)
                for item in raw_pairs:
                    if isinstance(item, (list, tuple)) and len(item) >= 2 and item[0] and item[1]:
                        pairs.append((str(item[0]), str(item[1])))
            except json.JSONDecodeError:
                logger.warning("SPOTIFY_APPS_LIST is not valid JSON.")

        random.shuffle(pairs)
        return pairs

    async def download(
        self,
        url_or_query: str,
        bitrate: int,
        cancel_event: asyncio.Event | None = None,
    ) -> SpotifyResult:
        async with self._semaphore:
            return await asyncio.wait_for(
                self._download(url_or_query, bitrate, cancel_event),
                timeout=settings.download_timeout_seconds,
            )

    async def inspect_playlist(self, url: str) -> PlaylistInfo:
        async with self._semaphore:
            return await asyncio.wait_for(
                asyncio.to_thread(self._inspect_playlist_sync, url),
                timeout=settings.spotify_metadata_timeout,
            )

    def _inspect_playlist_sync(self, url: str) -> PlaylistInfo:
        try:
            return self._inspect_with_spotipy(url)
        except Exception:
            logger.info("Spotipy Spotify metadata failed; falling back to spotDL.", exc_info=True)

        pairs = self._credential_pairs()
        try:
            from spotdl.types.playlist import Playlist
            from spotdl.utils.spotify import SpotifyClient

            if not pairs:
                raise DownloadError(
                    "Spotify API credentials are required when anonymous Spotify access is rate-limited."
                )
            if pairs:
                client_id, client_secret = pairs[0]
                SpotifyClient.init(
                    client_id=client_id,
                    client_secret=client_secret,
                    user_auth=False,
                    use_official_api=True,
                )

            metadata, songs = Playlist.get_metadata(url)
        except DownloadError:
            raise
        except Exception as exc:
            raise DownloadError("Could not read Spotify playlist metadata.") from exc

        items = [
            PlaylistItem(
                title=f"{song.artist} - {song.name}",
                webpage_url=song.url,
                duration=song.duration,
                uploader=song.artist,
                thumbnail=song.cover_url,
            )
            for song in songs[:settings.max_playlist_items]
        ]
        if not items:
            raise DownloadError("Spotify playlist has no supported tracks.")

        return PlaylistInfo(
            title=metadata.get("name") or "Spotify playlist",
            webpage_url=url,
            thumbnail=metadata.get("cover_url"),
            uploader=metadata.get("author_name"),
            items=items,
        )

    def _spotify_ref(self, url: str) -> tuple[str, str]:
        match = SPOTIFY_URL_RE.search(url)
        if not match:
            raise DownloadError("Unsupported Spotify URL. Send a Spotify track, album, or playlist link.")
        return match.group("kind").lower(), match.group("id")

    def _spotify_client(self):
        pairs = self._credential_pairs()
        if pairs:
            last_error: Exception | None = None
            for client_id, client_secret in pairs:
                try:
                    from spotipy import Spotify
                    from spotipy.oauth2 import SpotifyClientCredentials

                    auth = SpotifyClientCredentials(
                        client_id=client_id,
                        client_secret=client_secret,
                        requests_timeout=settings.spotify_metadata_timeout,
                    )
                    return Spotify(
                        auth_manager=auth,
                        requests_timeout=settings.spotify_metadata_timeout,
                        retries=0,
                        status_retries=0,
                        backoff_factor=0,
                    )
                except Exception as exc:
                    last_error = exc
            raise DownloadError("Could not initialize Spotify API client.") from last_error

        try:
            from spotipy import Spotify
            from spotipy_anon import SpotifyAnon

            return Spotify(
                auth_manager=SpotifyAnon(),
                requests_timeout=settings.spotify_metadata_timeout,
                retries=0,
                status_retries=0,
                backoff_factor=0,
            )
        except Exception:
            pass

        raise DownloadError("Spotify API access is unavailable.")

    @staticmethod
    def _best_image(images: list[dict[str, Any]] | None) -> str | None:
        if not images:
            return None
        image = max(
            images,
            key=lambda item: (
                item.get("width") or 0,
                item.get("height") or 0,
            ),
        )
        return image.get("url")

    def _track_item(self, track: dict[str, Any]) -> PlaylistItem | None:
        if not track or track.get("is_local") or track.get("type") != "track":
            return None

        track_id = track.get("id")
        url = (track.get("external_urls") or {}).get("spotify")
        if not url and track_id:
            url = f"https://open.spotify.com/track/{track_id}"
        if not url:
            return None

        artists = [artist.get("name") for artist in track.get("artists", []) if artist.get("name")]
        artist_text = ", ".join(artists) or None
        album = track.get("album") or {}
        return PlaylistItem(
            title=f"{artist_text} - {track.get('name')}" if artist_text else track.get("name", "Spotify track"),
            webpage_url=url,
            duration=int((track.get("duration_ms") or 0) / 1000) or None,
            uploader=artist_text,
            thumbnail=self._best_image(album.get("images")),
        )

    def _paged_items(self, spotify, first_page: dict[str, Any]) -> list[dict[str, Any]]:
        page = first_page
        items = list(page.get("items") or [])
        while page.get("next") and len(items) < settings.max_playlist_items:
            page = spotify.next(page)
            if not page:
                break
            items.extend(page.get("items") or [])
        return items[:settings.max_playlist_items]

    def _inspect_with_spotipy(self, url: str) -> PlaylistInfo:
        kind, spotify_id = self._spotify_ref(url)
        spotify = self._spotify_client()

        if kind == "track":
            track = spotify.track(spotify_id)
            item = self._track_item(track)
            if not item:
                raise DownloadError("Spotify track has no downloadable metadata.")
            return PlaylistInfo(
                title=item.title,
                webpage_url=url,
                thumbnail=item.thumbnail,
                uploader=item.uploader,
                items=[item],
            )

        if kind == "album":
            album = spotify.album(spotify_id)
            page_items = self._paged_items(spotify, spotify.album_tracks(spotify_id))
            items: list[PlaylistItem] = []
            for track in page_items:
                track["album"] = album
                item = self._track_item(track)
                if item:
                    items.append(item)
            if not items:
                raise DownloadError("Spotify album has no supported tracks.")
            return PlaylistInfo(
                title=album.get("name") or "Spotify album",
                webpage_url=url,
                thumbnail=self._best_image(album.get("images")) or items[0].thumbnail,
                uploader=", ".join(
                    artist.get("name") for artist in album.get("artists", []) if artist.get("name")
                ) or None,
                items=items,
            )

        playlist = spotify.playlist(spotify_id)
        page_items = self._paged_items(spotify, spotify.playlist_items(spotify_id))
        items = []
        for row in page_items:
            item = self._track_item(row.get("track") or row.get("item") or {})
            if item:
                items.append(item)
        if not items:
            raise DownloadError("Spotify playlist has no supported tracks.")

        return PlaylistInfo(
            title=playlist.get("name") or "Spotify playlist",
            webpage_url=url,
            thumbnail=self._best_image(playlist.get("images")) or items[0].thumbnail,
            uploader=(playlist.get("owner") or {}).get("display_name"),
            items=items,
        )

    async def _download(
        self,
        url_or_query: str,
        bitrate: int,
        cancel_event: asyncio.Event | None = None,
    ) -> SpotifyResult:
        job_dir = settings.download_dir / f"spotify-{uuid4().hex}"
        job_dir.mkdir(parents=True, exist_ok=True)
        command = [
            sys.executable, "-m", "spotdl", "download", url_or_query,
            "--output", str(job_dir / "{artists} - {title}.{output-ext}"),
            "--format", "mp3",
            "--bitrate", f"{bitrate}k",
            "--threads", "1",
            "--max-retries", "1",
        ]
        pairs = self._credential_pairs()
        if pairs:
            client_id, client_secret = pairs[0]
            command.extend([
                "--client-id", client_id,
                "--client-secret", client_secret,
                "--use-official-api",
            ])
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise DownloadError("spotDL is not installed or not available on PATH.") from exc

        communicate_task = asyncio.create_task(process.communicate())
        while not communicate_task.done():
            if cancel_event and cancel_event.is_set():
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
                communicate_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await communicate_task
                shutil.rmtree(job_dir, ignore_errors=True)
                raise DownloadError("Download cancelled.")
            await asyncio.sleep(0.5)

        stdout, stderr = await communicate_task
        output = (stdout + b"\n" + stderr).decode(errors="replace").strip()
        if process.returncode != 0:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise DownloadError(output[-1500:] or "spotDL failed.")

        candidates = list(job_dir.rglob("*.mp3"))
        if not candidates:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise DownloadError("spotDL completed but created no MP3 file.")

        path = max(candidates, key=lambda item: item.stat().st_size)
        if path.stat().st_size > settings.max_upload_bytes:
            size = path.stat().st_size / 1_000_000
            shutil.rmtree(job_dir, ignore_errors=True)
            raise FileTooLargeError(f"Spotify output is {size:.1f} MB, above the bot limit.")

        return SpotifyResult(path=path, title=path.stem)

    @staticmethod
    def cleanup(result: SpotifyResult) -> None:
        shutil.rmtree(result.path.parent, ignore_errors=True)
