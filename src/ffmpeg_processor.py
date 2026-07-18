"""Seek-range clip extraction and thumbnail generation via ffmpeg."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from src.config import AppConfig, VideoConfig

logger = logging.getLogger(__name__)


class FfmpegError(Exception):
    """Raised when ffmpeg fails or returns unusable output."""


class FfmpegProcessor:
    def __init__(self, app_config: AppConfig, video_config: VideoConfig):
        self._app_config = app_config
        self._video_config = video_config

    def extract_clip(
        self,
        source_path: Path,
        from_seek: float,
        to_seek: float,
        out_path: Path,
    ) -> None:
        duration = to_seek - from_seek

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._app_config.ffmpeg_bin,
            "-y",
            "-ss", f"{from_seek:.6f}",
            "-i", str(source_path),
            "-t", f"{duration:.6f}",
            "-c:v", self._video_config.video_codec,
            "-an",
            "-avoid_negative_ts", "make_zero",
            str(out_path),
        ]
        self._run(cmd, "ffmpeg clip extraction")

    def extract_thumbnail(
        self,
        source_path: Path,
        from_seek: float,
        out_path: Path,
    ) -> None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._app_config.ffmpeg_bin,
            "-y",
            "-ss", f"{from_seek:.6f}",
            "-i", str(source_path),
            "-frames:v", "1",
            "-q:v", "2",
            str(out_path),
        ]
        self._run(cmd, "ffmpeg thumbnail extraction")

    def _run(self, cmd: list[str], description: str) -> subprocess.CompletedProcess:
        logger.debug("Running %s", description, extra={"extra_fields": {"cmd": cmd}})
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300,
            )
        except FileNotFoundError as exc:
            raise FfmpegError(f"{description} failed: executable not found ({cmd[0]})") from exc
        except subprocess.TimeoutExpired as exc:
            raise FfmpegError(f"{description} timed out after {exc.timeout}s") from exc

        if result.returncode != 0:
            raise FfmpegError(
                f"{description} exited with code {result.returncode}: {result.stderr.strip()[-2000:]}"
            )
        return result
