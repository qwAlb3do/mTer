from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bot.config import settings


_lock = asyncio.Lock()
_SAFE_ID = re.compile(r"[^a-z0-9]+")
VALID_TEST_MODES = {
    "auto", "fastest", "video", "audio", "image", "file", "website", "playlist"
}


@dataclass(frozen=True, slots=True)
class SavedTestUrl:
    case_id: str
    platform: str
    url: str
    format: str
    expected_kind: str | None
    created: bool


def _platform(host: str) -> str:
    host = host.lower().removeprefix("www.").removeprefix("m.")
    if host in {"youtu.be", "youtube.com", "music.youtube.com"}:
        return "youtube"
    if host.endswith("tiktok.com"):
        return "tiktok"
    if host.endswith(("facebook.com", "fb.watch")):
        return "facebook"
    if host.endswith("instagram.com"):
        return "instagram"
    if host in {"x.com", "twitter.com"}:
        return "x"
    if host.endswith("pornhub.com"):
        return "pornhub"
    return host.split(".")[0] or "website"


def _content_identifier(url: str) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if query.get("v"):
        return query["v"][0]
    if query.get("viewkey"):
        return query["viewkey"][0]
    parts = [part for part in parsed.path.split("/") if part]
    ignored = {"video", "videos", "shorts", "reel", "share", "r", "watch"}
    useful = [part for part in parts if part.lower() not in ignored and not part.startswith("@")]
    return useful[-1] if useful else "url"


def _slug(value: str, fallback: str) -> str:
    value = _SAFE_ID.sub("-", value.lower()).strip("-")
    return value[:48] or fallback


def describe_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("A valid HTTP(S) URL is required.")
    platform = _platform(parsed.hostname)
    content_id = _slug(_content_identifier(url), "url")
    return platform, f"{_slug(platform, 'website')}-{content_id}"


def _read_payload(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "cases": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"Cannot read {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError(f"{path} does not use url-list schema version 1.")
    if not isinstance(payload.get("cases"), list):
        raise ValueError(f"{path} must contain a cases array.")
    return payload


async def save_test_url(url: str, format_name: str = "auto") -> SavedTestUrl:
    if format_name not in VALID_TEST_MODES:
        raise ValueError(f"Mode must be one of: {', '.join(sorted(VALID_TEST_MODES))}.")
    platform, suggested_id = describe_url(url)
    expected_kind = format_name if format_name in {
        "video", "audio", "image", "file", "website", "playlist"
    } else None
    path = settings.url_test_list_file

    async with _lock:
        payload = _read_payload(path)
        cases = payload["cases"]
        existing = next((case for case in cases if case.get("url") == url), None)
        if existing is not None:
            existing.update({
                "platform": platform,
                "enabled": True,
                "format": format_name,
                "expected_kind": expected_kind,
            })
            case_id = str(existing.get("id") or suggested_id)
            existing["id"] = case_id
            created = False
        else:
            used_ids = {str(case.get("id")) for case in cases}
            case_id = suggested_id
            suffix = 2
            while case_id in used_ids:
                case_id = f"{suggested_id}-{suffix}"
                suffix += 1
            cases.append({
                "id": case_id,
                "platform": platform,
                "url": url,
                "enabled": True,
                "format": format_name,
                "expected_kind": expected_kind,
            })
            created = True

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)

    return SavedTestUrl(case_id, platform, url, format_name, expected_kind, created)
