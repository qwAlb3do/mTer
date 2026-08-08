"""Admin-only network downloads driven by ``tests/url_list.json``.

The suite is inert unless RUN_ADMIN_URL_TESTS=1 and is intended to run only
through the Docker Compose profile documented in README.md.
"""

from __future__ import annotations

import json
import os
import unittest
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx

from bot.handlers.media import _inspect_direct_file
from bot.services.platform_resolver import resolve_unsupported_media, uses_platform_resolver
from bot.services.website_capture import capture_website, validate_public_url
from bot.services.ytdlp_service import FormatOption, YTDLPService
from bot.utils import is_known_media_url, is_likely_playlist_url


URL_LIST = Path(__file__).with_name("url_list.json")
ENABLED = os.getenv("RUN_ADMIN_URL_TESTS") == "1"
VALID_FORMATS = {"auto", "fastest", "video", "audio", "image", "file", "website", "playlist"}
VALID_KINDS = {"video", "audio", "image", "file", "website", "playlist"}


@dataclass(frozen=True, slots=True)
class UrlCase:
    id: str
    platform: str
    url: str
    format: str
    expected_kind: str | None


def _valid_http_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def load_cases(path: Path = URL_LIST) -> list[UrlCase]:
    if not path.is_file():
        raise AssertionError(
            "tests/url_list.json is missing; copy url_list.example.json first"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AssertionError(f"url_list.json contains invalid JSON: {exc}") from exc

    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise AssertionError("url_list.json must use schema_version 1")
    raw_cases = payload.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise AssertionError("url_list.json cases must be a non-empty array")

    cases: list[UrlCase] = []
    seen_ids: set[str] = set()
    for index, item in enumerate(raw_cases):
        location = f"cases[{index}]"
        if not isinstance(item, dict):
            raise AssertionError(f"{location} must be an object")

        case_id = item.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            raise AssertionError(f"{location}.id must be a non-empty string")
        if case_id in seen_ids:
            raise AssertionError(f"Duplicate case id: {case_id}")
        seen_ids.add(case_id)

        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise AssertionError(f"{location}.enabled must be true or false")
        if not enabled:
            continue

        platform = item.get("platform")
        if not isinstance(platform, str) or not platform.strip():
            raise AssertionError(f"{location}.platform must be a non-empty string")
        url = item.get("url")
        if not _valid_http_url(url):
            raise AssertionError(f"{location}.url must be a valid HTTP(S) URL when enabled")
        format_name = item.get("format", "auto")
        if format_name not in VALID_FORMATS:
            raise AssertionError(
                f"{location}.format must be one of {sorted(VALID_FORMATS)}"
            )
        expected_kind = item.get("expected_kind")
        if expected_kind is not None and expected_kind not in VALID_KINDS:
            raise AssertionError(
                f"{location}.expected_kind must be one of {sorted(VALID_KINDS)} or null"
            )
        cases.append(UrlCase(
            id=case_id,
            platform=platform.strip(),
            url=url,
            format=format_name,
            expected_kind=expected_kind,
        ))

    if not cases:
        raise AssertionError("url_list.json must contain at least one enabled case")
    return cases


def select_format(formats: list[FormatOption], preference: str) -> FormatOption | None:
    candidates = formats if preference == "fastest" else [
        option for option in formats if option.kind == preference
    ]
    return next((option for option in candidates if option.fastest), None) or (
        candidates[0] if candidates else None
    )


@unittest.skipUnless(ENABLED, "admin-only downloads disabled (set RUN_ADMIN_URL_TESTS=1)")
class AdminUrlDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def _media(self, service: YTDLPService, case: UrlCase, url: str, preference: str):
        info = await service.inspect(url)
        option = select_format(info.formats, preference)
        self.assertIsNotNone(option, f"{case.id}: no {preference} format was reported")
        result = await service.download(url, option, lambda _data: None)
        try:
            self.assertTrue(result.path.is_file(), f"{case.id}: output is missing")
            self.assertGreater(result.path.stat().st_size, 0, f"{case.id}: output is empty")
            if case.expected_kind in {"video", "audio"}:
                self.assertEqual(result.kind, case.expected_kind)
        finally:
            service.cleanup(result)

    async def _direct(self, case: UrlCase, url: str) -> None:
        async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
        self.assertGreater(size, 0, f"{case.id}: direct response is empty")

    async def _run_case(self, service: YTDLPService, case: UrlCase) -> None:
        await validate_public_url(case.url)
        mode = case.format
        resolved = (
            await resolve_unsupported_media(case.url)
            if uses_platform_resolver(case.url)
            else None
        )
        effective_url = resolved or case.url

        if mode == "website":
            bundle = await capture_website(case.url)
            self.assertGreater(len(bundle.html_zip.getvalue()), 0)
            return
        if mode in {"image", "file"}:
            info = await _inspect_direct_file(effective_url)
            self.assertIsNotNone(info, f"{case.id}: URL is not a direct file")
            await self._direct(case, info.url)
            return
        if mode == "playlist":
            playlist = await service.inspect_playlist(case.url)
            self.assertIsNotNone(playlist, f"{case.id}: playlist metadata is missing")
            self.assertTrue(playlist.items, f"{case.id}: playlist has no items")
            await self._media(service, case, playlist.items[0].webpage_url, "fastest")
            return
        if mode in {"video", "audio", "fastest"}:
            await self._media(service, case, effective_url, mode)
            return

        direct = await _inspect_direct_file(effective_url)
        if direct:
            await self._direct(case, direct.url)
        elif is_likely_playlist_url(case.url):
            playlist = await service.inspect_playlist(case.url)
            self.assertIsNotNone(playlist, f"{case.id}: playlist metadata is missing")
            self.assertTrue(playlist.items, f"{case.id}: playlist has no items")
            await self._media(service, case, playlist.items[0].webpage_url, "fastest")
        elif is_known_media_url(case.url):
            await self._media(service, case, effective_url, "fastest")
        else:
            bundle = await capture_website(case.url)
            self.assertGreater(len(bundle.html_zip.getvalue()), 0)

    async def test_every_enabled_case_downloads(self) -> None:
        service = YTDLPService()
        for case in load_cases():
            with self.subTest(id=case.id, platform=case.platform, url=case.url):
                await self._run_case(service, case)
