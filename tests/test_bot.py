from __future__ import annotations

import os
import importlib.util
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.error import Conflict, TelegramError


os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test-token"
os.environ["OWNER_ID"] = "999"
os.environ["USERS_FILE"] = "database/test-users.json"
os.environ["DOWNLOAD_DIR"] = "downloads"
os.environ["LOG_DIR"] = "logs"

from bot.formatter import ascii_banner, help_panel, id_lines, info_panel, send_sticker
from bot.handlers.media import _send_video_with_fallback
from bot.handlers.common import error_handler
from bot.handlers import tools
from bot.config import settings
from bot.config import Settings
from bot.errors import DownloadError
from bot.services.ytdlp_service import YTDLPService
from bot.system_dependencies import require_ffmpeg
from bot.utils import extract_url, is_http_url, is_likely_playlist_url
from bot.local_bot_api import ensure_disk_space, local_file_path


def _load_bot_entrypoint():
    path = Path(__file__).resolve().parents[1] / "bot.py"
    spec = importlib.util.spec_from_file_location("bot_entrypoint", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load bot.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _has_callback(markup, callback_data: str) -> bool:
    if markup is None:
        return False
    return any(
        button.callback_data == callback_data
        for row in markup.inline_keyboard
        for button in row
    )


class FormatterTests(unittest.TestCase):
    def test_help_and_info_commands_do_not_include_back_button_by_default(self) -> None:
        self.assertFalse(_has_callback(help_panel().keyboard, "menu:back"))
        self.assertFalse(_has_callback(info_panel().keyboard, "menu:back"))

    def test_help_and_info_can_include_back_button_for_start_menu_navigation(self) -> None:
        self.assertTrue(_has_callback(help_panel(include_back=True).keyboard, "menu:back"))
        self.assertTrue(_has_callback(info_panel(include_back=True).keyboard, "menu:back"))

    def test_ascii_banner_describes_current_bot_capabilities(self) -> None:
        banner = ascii_banner()

        self.assertIn("mTer is online.", banner)
        self.assertIn("screenshots, search, and music tools", banner)

    def test_id_lines_are_grouped_and_include_reply_file_ids(self) -> None:
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123, full_name="Test User", username="tester"),
            effective_chat=SimpleNamespace(id=-100, type="supergroup"),
            effective_message=SimpleNamespace(
                message_id=77,
                reply_to_message=SimpleNamespace(
                    message_id=76,
                    from_user=SimpleNamespace(id=456, full_name="Sender User"),
                    sticker=SimpleNamespace(file_id="sticker-file-id"),
                    photo=[SimpleNamespace(file_id="small-photo"), SimpleNamespace(file_id="big-photo")],
                    document=None,
                    audio=None,
                    video=None,
                    voice=None,
                ),
            ),
        )

        text = "\n".join(id_lines(update))

        self.assertIn("<b>👤 Current user</b>", text)
        self.assertIn("ID: <code>123</code>", text)
        self.assertIn("<b>💬 Current chat</b>", text)
        self.assertIn("ID: <code>-100</code>", text)
        self.assertIn("<b>↩️ Replied message</b>", text)
        self.assertIn("Sticker ID: <code>sticker-file-id</code>", text)
        self.assertIn("Photo ID: <code>big-photo</code>", text)


class TelegramDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_sticker_falls_back_to_explicit_bot(self) -> None:
        message = SimpleNamespace(
            reply_sticker=AsyncMock(side_effect=TelegramError("reply failed")),
            chat=SimpleNamespace(id=123),
        )
        bot = SimpleNamespace(send_sticker=AsyncMock())

        await send_sticker(message, "success", bot)

        bot.send_sticker.assert_awaited_once()

    async def test_video_retries_without_thumbnail(self) -> None:
        sent = SimpleNamespace(video=SimpleNamespace(file_id="video-id"), document=None)
        message = SimpleNamespace(
            reply_video=AsyncMock(side_effect=[TelegramError("bad thumb"), sent]),
            reply_document=AsyncMock(),
        )

        result = await _send_video_with_fallback(
            message, path="/shared/videos/video.mp4", filename="video.mp4",
            caption="video", duration=10, height=720, thumbnail="/shared/videos/thumb.jpg",
        )

        self.assertIs(result, sent)
        self.assertEqual(message.reply_video.await_count, 2)
        self.assertIsNone(message.reply_video.await_args_list[1].kwargs["thumbnail"])
        message.reply_document.assert_not_awaited()


