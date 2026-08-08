from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import io
import json
import mimetypes
import re
import socket
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urljoin, urlparse

import httpx


MAX_HTML_BYTES = 5 * 1024 * 1024
MAX_SCREENSHOT_BYTES = 10 * 1024 * 1024
MAX_ASSET_BYTES = 8 * 1024 * 1024
MAX_ARCHIVE_BYTES = 25 * 1024 * 1024
MAX_ASSETS = 48
MAX_PARALLEL_ASSETS = 8
MAX_REDIRECTS = 5
USER_AGENT = "mTer-Website-Capture/1.0"
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^)'\"]+)\1\s*\)", re.IGNORECASE)


class UnsafeWebsiteError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class WebsiteBundle:
    final_url: str
    hostname: str
    html_zip: io.BytesIO
    screenshot: io.BytesIO | None


class _AssetParser(HTMLParser):
    ATTRIBUTES = {
        "img": {"src", "srcset"},
        "script": {"src"},
        "link": {"href"},
        "source": {"src", "srcset"},
        "video": {"src", "poster"},
        "audio": {"src"},
        "track": {"src"},
        "object": {"data"},
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "link":
            relationships = set((values.get("rel") or "").lower().split())
            if not relationships.intersection({"stylesheet", "icon", "preload", "manifest"}):
                return
        for attribute in self.ATTRIBUTES.get(tag, set()):
            value = values.get(attribute)
            if not value:
                continue
            if attribute == "srcset":
                self.references.update(
                    item.strip().split()[0] for item in value.split(",") if item.strip()
                )
            else:
                self.references.add(value.strip())


class _MediaMetadataParser(HTMLParser):
    """Collect explicit page-level media metadata without crawling linked pages."""

    VIDEO_KEYS = {"og:video", "og:video:url", "og:video:secure_url", "twitter:player:stream"}
    IMAGE_KEYS = {"og:image", "og:image:url", "og:image:secure_url", "twitter:image"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.video_urls: list[str] = []
        self.image_urls: list[str] = []
        self.redirect_urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.lower(): value for key, value in attrs if value}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").lower()
            content = values.get("content")
            if (values.get("http-equiv") or "").lower() == "refresh" and content:
                match = re.search(r"(?:^|;)\s*url\s*=\s*(['\"]?)(.+?)\1\s*$", content, re.IGNORECASE)
                if match:
                    self.redirect_urls.append(match.group(2).strip())
            if content and key in self.VIDEO_KEYS:
                self.video_urls.append(content.strip())
            elif content and key in self.IMAGE_KEYS:
                self.image_urls.append(content.strip())
        elif tag in {"video", "source"} and values.get("src"):
            self.video_urls.append(values["src"].strip())
        elif tag == "link" and (values.get("rel") or "").lower() == "image_src":
            if values.get("href"):
                self.image_urls.append(values["href"].strip())


def _public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    return bool(ip.is_global)


async def validate_public_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise UnsafeWebsiteError("Only valid HTTP(S) website URLs are allowed.")
    if parsed.username or parsed.password:
        raise UnsafeWebsiteError("URLs containing usernames or passwords are blocked.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise UnsafeWebsiteError("The URL contains an invalid port.") from exc
    if port not in {None, 80, 443}:
        raise UnsafeWebsiteError("Only standard website ports 80 and 443 are allowed.")

    try:
        records = await asyncio.to_thread(
            socket.getaddrinfo,
            parsed.hostname,
            port or (443 if parsed.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except socket.gaierror as exc:
        raise UnsafeWebsiteError("The website hostname could not be resolved.") from exc
    addresses = {record[4][0] for record in records}
    if not addresses or any(not _public_address(address) for address in addresses):
        raise UnsafeWebsiteError(
            "Local, private, reserved, and non-public network addresses are blocked."
        )


async def _fetch_bytes(
    url: str,
    max_bytes: int,
    accept: str = "*/*",
    timeout_seconds: float = 25.0,
) -> tuple[str, bytes, str]:
    current = url
    headers = {"User-Agent": USER_AGENT, "Accept": accept}
    timeout = httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, headers=headers) as client:
        for redirect_count in range(MAX_REDIRECTS + 1):
            await validate_public_url(current)
            async with client.stream("GET", current) as response:
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise UnsafeWebsiteError("Website returned an invalid redirect.")
                    if redirect_count == MAX_REDIRECTS:
                        raise UnsafeWebsiteError("Website redirected too many times.")
                    current = urljoin(str(response.url), location)
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").lower()
                declared = response.headers.get("content-length")
                if declared:
                    try:
                        if int(declared) > max_bytes:
                            raise UnsafeWebsiteError(
                                "A website resource exceeds its safety size limit."
                            )
                    except ValueError:
                        pass
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > max_bytes:
                        raise UnsafeWebsiteError("A website resource exceeds its safety size limit.")
                return str(response.url), bytes(content), content_type
    raise UnsafeWebsiteError("Website request could not be completed.")


async def _fetch_html(url: str) -> tuple[str, bytes, str]:
    final_url, content, content_type = await _fetch_bytes(
        url,
        MAX_HTML_BYTES,
        "text/html,application/xhtml+xml",
    )
    if not (
        content_type.startswith("text/html")
        or content_type.startswith("application/xhtml+xml")
    ):
        raise UnsafeWebsiteError("The URL is not an HTML webpage.")
    return final_url, content, content_type


async def fetch_public_json(url: str, max_bytes: int = 2 * 1024 * 1024):
    """Fetch JSON through the same redirect and public-address safety checks."""
    _, content, _ = await _fetch_bytes(
        url,
        max_bytes,
        "application/json,text/json;q=0.9,*/*;q=0.1",
    )
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UnsafeWebsiteError("The public endpoint did not return valid JSON.") from exc


async def discover_page_media(url: str) -> str | None:
    """Return one explicit media URL advertised by a public webpage, if present."""
    final_url, content, content_type = await _fetch_html(url)
    parser = _MediaMetadataParser()
    parser.feed(_html_text(content, content_type))
    for candidate in [*parser.video_urls, *parser.image_urls, *parser.redirect_urls]:
        absolute = urljoin(final_url, candidate)
        try:
            await validate_public_url(absolute)
        except UnsafeWebsiteError:
            continue
        return absolute
    return None


async def capture_screenshot(url: str) -> io.BytesIO:
    await validate_public_url(url)
    screenshot_url = (
        "https://image.thum.io/get/width/1280/crop/900/noanimate/"
        f"{quote(url, safe='')}"
    )
    async with httpx.AsyncClient(timeout=45.0, follow_redirects=True) as client:
        response = await client.get(screenshot_url)
        response.raise_for_status()
    content_type = response.headers.get("content-type", "").lower()
    if not content_type.startswith("image/") or not response.content:
        raise UnsafeWebsiteError("The screenshot service did not return an image.")
    if len(response.content) > MAX_SCREENSHOT_BYTES:
        raise UnsafeWebsiteError("The screenshot is larger than the 10 MB safety limit.")
    output = io.BytesIO(response.content)
    output.name = "website-screenshot.jpg"
    return output


def _same_origin(candidate: str, page_url: str) -> bool:
    try:
        left, right = urlparse(candidate), urlparse(page_url)
        return (
            left.scheme == right.scheme
            and left.hostname == right.hostname
            and (left.port or (443 if left.scheme == "https" else 80))
            == (right.port or (443 if right.scheme == "https" else 80))
        )
    except ValueError:
        return False


def _asset_path(url: str, content_type: str) -> str:
    parsed = urlparse(url)
    basename = unquote(PurePosixPath(parsed.path).name) or "resource"
    basename = re.sub(r"[^A-Za-z0-9._-]", "-", basename)[:80]
    if "." not in basename:
        extension = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ".bin"
        basename += extension
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    return f"assets/{digest}-{basename}"


def _html_text(html: bytes, content_type: str) -> str:
    match = re.search(r"charset=([^;\s]+)", content_type, re.IGNORECASE)
    encoding = match.group(1).strip("\"'") if match else "utf-8"
    try:
        return html.decode(encoding, errors="replace")
    except LookupError:
        return html.decode("utf-8", errors="replace")


async def _capture_assets(
    page_url: str,
    html: bytes,
    content_type: str,
) -> tuple[bytes, dict[str, bytes], dict]:
    text = _html_text(html, content_type)
    parser = _AssetParser()
    parser.feed(text)
    parser.references.update(match.group(2).strip() for match in CSS_URL_RE.finditer(text))

    assets: dict[str, bytes] = {}
    mapping: dict[str, str | None] = {}
    skipped: list[dict[str, str]] = []
    total_bytes = 0
    fetch_slots = asyncio.Semaphore(MAX_PARALLEL_ASSETS)
    requested: set[str] = set()

    async def capture(reference: str, parent_url: str, depth: int = 0) -> str | None:
        nonlocal total_bytes
        if not reference or reference.startswith(("data:", "blob:", "javascript:", "mailto:", "#")):
            return None
        absolute = urljoin(parent_url, reference)
        if not _same_origin(absolute, page_url):
            skipped.append({"url": absolute, "reason": "cross-origin"})
            return None
        if absolute in mapping:
            return mapping[absolute]
        if len(requested) >= MAX_ASSETS:
            skipped.append({"url": absolute, "reason": "asset-count-limit"})
            return None
        requested.add(absolute)
        mapping[absolute] = None  # recursion/cycle guard
        try:
            async with fetch_slots:
                final_url, data, asset_type = await _fetch_bytes(
                    absolute, MAX_ASSET_BYTES, timeout_seconds=12.0
                )
            if not _same_origin(final_url, page_url):
                raise UnsafeWebsiteError("asset redirected across origins")
            if total_bytes + len(data) > MAX_ARCHIVE_BYTES:
                raise UnsafeWebsiteError("archive total-size limit reached")
            local_path = _asset_path(final_url, asset_type)
            mapping[absolute] = local_path
            mapping[final_url] = local_path

            if asset_type.startswith("text/css") and depth < 2:
                css = data.decode("utf-8", errors="replace")
                for match in list(CSS_URL_RE.finditer(css)):
                    raw = match.group(2).strip()
                    nested = await capture(raw, final_url, depth + 1)
                    if nested:
                        css = css.replace(raw, f"../{nested}")
                data = css.encode("utf-8")

            if total_bytes + len(data) > MAX_ARCHIVE_BYTES:
                raise UnsafeWebsiteError("archive total-size limit reached")

            assets[local_path] = data
            total_bytes += len(data)
            return local_path
        except (httpx.HTTPError, UnsafeWebsiteError, OSError) as exc:
            mapping[absolute] = None
            skipped.append({"url": absolute, "reason": str(exc)[:180]})
            return None

    references = sorted(parser.references)[:MAX_ASSETS]
    local_paths = await asyncio.gather(
        *(capture(reference, page_url) for reference in references)
    )
    for reference, local_path in zip(references, local_paths, strict=True):
        if local_path:
            text = text.replace(reference, local_path)

    manifest = {
        "captured_assets": len(assets),
        "asset_bytes": total_bytes,
        "limits": {
            "max_assets": MAX_ASSETS,
            "max_asset_bytes": MAX_ASSET_BYTES,
            "max_archive_asset_bytes": MAX_ARCHIVE_BYTES,
            "same_origin_only": True,
            "css_recursion_depth": 2,
        },
        "files": sorted(assets),
        "skipped": skipped,
    }
    return text.encode("utf-8"), assets, manifest


def _archive(
    final_url: str,
    html: bytes,
    content_type: str,
    assets: dict[str, bytes],
    manifest: dict,
) -> io.BytesIO:
    metadata = {
        "source_url": final_url,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "content_type": content_type,
        "note": "Same-origin static assets are stored locally; linked pages are not crawled.",
    }
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("index.html", html)
        archive.writestr("metadata.json", json.dumps(metadata, indent=2) + "\n")
        archive.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        for path, data in assets.items():
            archive.writestr(path, data)
    output.seek(0)
    return output


async def capture_website(url: str) -> WebsiteBundle:
    final_url, html, content_type = await _fetch_html(url)
    parsed = urlparse(final_url)
    hostname = parsed.hostname or "website"
    rewritten_html, assets, manifest = await _capture_assets(
        final_url, html, content_type
    )
    archive = _archive(
        final_url,
        rewritten_html,
        content_type,
        assets,
        manifest,
    )
    safe_host = "".join(char if char.isalnum() or char in ".-" else "-" for char in hostname)
    archive.name = f"{safe_host or 'website'}-page.zip"
    try:
        screenshot = await capture_screenshot(final_url)
    except (httpx.HTTPError, UnsafeWebsiteError):
        screenshot = None
    return WebsiteBundle(final_url, hostname, archive, screenshot)
