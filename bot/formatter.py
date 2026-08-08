from __future__ import annotations

import html
import logging
import math
from urllib.parse import quote_plus
from dataclasses import dataclass
from typing import Iterable

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Message, ReactionTypeEmoji, Update
from telegram.error import TelegramError
from bot.config import (
    DEFAULT_ANALYZING_STICKER_ID,
    DEFAULT_DOWNLOADING_STICKER_ID,
    DEFAULT_ERROR_STICKER_ID,
    DEFAULT_SUCCESS_STICKER_ID,
    DEFAULT_WELCOME_STICKER_ID,
    settings,
)

logger = logging.getLogger(__name__)

STICKER_FALLBACKS = {
    "welcome": DEFAULT_WELCOME_STICKER_ID,
    "download": DEFAULT_DOWNLOADING_STICKER_ID,
    "music": DEFAULT_ANALYZING_STICKER_ID,
    "success": DEFAULT_SUCCESS_STICKER_ID,
    "error": DEFAULT_ERROR_STICKER_ID,
}

@dataclass(slots=True)
class Panel:
    text: str
    keyboard: InlineKeyboardMarkup | None = None


def escape(value: object) -> str:
    return html.escape(str(value))


def progress_bar(percent: float) -> str:
    filled = max(0, min(10, int(percent / 10)))
    return "█" * filled + "░" * (10 - filled)


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🧭 Help", callback_data="menu:help"),
            InlineKeyboardButton("ℹ️ Info", callback_data="menu:info"),
        ],
        [InlineKeyboardButton("📣 Main channel", url=settings.main_channel_url)],
    ])


def help_panel(*, include_back: bool = False) -> Panel:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:back")]]) if include_back else None
    return Panel(
        "<b>🧭 Help</b>\n\n"
        "<b>Links</b>\n"
        "🔗 Send a URL or use <code>/url https://...</code>.\n"
        "📁 Direct files are inspected first; press Download file to fetch them.\n"
        "📚 Playlist links open a paginated panel.\n"
        "🎚 Media links show video/audio format buttons.\n\n"
        "<b>Downloads</b>\n"
        "✖️ Use Cancel while a download is running.\n"
        "⚡ Repeated completed URLs may resend from Telegram cache.\n"
        "🎵 Upload audio/video to analyze and find matching music.\n\n"
        "<b>Commands</b>\n"
        "<code>/stat</code> status · <code>/id</code> IDs · <code>/quote</code> reply sticker\n"
        "<code>/ss URL</code> screenshot · <code>/search query</code> web summary\n"
        "<code>/wiki query</code> Wikipedia link · <code>/ping host</code> latency\n\n"
        "Only download content you have permission to use.",
        keyboard,
    )


def info_panel(*, include_back: bool = False) -> Panel:
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="menu:back")]]) if include_back else None
    return Panel(
        "<b>ℹ️ Bot information</b>\n\n"
        "<b>Download engine</b>\n"
        "🎬 Video links are inspected with yt-dlp and sent as MP4.\n"
        "🎵 Audio choices are converted to MP3.\n"
        "📁 Direct files are detected before media extraction and sent as documents.\n"
        "📚 Playlists open a paginated selector and support bulk audio download.\n\n"
        "<b>Reliability</b>\n"
        "✖️ Active jobs can be cancelled.\n"
        "⚡ Successful repeated downloads can resend from Telegram file cache.\n"
        "🧹 Old panels expire automatically.\n"
        "🔒 Owner tools are hidden from the public command menu.",
        keyboard,
    )


def home_panel() -> Panel:
    return Panel(
        "<b>⚡ Media downloader</b>\n\n"
        "Send a media URL, playlist URL, or an audio/video file.",
        start_keyboard(),
    )


def media_caption(info, *, back_playlist_token: str | None = None) -> str:
    duration = f"{info.duration // 60}:{info.duration % 60:02d}" if info.duration else "unknown"
    views = f"{info.view_count:,}" if info.view_count is not None else "unknown"
    back_hint = "\n\n⬅️ Use Back to return to the playlist." if back_playlist_token else ""
    return (
        f"<b>🎞 {escape(info.title)}</b>\n"
        f"⏱ Duration: {duration} · 👁 Views: {views}\n"
        f"👤 Uploader: {escape(info.uploader or 'unknown')}\n\n"
        "Choose a format below. ⚡ = expected fastest path."
        f"{back_hint}"
    )


