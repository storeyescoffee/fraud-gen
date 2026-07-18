"""Structured logging configuration for the worker."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(extra)
        return json.dumps(payload)


class DailyFileHandler(logging.Handler):
    """Writes to logs/<YYYY-MM-DD>.log, switching files on the wall-clock date.

    Unlike TimedRotatingFileHandler, the *current* file is always named after
    today's date rather than a fixed base name that gets a date suffix on
    rotation — so a long-running daemon process naturally rolls over to a new
    file at midnight without ever mixing two days into one file.
    """

    def __init__(self, log_dir: Path, encoding: str = "utf-8"):
        super().__init__()
        self._log_dir = Path(log_dir)
        self._encoding = encoding
        self._current_date: str | None = None
        self._stream = None
        self._log_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _today() -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _ensure_current_stream(self) -> None:
        date_str = self._today()
        if self._stream is not None and date_str == self._current_date:
            return
        if self._stream is not None:
            self._stream.close()
        self._current_date = date_str
        path = self._log_dir / f"{date_str}.log"
        self._stream = open(path, "a", encoding=self._encoding)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._ensure_current_stream()
            self._stream.write(self.format(record) + "\n")
            self._stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        self.acquire()
        try:
            if self._stream is not None:
                self._stream.close()
                self._stream = None
        finally:
            self.release()
        super().close()


def setup_logging(level: str = "INFO", json_format: bool = True, log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    if json_format:
        formatter: logging.Formatter = JsonFormatter()
    else:
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root.addHandler(console_handler)

    if log_dir is not None:
        file_handler = DailyFileHandler(log_dir)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
