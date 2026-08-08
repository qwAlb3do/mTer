from __future__ import annotations

import asyncio
import json
import os
import importlib.util
import logging
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
from telegram.error import Conflict, TelegramError


os.environ["TELEGRAM_BOT_TOKEN"] = "123456:test-token"
os.environ["OWNER_ID"] = "999"
os.environ["USERS_FILE"] = "database/test-users.json"
os.environ["DOWNLOAD_DIR"] = "downloads"
os.environ["LOG_DIR"] = "logs"

from bot.formatter import STICKER_FALLBACKS, ascii_banner, help_panel, id_lines, info_panel, send_sticker
from bot.handlers.media import _send_video_with_fallback
from bot.handlers.common import error_handler
from bot.handlers import common, media, tools
from bot.config import settings
from bot.config import Settings
from bot.errors import DownloadError
from bot.error_store import JsonErrorHandler
from bot.services.platform_resolver import resolve_unsupported_media
from bot.services.ytdlp_service import YTDLPService
from bot.services.website_capture import (
    UnsafeWebsiteError,
    _capture_assets,
    discover_page_media,
    validate_public_url,
)
from bot.system_dependencies import require_ffmpeg
from bot.utils import extract_url, is_http_url, is_likely_playlist_url
from bot.local_bot_api import ensure_disk_space, local_file_path
from bot.test_url_store import describe_url, save_test_url
from bot.users import UserStore


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

    async def test_invalid_file_id_does_not_send_generated_sticker(self) -> None:
        message = SimpleNamespace(
            reply_sticker=AsyncMock(side_effect=TelegramError("wrong file id")),
            chat=SimpleNamespace(id=123),
        )
        bot = SimpleNamespace(send_sticker=AsyncMock(side_effect=TelegramError("wrong file id")))

        await send_sticker(message, "welcome", bot)

        bot.send_sticker.assert_awaited_once()
        self.assertEqual(
            bot.send_sticker.await_args.kwargs["sticker"],
            STICKER_FALLBACKS["welcome"],
        )

    async def test_docker_sticker_uses_hosted_api_when_local_api_rejects_id(self) -> None:
        message = SimpleNamespace(
            reply_sticker=AsyncMock(side_effect=TelegramError("local API rejected ID")),
            chat=SimpleNamespace(id=123),
        )
        local_bot = SimpleNamespace(send_sticker=AsyncMock())
        original_mode = settings.runtime_mode
        settings.runtime_mode = "docker"
        try:
            with patch(
                "bot.formatter._send_sticker_via_hosted_api", new=AsyncMock()
            ) as hosted_send:
                await send_sticker(message, "welcome", local_bot)
        finally:
            settings.runtime_mode = original_mode

        hosted_send.assert_awaited_once_with(123, STICKER_FALLBACKS["welcome"])
        local_bot.send_sticker.assert_not_awaited()

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


class BusyRetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self) -> None:
        await media.download_guard.release(123)

    @staticmethod
    def _update():
        message = SimpleNamespace(edit_text=AsyncMock())
        query = SimpleNamespace(
            data="retry:url:token",
            message=message,
            answer=AsyncMock(),
        )
        return SimpleNamespace(
            callback_query=query,
            effective_user=SimpleNamespace(id=123),
            effective_message=message,
        )

    async def test_try_again_keeps_button_while_transfer_is_active(self) -> None:
        update = self._update()
        context = SimpleNamespace(bot_data={"retry_sessions": {
            "token": {"owner_id": 123, "url": "https://example.com/video", "created": 1},
        }})
        await media.download_guard.acquire(123)

        await media.retry_url_callback.__wrapped__(update, context)

        self.assertTrue(update.callback_query.answer.await_args.kwargs["show_alert"])
        markup = update.callback_query.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "retry:url:token")

    async def test_try_again_starts_saved_url_when_slot_is_free(self) -> None:
        update = self._update()
        context = SimpleNamespace(bot_data={"retry_sessions": {
            "token": {"owner_id": 123, "url": "https://example.com/video", "created": 1},
        }})

        with patch("bot.handlers.media._process_url", new=AsyncMock()) as process:
            await media.retry_url_callback.__wrapped__(update, context)

        process.assert_awaited_once_with(update, context, "https://example.com/video")
        self.assertNotIn("token", context.bot_data["retry_sessions"])
        self.assertFalse(await media.download_guard.is_active(123))


class GeneralUrlMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_website_gets_relevant_actions_without_quality_buttons(self) -> None:
        status = SimpleNamespace(edit_text=AsyncMock())
        message = SimpleNamespace(reply_text=AsyncMock(return_value=status))
        update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_message=message,
        )
        context = SimpleNamespace(bot_data={})

        with (
            patch("bot.handlers.media.react_to_user", new=AsyncMock()),
            patch("bot.handlers.media.validate_public_url", new=AsyncMock()),
            patch("bot.handlers.media._inspect_direct_file", new=AsyncMock(return_value=None)),
            patch("bot.handlers.media.is_known_media_url", return_value=False),
            patch("bot.handlers.media._send_website_capture", new=AsyncMock()) as capture,
        ):
            await media._process_url(update, context, "https://example.com/article")

        capture.assert_not_awaited()
        markup = status.edit_text.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in markup.inline_keyboard for button in row]
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertTrue(any(value.startswith("website:capture:") for value in callbacks))
        self.assertTrue(any(value.startswith("website:media:") for value in callbacks))
        self.assertFalse(any("kbps" in label or label.endswith("p") for label in labels))

    def test_direct_image_uses_single_image_download_action(self) -> None:
        info = media.DirectFileInfo(
            "https://example.com/photo.jpg", "photo.jpg", "image/jpeg", 1024
        )

        markup = media._direct_file_keyboard("token", info)
        labels = [button.text for row in markup.inline_keyboard for button in row]

        self.assertIn("🖼 Download image", labels)
        self.assertFalse(any("kbps" in label or label.endswith("p") for label in labels))

    def test_signed_cdn_url_gets_bounded_filesystem_name(self) -> None:
        long_name = f"{'token' * 100}.jpg"
        result = media._safe_download_filename(long_name, f"https://cdn.example/{long_name}")

        self.assertLessEqual(len(result), 120)
        self.assertTrue(result.endswith(".jpg"))
        self.assertNotIn("/", result)

    async def test_reddit_media_redirect_is_resolved_without_ytdlp(self) -> None:
        url = "https://www.reddit.com/media?url=https%3A%2F%2Fi.redd.it%2Fimage.png"
        with patch(
            "bot.services.platform_resolver.validate_public_url", new=AsyncMock()
        ) as validate:
            result = await resolve_unsupported_media(url)

        self.assertEqual(result, "https://i.redd.it/image.png")
        validate.assert_awaited_once_with("https://i.redd.it/image.png")

    async def test_reddit_post_uses_public_json_media_metadata(self) -> None:
        post = "https://www.reddit.com/r/example/comments/abc123/a_post/"
        payload = [{"data": {"children": [{"data": {
            "url_overridden_by_dest": "https://i.redd.it/photo.jpg"
        }}]}}]
        with (
            patch(
                "bot.services.platform_resolver.fetch_public_json",
                new=AsyncMock(return_value=payload),
            ) as fetch,
            patch(
                "bot.services.platform_resolver.validate_public_url",
                new=AsyncMock(),
            ),
            patch(
                "bot.services.platform_resolver.discover_page_media",
                new=AsyncMock(),
            ) as discover,
        ):
            result = await resolve_unsupported_media(post)

        self.assertEqual(result, "https://i.redd.it/photo.jpg")
        self.assertIn("/comments/abc123/a_post.json", fetch.await_args.args[0])
        discover.assert_not_awaited()

    async def test_reddit_json_failure_falls_back_and_unwraps_html_media_url(self) -> None:
        post = "https://www.reddit.com/r/example/comments/abc123/a_post/"
        wrapper = (
            "https://www.reddit.com/media?url="
            "https%3A%2F%2Fi.redd.it%2Ffallback-image.png"
        )
        with (
            patch(
                "bot.services.platform_resolver.fetch_public_json",
                new=AsyncMock(side_effect=httpx.HTTPError("blocked")),
            ),
            patch(
                "bot.services.platform_resolver.discover_page_media",
                new=AsyncMock(return_value=wrapper),
            ) as discover,
            patch(
                "bot.services.platform_resolver.validate_public_url",
                new=AsyncMock(),
            ) as validate,
        ):
            result = await resolve_unsupported_media(post)

        self.assertEqual(result, "https://i.redd.it/fallback-image.png")
        discover.assert_awaited_once_with(post)
        validate.assert_awaited_once_with("https://i.redd.it/fallback-image.png")

    async def test_meta_refresh_media_wrapper_is_discovered_and_unwrapped(self) -> None:
        post = "https://www.reddit.com/r/example/comments/abc123/a_post/"
        html_page = (
            b'<meta http-equiv="refresh" content="0; url=https://www.reddit.com/media?'
            b'url=https%3A%2F%2Fi.redd.it%2Frefresh-image.png">'
        )
        with (
            patch(
                "bot.services.platform_resolver.fetch_public_json",
                new=AsyncMock(side_effect=httpx.HTTPError("blocked")),
            ),
            patch(
                "bot.services.website_capture._fetch_html",
                new=AsyncMock(return_value=(post, html_page, "text/html")),
            ),
            patch(
                "bot.services.website_capture.validate_public_url",
                new=AsyncMock(),
            ),
            patch(
                "bot.services.platform_resolver.validate_public_url",
                new=AsyncMock(),
            ),
        ):
            result = await resolve_unsupported_media(post)

        self.assertEqual(result, "https://i.redd.it/refresh-image.png")


