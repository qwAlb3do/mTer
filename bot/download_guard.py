from __future__ import annotations

import asyncio


class DownloadGuard:
    def __init__(self) -> None:
        self._active_users: set[int] = set()
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: int) -> bool:
        async with self._lock:
            if user_id in self._active_users:
                return False
            self._active_users.add(user_id)
            return True

    async def release(self, user_id: int) -> None:
        async with self._lock:
            self._active_users.discard(user_id)

    async def is_active(self, user_id: int) -> bool:
        async with self._lock:
            return user_id in self._active_users

    async def active_count(self) -> int:
        async with self._lock:
            return len(self._active_users)


download_guard = DownloadGuard()
