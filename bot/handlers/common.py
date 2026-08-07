from __future__ import annotations

import asyncio
import logging
import time
from functools import wraps
from typing import Awaitable, Callable

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import Conflict
from telegram.ext import ContextTypes

from bot.config import settings
from bot.download_guard import download_guard
from bot.formatter import (
    escape,
    help_panel,
    home_panel,
    id_lines,
    info_panel,
    react_to_user,
    send_sticker,
    start_keyboard,
)
from bot.users import users

logger = logging.getLogger(__name__)
Handler = Callable[[Update, ContextTypes.DEFAULT_TYPE], Awaitable[None]]
START_TIME = time.monotonic()


def _user_is_owner(user) -> bool:
    return bool(user and user.id == settings.owner_id)


def owner_only(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not _user_is_owner(update.effective_user):
            if update.effective_message:
                await update.effective_message.reply_text("Owner only.")
            return
        return await handler(update, context)
    return wrapped


def cleanup_expired_sessions(context: ContextTypes.DEFAULT_TYPE) -> None:
    now = time.time()
    for key in (
        "media_sessions",
        "playlist_sessions",
        "direct_file_sessions",
        "music_sessions",
    ):
        sessions = context.bot_data.get(key)
        if not isinstance(sessions, dict):
            continue
        expired = [
            token for token, session in sessions.items()
            if now - float(session.get("created", now)) > settings.session_ttl_seconds
        ]
        for token in expired:
            sessions.pop(token, None)


def registered(handler: Handler) -> Handler:
    @wraps(handler)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        if user is None:
            return
        if user.is_bot:
            if update.callback_query:
                await update.callback_query.answer("Bots are blocked.", show_alert=True)
            return
        record = await users.touch(user)
        if record.get("banned") and user.id != settings.owner_id:
            await react_to_user(update, "blocked")
            if update.effective_message:
                await update.effective_message.reply_text("🚫 You are blocked from using this bot.")
            elif update.callback_query:
                await update.callback_query.answer("You are blocked.", show_alert=True)
            return
        cleanup_expired_sessions(context)
        await handler(update, context)
    return wrapped


@registered
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    await react_to_user(update, "welcome")
    await send_sticker(message, "welcome")

    await message.reply_text(
        "<b>⚡ Welcome to the media downloader.</b>\n\n"
        "🔗 Send a URL, playlist URL, or upload audio/video for music analysis.",
        parse_mode=ParseMode.HTML,
        reply_markup=start_keyboard(),
    )


@registered
async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    panel = help_panel(include_back=True) if query.data == "menu:help" else info_panel(include_back=True)
    await query.edit_message_text(
        panel.text,
        parse_mode=ParseMode.HTML,
        reply_markup=panel.keyboard,
    )


@registered
async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    panel = home_panel()
    await query.edit_message_text(
        panel.text,
        parse_mode=ParseMode.HTML,
        reply_markup=panel.keyboard,
    )


@registered
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    panel = help_panel()
    await update.effective_message.reply_text(
        panel.text,
        parse_mode=ParseMode.HTML,
        reply_markup=panel.keyboard,
    )


@registered
async def info_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    panel = info_panel()
    await update.effective_message.reply_text(
        panel.text,
        parse_mode=ParseMode.HTML,
        reply_markup=panel.keyboard,
    )


@registered
async def stat_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uptime = int(time.monotonic() - START_TIME)
    hours, remainder = divmod(uptime, 3600)
    minutes, seconds = divmod(remainder, 60)
    media_sessions = len(context.bot_data.get("media_sessions", {}))
    playlist_sessions = len(context.bot_data.get("playlist_sessions", {}))
    direct_sessions = len(context.bot_data.get("direct_file_sessions", {}))
    jobs = len(context.bot_data.get("download_jobs", {}))
    active_downloads = await download_guard.active_count()
    await update.effective_message.reply_text(
        "<b>📊 Bot status</b>\n\n"
        f"⏱ Uptime: <code>{hours}h {minutes}m {seconds}s</code>\n"
        f"🎚 Media panels: <code>{media_sessions}</code>\n"
        f"📚 Playlist panels: <code>{playlist_sessions}</code>\n"
        f"📁 File panels: <code>{direct_sessions}</code>\n"
        f"⬇️ Active users: <code>{active_downloads}</code>\n"
        f"🧰 Job records: <code>{jobs}</code>",
        parse_mode=ParseMode.HTML,
    )


@registered
@owner_only
async def stop_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text("Stopping bot…")
    context.application.stop_running()


@registered
@owner_only
async def restart_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Restarting bot process… If the container restart policy is enabled, the service will come back up."
    )
    context.application.stop_running()


@registered
@owner_only
async def jobs_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    jobs = context.bot_data.get("download_jobs", {})
    if not jobs:
        await update.effective_message.reply_text("No active jobs.")
        return
    lines = ["<b>🧰 Active jobs</b>"]
    rows = []
    for token, job in list(jobs.items())[:20]:
        age = int(time.time() - float(job.get("created", time.time())))
        state = "cancelling" if job.get("event") and job["event"].is_set() else "running"
        lines.append(
            f"\n<code>{token}</code>\n"
            f"User: <code>{job.get('owner_id')}</code>\n"
            f"Type: {job.get('kind', 'download')} · State: {state} · Age: {age}s\n"
            f"Item: {escape(job.get('title', 'unknown'))}"
        )
        rows.append([InlineKeyboardButton(f"Cancel {token}", callback_data=f"cancel:{token}")])
    await update.effective_message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows[:10]) if rows else None,
    )


@registered
@owner_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("Usage: /broadcast message text")
        return
    sent = 0
    failed = 0
    status = await update.effective_message.reply_text("Broadcast started…")
    for user_id in await users.user_ids():
        try:
            await context.bot.send_message(user_id, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    await status.edit_text(f"Broadcast finished. Sent: {sent} · Failed: {failed}")


@registered
async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "\n".join(id_lines(update)),
        parse_mode=ParseMode.HTML,
    )


@registered
@owner_only
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /ban USER_ID")
        return
    target = int(context.args[0])
    await users.set_banned(target, True)
    await update.effective_message.reply_text(f"Banned {target}.")


@registered
@owner_only
async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].isdigit():
        await update.effective_message.reply_text("Usage: /unban USER_ID")
        return
    target = int(context.args[0])
    await users.set_banned(target, False)
    await update.effective_message.reply_text(f"Unbanned {target}.")


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(context.error, Conflict):
        logger.error(
            "Telegram polling conflict: another bot instance is already using "
            "getUpdates for this token. Stop the other process or use a "
            "different TELEGRAM_BOT_TOKEN. Shutting down this instance."
        )
        context.application.stop_running()
        return

    logger.exception("Unhandled Telegram update error", exc_info=context.error)
