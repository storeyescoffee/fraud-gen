"""Frame-range clip extraction and thumbnail generation via ffmpeg/ffprobe."""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

from src.config import AppConfig, VideoConfig

logger = logging.getLogger(__name__)


class FfmpegError(Exception):
    """Raised when ffmpeg or ffprobe fails or returns unusable output."""


class FfmpegProcessor:
    def __init__(self, app_config: AppConfig, video_config: VideoConfig):
        self._app_config = app_config
        self._video_config = video_config
        self._fps_cache: dict[Path, float] = {}

    def get_fps(self, source_path: Path) -> float:
        if self._video_config.fps_override:
            return self._video_config.fps_override
        if source_path in self._fps_cache:
            return self._fps_cache[source_path]

        cmd = [
            self._app_config.ffprobe_bin,
            "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=r_frame_rate",
            "-of", "json",
            str(source_path),
        ]
        result = self._run(cmd, "ffprobe")
        try:
            data = json.loads(result.stdout)
            rate = data["streams"][0]["r_frame_rate"]
            num, _, den = rate.partition("/")
            fps = float(num) / float(den) if den else float(num)
        except (KeyError, IndexError, ValueError, json.JSONDecodeError) as exc:
            raise FfmpegError(f"Could not parse fps from ffprobe output: {result.stdout!r}") from exc

        if fps <= 0:
            raise FfmpegError(f"ffprobe reported non-positive fps: {fps}")

        self._fps_cache[source_path] = fps
        return fps

    def extract_clip(
        self,
        source_path: Path,
        from_frame: int,
        to_frame: int,
        fps: float,
        out_path: Path,
    ) -> None:
        start_time = from_frame / fps
        duration = (to_frame - from_frame + 1) / fps

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._app_config.ffmpeg_bin,
            "-y",
            "-i", str(source_path),
            "-ss", f"{start_time:.6f}",
            "-t", f"{duration:.6f}",
            "-c:v", self._video_config.video_codec,
            "-c:a", self._video_config.audio_codec,
            "-avoid_negative_ts", "make_zero",
            str(out_path),
        ]
        self._run(cmd, "ffmpeg clip extraction")

    def extract_thumbnail(
        self,
        source_path: Path,
        from_frame: int,
        fps: float,
        out_path: Path,
    ) -> None:
        start_time = from_frame / fps

        out_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            self._app_config.ffmpeg_bin,
            "-y",
            "-i", str(source_path),
            "-ss", f"{start_time:.6f}",
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
