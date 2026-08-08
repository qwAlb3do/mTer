from __future__ import annotations

import asyncio
import html
from io import BytesIO
import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import unquote, urlparse

import httpx
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, TelegramError
from telegram.ext import ContextTypes

from bot.errors import DownloadError, FileTooLargeError
from bot.download_guard import download_guard
from bot.config import settings
from bot.formatter import (
    escape,
    media_caption,
    media_keyboard,
    playlist_caption,
    playlist_keyboard,
    progress_bar,
    react_to_user,
    send_sticker,
)
from bot.handlers.common import registered
from bot.local_bot_api import ensure_disk_space, local_file_path
from bot.services.ytdlp_service import FormatOption, MediaInfo, PlaylistInfo, YTDLPService
from bot.services.spotdl_service import SpotDLService
from bot.users import users
from bot.utils import (
    extract_url,
    human_size,
    is_http_url,
    is_likely_playlist_url,
    is_spotify_playlist_url,
    is_spotify_url,
)

logger = logging.getLogger(__name__)
service = YTDLPService()
spotdl = SpotDLService()


class DownloadCancelled(DownloadError):
    pass


@dataclass(slots=True)
class DirectFileInfo:
    url: str
    filename: str
    content_type: str | None
    size: int | None


DIRECT_FILE_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".txt", ".csv", ".json", ".xml",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg",
    ".apk", ".epub", ".mobi",
}

DIRECT_FILE_CONTENT_TYPES = {
    "application/pdf",
    "application/zip",
    "application/x-zip-compressed",
    "application/x-rar-compressed",
    "application/x-7z-compressed",
    "application/json",
    "application/xml",
    "application/msword",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/epub+zip",
}

DIRECT_FILE_CONTENT_PREFIXES = ("image/", "text/")

MEDIA_CONTENT_PREFIXES = (
    "audio/",
    "video/",
)


def _log_background_failure(task: asyncio.Task) -> None:
    try:
        task.result()
    except BadRequest as exc:
        if _is_ignorable_edit_error(exc):
            return
        logger.warning("Could not update download progress message", exc_info=True)
    except TelegramError as exc:
        logger.debug("Could not update download progress message: %s", exc)
    except Exception:
        logger.warning("Could not update download progress message", exc_info=True)


def _is_ignorable_edit_error(exc: BadRequest) -> bool:
    message = str(exc).lower()
    return (
        "message is not modified" in message
        or "message to edit not found" in message
    )


async def _safe_delete_message(message) -> None:
    if not message:
        return
    try:
        await message.delete()
    except TelegramError:
        pass


def _cancel_keyboard(job_token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✖️ Cancel", callback_data=f"cancel:{job_token}")
    ]])


def _direct_file_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥 Download file", callback_data=f"direct:download:{token}")],
        [InlineKeyboardButton("✖️ Close", callback_data=f"close:{token}")],
    ])


def _busy_retry_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 Try again", callback_data=f"retry:url:{token}")
    ]])


def _busy_retry_text() -> str:
    return (
        "<b>⏳ Your current transfer is still running</b>\n\n"
        "To keep downloads reliable, only one download or Telegram upload can run "
        "for your account at a time. Wait for the current transfer to finish, then "
        "press <b>Try again</b>—you do not need to resend the URL."
    )


async def _safe_edit_text(message, text: str, **kwargs):
    try:
        return await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if _is_ignorable_edit_error(exc):
            return message
        raise


async def _reply_photo_or_text(message, *, photo: str | None, text: str, **kwargs):
    if photo:
        try:
            return await message.reply_photo(
                photo=photo,
                caption=text,
                **kwargs,
            )
        except TelegramError as exc:
            logger.info("Telegram could not fetch remote thumbnail; uploading it directly: %s", exc)
        try:
            async with httpx.AsyncClient(follow_redirects=True, timeout=20.0) as client:
                response = await client.get(photo)
                response.raise_for_status()
                content_type = response.headers.get("content-type", "")
                if not content_type.startswith("image/"):
                    raise ValueError(f"unexpected thumbnail content type: {content_type}")
                if len(response.content) > 10 * 1024 * 1024:
                    raise ValueError("thumbnail is larger than 10 MiB")
                image = BytesIO(response.content)
                image.name = "thumbnail.jpg"
                return await message.reply_photo(photo=image, caption=text, **kwargs)
        except (httpx.HTTPError, TelegramError, ValueError) as exc:
            logger.info("Could not upload thumbnail; falling back to text panel: %s", exc)
    return await message.reply_text(text, **kwargs)


