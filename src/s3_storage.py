"""Thin wrapper around boto3 S3 operations used by the worker."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

from src.config import AwsConfig, S3Config

logger = logging.getLogger(__name__)


class S3Error(Exception):
    """Raised when an S3 operation fails."""


class S3Storage:
    def __init__(self, aws_config: AwsConfig, s3_config: S3Config):
        self._aws_config = aws_config
        self._s3_config = s3_config

        session_kwargs = {"region_name": aws_config.region}
        if aws_config.access_key_id and aws_config.secret_access_key:
            session_kwargs["aws_access_key_id"] = aws_config.access_key_id
            session_kwargs["aws_secret_access_key"] = aws_config.secret_access_key

        client_kwargs = {"config": BotoConfig(retries={"max_attempts": 3, "mode": "standard"})}
        if aws_config.endpoint_url:
            client_kwargs["endpoint_url"] = aws_config.endpoint_url

        session = boto3.session.Session(**session_kwargs)
        self._client = session.client("s3", **client_kwargs)

    def head_object(self, key: str) -> Optional[str]:
        """Return the object's ETag, or None if it does not exist."""
        try:
            resp = self._client.head_object(Bucket=self._s3_config.bucket, Key=key)
            return resp.get("ETag", "").strip('"')
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("404", "NoSuchKey", "NotFound"):
                return None
            raise S3Error(f"head_object failed for s3://{self._s3_config.bucket}/{key}: {exc}") from exc

    def download_file(self, key: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = local_path.with_suffix(local_path.suffix + ".part")
        try:
            self._client.download_file(self._s3_config.bucket, key, str(tmp_path))
            tmp_path.replace(local_path)
        except ClientError as exc:
            tmp_path.unlink(missing_ok=True)
            raise S3Error(f"download_file failed for s3://{self._s3_config.bucket}/{key}: {exc}") from exc

    def upload_file(self, local_path: Path, key: str, content_type: Optional[str] = None) -> str:
        extra_args = {"ContentType": content_type} if content_type else None
        try:
            self._client.upload_file(
                str(local_path), self._s3_config.bucket, key, ExtraArgs=extra_args
            )
        except ClientError as exc:
            raise S3Error(f"upload_file failed for s3://{self._s3_config.bucket}/{key}: {exc}") from exc
        return self.build_url(key)

    def build_url(self, key: str) -> str:
        if self._s3_config.public_url_base:
            base = self._s3_config.public_url_base.rstrip("/")
            return f"{base}/{key}"
        region = self._aws_config.region
        return f"https://{self._s3_config.bucket}.s3.{region}.amazonaws.com/{key}"
