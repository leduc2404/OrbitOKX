"""Thread-safe structured log buffer with Python logging bridge & SSE pub/sub."""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from collections import deque
from datetime import datetime
from typing import List, Optional


class LogBuffer:
    """In-memory ring buffer storing structured logs and fanning out to SSE listeners."""

    def __init__(self, maxlen: int = 1500):
        self._lock = threading.Lock()
        self._entries: deque = deque(maxlen=maxlen)
        self._next_id: int = 1
        self._subscribers: List[queue.Queue] = []

    def emit(
        self,
        level: str,
        source: str,
        message: str,
        details: Optional[str] = None,
    ) -> dict:
        now = time.time()
        dt = datetime.fromtimestamp(now)
        entry = {
            "id": 0,
            "ts": now,
            "time": dt.strftime("%H:%M:%S.%f")[:-3],
            "iso": dt.strftime("%Y-%m-%d %H:%M:%S"),
            "level": level.upper(),
            "source": source.upper(),
            "message": str(message),
            "details": details or "",
        }
        with self._lock:
            entry["id"] = self._next_id
            self._next_id += 1
            self._entries.append(entry)
            dead: List[queue.Queue] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(entry)
                except queue.Full:
                    dead.append(q)
            for q in dead:
                try:
                    self._subscribers.remove(q)
                except ValueError:
                    pass
        return entry

    def info(self, source: str, message: str, details: Optional[str] = None) -> dict:
        return self.emit("INFO", source, message, details)

    def success(self, source: str, message: str, details: Optional[str] = None) -> dict:
        return self.emit("SUCCESS", source, message, details)

    def warning(self, source: str, message: str, details: Optional[str] = None) -> dict:
        return self.emit("WARNING", source, message, details)

    def error(self, source: str, message: str, details: Optional[str] = None) -> dict:
        return self.emit("ERROR", source, message, details)

    def debug(self, source: str, message: str, details: Optional[str] = None) -> dict:
        return self.emit("DEBUG", source, message, details)

    def list_entries(
        self,
        limit: int = 300,
        level: Optional[str] = None,
        source: Optional[str] = None,
        since_id: int = 0,
    ) -> List[dict]:
        with self._lock:
            items = list(self._entries)
        if since_id > 0:
            items = [e for e in items if e["id"] > since_id]
        if level and level.upper() != "ALL":
            lvl = level.upper()
            items = [e for e in items if e["level"] == lvl]
        if source and source.upper() != "ALL":
            src = source.upper()
            items = [e for e in items if e["source"] == src]
        if limit > 0:
            items = items[-limit:]
        return items

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=250)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def stream_sse(self):
        """Generator yielding Server-Sent Events for Flask streaming response."""
        q = self.subscribe()
        try:
            yield "retry: 2000\n\n"
            while True:
                try:
                    entry = q.get(timeout=15)
                    payload = json.dumps(entry, ensure_ascii=False)
                    yield f"id: {entry['id']}\ndata: {payload}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            self.unsubscribe(q)


class BufferLoggingHandler(logging.Handler):
    """Bridges standard Python loggers (services.crawler, services.processor, etc.) to LogBuffer."""

    _IGNORE_PREFIXES = ("werkzeug", "urllib3.connectionpool", "httpx", "httpcore")

    def __init__(self, buffer: LogBuffer):
        super().__init__(level=logging.INFO)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            name = record.name or ""
            if any(name.startswith(p) for p in self._IGNORE_PREFIXES):
                return
            msg = record.getMessage()
            if not msg:
                return

            lower_name = name.lower()
            if "crawler" in lower_name:
                source = "CRAWL"
            elif "processor" in lower_name or "gemini" in lower_name:
                source = "REWRITE"
            elif "database" in lower_name or "storage" in lower_name:
                source = "STORAGE"
            else:
                source = (name.rsplit(".", 1)[-1] or "SYSTEM").upper()[:12]

            details = ""
            if record.exc_info:
                details = self.formatException(record.exc_info)

            self.buffer.emit(record.levelname, source, msg, details=details)
        except Exception:
            pass