def media_keyboard(token: str, info, *, back_playlist_token: str | None = None) -> InlineKeyboardMarkup:
    video = [fmt for fmt in info.formats if fmt.kind == "video"]
    audio = [fmt for fmt in info.formats if fmt.kind == "audio"]
    rows: list[list[InlineKeyboardButton]] = []

    for i in range(0, len(video), 3):
        rows.append([
            InlineKeyboardButton(
                f"🎥 {fmt.label}{' ⚡' if fmt.fastest else ''}",
                callback_data=f"dl:{token}:{fmt.key}",
            )
            for fmt in video[i:i + 3]
        ])

    for i in range(0, len(audio), 2):
        rows.append([
            InlineKeyboardButton(
                f"🎵 {fmt.label}{' ⚡' if fmt.fastest else ''}",
                callback_data=f"dl:{token}:{fmt.key}",
            )
            for fmt in audio[i:i + 2]
        ])

    if back_playlist_token:
        rows.append([InlineKeyboardButton("⬅️ Back to playlist", callback_data=f"pl:back:{back_playlist_token}")])
    rows.extend([
        [InlineKeyboardButton("🔄 Refresh metadata", callback_data=f"refresh:{token}")],
        [InlineKeyboardButton("✖️ Close", callback_data=f"close:{token}")],
    ])
    return InlineKeyboardMarkup(rows)


def playlist_caption(playlist, page: int) -> str:
    total_pages = max(1, math.ceil(len(playlist.items) / settings.playlist_page_size))
    start = page * settings.playlist_page_size + 1
    end = min(len(playlist.items), (page + 1) * settings.playlist_page_size)
    return (
        f"<b>📚 Playlist: {escape(playlist.title)}</b>\n"
        f"📦 Total items: {len(playlist.items)}\n"
        f"📄 Showing: {start}-{end} / {len(playlist.items)}\n\n"
        "Select an item below to open the download panel."
    )


def playlist_keyboard(token: str, playlist, page: int) -> InlineKeyboardMarkup:
    size = settings.playlist_page_size
    total_pages = max(1, math.ceil(len(playlist.items) / size))
    page = max(0, min(page, total_pages - 1))
    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton("📥 Download entire playlist", callback_data=f"pl:all:{token}")]
    ]

    for index, item in enumerate(playlist.items[page * size:(page + 1) * size], start=page * size):
        title = item.title
        if len(title) > 54:
            title = f"{title[:51]}..."
        rows.append([InlineKeyboardButton(f"{index + 1}. {title}", callback_data=f"pl:item:{token}:{index}")])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"pl:page:{token}:{page - 1}"))
    nav.append(InlineKeyboardButton(f"📄 {page + 1}/{total_pages}", callback_data=f"pl:noop:{token}"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"pl:page:{token}:{page + 1}"))
    rows.append(nav)
    rows.append([
        InlineKeyboardButton("🔄 Refresh", callback_data=f"pl:refresh:{token}"),
        InlineKeyboardButton("✖️ Close", callback_data=f"close:{token}"),
    ])
    return InlineKeyboardMarkup(rows)


def recognized_track_panel(track) -> Panel:
    query = f"{track.artist} {track.title}"
    google_url = f"https://www.google.com/search?q={quote_plus(query)}"
    ytm_url = f"https://music.youtube.com/search?q={quote_plus(query)}"
    spotify_url = track.spotify_url or f"https://open.spotify.com/search/{quote_plus(query)}"
    artist_url = f"https://www.google.com/search?q={quote_plus(track.artist)}"
    lyrics_url = f"https://www.google.com/search?q={quote_plus(query + ' lyrics')}"
    return Panel(
        f"<b>{escape(track.title)} — {escape(track.artist)}</b>\n\n"
        "🎙 Shazam - music finder",
        InlineKeyboardMarkup([
            [
                InlineKeyboardButton("Google ↗", url=google_url),
                InlineKeyboardButton("YouTube Music ↗", url=ytm_url),
                InlineKeyboardButton("Spotify ↗", url=spotify_url),
            ],
            [InlineKeyboardButton("🔍 Search by artist", url=artist_url)],
            [InlineKeyboardButton("🔤 Lyrics", url=lyrics_url)],
        ]),
    )