class UrlClassificationTests(unittest.TestCase):
    def test_extract_url_trims_common_trailing_punctuation(self) -> None:
        self.assertEqual(
            extract_url("open https://example.com/file.pdf)."),
            "https://example.com/file.pdf",
        )

    def test_http_url_validation_rejects_missing_host(self) -> None:
        self.assertTrue(is_http_url("https://example.com/video"))
        self.assertFalse(is_http_url("https:///missing-host"))
        self.assertFalse(is_http_url("ftp://example.com/file"))

    def test_youtube_watch_url_is_not_treated_as_playlist(self) -> None:
        self.assertFalse(is_likely_playlist_url("https://www.youtube.com/watch?v=abc123"))
        self.assertFalse(is_likely_playlist_url("https://youtu.be/abc123"))

    def test_known_playlist_urls_are_detected(self) -> None:
        self.assertTrue(is_likely_playlist_url("https://www.youtube.com/playlist?list=PL123"))
        self.assertTrue(is_likely_playlist_url("https://open.spotify.com/playlist/abc123"))
        self.assertTrue(is_likely_playlist_url("https://example.com/album/example-title"))


class YTDLPServiceTests(unittest.TestCase):
    def test_progressive_video_uses_exact_selected_format(self) -> None:
        option = SimpleNamespace(format_id="hls-480", has_audio=True)

        self.assertEqual(
            YTDLPService._video_format_selector(option),
            "hls-480",
        )

    def test_video_only_format_never_falls_back_to_best_resolution(self) -> None:
        option = SimpleNamespace(format_id="137", has_audio=False)
        selector = YTDLPService._video_format_selector(option)

        self.assertEqual(selector, "137+bestaudio/137")
        self.assertNotIn("/best", selector)

    def test_base_options_include_configured_javascript_runtimes(self) -> None:
        original = settings.ytdlp_js_runtimes
        settings.ytdlp_js_runtimes = ["node:/usr/bin/node", "deno"]
        try:
            opts = YTDLPService()._base(Path("downloads"))
        finally:
            settings.ytdlp_js_runtimes = original

        self.assertEqual(
            opts["js_runtimes"],
            {"node": {"path": "/usr/bin/node"}, "deno": {}},
        )

    def test_base_options_include_cookie_file_when_configured(self) -> None:
        cookie_file = Path("database/test-youtube-cookies.txt")
        cookie_file.write_text(
            "# Netscape HTTP Cookie File\n"
            ".youtube.com\tTRUE\t/\tTRUE\t2147483647\tSID\ttest-value\n"
            "IG\n",
            encoding="utf-8",
        )
        cookie_file.chmod(0o600)
        original = settings.ytdlp_cookie_file
        settings.ytdlp_cookie_file = cookie_file
        service = YTDLPService()
        try:
            opts = service._base(Path("downloads"))
            working_cookie_text = service._cookie_work_file.read_text(encoding="utf-8")
        finally:
            settings.ytdlp_cookie_file = original
            cookie_file.unlink(missing_ok=True)
            service._cookie_work_file.unlink(missing_ok=True)

        self.assertEqual(opts["cookiefile"], str(service._cookie_work_file))
        self.assertNotEqual(opts["cookiefile"], str(cookie_file))
        self.assertNotIn(
            "\nIG\n",
            working_cookie_text,
        )

    def test_missing_cookie_file_allows_unauthenticated_attempt(self) -> None:
        original = settings.ytdlp_cookie_file
        settings.ytdlp_cookie_file = Path("database/missing-youtube-cookies.txt")
        try:
            opts = YTDLPService()._base(Path("downloads"))
        finally:
            settings.ytdlp_cookie_file = original

        self.assertNotIn("cookiefile", opts)

    def test_empty_cookie_file_is_rejected_without_reading_contents_to_logs(self) -> None:
        cookie_file = Path("database/test-empty-cookies.txt")
        cookie_file.touch()
        cookie_file.chmod(0o600)
        original = settings.ytdlp_cookie_file
        settings.ytdlp_cookie_file = cookie_file
        try:
            with self.assertRaises(DownloadError) as raised:
                YTDLPService()._base(Path("downloads"))
        finally:
            settings.ytdlp_cookie_file = original
            cookie_file.unlink(missing_ok=True)

        self.assertIn("empty", str(raised.exception))

    def test_non_netscape_cookie_file_is_rejected(self) -> None:
        cookie_file = Path("database/test-invalid-cookies.txt")
        cookie_file.write_text("not a cookie export\n", encoding="utf-8")
        cookie_file.chmod(0o600)
        original = settings.ytdlp_cookie_file
        settings.ytdlp_cookie_file = cookie_file
        try:
            with self.assertRaises(DownloadError) as raised:
                YTDLPService()._base(Path("downloads"))
        finally:
            settings.ytdlp_cookie_file = original
            cookie_file.unlink(missing_ok=True)

        self.assertIn("Netscape", str(raised.exception))

    def test_cloud_ip_challenge_without_cookies_has_actionable_error(self) -> None:
        original = settings.ytdlp_cookie_file
        settings.ytdlp_cookie_file = None
        try:
            error = YTDLPService._download_error(
                RuntimeError("Sign in to confirm you're not a bot")
            )
        finally:
            settings.ytdlp_cookie_file = original

        self.assertIn("hosting provider", str(error))
        self.assertIn("YTDLP_COOKIES_FILE", str(error))

    def test_cloud_ip_challenge_with_cookie_warns_cookies_are_not_guaranteed(self) -> None:
        cookie_file = Path("database/test-present-cookies.txt")
        cookie_file.touch()
        original = settings.ytdlp_cookie_file
        settings.ytdlp_cookie_file = cookie_file
        try:
            error = YTDLPService._download_error(
                RuntimeError("Sign in to confirm you're not a bot")
            )
        finally:
            settings.ytdlp_cookie_file = original
            cookie_file.unlink(missing_ok=True)

        self.assertIn("expired or invalid", str(error))
        self.assertIn("cannot guarantee", str(error))


