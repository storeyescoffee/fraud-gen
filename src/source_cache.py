"""Keeps a local copy of the S3 source video, refreshing only on ETag change."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from src.config import CacheConfig, S3Config
from src.s3_storage import S3Storage

logger = logging.getLogger(__name__)


class SourceCacheError(Exception):
    """Raised when the source video cannot be fetched or is missing remotely."""


class SourceCache:
    """Downloads s3://bucket/source_key once and reuses it until the ETag changes."""

    def __init__(self, s3_storage: S3Storage, s3_config: S3Config, cache_config: CacheConfig):
        self._s3_storage = s3_storage
        self._s3_config = s3_config
        self._cache_config = cache_config
        self._meta_path = cache_config.dir / f"{cache_config.source_filename}.meta.json"

    def _read_cached_etag(self) -> str | None:
        if not self._meta_path.is_file():
            return None
        try:
            return json.loads(self._meta_path.read_text(encoding="utf-8")).get("etag")
        except (json.JSONDecodeError, OSError):
            return None

    def _write_cached_etag(self, etag: str) -> None:
        self._meta_path.parent.mkdir(parents=True, exist_ok=True)
        self._meta_path.write_text(json.dumps({"etag": etag}), encoding="utf-8")

    def ensure_cached(self) -> Path:
        """Return a local path to the up-to-date source video, downloading if needed."""
        remote_etag = self._s3_storage.head_object(self._s3_config.source_key)
        if remote_etag is None:
            raise SourceCacheError(
                f"Source object s3://{self._s3_config.bucket}/{self._s3_config.source_key} does not exist"
            )

        local_path = self._cache_config.source_path
        cached_etag = self._read_cached_etag()

        if cached_etag == remote_etag and local_path.is_file():
            logger.info("Source cache is up to date", extra={"extra_fields": {"etag": remote_etag}})
            return local_path

        logger.info(
            "Downloading source video",
            extra={"extra_fields": {"key": self._s3_config.source_key, "etag": remote_etag}},
        )
        self._s3_storage.download_file(self._s3_config.source_key, local_path)
        self._write_cached_etag(remote_etag)
        return local_path
