from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from telegram import User

from bot.config import settings


class UserStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = asyncio.Lock()

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        temp = self.path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp.replace(self.path)

    async def touch(self, user: User) -> dict[str, Any]:
        async with self._lock:
            data = self._read()
            key = str(user.id)
            now = datetime.now(timezone.utc).isoformat()
            record = data.get(key, {})
            record.update({
                "id": user.id,
                "username": user.username,
                "first_name": user.first_name,
                "last_name": user.last_name,
                "language_code": user.language_code,
                "is_bot": user.is_bot,
                "last_seen": now,
            })
            record.setdefault("first_seen", now)
            record.setdefault("banned", False)
            record.setdefault("successful_urls", [])
            data[key] = record
            self._write(data)
            return record

    async def is_banned(self, user_id: int) -> bool:
        async with self._lock:
            return bool(self._read().get(str(user_id), {}).get("banned", False))

    async def add_success(
        self,
        user_id: int,
        url: str,
        title: str,
        *,
        cache_key: str | None = None,
        file_id: str | None = None,
        file_kind: str | None = None,
    ) -> None:
        async with self._lock:
            data = self._read()
            record = data.setdefault(str(user_id), {
                "id": user_id,
                "banned": False,
                "successful_urls": [],
            })
            history = record.setdefault("successful_urls", [])
            now = datetime.now(timezone.utc).isoformat()

            existing = next((item for item in history if item.get("url") == url), None)
            if existing:
                existing["title"] = title
                existing["last_downloaded_at"] = now
                existing["download_count"] = int(existing.get("download_count", 1)) + 1
            else:
                history.append({
                    "url": url,
                    "title": title,
                    "first_downloaded_at": now,
                    "last_downloaded_at": now,
                    "download_count": 1,
                })

            record["successful_urls"] = history[-200:]
            if cache_key and file_id and file_kind:
                cache = data.setdefault("_cache", {})
                cache[cache_key] = {
                    "file_id": file_id,
                    "kind": file_kind,
                    "title": title,
                    "url": url,
                    "updated_at": now,
                }
            self._write(data)

    async def set_banned(self, user_id: int, banned: bool) -> None:
        async with self._lock:
            data = self._read()
            record = data.setdefault(str(user_id), {
                "id": user_id,
                "successful_urls": [],
            })
            record["banned"] = banned
            self._write(data)

    async def get_cached_file(self, cache_key: str) -> dict[str, Any] | None:
        async with self._lock:
            item = self._read().get("_cache", {}).get(cache_key)
            return item if isinstance(item, dict) else None

    async def user_ids(self) -> list[int]:
        async with self._lock:
            ids = []
            for key, record in self._read().items():
                if key.startswith("_") or not isinstance(record, dict):
                    continue
                user_id = record.get("id")
                if isinstance(user_id, int) and not record.get("banned"):
                    ids.append(user_id)
            return ids


users = UserStore(settings.users_file)