def reaction_set(kind: str) -> list[str]:
    return {
        "welcome": [settings.welcome_reaction],
        "url": settings.url_reactions,
        "download": settings.download_reactions,
        "music": settings.music_reactions,
        "success": settings.success_reactions,
        "blocked": settings.blocked_reactions,
    }.get(kind, [])


async def react_to_user(update: Update, kind: str) -> None:
    message = update.message
    if not message:
        return
    if message.from_user and message.from_user.is_bot:
        return
    emojis = reaction_set(kind)
    if not emojis:
        return
    try:
        await message.set_reaction([ReactionTypeEmoji(emoji) for emoji in emojis[:3]])
    except TelegramError:
        pass


async def send_sticker(message: Message, kind: str, bot: Bot | None = None) -> None:
    sticker_id = STICKER_FALLBACKS.get(kind)
    if sticker_id:
        try:
            await message.reply_sticker(sticker_id)
            return
        except TelegramError as exc:
            logger.warning("Configured sticker ID failed for %s: %s", kind, exc)

    chat = getattr(message, "chat", None)
    chat_id = getattr(chat, "id", None) or getattr(message, "chat_id", None)
    if chat_id is None:
        logger.warning("Could not determine chat_id for sticker fallback: %s", kind)
        return
    bot = bot or getattr(message, "bot", None) or getattr(message, "_bot", None)
    if bot is None:
        logger.warning("Could not find bot instance for sticker fallback: %s", kind)
        return
    if sticker_id:
        try:
            await bot.send_sticker(chat_id=chat_id, sticker=sticker_id)
            return
        except TelegramError as exc:
            logger.warning(
                "Configured sticker %s could not be sent to chat %s: %s",
                kind,
                chat_id,
                exc,
            )


def ascii_banner() -> str:
    return r"""
 _______  ___      ___   __   __  _______ 
|   _   ||   |    |   | |  | |  ||       |
|  |_|  ||   |    |   | |  |_|  ||    ___|
|       ||   |    |   | |       ||   |___ 
|       ||   |___ |   | |       ||    ___|
|   _   ||       ||   |  |     | |   |___ 
|__| |__||_______||___|   |___|  |_______|

        mTer is online.
        Downloads, playlists, screenshots, search, and music tools are ready.
        -$ status -> alive!
        -$ logs   -> logs/bot.log
      
"""


def id_lines(update: Update) -> Iterable[str]:
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message
    if user:
        yield "<b>👤 Current user</b>"
        yield f"ID: <code>{user.id}</code>"
        yield f"Name: <code>{escape(user.full_name)}</code>"
        if user.username:
            yield f"Username: @{escape(user.username)}"
    if chat:
        yield ""
        yield "<b>💬 Current chat</b>"
        yield f"ID: <code>{chat.id}</code>"
        yield f"Type: <code>{escape(chat.type)}</code>"
    if message:
        yield ""
        yield "<b>✉️ Command message</b>"
        yield f"ID: <code>{message.message_id}</code>"
    reply = message.reply_to_message if message else None
    if not reply:
        return
    yield ""
    yield "<b>↩️ Replied message</b>"
    yield f"Message ID: <code>{reply.message_id}</code>"
    if reply.from_user:
        yield f"Sender ID: <code>{reply.from_user.id}</code>"
        yield f"Sender name: <code>{escape(reply.from_user.full_name)}</code>"
    if reply.sticker:
        yield f"🏷 Sticker ID: <code>{escape(reply.sticker.file_id)}</code>"
    if reply.photo:
        yield f"🖼 Photo ID: <code>{escape(reply.photo[-1].file_id)}</code>"
    document = reply.document or reply.audio or reply.video or reply.voice
    if document:
        yield f"📎 File ID: <code>{escape(document.file_id)}</code>"