class SettingsTests(unittest.TestCase):
    def test_local_bot_api_is_the_default_upload_endpoint(self) -> None:
        loaded = Settings(
            TELEGRAM_BOT_TOKEN="123456:test-token",
            OWNER_ID=999,
            _env_file=None,
        )
        self.assertEqual(
            loaded.telegram_api_base_url,
            "http://telegram-bot-api:8081/bot",
        )
        self.assertEqual(loaded.max_upload_bytes, 2_000_000_000)

    def test_comma_separated_list_values_load_from_env_file(self) -> None:
        env_file = Path("database/test-settings.env")
        env_file.write_text(
            "\n".join([
                "TELEGRAM_BOT_TOKEN=123456:test-token",
                "OWNER_ID=999",
                "YTDLP_JS_RUNTIMES=node,deno",
                "URL_REACTIONS=🌚,⚡",
            ]),
            encoding="utf-8",
        )
        try:
            loaded = Settings(_env_file=env_file)
        finally:
            env_file.unlink(missing_ok=True)

        self.assertEqual(loaded.ytdlp_js_runtimes, ["node", "deno"])
        self.assertEqual(loaded.url_reactions, ["🌚", "⚡"])

    def test_new_cookie_environment_name_is_supported(self) -> None:
        loaded = Settings(
            TELEGRAM_BOT_TOKEN="123456:test-token",
            OWNER_ID=999,
            YTDLP_COOKIES_FILE="~/.config/telegram-bot/youtube-cookies.txt",
            _env_file=None,
        )
        self.assertEqual(
            loaded.ytdlp_cookie_file,
            Path("~/.config/telegram-bot/youtube-cookies.txt"),
        )


class SystemDependencyTests(unittest.TestCase):
    def test_missing_ffmpeg_raises_cloud_shell_install_hint(self) -> None:
        with patch("bot.system_dependencies.shutil.which", return_value=None):
            with self.assertRaises(DownloadError) as raised:
                require_ffmpeg()

        self.assertIn("docker compose build", str(raised.exception))


