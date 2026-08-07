from __future__ import annotations

import logging
import time
from secrets import token_urlsafe

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import settings
from bot.download_guard import download_guard
from bot.errors import DownloadError, FileTooLargeError
from bot.formatter import react_to_user, recognized_track_panel, send_sticker
from bot.handlers.common import registered
from bot.local_bot_api import ensure_disk_space, local_file_path
from bot.services.music_finder import MusicFinderService
from bot.services.spotdl_service import SpotDLService
from bot.users import users

logger = logging.getLogger(__name__)
finder = MusicFinderService()
spotdl = SpotDLService()


@registered
async def uploaded_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    media = message.audio or message.video or message.voice or message.document
    if media is None:
        return

    mime = getattr(media, "mime_type", "") or ""
    if message.document and not (
        mime.startswith("audio/") or mime.startswith("video/")
    ):
        return

    if await download_guard.is_active(update.effective_user.id):
        await message.reply_text("⏳ Finish your current task before starting another one.")
        return

    await react_to_user(update, "music")
    token = token_urlsafe(6)
    context.bot_data.setdefault("music_sessions", {})[token] = {
        "file_id": media.file_id,
        "user_id": update.effective_user.id,
        "mime_type": mime,
        "created": time.time(),
    }

    await message.reply_text(
        "Do you want me to analyze the music in this file?",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("Yes, analyze", callback_data=f"music:yes:{token}"),
            InlineKeyboardButton("No", callback_data=f"music:no:{token}"),
        ]]),
    )


@registered
async def music_no(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("Music analysis cancelled.")


@registered
async def music_yes(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    _, _, token = query.data.split(":", 2)
    session = context.bot_data.get("music_sessions", {}).get(token)
    if not session:
        await query.edit_message_text("This request expired.")
        return
    if session["user_id"] != update.effective_user.id:
        await query.answer("This request belongs to another user.", show_alert=True)
        return

    user_id = update.effective_user.id
    if not await download_guard.acquire(user_id):
        await query.answer(
            "You already have an active download or analysis.",
            show_alert=True,
        )
        return

    source_path = None
    sample_path = None
    result = None
    prepare_message = None
    try:
        await send_sticker(query.message, "music", context.bot)
        await query.edit_message_text("Downloading the sample for analysis…")
        telegram_file = await context.bot.get_file(session["file_id"])
        ext = ".bin"
        if session["mime_type"].startswith("audio/"):
            ext = ".audio"
        elif session["mime_type"].startswith("video/"):
            ext = ".video"

        source_path = settings.download_dir / f"upload-{token}{ext}"
        ensure_disk_space()
        logger.info("Telegram sample download starting: %s", source_path)
        await telegram_file.download_to_drive(source_path)
        logger.info("Telegram sample stored: %s", source_path)

        await query.edit_message_text("Analyzing the music…")
        sample_path = await finder.prepare_audio(source_path)
        track = await finder.recognize(sample_path)
        panel = recognized_track_panel(track)
        if track.cover_url:
            await query.edit_message_text("✅ Recognition complete.")
            try:
                await query.message.reply_photo(
                    photo=track.cover_url,
                    caption=panel.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=panel.keyboard,
                )
            except TelegramError:
                await query.edit_message_text(
                    panel.text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=panel.keyboard,
                )
        else:
            await query.edit_message_text(
                panel.text,
                parse_mode=ParseMode.HTML,
                reply_markup=panel.keyboard,
            )

        prepare_message = await query.message.reply_text("🎧 Preparing the matched music file…")
        source = track.spotify_url or track.query
        result = await spotdl.download(source, 192)
        logger.info("Matched music upload started: %s", result.path)
        await query.message.reply_audio(
            audio=local_file_path(result.path),
            filename=result.path.name,
            title=track.title[:64],
            performer=track.artist[:64],
            caption=f"{track.artist} - {track.title}",
            read_timeout=settings.telegram_read_timeout,
            write_timeout=settings.telegram_write_timeout,
        )
        logger.info("Matched music upload succeeded: %s", result.path)
        try:
            await prepare_message.delete()
        except TelegramError:
            pass

        await users.add_success(
            user_id,
            f"music-recognition:{track.artist} - {track.title}",
            f"{track.artist} - {track.title}",
        )
        await react_to_user(update, "success")
        await send_sticker(query.message, "success", context.bot)
    except (DownloadError, FileTooLargeError):
        await send_sticker(query.message, "error", context.bot)
        if prepare_message:
            try:
                await prepare_message.delete()
            except TelegramError:
                pass
        await query.edit_message_text(
            "<b>⚠️ Music analysis failed</b>\n\nPlease try another file.",
            parse_mode=ParseMode.HTML,
        )
    finally:
        if source_path and source_path.exists():
            source_path.unlink(missing_ok=True)
        if sample_path:
            finder.cleanup(sample_path)
        if result:
            spotdl.cleanup(result)
        await download_guard.release(user_id)
