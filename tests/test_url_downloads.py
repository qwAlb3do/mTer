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

from bot.services.ytdlp_service import FormatOption, YTDLPService


URL_LIST = Path(__file__).with_name("url_list.json")
ENABLED = os.getenv("RUN_ADMIN_URL_TESTS") == "1"
VALID_FORMATS = {"fastest", "video", "audio"}
VALID_KINDS = {"video", "audio"}


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
        format_name = item.get("format", "fastest")
        if format_name not in VALID_FORMATS:
            raise AssertionError(
                f"{location}.format must be one of {sorted(VALID_FORMATS)}"
            )
        expected_kind = item.get("expected_kind")
        if expected_kind is not None and expected_kind not in VALID_KINDS:
            raise AssertionError(
                f"{location}.expected_kind must be video, audio, or null"
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
    async def test_every_enabled_case_downloads(self) -> None:
        service = YTDLPService()
        for case in load_cases():
            with self.subTest(id=case.id, platform=case.platform, url=case.url):
                info = await service.inspect(case.url)
                option = select_format(info.formats, case.format)
                self.assertIsNotNone(
                    option, f"{case.id}: no {case.format} format was reported"
                )
                result = await service.download(case.url, option, lambda _data: None)
                try:
                    self.assertTrue(result.path.is_file(), f"{case.id}: output is missing")
                    self.assertGreater(
                        result.path.stat().st_size, 0, f"{case.id}: output is empty"
                    )
                    if case.expected_kind:
                        self.assertEqual(result.kind, case.expected_kind)
                finally:
                    service.cleanup(result)
