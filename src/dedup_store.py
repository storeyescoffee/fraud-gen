"""SQLite-backed idempotency store, keyed per (date, slice_id).

QoS 1 MQTT delivery means a request can arrive more than once. Deduping at
the individual slice level (rather than just the request's `date`) lets a
redelivered or retried request skip slices that already succeeded while
still allowing previously failed slices to be retried.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SliceRecord:
    date: str
    slice_id: str
    from_frame: int
    to_frame: int
    status: str  # "success" | "failed"
    video_url: Optional[str]
    thumbnail_url: Optional[str]
    error: Optional[str]


class DedupStore:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_slices (
                    date TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    from_frame INTEGER NOT NULL,
                    to_frame INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    video_url TEXT,
                    thumbnail_url TEXT,
                    error TEXT,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (date, slice_id)
                )
                """
            )

    def get(self, date: str, slice_id: str) -> Optional[SliceRecord]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT date, slice_id, from_frame, to_frame, status, video_url, thumbnail_url, error
                FROM processed_slices WHERE date = ? AND slice_id = ?
                """,
                (date, slice_id),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return SliceRecord(*row)

    def upsert(
        self,
        date: str,
        slice_id: str,
        from_frame: int,
        to_frame: int,
        status: str,
        video_url: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO processed_slices
                    (date, slice_id, from_frame, to_frame, status, video_url, thumbnail_url, error, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, slice_id) DO UPDATE SET
                    from_frame=excluded.from_frame,
                    to_frame=excluded.to_frame,
                    status=excluded.status,
                    video_url=excluded.video_url,
                    thumbnail_url=excluded.thumbnail_url,
                    error=excluded.error,
                    processed_at=excluded.processed_at
                """,
                (
                    date,
                    slice_id,
                    from_frame,
                    to_frame,
                    status,
                    video_url,
                    thumbnail_url,
                    error,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def close(self) -> None:
        self._conn.close()
