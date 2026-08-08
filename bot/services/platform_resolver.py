from __future__ import annotations

import html
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import httpx

from bot.services.website_capture import (
    UnsafeWebsiteError,
    discover_page_media,
    fetch_public_json,
    validate_public_url,
)


MEDIA_SUFFIXES = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
    ".mp4", ".webm", ".mov", ".m3u8", ".mpd",
}


def _is_reddit(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "reddit.com" or host.endswith(".reddit.com")


def uses_platform_resolver(url: str) -> bool:
    """True when a URL must not be delegated to yt-dlp."""
    return _is_reddit(url)


def _reddit_media_redirect(url: str) -> str | None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if not (host == "reddit.com" or host.endswith(".reddit.com")):
        return None
    if parsed.path.rstrip("/") != "/media":
        return None
    nested = parse_qs(parsed.query).get("url", [None])[0]
    return unquote(nested) if nested else None


def _reddit_json_urls(url: str) -> list[str]:
    if not _is_reddit(url):
        return []
    parsed = urlparse(url)
    if "/comments/" not in parsed.path:
        return []
    path = parsed.path.rstrip("/") + ".json"
    parts = [part for part in parsed.path.split("/") if part]
    try:
        post_id = parts[parts.index("comments") + 1]
    except (ValueError, IndexError):
        return []
    query = urlencode({"raw_json": "1"})
    return [
        urlunparse((parsed.scheme, parsed.netloc, path, "", query, "")),
        f"https://www.reddit.com/comments/{post_id}.json?{query}",
        f"https://api.reddit.com/comments/{post_id}?{query}",
        f"https://www.reddit.com/oembed?{urlencode({'url': url, 'format': 'json'})}",
    ]


def _reddit_json_media(value) -> str | None:
    candidates: list[str] = []

    def visit(node) -> None:
        if isinstance(node, dict):
            for key in ("url_overridden_by_dest", "fallback_url", "url"):
                candidate = node.get(key)
                if isinstance(candidate, str):
                    candidates.append(html.unescape(candidate))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(value)
    for candidate in candidates:
        parsed = urlparse(candidate)
        host = (parsed.hostname or "").lower()
        suffix = PurePosixPath(parsed.path).suffix.lower()
        if (
            parsed.scheme in {"http", "https"}
            and (suffix in MEDIA_SUFFIXES or host.endswith(("i.redd.it", "v.redd.it")))
        ):
            return candidate
    return None


async def _resolve_reddit_post(url: str) -> str | None:
    for endpoint in _reddit_json_urls(url):
        try:
            payload = await fetch_public_json(endpoint)
        except (UnsafeWebsiteError, httpx.HTTPError):
            continue
        if candidate := _reddit_json_media(payload):
            return candidate
    return None


async def resolve_unsupported_media(url: str) -> str | None:
    """Resolve explicit media from platforms/pages that yt-dlp cannot extract."""
    candidate = _reddit_media_redirect(url)
    if not candidate:
        candidate = await _resolve_reddit_post(url)
    if not candidate:
        candidate = await discover_page_media(url)
    # Reddit commonly advertises an HTML /media wrapper rather than the actual
    # i.redd.it file. Unwrap it after either JSON or page metadata discovery.
    if candidate:
        candidate = _reddit_media_redirect(candidate) or candidate
    if not candidate or candidate == url:
        return None
    await validate_public_url(candidate)
    return candidate