async def _send_video_with_fallback(message, *, path: str, filename: str, caption: str,
                                    duration: int | None, height: int | None,
                                    thumbnail: str | None):
    """Retry without an optional thumbnail, then preserve delivery as a document."""
    common = {
        "filename": filename,
        "caption": caption,
        "duration": duration,
        "height": height,
        "supports_streaming": True,
        "read_timeout": settings.telegram_read_timeout,
        "write_timeout": settings.telegram_write_timeout,
    }
    try:
        return await message.reply_video(video=path, thumbnail=thumbnail, **common)
    except TelegramError as first_error:
        if thumbnail:
            logger.warning("Video upload with thumbnail failed; retrying without it: %s", first_error)
            try:
                return await message.reply_video(video=path, thumbnail=None, **common)
            except TelegramError as retry_error:
                logger.warning("Video retry failed; sending as document: %s", retry_error)
        else:
            logger.warning("Video upload failed; sending as document: %s", first_error)
        return await message.reply_document(
            document=path,
            filename=filename,
            caption=caption,
            read_timeout=settings.telegram_read_timeout,
            write_timeout=settings.telegram_write_timeout,
        )


def _direct_file_text(info: DirectFileInfo) -> str:
    return (
        f"<b>📁 File detected</b>\n\n"
        f"Name: <code>{escape(info.filename)}</code>\n"
        f"Type: <code>{escape(info.content_type or 'unknown')}</code>\n"
        f"Size: {human_size(info.size) if info.size else 'unknown'}\n\n"
        "Press Download file to fetch it."
    )


def _download_job(context: ContextTypes.DEFAULT_TYPE, job_token: str) -> dict | None:
    return context.bot_data.get("download_jobs", {}).get(job_token)


def _job_cancelled(context: ContextTypes.DEFAULT_TYPE, job_token: str) -> bool:
    job = _download_job(context, job_token)
    return bool(job and job["event"].is_set())


def _media_cache_key(url: str, option_key: str) -> str:
    # v2 invalidates file IDs cached while progressive selections could
    # incorrectly fall back to the site's best-quality format.
    return f"media:v2:{url}:{option_key}"


def _direct_cache_key(url: str) -> str:
    return f"direct:{url}"


def _get_session(context, store: str, token: str) -> dict | None:
    return context.bot_data.get(store, {}).get(token)


def _authorize_session(session: dict | None, user_id: int) -> bool:
    return bool(session and session.get("owner_id") == user_id)


async def _acquire_download_guard(
    query,
    user_id: int,
    message: str = "Finish your current task first.",
) -> bool:
    if not await download_guard.acquire(user_id):
        answer = getattr(query, "answer", None)
        if callable(answer):
            await answer(message, show_alert=True)
        elif getattr(query, "reply_text", None):
            await query.reply_text(message)
        return False
    return True


async def _send_cached_file(message, cached: dict, caption: str | None = None) -> bool:
    try:
        kind = cached.get("kind")
        file_id = cached.get("file_id")
        if not file_id:
            return False
        if kind == "audio":
            await message.reply_audio(file_id, caption=caption)
        elif kind == "video":
            await message.reply_video(file_id, caption=caption, supports_streaming=True)
        elif kind == "document":
            await message.reply_document(file_id, caption=caption)
        else:
            return False
        return True
    except TelegramError:
        return False


def _filename_from_headers(url: str, headers: httpx.Headers) -> str:
    disposition = headers.get("content-disposition", "")
    filename = ""
    if "filename*=" in disposition:
        filename = disposition.split("filename*=", 1)[1].split(";", 1)[0].strip()
        if "''" in filename:
            filename = filename.split("''", 1)[1]
    elif "filename=" in disposition:
        filename = disposition.split("filename=", 1)[1].split(";", 1)[0].strip()

    filename = unquote(filename.strip("\"' "))
    if not filename:
        filename = unquote(Path(urlparse(url).path).name)
    return filename or "downloaded-file"


def _content_length(headers: httpx.Headers) -> int | None:
    try:
        return int(headers.get("content-length") or "")
    except ValueError:
        return None


def _is_direct_file_type(filename: str, content_type: str | None, headers: httpx.Headers) -> bool:
    content_type = (content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(filename).suffix.lower()
    disposition = headers.get("content-disposition", "").lower()

    if content_type.startswith(MEDIA_CONTENT_PREFIXES):
        return False
    if suffix in DIRECT_FILE_EXTENSIONS:
        return True
    if "attachment" in disposition:
        return True
    if content_type == "application/octet-stream" and suffix:
        return True
    if content_type in DIRECT_FILE_CONTENT_TYPES:
        return True
    if content_type.startswith(DIRECT_FILE_CONTENT_PREFIXES) and content_type != "text/html":
        return True
    return False


async def _inspect_direct_file(url: str) -> DirectFileInfo | None:
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=10.0) as client:
            response = await client.head(url)
            response.raise_for_status()
    except httpx.HTTPError:
        filename = _filename_from_headers(url, httpx.Headers())
        content_type = mimetypes.guess_type(filename)[0]
        if Path(filename).suffix.lower() in DIRECT_FILE_EXTENSIONS:
            return DirectFileInfo(url, filename, content_type, None)
        return None

    filename = _filename_from_headers(str(response.url), response.headers)
    content_type = response.headers.get("content-type")
    size = _content_length(response.headers)
    if not _is_direct_file_type(filename, content_type, response.headers):
        return None
    return DirectFileInfo(str(response.url), filename, content_type, size)


