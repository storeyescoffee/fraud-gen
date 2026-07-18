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

_EXPECTED_COLUMNS = [
    "date",
    "slice_id",
    "from_seek",
    "to_seek",
    "status",
    "video_url",
    "thumbnail_url",
    "error",
    "processed_at",
]


@dataclass(frozen=True)
class SliceRecord:
    date: str
    slice_id: str
    from_seek: float
    to_seek: float
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
        self._migrate_if_outdated()
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS processed_slices (
                    date TEXT NOT NULL,
                    slice_id TEXT NOT NULL,
                    from_seek REAL NOT NULL,
                    to_seek REAL NOT NULL,
                    status TEXT NOT NULL,
                    video_url TEXT,
                    thumbnail_url TEXT,
                    error TEXT,
                    processed_at TEXT NOT NULL,
                    PRIMARY KEY (date, slice_id)
                )
                """
            )

    def _migrate_if_outdated(self) -> None:
        """Drop processed_slices if its columns don't match the current schema.

        CREATE TABLE IF NOT EXISTS silently no-ops against a table that
        already exists with an old schema (e.g. a pre-rename from_frame/
        to_frame layout), which otherwise surfaces as a cryptic
        "no such column" error deep inside get()/upsert() on every message
        instead of a clear one-time fixup at startup. The dedup store is
        just an idempotency cache, not a source of truth, so dropping and
        recreating it on a schema change is safe — it only means already
        -processed slices get reprocessed once.
        """
        with closing(self._conn.cursor()) as cur:
            cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='processed_slices'")
            if cur.fetchone() is None:
                return
            cur.execute("PRAGMA table_info(processed_slices)")
            existing_columns = [row[1] for row in cur.fetchall()]

        if existing_columns != _EXPECTED_COLUMNS:
            logger.warning(
                "processed_slices has an outdated schema, recreating it (dedup history for this table is reset)",
                extra={
                    "extra_fields": {
                        "existing_columns": existing_columns,
                        "expected_columns": _EXPECTED_COLUMNS,
                    }
                },
            )
            with self._conn:
                self._conn.execute("DROP TABLE processed_slices")

    def get(self, date: str, slice_id: str) -> Optional[SliceRecord]:
        with closing(self._conn.cursor()) as cur:
            cur.execute(
                """
                SELECT date, slice_id, from_seek, to_seek, status, video_url, thumbnail_url, error
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
        from_seek: float,
        to_seek: float,
        status: str,
        video_url: Optional[str] = None,
        thumbnail_url: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO processed_slices
                    (date, slice_id, from_seek, to_seek, status, video_url, thumbnail_url, error, processed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(date, slice_id) DO UPDATE SET
                    from_seek=excluded.from_seek,
                    to_seek=excluded.to_seek,
                    status=excluded.status,
                    video_url=excluded.video_url,
                    thumbnail_url=excluded.thumbnail_url,
                    error=excluded.error,
                    processed_at=excluded.processed_at
                """,
                (
                    date,
                    slice_id,
                    from_seek,
                    to_seek,
                    status,
                    video_url,
                    thumbnail_url,
                    error,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )

    def close(self) -> None:
        self._conn.close()
