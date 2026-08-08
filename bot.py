from __future__ import annotations

import logging
import time

import httpx
from telegram import BotCommand, BotCommandScopeChat
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import settings
from bot.handlers.common import (
    back_callback,
    ban_command,
    broadcast_command,
    error_handler,
    help_command,
    id_command,
    info_command,
    jobs_command,
    menu_callback,
    restart_command,
    resume_command,
    stat_command,
    start,
    stop_command,
    testurl_command,
    unban_command,
)
from bot.handlers.media import (
    cancel_callback,
    close_callback,
    direct_file_callback,
    download_callback,
    plain_url,
    playlist_callback,
    refresh_callback,
    retry_url_callback,
    url_command,
    website_callback,
)
from bot.formatter import ascii_banner
from bot.logging_config import configure_logging
from bot.handlers.music import uploaded_media, music_yes, music_no
from bot.handlers.tools import (
    ping_command,
    quote_command,
    screenshot_command,
    search_command,
    wiki_command,
)
from bot.users import users


async def post_init(application: Application) -> None:
    if await users.remove_user(settings.owner_id):
        logging.getLogger(__name__).info(
            "Removed legacy owner profile/history from users storage."
        )
    public_commands = [
        BotCommand("start", "Open the welcome menu"),
        BotCommand("help", "Open help"),
        BotCommand("info", "Open bot information"),
        BotCommand("stat", "Show bot status"),
        BotCommand("id", "Show chat/user/replied IDs"),
        BotCommand("url", "Inspect a media URL"),
        BotCommand("quote", "Reply to text and make a sticker"),
        BotCommand("ss", "Take a website screenshot"),
        BotCommand("search", "Search the web"),
        BotCommand("wiki", "Find a Wikipedia page"),
        BotCommand("ping", "Check website latency"),
    ]
    await application.bot.set_my_commands(public_commands)
    try:
        await application.bot.set_my_commands([
            *public_commands,
            BotCommand("jobs", "Owner: show active jobs"),
            BotCommand("broadcast", "Owner: message all users"),
            BotCommand("restart", "Owner: restart the bot"),
            BotCommand("stop", "Owner: enter maintenance mode"),
            BotCommand("resume", "Owner: leave maintenance mode"),
            BotCommand("ban", "Owner: ban a user"),
            BotCommand("unban", "Owner: unban a user"),
            BotCommand("testurl", "Owner: save an admin test URL"),
        ], scope=BotCommandScopeChat(chat_id=settings.owner_id))
    except BadRequest as exc:
        logging.getLogger(__name__).warning(
            "Could not set owner-scoped commands: %s. Owner chat may not be accessible yet.",
            exc,
        )


def build_application() -> Application:
    builder = (
        ApplicationBuilder()
        .token(settings.telegram_bot_token)
        .local_mode(settings.is_docker)
        .connect_timeout(settings.telegram_connect_timeout)
        .read_timeout(settings.telegram_read_timeout)
        .write_timeout(settings.telegram_write_timeout)
        .pool_timeout(settings.telegram_pool_timeout)
        .get_updates_connect_timeout(settings.telegram_connect_timeout)
        .get_updates_read_timeout(settings.telegram_read_timeout)
        .get_updates_write_timeout(settings.telegram_write_timeout)
        .get_updates_pool_timeout(settings.telegram_pool_timeout)
        .post_init(post_init)
        .concurrent_updates(True)
    )
    builder = builder.base_url(settings.telegram_api_base_url)
    builder = builder.base_file_url(settings.telegram_api_base_file_url)
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("info", info_command))
    app.add_handler(CommandHandler("stat", stat_command))
    app.add_handler(CommandHandler("stop", stop_command))
    app.add_handler(CommandHandler("resume", resume_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("url", url_command))
    app.add_handler(CommandHandler("quote", quote_command))
    app.add_handler(CommandHandler("ss", screenshot_command))
    app.add_handler(CommandHandler("search", search_command))
    app.add_handler(CommandHandler("wiki", wiki_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("jobs", jobs_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("restart", restart_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("testurl", testurl_command))

    app.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^menu:(help|info)$"))
    app.add_handler(CallbackQueryHandler(back_callback, pattern=r"^menu:back$"))
    app.add_handler(CallbackQueryHandler(download_callback, pattern=r"^dl:"))
    app.add_handler(CallbackQueryHandler(direct_file_callback, pattern=r"^direct:"))
    app.add_handler(CallbackQueryHandler(website_callback, pattern=r"^website:"))
    app.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel:"))
    app.add_handler(CallbackQueryHandler(refresh_callback, pattern=r"^refresh:"))
    app.add_handler(CallbackQueryHandler(retry_url_callback, pattern=r"^retry:url:"))
    app.add_handler(CallbackQueryHandler(playlist_callback, pattern=r"^pl:"))
    app.add_handler(CallbackQueryHandler(close_callback, pattern=r"^close:"))

    app.add_handler(CallbackQueryHandler(music_yes, pattern=r"^music:yes:"))
    app.add_handler(CallbackQueryHandler(music_no, pattern=r"^music:no:"))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, plain_url))
    app.add_handler(MessageHandler(
        filters.AUDIO | filters.VIDEO | filters.VOICE | filters.Document.ALL,
        uploaded_media,
    ))
    app.add_error_handler(error_handler)
    return app


def wait_for_local_bot_api() -> None:
    logger = logging.getLogger(__name__)
    deadline = time.monotonic() + settings.bot_api_startup_timeout
    url = (
        f"{settings.telegram_api_base_url}"
        f"{settings.telegram_bot_token}/getMe"
    )
    last_error = "not reachable"
    logger.info(
        "Waiting up to %.0f seconds for local Telegram Bot API at %s",
        settings.bot_api_startup_timeout,
        settings.telegram_bot_api_url,
    )
    while time.monotonic() < deadline:
        try:
            response = httpx.get(url, timeout=5.0)
            data = response.json()
            if response.is_success and data.get("ok") is True:
                logger.info("Local Telegram Bot API is ready.")
                return
            last_error = str(data.get("description") or f"HTTP {response.status_code}")
            if response.status_code == 401:
                break
        except (httpx.HTTPError, ValueError) as exc:
            last_error = type(exc).__name__
        time.sleep(2)
    raise RuntimeError(
        "Local Telegram Bot API did not become ready within "
        f"{settings.bot_api_startup_timeout:.0f} seconds: {last_error}"
    )


def main() -> None:
    configure_logging()
    logging.getLogger(__name__).info(ascii_banner())
    try:
        if settings.is_docker:
            wait_for_local_bot_api()
        else:
            logging.getLogger(__name__).info(
                "Local runtime: using Telegram hosted Bot API and repository storage."
            )
        build_application().run_polling(
            timeout=settings.telegram_get_updates_timeout,
            bootstrap_retries=settings.telegram_bootstrap_retries,
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
        )
    except Conflict:
        logging.getLogger(__name__).error(
            "Telegram polling conflict: another bot instance is already using "
            "getUpdates for this token. Stop the other process or use a "
            "different TELEGRAM_BOT_TOKEN."
        )
    except RuntimeError as exc:
        logging.getLogger(__name__).critical("Bot startup failed: %s", exc)


if __name__ == "__main__":
    main()