class LocalBotApiUploadTests(unittest.TestCase):
    def test_upload_uses_an_absolute_path_without_reading_file(self) -> None:
        path = Path("downloads/local-upload-test.bin")
        path.write_bytes(b"test")
        try:
            self.assertEqual(local_file_path(path), str(path.resolve()))
        finally:
            path.unlink(missing_ok=True)

    def test_missing_upload_file_is_rejected(self) -> None:
        with self.assertRaises(DownloadError):
            local_file_path(Path("downloads/does-not-exist.bin"))

    def test_insufficient_disk_space_is_reported(self) -> None:
        with patch("bot.local_bot_api.shutil.disk_usage") as disk_usage:
            disk_usage.return_value = SimpleNamespace(free=1)
            with self.assertRaises(DownloadError) as raised:
                ensure_disk_space()
        self.assertIn("Insufficient disk space", str(raised.exception))


class CommandRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_wait_accepts_ready_local_bot_api(self) -> None:
        bot_entry = _load_bot_entrypoint()
        response = SimpleNamespace(
            is_success=True,
            status_code=200,
            json=lambda: {"ok": True},
        )
        with patch.object(bot_entry.httpx, "get", return_value=response) as request:
            bot_entry.wait_for_local_bot_api()

        request.assert_called_once()

    async def test_application_uses_python_telegram_bot_local_mode(self) -> None:
        bot_entry = _load_bot_entrypoint()
        application = bot_entry.build_application()
        self.assertTrue(application.bot.local_mode)
        self.assertTrue(
            str(application.bot.base_url).startswith(
                "http://telegram-bot-api:8081/bot"
            )
        )

    async def test_post_init_hides_owner_commands_from_public_menu(self) -> None:
        bot_entry = _load_bot_entrypoint()
        application = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()))

        await bot_entry.post_init(application)

        public_call = application.bot.set_my_commands.await_args_list[0]
        owner_call = application.bot.set_my_commands.await_args_list[1]
        public_commands = [command.command for command in public_call.args[0]]
        owner_commands = [command.command for command in owner_call.args[0]]

        self.assertIn("quote", public_commands)
        self.assertIn("ss", public_commands)
        self.assertIn("search", public_commands)
        self.assertIn("wiki", public_commands)
        self.assertIn("ping", public_commands)
        self.assertNotIn("jobs", public_commands)
        self.assertNotIn("broadcast", public_commands)
        self.assertNotIn("restart", public_commands)
        self.assertNotIn("stop", public_commands)
        self.assertIn("jobs", owner_commands)
        self.assertEqual(owner_call.kwargs["scope"].chat_id, 999)


class ErrorHandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_conflict_stops_current_instance(self) -> None:
        application = SimpleNamespace(stop_running=MagicMock())
        context = SimpleNamespace(
            error=Conflict("terminated by other getUpdates request"),
            application=application,
        )

        with self.assertLogs("bot.handlers.common", level="ERROR") as logs:
            await error_handler(None, context)

        application.stop_running.assert_called_once_with()
        self.assertIn("another bot instance", "\n".join(logs.output))


class ToolCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_command_requires_reply_message(self) -> None:
        message = SimpleNamespace(reply_to_message=None, reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(args=[])

        await tools.quote_command.__wrapped__(update, context)

        message.reply_text.assert_awaited_once()
        self.assertIn("Missing reply", message.reply_text.await_args.args[0])

    async def test_quote_command_generates_webp_sticker_from_reply_text(self) -> None:
        sticker_mock = AsyncMock()
        message = SimpleNamespace(
            reply_to_message=SimpleNamespace(
                text="A useful quoted message",
                caption=None,
                from_user=SimpleNamespace(full_name="Quote Author"),
            ),
            reply_sticker=sticker_mock,
        )
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(args=[])

        await tools.quote_command.__wrapped__(update, context)

        sticker_mock.assert_awaited_once()
        sticker_file = sticker_mock.await_args.args[0]
        self.assertIsInstance(sticker_file, BytesIO)
        self.assertEqual(sticker_file.name, "quote.webp")
        self.assertGreater(len(sticker_file.getvalue()), 100)

    async def test_screenshot_command_validates_url_before_request(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(args=["not-a-url"])

        await tools.screenshot_command.__wrapped__(update, context)

        message.reply_text.assert_awaited_once()
        self.assertIn("Missing website URL", message.reply_text.await_args.args[0])

    async def test_ping_command_validates_missing_host(self) -> None:
        message = SimpleNamespace(reply_text=AsyncMock())
        update = SimpleNamespace(effective_message=message)
        context = SimpleNamespace(args=[])

        await tools.ping_command.__wrapped__(update, context)

        message.reply_text.assert_awaited_once()
        self.assertIn("Missing host", message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
