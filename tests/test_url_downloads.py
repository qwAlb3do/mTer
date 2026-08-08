"""Destructive/network integration checks for URLs supplied by an administrator.

These tests are deliberately inert unless RUN_ADMIN_URL_TESTS=1. Run them only
through the Docker Compose profile documented in README.md.
"""

from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from bot.services.ytdlp_service import YTDLPService


URL_LIST = Path(__file__).with_name("url_list.json")
ENABLED = os.getenv("RUN_ADMIN_URL_TESTS") == "1"


def load_urls() -> list[str]:
    if not URL_LIST.is_file():
        raise AssertionError("tests/url_list.json is missing; copy url_list.example.json first")
    payload = json.loads(URL_LIST.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        payload = payload.get("urls")
    if not isinstance(payload, list) or not payload:
        raise AssertionError("url_list.json must be a non-empty JSON list or {\"urls\": [...]}")
    urls = [item.get("url") if isinstance(item, dict) else item for item in payload]
    if not all(isinstance(url, str) and url.startswith(("http://", "https://")) for url in urls):
        raise AssertionError("Every URL entry must be an HTTP(S) string or an object with a url field")
    return urls


@unittest.skipUnless(ENABLED, "admin-only downloads disabled (set RUN_ADMIN_URL_TESTS=1)")
class AdminUrlDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_every_configured_url_downloads(self) -> None:
        service = YTDLPService()
        for url in load_urls():
            with self.subTest(url=url):
                info = await service.inspect(url)
                option = next((item for item in info.formats if item.fastest), None)
                self.assertIsNotNone(option, "no downloadable format was reported")
                result = await service.download(url, option, lambda _data: None)
                try:
                    self.assertTrue(result.path.is_file())
                    self.assertGreater(result.path.stat().st_size, 0)
                finally:
                    service.cleanup(result)