class JsonErrorStoreTests(unittest.TestCase):
    def test_warning_and_traceback_are_written_to_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "errors.json"
            handler = JsonErrorHandler(path)
            logger = logging.getLogger("tests.error-store")
            logger.handlers = [handler]
            logger.propagate = False
            logger.setLevel(logging.WARNING)
            try:
                try:
                    raise OSError("file name too long")
                except OSError:
                    logger.warning("Direct file failed: %s", "https://example.com/file", exc_info=True)
                records = json.loads(path.read_text(encoding="utf-8"))
            finally:
                logger.handlers = []
                handler.close()

        self.assertEqual(records[0]["level"], "WARNING")
        self.assertIn("https://example.com/file", records[0]["message"])
        self.assertEqual(records[0]["exception"]["type"], "OSError")
        self.assertIn("file name too long", records[0]["exception"]["traceback"])


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


class WebsiteSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_private_ip_url_is_blocked(self) -> None:
        private_record = [(2, 1, 6, "", ("127.0.0.1", 80))]
        with patch(
            "bot.services.website_capture.asyncio.to_thread",
            new=AsyncMock(return_value=private_record),
        ):
            with self.assertRaises(UnsafeWebsiteError):
                await validate_public_url("http://127.0.0.1/admin")

    async def test_hostname_resolving_to_private_ip_is_blocked(self) -> None:
        private_record = [(2, 1, 6, "", ("10.0.0.5", 443))]
        with patch(
            "bot.services.website_capture.asyncio.to_thread",
            new=AsyncMock(return_value=private_record),
        ):
            with self.assertRaises(UnsafeWebsiteError):
                await validate_public_url("https://internal.example/")

    async def test_public_hostname_is_allowed(self) -> None:
        public_record = [(2, 1, 6, "", ("93.184.216.34", 443))]
        with patch(
            "bot.services.website_capture.asyncio.to_thread",
            new=AsyncMock(return_value=public_record),
        ):
            await validate_public_url("https://example.com/")

    async def test_nonstandard_port_is_blocked_before_dns(self) -> None:
        with self.assertRaises(UnsafeWebsiteError):
            await validate_public_url("https://example.com:8080/")

    async def test_static_same_origin_assets_are_captured_and_rewritten(self) -> None:
        html = (
            b'<link rel="stylesheet" href="/style.css">'
            b'<img src="/images/photo.png">'
        )

        async def fetch(url, _limit, _accept="*/*", **_kwargs):
            if url.endswith("style.css"):
                return url, b"body{background:url('/images/bg.png')}", "text/css"
            return url, b"image-bytes", "image/png"

        with patch("bot.services.website_capture._fetch_bytes", side_effect=fetch):
            rewritten, assets, manifest = await _capture_assets(
                "https://example.com/page", html, "text/html; charset=utf-8"
            )

        self.assertNotIn(b'href="/style.css"', rewritten)
        self.assertNotIn(b'src="/images/photo.png"', rewritten)
        self.assertEqual(len(assets), 3)
        self.assertEqual(manifest["captured_assets"], 3)
        css = next(data for path, data in assets.items() if path.endswith("style.css"))
        self.assertIn(b"../assets/", css)

    async def test_page_media_discovery_resolves_reddit_style_image_metadata(self) -> None:
        html = b'<meta property="og:image" content="https://i.redd.it/example.png">'
        with (
            patch(
                "bot.services.website_capture._fetch_html",
                new=AsyncMock(return_value=("https://reddit.com/post", html, "text/html")),
            ),
            patch(
                "bot.services.website_capture.validate_public_url",
                new=AsyncMock(),
            ),
        ):
            result = await discover_page_media("https://reddit.com/post")

        self.assertEqual(result, "https://i.redd.it/example.png")


