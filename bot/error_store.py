from __future__ import annotations

import json
import logging
import os
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path


class JsonErrorHandler(logging.Handler):
    """Persist warning/error records as bounded, atomic JSON without user profiles."""

    def __init__(self, path: Path, max_records: int = 1000) -> None:
        super().__init__(level=logging.WARNING)
        self.path = path
        self.max_records = max_records
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exception = None
            if record.exc_info:
                exc_type, exc_value, _ = record.exc_info
                exception = {
                    "type": exc_type.__name__ if exc_type else None,
                    "message": str(exc_value) if exc_value else None,
                    "traceback": "".join(traceback.format_exception(*record.exc_info)),
                }
            item = {
                "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "source": {
                    "file": record.pathname,
                    "line": record.lineno,
                    "function": record.funcName,
                },
                "exception": exception,
                "process_id": record.process,
                "thread_name": record.threadName,
            }
            with self._lock:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                records = self._read_records()
                records.append(item)
                records = records[-self.max_records :]
                temporary = self.path.with_suffix(f"{self.path.suffix}.tmp")
                temporary.write_text(
                    json.dumps(records, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(temporary, self.path)
        except Exception:
            self.handleError(record)

    def _read_records(self) -> list[dict]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            return value if isinstance(value, list) else []
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return []
