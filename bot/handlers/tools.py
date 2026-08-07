from __future__ import annotations

import io
import textwrap
import time
from urllib.parse import quote_plus, urlparse

import httpx
from PIL import Image, ImageDraw, ImageFont
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot.formatter import escape
from bot.handlers.common import registered
from bot.utils import is_http_url


ERROR_PREFIX = "<b>⚠️ Request failed</b>\n\n"


def _query(context: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(context.args).strip()


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for path in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
    ):
        try:
            return ImageFont.truetype(path, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


async def _reply_error(message, text: str) -> None:
    await message.reply_text(
        f"{ERROR_PREFIX}{text}",
        parse_mode=ParseMode.HTML,
    )


@registered
async def quote_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    reply = update.effective_message.reply_to_message
    if not reply:
        await update.effective_message.reply_text(
            "<b>⚠️ Missing reply</b>\n\nReply to a text message with <code>/quote</code>.",
            parse_mode=ParseMode.HTML,
        )
        return

    text = reply.text or reply.caption
    if not text:
        await update.effective_message.reply_text(
            "<b>⚠️ No text found</b>\n\nThe replied message has no text or caption to quote.",
            parse_mode=ParseMode.HTML,
        )
        return

    author = reply.from_user.full_name if reply.from_user else "Unknown"
    body_font = _font(34)
    name_font = _font(24)
    wrapped = textwrap.wrap(text[:700], width=28)[:12]
    width = 512
    height = max(220, 92 + len(wrapped) * 44)
    image = Image.new("RGBA", (width, height), (18, 24, 32, 255))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((18, 18, width - 18, height - 18), radius=28, fill=(31, 41, 55, 255))
    draw.text((42, 36), author[:34], font=name_font, fill=(147, 197, 253, 255))
    y = 82
    for line in wrapped:
        draw.text((42, y), line, font=body_font, fill=(245, 247, 250, 255))
        y += 44

    output = io.BytesIO()
    image.thumbnail((512, 512))
    image.save(output, format="WEBP")
    output.seek(0)
    output.name = "quote.webp"
    await update.effective_message.reply_sticker(output)


@registered
async def screenshot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = _query(context)
    if not is_http_url(url):
        await update.effective_message.reply_text(
            "<b>⚠️ Missing website URL</b>\n\nUsage: <code>/ss https://example.com</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    screenshot_url = f"https://image.thum.io/get/width/1280/crop/900/{url}"
    try:
        await update.effective_message.reply_photo(
            screenshot_url,
            caption=f"<b>📸 Website screenshot</b>\n<code>{escape(url)}</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        await _reply_error(
            update.effective_message,
            f"URL: <code>{escape(url)}</code>\nError: <code>{escape(type(exc).__name__)}</code>",
        )


@registered
async def search_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = _query(context)
    if not query:
        await update.effective_message.reply_text(
            "<b>⚠️ Missing search query</b>\n\nUsage: <code>/search your question</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    params = {
        "q": query,
        "format": "json",
        "no_html": "1",
        "skip_disambig": "1",
    }
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            response = await client.get("https://api.duckduckgo.com/", params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        await update.effective_message.reply_text(
            f"{ERROR_PREFIX}Query: <code>{escape(query)}</code>\nError: <code>{escape(type(exc).__name__)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    title = data.get("Heading") or query
    summary = data.get("AbstractText") or ""
    url = data.get("AbstractURL") or f"https://www.google.com/search?q={quote_plus(query)}"
    image = data.get("Image")
    if image and image.startswith("/"):
        image = f"https://duckduckgo.com{image}"
    if not summary:
        summary = "No instant summary was available. Open the search link for top results."

    text = (
        f"<b>🔎 {escape(title)}</b>\n\n"
        f"{escape(summary[:900])}\n\n"
        f"<a href=\"{escape(url)}\">Open result</a> · "
        f"<a href=\"https://www.google.com/search?q={quote_plus(query)}\">Google search</a>"
    )
    if image:
        try:
            await update.effective_message.reply_photo(image, caption=text, parse_mode=ParseMode.HTML)
        except Exception:
            await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)
    else:
        await update.effective_message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=False)


@registered
async def wiki_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = _query(context)
    if not query:
        await update.effective_message.reply_text(
            "<b>⚠️ Missing Wikipedia query</b>\n\nUsage: <code>/wiki topic name</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    params = {"action": "opensearch", "search": query, "limit": 1, "namespace": 0, "format": "json"}
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get("https://en.wikipedia.org/w/api.php", params=params)
            response.raise_for_status()
            data = response.json()
    except Exception as exc:
        await update.effective_message.reply_text(
            f"{ERROR_PREFIX}Query: <code>{escape(query)}</code>\nError: <code>{escape(type(exc).__name__)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    titles = data[1]
    descriptions = data[2]
    urls = data[3]
    if not urls:
        await update.effective_message.reply_text(
            f"<b>⚠️ No Wikipedia page found</b>\n\nQuery: <code>{escape(query)}</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    await update.effective_message.reply_text(
        f"<b>{escape(titles[0])}</b>\n{escape(descriptions[0] if descriptions else '')}\n{escape(urls[0])}",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=False,
    )


@registered
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    target = _query(context)
    if not target:
        await update.effective_message.reply_text(
            "<b>⚠️ Missing host</b>\n\nUsage: <code>/ping example.com</code>",
            parse_mode=ParseMode.HTML,
        )
        return
    if not target.startswith(("http://", "https://")):
        target = f"https://{target}"
    host = urlparse(target).hostname or target
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            response = await client.get(target)
        elapsed = int((time.perf_counter() - started) * 1000)
        await update.effective_message.reply_text(
            f"<b>🏓 Ping result</b>\nHost: <code>{escape(host)}</code>\nHTTP: <code>{response.status_code}</code>\nTime: <code>{elapsed} ms</code>",
            parse_mode=ParseMode.HTML,
        )
    except Exception as exc:
        elapsed = int((time.perf_counter() - started) * 1000)
        await _reply_error(
            update.effective_message,
            f"Host: <code>{escape(host)}</code>\nTime: <code>{elapsed} ms</code>\nError: <code>{escape(type(exc).__name__)}</code>",
        )
