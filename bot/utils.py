from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)


def extract_url(text: str | None) -> str | None:
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    return match.group(0).rstrip(".,);]}>\"'")


def is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)
    except ValueError:
        return False


def is_spotify_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower()
        return host == "spotify.com" or host.endswith(".spotify.com")
    except ValueError:
        return False


def is_known_media_url(url: str) -> bool:
    try:
        host = (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return False
    domains = (
        "youtube.com", "youtu.be", "tiktok.com", "facebook.com", "fb.watch",
        "instagram.com", "x.com", "twitter.com", "spotify.com", "pornhub.com",
        "soundcloud.com", "vimeo.com", "twitch.tv",
    )
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def is_spotify_playlist_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        return (host == "spotify.com" or host.endswith(".spotify.com")) and "/playlist/" in parsed.path
    except ValueError:
        return False


def is_likely_playlist_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    host = (parsed.hostname or "").lower()
    path = parsed.path.lower()
    query = parse_qs(parsed.query)

    if is_spotify_playlist_url(url):
        return True

    if host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if path == "/playlist" and query.get("list"):
            return True
        if path.startswith(("/watch", "/shorts/", "/embed/")):
            return False
        return False

    if host == "youtu.be" or host.endswith(".youtu.be"):
        return False

    playlist_markers = (
        "/playlist/",
        "/playlists/",
        "/album/",
        "/albums/",
        "/set/",
        "/sets/",
        "/collection/",
        "/collections/",
    )
    return any(marker in path for marker in playlist_markers)


def human_size(size: int | None) -> str:
    if size is None:
        return "unknown size"
    units = ["B", "KB", "MB", "GB", "TB"]
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def cleanup_paths(paths: list[Path]) -> None:
    for path in paths:
        try:
            if path.exists() and path.is_file():
                path.unlink()
        except OSError:
            pass
