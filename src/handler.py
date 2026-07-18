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
from src.source_cache import SourceCache, SourceCacheError

logger = logging.getLogger(__name__)


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
    ):
        self._source_cache = source_cache
        self._ffmpeg = ffmpeg_processor
        self._s3_storage = s3_storage
        self._dedup_store = dedup_store
        self._s3_config = s3_config
        self._work_dir = work_dir
        self._dry_run = dry_run
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

        try:
            source_path = self._source_cache.ensure_cached()
            fps = self._ffmpeg.get_fps(source_path)
        except (SourceCacheError, FfmpegError) as exc:
            logger.error("Cannot process request, source/fps unavailable: %s", exc)
            return {"date": date, "success": 0, "failed": len(slices), "clips": []}

        clips = []
        success = 0
        failed = 0
        for slice_payload in slices:
            clip = self._process_slice(date, slice_payload, source_path, fps)
            if clip is None:
                failed += 1
            else:
                success += 1
                clips.append(clip)

        response = {"date": date, "success": success, "failed": failed, "clips": clips}
        logger.info(
            "Finished request",
            extra={"extra_fields": {"date": date, "success": success, "failed": failed}},
        )
        return response

    def _process_slice(
        self, date: str, slice_payload: Any, source_path: Path, fps: float
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
            from_frame = int(slice_payload["from_frame"])
            to_frame = int(slice_payload["to_frame"])
        except (KeyError, TypeError, ValueError):
            logger.error("Skipping malformed slice", extra={"extra_fields": {"slice": slice_payload}})
            return None

        if to_frame < from_frame or from_frame < 0:
            logger.error(
                "Skipping slice with invalid frame range",
                extra={"extra_fields": {"slice_id": slice_id, "from_frame": from_frame, "to_frame": to_frame}},
            )
            if not self._dry_run:
                self._dedup_store.upsert(
                    date, dedup_key, from_frame, to_frame, "failed", error="invalid frame range"
                )
            return None

        existing = self._dedup_store.get(date, dedup_key)
        if existing is not None and existing.status == "success":
            logger.info(
                "Slice already processed, reusing cached result",
                extra={"extra_fields": {"date": date, "slice_id": slice_id}},
            )
            return {"id": slice_id, "videoUrl": existing.video_url, "thumbnailUrl": existing.thumbnail_url}

        clip_key = f"{self._s3_config.clips_prefix}{date}_{from_frame}_{to_frame}.mp4"
        thumb_key = f"{self._s3_config.thumbnails_prefix}{date}_{from_frame}_{to_frame}.jpg"
        local_clip = self._work_dir / f"{date}_{from_frame}_{to_frame}.mp4"
        local_thumb = self._work_dir / f"{date}_{from_frame}_{to_frame}.jpg"

        try:
            if self._dry_run:
                logger.info(
                    "[dry-run] would extract and upload clip/thumbnail",
                    extra={"extra_fields": {"clip_key": clip_key, "thumb_key": thumb_key}},
                )
                video_url = self._s3_storage.build_url(clip_key)
                thumbnail_url = self._s3_storage.build_url(thumb_key)
            else:
                self._ffmpeg.extract_clip(source_path, from_frame, to_frame, fps, local_clip)
                self._ffmpeg.extract_thumbnail(source_path, from_frame, fps, local_thumb)
                video_url = self._s3_storage.upload_file(local_clip, clip_key, content_type="video/mp4")
                thumbnail_url = self._s3_storage.upload_file(local_thumb, thumb_key, content_type="image/jpeg")
                self._dedup_store.upsert(
                    date, dedup_key, from_frame, to_frame, "success", video_url, thumbnail_url
                )
        except (FfmpegError, S3Error) as exc:
            logger.exception(
                "Slice processing failed", extra={"extra_fields": {"date": date, "slice_id": slice_id}}
            )
            if not self._dry_run:
                self._dedup_store.upsert(date, dedup_key, from_frame, to_frame, "failed", error=str(exc))
            return None
        finally:
            local_clip.unlink(missing_ok=True)
            local_thumb.unlink(missing_ok=True)

        return {"id": slice_id, "videoUrl": video_url, "thumbnailUrl": thumbnail_url}
