"""Orchestrates a single clip-generation request: validate, extract, upload, aggregate."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

from src.config import S3Config
from src.dedup_store import DedupStore
from src.ffmpeg_processor import FfmpegError, FfmpegProcessor
from src.s3_storage import S3Error, S3Storage
from src.snapshot_patcher import SnapshotPatchError, SnapshotPatcher
from src.source_cache import SourceCache, SourceCacheError

logger = logging.getLogger(__name__)


def _format_seek(seek: float) -> str:
    """Deterministic, filename-safe formatting for a seek time in seconds."""
    return f"{seek:.3f}"


class RequestHandler:
    def __init__(
        self,
        source_cache: SourceCache,
        ffmpeg_processor: FfmpegProcessor,
        s3_storage: S3Storage,
        dedup_store: DedupStore,
        s3_config: S3Config,
        work_dir: Path,
        dry_run: bool = False,
        snapshot_patcher: Optional[SnapshotPatcher] = None,
    ):
        self._source_cache = source_cache
        self._ffmpeg = ffmpeg_processor
        self._s3_storage = s3_storage
        self._dedup_store = dedup_store
        self._s3_config = s3_config
        self._work_dir = work_dir
        self._dry_run = dry_run
        self._snapshot_patcher = snapshot_patcher
        self._work_dir.mkdir(parents=True, exist_ok=True)

    def handle_request(self, raw_payload: bytes) -> Optional[dict[str, Any]]:
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError:
            logger.error("Dropping message: invalid JSON payload")
            return None

        date = payload.get("date")
        slices = payload.get("slices")
        if not date or not isinstance(date, str) or not isinstance(slices, list):
            logger.error(
                "Dropping message: missing/invalid 'date' or 'slices'",
                extra={"extra_fields": {"payload_keys": list(payload.keys())}},
            )
            return None

        logger.info(
            "Processing request",
            extra={"extra_fields": {"date": date, "slice_count": len(slices)}},
        )

        clips: list[dict[str, Any]] = []
        success = 0
        failed = 0

        try:
            source_path = self._source_cache.ensure_cached()
        except SourceCacheError as exc:
            logger.error("Cannot process request, source unavailable: %s", exc)
            failed = len(slices)
        else:
            for slice_payload in slices:
                clip = self._process_slice(date, slice_payload, source_path)
                if clip is None:
                    failed += 1
                else:
                    success += 1
                    clips.append(clip)

        # Sync the resulting URLs to the panel before the MQTT response goes out,
        # so consumers reading the response can rely on the snapshot rows already
        # being up to date. A patch failure is logged but doesn't drop the
        # response — the clips themselves were still generated successfully.
        self._patch_snapshots(clips)

        response = {"date": date, "success": success, "failed": failed, "clips": clips}
        logger.info(
            "Finished request",
            extra={"extra_fields": {"date": date, "success": success, "failed": failed}},
        )
        return response

    def _patch_snapshots(self, clips: list[dict[str, Any]]) -> None:
        if self._snapshot_patcher is None or self._dry_run or not clips:
            return
        try:
            self._snapshot_patcher.patch_clips(clips)
        except SnapshotPatchError as exc:
            logger.error("Failed to patch pulse-fraud-snapshots: %s", exc)

    def _process_slice(
        self, date: str, slice_payload: Any, source_path: Path
    ) -> Optional[dict[str, Any]]:
        try:
            raw_id = slice_payload["id"]
            if isinstance(raw_id, bool) or not isinstance(raw_id, (str, int)):
                raise TypeError(f"id must be a string or number, got {type(raw_id).__name__}")
            # The id may be a UUID string or a plain number; preserve whatever
            # type the caller sent it as in the response, but use a stable
            # string form as the dedup-store key so lookups aren't type-sensitive.
            slice_id = raw_id
            dedup_key = str(raw_id)
            from_seek = float(slice_payload["from_seek"])
            to_seek = float(slice_payload["to_seek"])
        except (KeyError, TypeError, ValueError):
            logger.error("Skipping malformed slice", extra={"extra_fields": {"slice": slice_payload}})
            return None

        if to_seek <= from_seek or from_seek < 0:
            logger.error(
                "Skipping slice with invalid seek range",
                extra={"extra_fields": {"slice_id": slice_id, "from_seek": from_seek, "to_seek": to_seek}},
            )
            if not self._dry_run:
                self._dedup_store.upsert(
                    date, dedup_key, from_seek, to_seek, "failed", error="invalid seek range"
                )
            return None

        existing = self._dedup_store.get(date, dedup_key)
        if existing is not None and existing.status == "success":
            logger.info(
                "Slice already processed, reusing cached result",
                extra={"extra_fields": {"date": date, "slice_id": slice_id}},
            )
            return {"id": slice_id, "videoUrl": existing.video_url, "thumbnailUrl": existing.thumbnail_url}

        seek_suffix = f"{_format_seek(from_seek)}_{_format_seek(to_seek)}"
        clip_key = f"{self._s3_config.clips_prefix}{date}_{seek_suffix}.mp4"
        thumb_key = f"{self._s3_config.thumbnails_prefix}{date}_{seek_suffix}.jpg"
        local_clip = self._work_dir / f"{date}_{seek_suffix}.mp4"
        local_thumb = self._work_dir / f"{date}_{seek_suffix}.jpg"

        try:
            if self._dry_run:
                logger.info(
                    "[dry-run] would extract and upload clip/thumbnail",
                    extra={"extra_fields": {"clip_key": clip_key, "thumb_key": thumb_key}},
                )
                video_url = self._s3_storage.build_url(clip_key)
                thumbnail_url = self._s3_storage.build_url(thumb_key)
            else:
                self._ffmpeg.extract_clip(source_path, from_seek, to_seek, local_clip)
                self._ffmpeg.extract_thumbnail(source_path, from_seek, local_thumb)
                video_url = self._s3_storage.upload_file(local_clip, clip_key, content_type="video/mp4")
                thumbnail_url = self._s3_storage.upload_file(local_thumb, thumb_key, content_type="image/jpeg")
                self._dedup_store.upsert(
                    date, dedup_key, from_seek, to_seek, "success", video_url, thumbnail_url
                )
        except (FfmpegError, S3Error) as exc:
            logger.exception(
                "Slice processing failed", extra={"extra_fields": {"date": date, "slice_id": slice_id}}
            )
            if not self._dry_run:
                self._dedup_store.upsert(date, dedup_key, from_seek, to_seek, "failed", error=str(exc))
            return None
        finally:
            local_clip.unlink(missing_ok=True)
            local_thumb.unlink(missing_ok=True)

        return {"id": slice_id, "videoUrl": video_url, "thumbnailUrl": thumbnail_url}
