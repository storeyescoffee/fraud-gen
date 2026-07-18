"""Loads and validates config.conf into typed, immutable config objects."""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


class ConfigError(Exception):
    """Raised when config.conf is missing, malformed, or fails validation."""


def _resolve(value: Optional[str]) -> Optional[str]:
    """Resolve "env:VAR_NAME" values from the environment; blank -> None."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("env:"):
        var_name = value[len("env:"):]
        resolved = os.environ.get(var_name)
        if resolved is None:
            raise ConfigError(f"Environment variable '{var_name}' referenced by config is not set")
        return resolved
    return value


@dataclass(frozen=True)
class MqttConfig:
    host: str
    port: int
    username: Optional[str]
    password: Optional[str]
    client_id: str
    qos: int
    keepalive: int
    tls: bool
    topic_request: str
    topic_response: str


@dataclass(frozen=True)
class AwsConfig:
    access_key_id: Optional[str]
    secret_access_key: Optional[str]
    region: str
    endpoint_url: Optional[str]


@dataclass(frozen=True)
class S3Config:
    bucket: str
    source_key: str
    clips_prefix: str
    thumbnails_prefix: str
    public_url_base: Optional[str]


@dataclass(frozen=True)
class VideoConfig:
    fps_override: Optional[float]
    video_codec: str
    audio_codec: str


@dataclass(frozen=True)
class CacheConfig:
    dir: Path
    source_filename: str

    @property
    def source_path(self) -> Path:
        return self.dir / self.source_filename


@dataclass(frozen=True)
class DedupConfig:
    db_path: Path


@dataclass(frozen=True)
class GatewayConfig:
    base_url: str
    login_path: str
    username: Optional[str]
    password: Optional[str]
    token_cache_path: Path
    refresh_margin_seconds: int


@dataclass(frozen=True)
class AppConfig:
    log_level: str
    log_json: bool
    log_dir: Path
    work_dir: Path
    ffmpeg_bin: str
    ffprobe_bin: str


@dataclass(frozen=True)
class Config:
    mqtt: MqttConfig
    aws: AwsConfig
    s3: S3Config
    video: VideoConfig
    cache: CacheConfig
    dedup: DedupConfig
    gateway: GatewayConfig
    app: AppConfig


def load_config(path: str | Path) -> Config:
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}")

    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")

    try:
        mqtt = MqttConfig(
            host=parser.get("mqtt", "host"),
            port=parser.getint("mqtt", "port", fallback=1883),
            username=_resolve(parser.get("mqtt", "username", fallback="")),
            password=_resolve(parser.get("mqtt", "password", fallback="")),
            client_id=parser.get("mqtt", "client_id", fallback="fraud-gen-clip-worker"),
            qos=parser.getint("mqtt", "qos", fallback=1),
            keepalive=parser.getint("mqtt", "keepalive", fallback=60),
            tls=parser.getboolean("mqtt", "tls", fallback=False),
            topic_request=parser.get("mqtt", "topic_request"),
            topic_response=parser.get("mqtt", "topic_response"),
        )

        aws = AwsConfig(
            access_key_id=_resolve(parser.get("aws", "access_key_id", fallback="")),
            secret_access_key=_resolve(parser.get("aws", "secret_access_key", fallback="")),
            region=parser.get("aws", "region", fallback="us-east-1"),
            endpoint_url=_resolve(parser.get("aws", "endpoint_url", fallback="")),
        )

        s3 = S3Config(
            bucket=parser.get("s3", "bucket"),
            source_key=parser.get("s3", "source_key"),
            clips_prefix=parser.get("s3", "clips_prefix", fallback="clips/"),
            thumbnails_prefix=parser.get("s3", "thumbnails_prefix", fallback="clips/thumbnail/"),
            public_url_base=_resolve(parser.get("s3", "public_url_base", fallback="")),
        )

        fps_raw = _resolve(parser.get("video", "fps_override", fallback=""))
        video = VideoConfig(
            fps_override=float(fps_raw) if fps_raw else None,
            video_codec=parser.get("video", "video_codec", fallback="libx264"),
            audio_codec=parser.get("video", "audio_codec", fallback="aac"),
        )

        cache = CacheConfig(
            dir=Path(parser.get("cache", "dir", fallback="./cache")),
            source_filename=parser.get("cache", "source_filename", fallback="source.mp4"),
        )

        dedup = DedupConfig(
            db_path=Path(parser.get("dedup", "db_path", fallback="./data/dedup.sqlite3")),
        )

        gateway = GatewayConfig(
            base_url=parser.get("gateway", "base_url", fallback="https://panel.storeyes.io"),
            login_path=parser.get("gateway", "login_path", fallback="/api/auth/login"),
            username=_resolve(parser.get("gateway", "username", fallback="")),
            password=_resolve(parser.get("gateway", "password", fallback="")),
            token_cache_path=Path(
                parser.get("gateway", "token_cache_path", fallback="./cache/gateway_token.json")
            ),
            refresh_margin_seconds=parser.getint("gateway", "refresh_margin_seconds", fallback=60),
        )

        app = AppConfig(
            log_level=parser.get("app", "log_level", fallback="INFO"),
            log_json=parser.getboolean("app", "log_json", fallback=True),
            log_dir=Path(parser.get("app", "log_dir", fallback="./logs")),
            work_dir=Path(parser.get("app", "work_dir", fallback="./work")),
            ffmpeg_bin=parser.get("app", "ffmpeg_bin", fallback="ffmpeg"),
            ffprobe_bin=parser.get("app", "ffprobe_bin", fallback="ffprobe"),
        )
    except (configparser.NoSectionError, configparser.NoOptionError) as exc:
        raise ConfigError(f"Missing required config value: {exc}") from exc
    except ValueError as exc:
        raise ConfigError(f"Invalid config value: {exc}") from exc

    if mqtt.qos not in (0, 1, 2):
        raise ConfigError(f"mqtt.qos must be 0, 1, or 2, got {mqtt.qos}")
    if not s3.bucket:
        raise ConfigError("s3.bucket must not be empty")

    return Config(mqtt=mqtt, aws=aws, s3=s3, video=video, cache=cache, dedup=dedup, gateway=gateway, app=app)