class TestUrlStoreTests(unittest.IsolatedAsyncioTestCase):
    def test_describe_url_generates_platform_and_case_id(self) -> None:
        platform, case_id = describe_url(
            "https://www.youtube.com/watch?v=Cyl3X88KEgg"
        )

        self.assertEqual(platform, "youtube")
        self.assertEqual(case_id, "youtube-cyl3x88kegg")

    async def test_save_test_url_creates_then_updates_same_case(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "url_list.json"
            original = settings.url_test_list_file
            settings.url_test_list_file = path
            try:
                created = await save_test_url(
                    "https://www.tiktok.com/@creator/video/7517763008584568086",
                    "video",
                )
                updated = await save_test_url(
                    "https://www.tiktok.com/@creator/video/7517763008584568086",
                    "audio",
                )
            finally:
                settings.url_test_list_file = original

            payload = json.loads(path.read_text(encoding="utf-8"))

        self.assertTrue(created.created)
        self.assertFalse(updated.created)
        self.assertEqual(created.case_id, "tiktok-7517763008584568086")
        self.assertEqual(len(payload["cases"]), 1)
        self.assertEqual(payload["cases"][0]["format"], "audio")


class UserPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def test_owner_touch_and_history_are_not_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.json"
            store = UserStore(path)
            owner = SimpleNamespace(
                id=settings.owner_id,
                username="owner",
                first_name="Private",
                last_name="Owner",
                language_code="en",
                is_bot=False,
            )

            await store.touch(owner)
            await store.add_success(settings.owner_id, "https://example.com", "Private title")

            self.assertFalse(path.exists())

    async def test_owner_cache_is_shared_without_owner_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.json"
            store = UserStore(path)

            await store.add_success(
                settings.owner_id,
                "https://example.com/video",
                "Cached title",
                cache_key="cache-key",
                file_id="telegram-file-id",
                file_kind="video",
            )
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertNotIn(str(settings.owner_id), payload)
            self.assertEqual(payload["_cache"]["cache-key"]["file_id"], "telegram-file-id")

    async def test_legacy_owner_record_is_removed_without_deleting_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "users.json"
            path.write_text(json.dumps({
                str(settings.owner_id): {"id": settings.owner_id, "username": "owner"},
                "_cache": {"key": {"file_id": "cached"}},
            }), encoding="utf-8")
            store = UserStore(path)

            removed = await store.remove_user(settings.owner_id)
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertTrue(removed)
            self.assertNotIn(str(settings.owner_id), payload)
            self.assertIn("_cache", payload)


class YTDLPServiceTests(unittest.TestCase):
    def test_empty_extractor_result_becomes_download_error(self) -> None:
        downloader = MagicMock()
        downloader.__enter__.return_value.extract_info.return_value = None
        downloader.__exit__.return_value = False

        with patch("bot.services.ytdlp_service.yt_dlp.YoutubeDL", return_value=downloader):
            with self.assertRaises(DownloadError) as raised:
                YTDLPService()._inspect_sync("https://example.com/unsupported")

        self.assertIn("no downloadable media metadata", str(raised.exception))

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

    def test_tiktok_rehydration_failure_has_docker_and_cloud_ip_guidance(self) -> None:
        error = YTDLPService._download_error(
            RuntimeError("Unable to extract universal data for rehydration")
        )

        self.assertIn("Rebuild the Docker image", str(error))
        self.assertIn("Google Cloud Shell IP", str(error))


class SettingsTests(unittest.TestCase):
    def test_local_runtime_uses_hosted_bot_api(self) -> None:
        loaded = Settings(
            TELEGRAM_BOT_TOKEN="123456:test-token",
            OWNER_ID=999,
            _env_file=None,
        )
        self.assertEqual(
            loaded.telegram_api_base_url,
            "https://api.telegram.org/bot",
        )
        self.assertEqual(loaded.max_upload_bytes, 2_000_000_000)

    def test_docker_runtime_uses_local_bot_api(self) -> None:
        loaded = Settings(
            TELEGRAM_BOT_TOKEN="123456:test-token",
            OWNER_ID=999,
            RUNTIME_MODE="docker",
            _env_file=None,
        )
        self.assertEqual(loaded.telegram_api_base_url, "http://telegram-bot-api:8081/bot")

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
    def test_local_runtime_upload_uses_a_path_for_multipart(self) -> None:
        path = Path("downloads/local-upload-test.bin")
        path.write_bytes(b"test")
        try:
            self.assertEqual(local_file_path(path), path.resolve())
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

    async def test_local_application_uses_hosted_telegram_api(self) -> None:
        bot_entry = _load_bot_entrypoint()
        application = bot_entry.build_application()
        self.assertFalse(application.bot.local_mode)
        self.assertTrue(
            str(application.bot.base_url).startswith(
                "https://api.telegram.org/bot"
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
        self.assertIn("stop", owner_commands)
        self.assertIn("resume", owner_commands)
        self.assertIn("ban", owner_commands)
        self.assertIn("unban", owner_commands)
        self.assertIn("testurl", owner_commands)
        self.assertEqual(owner_call.kwargs["scope"].chat_id, 999)


class DockerLifecycleCommandTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_enables_maintenance_and_cancels_jobs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / ".maintenance"
            event = asyncio.Event()
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=settings.owner_id),
                effective_message=message,
            )
            context = SimpleNamespace(bot_data={"download_jobs": {"job": {"event": event}}})
            with patch.object(common, "MAINTENANCE_FILE", marker):
                await common.stop_command.__wrapped__(update, context)

            self.assertTrue(marker.is_file())
            self.assertTrue(event.is_set())
            self.assertIn("Bot stopped for maintenance", message.reply_text.await_args.args[0])

    async def test_resume_removes_maintenance_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / ".maintenance"
            marker.touch()
            message = SimpleNamespace(reply_text=AsyncMock())
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=settings.owner_id),
                effective_message=message,
            )
            with patch.object(common, "MAINTENANCE_FILE", marker):
                await common.resume_command.__wrapped__(update, SimpleNamespace(bot_data={}))

            self.assertFalse(marker.exists())


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