async def _edit_panel_message(message, text: str, reply_markup, photo: str | None = None):
    if photo and message.photo:
        try:
            return await message.edit_media(
                media=InputMediaPhoto(photo, caption=text, parse_mode=ParseMode.HTML),
                reply_markup=reply_markup,
            )
        except BadRequest:
            pass
    if message.photo:
        await message.edit_caption(
            caption=text,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
        return message
    if photo:
        try:
            new_message = await _reply_photo_or_text(
                message,
                photo=photo,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=reply_markup,
            )
            await message.delete()
            return new_message
        except TelegramError:
            pass
    await _safe_edit_text(
        message,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=reply_markup,
    )
    return message


def _spotify_media_info(url: str) -> MediaInfo:
    return MediaInfo(
        title="Spotify audio",
        webpage_url=url,
        thumbnail=None,
        duration=None,
        uploader="Spotify / spotDL",
        view_count=None,
        upload_date=None,
        formats=[
            FormatOption("a320", "320 kbps", "audio", "spotdl", None, 320, "mp3", None),
            FormatOption("a192", "192 kbps", "audio", "spotdl", None, 192, "mp3", None),
            FormatOption("a128", "128 kbps", "audio", "spotdl", None, 128, "mp3", None, True),
            FormatOption("a64", "64 kbps", "audio", "spotdl", None, 64, "mp3", None),
        ],
    )


def _spotify_media_info_from_item(item) -> MediaInfo:
    info = _spotify_media_info(item.webpage_url)
    info.title = item.title
    info.thumbnail = item.thumbnail
    info.duration = item.duration
    info.uploader = item.uploader or "Spotify / spotDL"
    return info


async def _inspect_url(url: str, playlist_item=None) -> tuple[MediaInfo, bool]:
    if is_spotify_url(url):
        if playlist_item is not None:
            return _spotify_media_info_from_item(playlist_item), True
        return _spotify_media_info(url), True
    info = await service.inspect(url)
    if playlist_item is not None:
        info.thumbnail = info.thumbnail or playlist_item.thumbnail
    return info, False


async def _render_media_panel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    *,
    status=None,
    edit_message=None,
    back_playlist_token: str | None = None,
    playlist_item=None,
) -> None:
    try:
        info, spotify_url = await _inspect_url(url, playlist_item=playlist_item)
        token = token_urlsafe(6)
        context.bot_data.setdefault("media_sessions", {})[token] = {
            "url": url,
            "info": info,
            "spotify": spotify_url,
            "owner_id": update.effective_user.id,
            "created": time.time(),
            "back_playlist_token": back_playlist_token,
        }
        caption = media_caption(info, back_playlist_token=back_playlist_token)
        keyboard = media_keyboard(token, info, back_playlist_token=back_playlist_token)
        if edit_message:
            await _edit_panel_message(edit_message, caption, keyboard, photo=info.thumbnail)
        elif info.thumbnail:
            if status:
                await _safe_edit_text(status, "🖼 Metadata found. Opening panel…")
            sent = await _reply_photo_or_text(
                update.effective_message,
                photo=info.thumbnail,
                text=caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )
            if status and sent != status:
                await _safe_delete_message(status)
        else:
            target = status or update.effective_message
            if status:
                await _safe_edit_text(
                    status,
                    caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
            else:
                await target.reply_text(
                    caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )
    except DownloadError as exc:
        logger.warning("Media inspection failed for %s: %s", url, exc)
        detail = escape(str(exc))
        if edit_message:
            await _edit_panel_message(
                edit_message,
                f"<b>⚠️ Inspect failed</b>\n\n{detail}",
                None,
            )
        elif status:
            await _safe_edit_text(
                status,
                f"<b>⚠️ Inspect failed</b>\n\n{detail}",
                parse_mode=ParseMode.HTML,
            )
    except asyncio.TimeoutError:
        if edit_message:
            await _edit_panel_message(
                edit_message,
                "<b>⚠️ Inspect timed out</b>\n\nPlease try again later.",
                None,
            )
        elif status:
            await _safe_edit_text(
                status,
                "<b>⚠️ Inspect timed out</b>\n\nPlease try again later.",
                parse_mode=ParseMode.HTML,
            )


async def _show_playlist(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    status,
) -> bool:
    playlist: PlaylistInfo | None = None
    try:
        if is_spotify_url(url):
            playlist = await spotdl.inspect_playlist(url)
        else:
            playlist = await service.inspect_playlist(url)
    except asyncio.TimeoutError as exc:
        raise DownloadError("Playlist metadata timed out.") from exc
    if not playlist:
        return False

    token = token_urlsafe(6)
    context.bot_data.setdefault("playlist_sessions", {})[token] = {
        "url": url,
        "playlist": playlist,
        "owner_id": update.effective_user.id,
        "created": time.time(),
        "page": 0,
    }
    caption = playlist_caption(playlist, 0)
    keyboard = playlist_keyboard(token, playlist, 0)
    if playlist.thumbnail:
        await _safe_edit_text(status, "🖼 Playlist metadata found. Opening panel…")
        sent = await _reply_photo_or_text(
            update.effective_message,
            photo=playlist.thumbnail,
            text=caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
        if sent != status:
            await _safe_delete_message(status)
    else:
        await _safe_edit_text(
            status,
            caption,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )
    return True


async def _download_direct_file(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    info: DirectFileInfo,
    status,
) -> None:
    job_token = token_urlsafe(8)
    context.bot_data.setdefault("download_jobs", {})[job_token] = {
        "owner_id": update.effective_user.id,
        "event": asyncio.Event(),
        "created": time.time(),
        "message": status,
        "kind": "document",
        "title": info.filename,
    }

    safe_name = Path(info.filename).name or "downloaded-file"
    cache_key = _direct_cache_key(info.url)
    cached = await users.get_cached_file(cache_key)
    if cached and await _send_cached_file(status, cached, html.escape(safe_name)[:900]):
        await _safe_delete_message(status)
        await users.add_success(update.effective_user.id, info.url, safe_name)
        return

    target = settings.download_dir / f"direct-{token_urlsafe(8)}-{safe_name}"
    downloaded = 0
    total = info.size or 0
    last_update = {"time": 0.0, "percent": -1}

    try:
        ensure_disk_space(info.size or 0)
        if info.size and info.size > settings.max_upload_bytes:
            raise FileTooLargeError(
                f"File is {human_size(info.size)}, above the bot limit of {human_size(settings.max_upload_bytes)}."
            )

        await status.edit_text(
            f"📁 Downloading file…\n"
            f"Name: <code>{escape(safe_name)}</code>\n"
            f"Type: <code>{escape(info.content_type or 'unknown')}</code>",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_keyboard(job_token),
        )

        headers = {"User-Agent": "Mozilla/5.0"}
        async with httpx.AsyncClient(headers=headers, follow_redirects=True, timeout=None) as client:
            async with client.stream("GET", info.url) as response:
                response.raise_for_status()
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("wb") as file:
                    async for chunk in response.aiter_bytes(chunk_size=128 * 1024):
                        if _job_cancelled(context, job_token):
                            raise DownloadCancelled("Download cancelled.")
                        if not chunk:
                            continue
                        file.write(chunk)
                        downloaded += len(chunk)
                        if downloaded > settings.max_upload_bytes:
                            raise FileTooLargeError(
                                f"File is above the bot limit of {human_size(settings.max_upload_bytes)}."
                            )
                        percent = (downloaded / total * 100) if total else 0
                        now = time.monotonic()
                        if now - last_update["time"] < 1.5 and int(percent) == last_update["percent"]:
                            continue
                        last_update["time"] = now
                        last_update["percent"] = int(percent)
                        await status.edit_text(
                            f"<b>⬇️ Downloading file</b>\n\n"
                            f"Name: <code>{escape(safe_name)}</code>\n"
                            f"Size: {human_size(downloaded)} / {human_size(total) if total else 'unknown'}\n"
                            f"{progress_bar(percent)} {percent:.1f}%",
                            parse_mode=ParseMode.HTML,
                            reply_markup=_cancel_keyboard(job_token),
                        )

        if _job_cancelled(context, job_token):
            raise DownloadCancelled("Download cancelled.")

        await status.edit_text(
            "📤 Uploading file to Telegram…",
            reply_markup=_cancel_keyboard(job_token),
        )
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_DOCUMENT)
        logger.info("Telegram upload started: %s (%d bytes)", target, target.stat().st_size)
        sent = await update.effective_message.reply_document(
            document=local_file_path(target),
            filename=safe_name,
            caption=html.escape(safe_name)[:900],
            read_timeout=settings.telegram_read_timeout,
            write_timeout=settings.telegram_write_timeout,
        )
        logger.info("Telegram upload succeeded: %s", target)
        file_id = sent.document.file_id if sent.document else None
        await users.add_success(
            update.effective_user.id,
            info.url,
            safe_name,
            cache_key=cache_key,
            file_id=file_id,
            file_kind="document",
        )
        await _safe_delete_message(status)
    except DownloadCancelled:
        await status.edit_text("🛑 File download cancelled.")
    except FileTooLargeError as exc:
        await status.edit_text(
            f"<b>⚠️ File too large</b>\n\n{escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
    except (httpx.HTTPError, OSError, asyncio.TimeoutError):
        logger.warning("Direct file download failed: %s", info.url, exc_info=True)
        await _safe_edit_text(
            status,
            "<b>⚠️ Download failed</b>\n\nFile download failed. Try another link.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramError:
        logger.warning("Direct file Telegram upload failed: %s", info.url, exc_info=True)
        try:
            await _safe_edit_text(
                status,
                "<b>⚠️ Upload failed</b>\n\nThe file downloaded, but Telegram could not receive it.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    finally:
        context.bot_data.get("download_jobs", {}).pop(job_token, None)
        try:
            target.unlink(missing_ok=True)
            logger.info("Temporary file cleaned: %s", target)
        except OSError:
            logger.warning("Could not clean temporary file: %s", target, exc_info=True)


async def _show_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    user_id = update.effective_user.id
    if not await download_guard.acquire(user_id):
        token = token_urlsafe(6)
        context.bot_data.setdefault("retry_sessions", {})[token] = {
            "url": url,
            "owner_id": user_id,
            "created": time.time(),
        }
        await update.effective_message.reply_text(
            _busy_retry_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_busy_retry_keyboard(token),
        )
        return

    try:
        await _process_url(update, context, url)
    finally:
        await download_guard.release(user_id)


async def _process_url(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str) -> None:
    """Inspect a URL after the caller has atomically acquired the user's slot."""
    try:
        await react_to_user(update, "url")
        status = await update.effective_message.reply_text("🔎 Extracting URL data…")
        await status.edit_text("🧾 Checking file type…")
        direct_file = await _inspect_direct_file(url)
        if direct_file:
            token = token_urlsafe(6)
            context.bot_data.setdefault("direct_file_sessions", {})[token] = {
                "info": direct_file,
                "owner_id": update.effective_user.id,
                "created": time.time(),
            }
            await status.edit_text(
                _direct_file_text(direct_file),
                parse_mode=ParseMode.HTML,
                reply_markup=_direct_file_keyboard(token),
            )
            return

        if is_likely_playlist_url(url):
            try:
                await status.edit_text("📡 Reading playlist metadata…")
                if await _show_playlist(update, context, url, status):
                    return
            except DownloadError as exc:
                if is_spotify_url(url):
                    await status.edit_text(
                        f"<b>⚠️ Playlist inspect failed</b>\n\n{escape(str(exc))}",
                        parse_mode=ParseMode.HTML,
                    )
                    return
                logger.info("Playlist inspection failed; falling back to single item.", exc_info=True)
        await status.edit_text("🎚 Reading available formats…")
        await _render_media_panel(update, context, url, status=status)
    except TelegramError:
        logger.warning("Could not render URL workflow for %s", url, exc_info=True)
        raise


@registered
async def retry_url_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, _, token = query.data.split(":", 2)
    session = _get_session(context, "retry_sessions", token)
    if not _authorize_session(session, update.effective_user.id):
        await query.answer("This retry button expired. Send the URL again.", show_alert=True)
        return

    user_id = update.effective_user.id
    if not await download_guard.acquire(user_id):
        await query.answer("Your current transfer is still running.", show_alert=True)
        await _safe_edit_text(
            query.message,
            _busy_retry_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=_busy_retry_keyboard(token),
        )
        return

    context.bot_data.get("retry_sessions", {}).pop(token, None)
    await query.answer("Transfer slot available—trying again.")
    await _safe_edit_text(
        query.message,
        "<b>✅ Transfer slot available</b>\n\nChecking your saved URL now…",
        parse_mode=ParseMode.HTML,
    )
    try:
        await _process_url(update, context, session["url"])
    finally:
        await download_guard.release(user_id)


@registered
async def url_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args)
    url = extract_url(text)
    if not url or not is_http_url(url):
        await update.effective_message.reply_text(
            "<b>⚠️ Missing URL</b>\n\nUsage: <code>/url https://example.com/media</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await _show_url(update, context, url)


@registered
async def plain_url(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = extract_url(update.effective_message.text)
    if url and is_http_url(url):
        await _show_url(update, context, url)


@registered
async def direct_file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    token = parts[2]
    session = _get_session(context, "direct_file_sessions", token)
    if not _authorize_session(session, update.effective_user.id):
        await query.answer("This file menu expired.", show_alert=True)
        return

    if action != "download":
        await query.answer()
        return

    user_id = update.effective_user.id
    if not await _acquire_download_guard(query, user_id):
        return

    await query.answer("Starting download…")
    try:
        await _download_direct_file(update, context, session["info"], query.message)
    finally:
        await download_guard.release(user_id)


@registered
async def download_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, token, key = query.data.split(":", 2)
    session = _get_session(context, "media_sessions", token)
    if not _authorize_session(session, update.effective_user.id):
        await query.answer("This menu expired. Send the URL again.", show_alert=True)
        return

    info: MediaInfo = session["info"]
    option = next((f for f in info.formats if f.key == key), None)
    if option is None:
        await query.answer("Format unavailable.", show_alert=True)
        return

    cache_key = _media_cache_key(session["url"], option.key)
    cached = await users.get_cached_file(cache_key)
    if cached and await _send_cached_file(query.message, cached, html.escape(info.title)[:900]):
        await query.answer("Sent from cache.")
        await users.add_success(update.effective_user.id, session["url"], info.title)
        return

    user_id = update.effective_user.id
    if not await _acquire_download_guard(query, user_id, "You already have an active download."):
        return

    await react_to_user(update, "download")
    await send_sticker(query.message, "download", context.bot)
    job_token = token_urlsafe(8)
    cancel_event = asyncio.Event()
    context.bot_data.setdefault("download_jobs", {})[job_token] = {
        "owner_id": user_id,
        "event": cancel_event,
        "created": time.time(),
        "message": None,
        "kind": option.kind,
        "title": info.title,
    }
    progress_message = await query.message.reply_text(
        f"<b>⚙️ Preparing download</b>\n\n"
        f"Title: <code>{escape(info.title)}</code>\n"
        f"Format: <code>{escape(option.label)}</code>\n"
        f"{progress_bar(0)} 0%",
        parse_mode=ParseMode.HTML,
        reply_markup=_cancel_keyboard(job_token),
    )
    context.bot_data["download_jobs"][job_token]["message"] = progress_message
    last_update = {"time": 0.0, "percent": -1, "text": ""}

    def progress_hook(data):
        if _job_cancelled(context, job_token):
            return
        if data.get("status") != "downloading":
            return
        total = data.get("total_bytes") or data.get("total_bytes_estimate")
        downloaded = data.get("downloaded_bytes") or 0
        percent = (downloaded / total * 100) if total else 0
        now = time.monotonic()
        if now - last_update["time"] < 1.5 and int(percent) == last_update["percent"]:
            return
        last_update["time"] = now
        last_update["percent"] = int(percent)
        speed = human_size(int(data.get("speed") or 0)) + "/s" if data.get("speed") else "calculating"
        eta = data.get("eta")
        text = (
            f"<b>⬇️ Downloading media</b>\n\n"
            f"Title: <code>{html.escape(info.title[:80])}</code>\n"
            f"Format: <code>{html.escape(option.label)}</code>\n"
            f"{progress_bar(percent)} {percent:.1f}%\n\n"
            f"🚀 Speed: {speed} · ⏳ ETA: {eta if eta is not None else '?'}s"
        )
        if text == last_update["text"]:
            return
        last_update["text"] = text
        task = asyncio.create_task(
            progress_message.edit_text(
                text,
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job_token),
            )
        )
        task.add_done_callback(_log_background_failure)

    result = None
    try:
        ensure_disk_space(option.size or 0)
        logger.info("Media download starting: %s (%s)", session["url"], option.label)
        if session.get("spotify"):
            await progress_message.edit_text(
                f"<b>🎧 Downloading Spotify match</b>\n\n"
                f"Title: <code>{escape(info.title)}</code>\n"
                f"Format: <code>{escape(option.label)}</code>",
                parse_mode=ParseMode.HTML,
                reply_markup=_cancel_keyboard(job_token),
            )
            result = await spotdl.download(
                session["url"],
                int(option.abr or 128),
                cancel_event,
            )
        else:
            result = await service.download(
                session["url"],
                option,
                progress_hook,
                lambda: _job_cancelled(context, job_token),
            )
        if _job_cancelled(context, job_token):
            raise DownloadCancelled("Download cancelled.")
        logger.info("Local media file ready: %s (%d bytes)", result.path, result.path.stat().st_size)
        await progress_message.edit_text(
            "<b>📤 Uploading to Telegram</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=_cancel_keyboard(job_token),
        )
        action = ChatAction.UPLOAD_DOCUMENT if option.kind == "audio" else ChatAction.UPLOAD_VIDEO
        await context.bot.send_chat_action(update.effective_chat.id, action)

        if _job_cancelled(context, job_token):
            raise DownloadCancelled("Download cancelled.")
        logger.info("Telegram upload started: %s", result.path)
        upload_path = local_file_path(result.path)
        if result.kind == "audio":
            sent = await query.message.reply_audio(
                audio=upload_path,
                filename=result.path.name,
                title=result.title[:64],
                caption=html.escape(result.title)[:900],
                duration=info.duration,
                read_timeout=settings.telegram_read_timeout,
                write_timeout=settings.telegram_write_timeout,
            )
        else:
            thumbnail_path = (
                local_file_path(result.thumbnail)
                if result.thumbnail is not None
                else None
            )
            sent = await _send_video_with_fallback(
                query.message,
                path=upload_path,
                filename=result.path.name,
                caption=html.escape(result.title)[:900],
                duration=info.duration,
                height=option.height,
                thumbnail=thumbnail_path,
            )
        logger.info("Telegram upload succeeded: %s", result.path)
        file_id = None
        if result.kind == "audio" and sent.audio:
            file_id = sent.audio.file_id
        elif result.kind == "video":
            if sent.video:
                file_id = sent.video.file_id
            elif sent.document:
                file_id = sent.document.file_id
        await users.add_success(
            update.effective_user.id,
            session["url"],
            result.title,
            cache_key=cache_key,
            file_id=file_id,
            file_kind=("document" if result.kind == "video" and sent.document else result.kind),
        )
        await react_to_user(update, "success")
        await send_sticker(query.message, "success", context.bot)
        await _safe_delete_message(progress_message)
    except DownloadCancelled:
        await progress_message.edit_text("🛑 Download cancelled.")
    except asyncio.TimeoutError:
        await _safe_edit_text(
            progress_message,
            "<b>⚠️ Download timed out</b>\n\nPlease try again later.",
            parse_mode=ParseMode.HTML,
        )
    except (DownloadError, FileTooLargeError) as exc:
        logger.warning("Media download failed for %s: %s", session["url"], exc)
        if _job_cancelled(context, job_token):
            await progress_message.edit_text("🛑 Download cancelled.")
        else:
            await send_sticker(query.message, "error", context.bot)
            await _safe_edit_text(
                progress_message,
                f"<b>⚠️ Download failed</b>\n\n{escape(str(exc))}",
                parse_mode=ParseMode.HTML,
            )
    except TelegramError:
        logger.warning("Media Telegram upload failed: %s", session["url"], exc_info=True)
        try:
            await send_sticker(query.message, "error", context.bot)
            await _safe_edit_text(
                progress_message,
                "<b>⚠️ Upload failed</b>\n\nThe media downloaded, but Telegram could not receive it.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramError:
            pass
    finally:
        context.bot_data.get("download_jobs", {}).pop(job_token, None)
        if result:
            if session.get("spotify"):
                spotdl.cleanup(result)
            else:
                service.cleanup(result)
            logger.info("Temporary media cleaned: %s", result.path)
        await download_guard.release(user_id)


@registered
async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, job_token = query.data.split(":", 1)
    job = _download_job(context, job_token)
    if not job:
        await query.answer("This task is already finished.", show_alert=True)
        return
    if job["owner_id"] != update.effective_user.id and update.effective_user.id != settings.owner_id:
        await query.answer("This download belongs to another user.", show_alert=True)
        return

    job["event"].set()
    await query.answer("Cancelling…")
    message = job.get("message") or query.message
    try:
        await message.edit_text("🛑 Cancelling download…")
    except TelegramError:
        pass


@registered
async def refresh_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Refreshing…")
    _, token = query.data.split(":", 1)
    session = _get_session(context, "media_sessions", token)
    if not _authorize_session(session, update.effective_user.id):
        await query.answer("Menu expired.", show_alert=True)
        return
    user_id = update.effective_user.id
    if not await _acquire_download_guard(query, user_id):
        return
    try:
        info, _ = await _inspect_url(session["url"])
        session["info"] = info
        await _edit_panel_message(
            query.message,
            media_caption(info, back_playlist_token=session.get("back_playlist_token")),
            media_keyboard(token, info, back_playlist_token=session.get("back_playlist_token")),
            photo=info.thumbnail,
        )
    except DownloadError:
        await query.answer("Could not refresh metadata.", show_alert=True)
    finally:
        await download_guard.release(user_id)


@registered
async def playlist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    parts = query.data.split(":")
    action = parts[1]
    token = parts[2]
    session = _get_session(context, "playlist_sessions", token)
    if not _authorize_session(session, update.effective_user.id):
        await query.answer("Playlist menu expired.", show_alert=True)
        return

    playlist: PlaylistInfo = session["playlist"]
    if action == "noop":
        await query.answer()
        return
    if action == "all":
        await query.answer("Starting playlist download…")
        await _download_playlist(update, context, token, session)
        return
    if action == "page":
        page = int(parts[3])
        session["page"] = page
        await query.answer()
        await _edit_panel_message(
            query.message,
            playlist_caption(playlist, page),
            playlist_keyboard(token, playlist, page),
            photo=playlist.thumbnail,
        )
        return
    if action == "refresh":
        user_id = update.effective_user.id
        if not await _acquire_download_guard(query, user_id):
            return
        await query.answer("Refreshing…")
        try:
            refreshed = await (
                spotdl.inspect_playlist(session["url"])
                if is_spotify_url(session["url"])
                else service.inspect_playlist(session["url"])
            )
        except (DownloadError, asyncio.TimeoutError):
            await query.answer("Could not refresh playlist metadata.", show_alert=True)
            return
        finally:
            await download_guard.release(user_id)
        if refreshed:
            session["playlist"] = refreshed
            playlist = refreshed
        await _edit_panel_message(
            query.message,
            playlist_caption(playlist, session.get("page", 0)),
            playlist_keyboard(token, playlist, session.get("page", 0)),
            photo=playlist.thumbnail,
        )
        return
    if action == "back":
        await query.answer()
        await _edit_panel_message(
            query.message,
            playlist_caption(playlist, session.get("page", 0)),
            playlist_keyboard(token, playlist, session.get("page", 0)),
            photo=playlist.thumbnail,
        )
        return
    if action == "item":
        user_id = update.effective_user.id
        if not await _acquire_download_guard(query, user_id):
            return
        index = int(parts[3])
        if index >= len(playlist.items):
            await query.answer("Item unavailable.", show_alert=True)
            await download_guard.release(user_id)
            return
        try:
            await query.answer("Opening item…")
            await _edit_panel_message(query.message, "🎚 Extracting item formats…", None)
            await _render_media_panel(
                update,
                context,
                playlist.items[index].webpage_url,
                edit_message=query.message,
                back_playlist_token=token,
                playlist_item=playlist.items[index],
            )
        finally:
            await download_guard.release(user_id)


async def _download_playlist(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    token: str,
    session: dict,
) -> None:
    query = update.callback_query
    user_id = update.effective_user.id
    if not await _acquire_download_guard(query, user_id, "You already have an active download."):
        return

    playlist: PlaylistInfo = session["playlist"]
    items = playlist.items[:settings.max_playlist_bulk_items]
    job_token = token_urlsafe(8)
    cancel_event = asyncio.Event()
    context.bot_data.setdefault("download_jobs", {})[job_token] = {
        "owner_id": user_id,
        "event": cancel_event,
        "created": time.time(),
        "message": None,
        "kind": "playlist",
        "title": playlist.title,
    }
    status = await query.message.reply_text(
        f"<b>📥 Playlist download</b>\n\n"
        f"Title: <code>{escape(playlist.title)}</code>\n"
        f"Progress: 0/{len(items)} completed.",
        parse_mode=ParseMode.HTML,
        reply_markup=_cancel_keyboard(job_token),
    )
    context.bot_data["download_jobs"][job_token]["message"] = status
    completed = 0
    failed = 0
    try:
        for index, item in enumerate(items, start=1):
            if _job_cancelled(context, job_token):
                break
            result = None
            try:
                await status.edit_text(
                    f"<b>📥 Playlist download</b>\n\n"
                    f"Title: <code>{escape(playlist.title)}</code>\n"
                    f"Progress: {completed}/{len(items)} completed.\n"
                    f"Current: <code>{index}. {escape(item.title)}</code>",
                    parse_mode=ParseMode.HTML,
                    reply_markup=_cancel_keyboard(job_token),
                )
                info, spotify_url = await _inspect_url(item.webpage_url, playlist_item=item)
                option = next((fmt for fmt in info.formats if fmt.key == "a128"), None)
                if option is None:
                    failed += 1
                    continue
                if spotify_url:
                    result = await spotdl.download(
                        item.webpage_url,
                        int(option.abr or 128),
                        cancel_event,
                    )
                else:
                    result = await service.download(
                        item.webpage_url,
                        option,
                        lambda data: None,
                        lambda: _job_cancelled(context, job_token),
                    )
                if _job_cancelled(context, job_token):
                    break
                await query.message.reply_audio(
                    audio=local_file_path(result.path),
                    filename=result.path.name,
                    title=result.title[:64],
                    caption=html.escape(result.title)[:900],
                    duration=info.duration,
                    read_timeout=settings.telegram_read_timeout,
                    write_timeout=settings.telegram_write_timeout,
                )
                await users.add_success(user_id, item.webpage_url, result.title)
                completed += 1
            except (DownloadError, FileTooLargeError, asyncio.TimeoutError):
                logger.warning("Playlist item download failed: %s", item.webpage_url, exc_info=True)
                failed += 1
            finally:
                if result:
                    if is_spotify_url(item.webpage_url):
                        spotdl.cleanup(result)
                    else:
                        service.cleanup(result)

        suffix = ""
        if len(playlist.items) > len(items):
            suffix = f"\nLimited to first {len(items)} items by current settings."
        if _job_cancelled(context, job_token):
            await status.edit_text(
                f"🛑 Playlist download cancelled.\n"
                f"Sent: {completed} · Skipped: {failed}"
            )
        else:
            await status.edit_text(
                f"✅ Playlist job finished.\n"
                f"Sent: {completed} · Skipped: {failed}{suffix}"
            )
    finally:
        context.bot_data.get("download_jobs", {}).pop(job_token, None)
        await download_guard.release(user_id)


@registered
async def close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await _edit_panel_message(query.message, "Closed.", None)
