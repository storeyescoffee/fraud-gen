"""Bulk-updates videoUrl/thumbnailUrl on pulse_fraud_snapshots rows via the gateway API.

Maps the clip results produced by the request handler (id, videoUrl,
thumbnailUrl) onto the PATCH /pulse-fraud-snapshots contract and sends them
in a single authenticated bulk request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import requests

from src.gateway import GatewayClient

logger = logging.getLogger(__name__)

DEFAULT_PATCH_PATH = "/pulse-fraud-snapshots"


class SnapshotPatchError(Exception):
    """Raised when the bulk PATCH request fails or is rejected by the API."""


@dataclass(frozen=True)
class SnapshotUpdate:
    """One row of the PATCH /pulse-fraud-snapshots request body.

    id must reference an existing pulse_fraud_snapshots row (numeric Long).
    Per the API contract, an omitted field and an explicit null are
    equivalent (both clear the existing value), so both fields are always
    sent — None simply means "clear this on the server".
    """

    id: int
    video_url: Optional[str] = None
    thumbnail_url: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        return {"id": self.id, "videoUrl": self.video_url, "thumbnailUrl": self.thumbnail_url}


def updates_from_clips(clips: list[dict[str, Any]]) -> tuple[list[SnapshotUpdate], list[Any]]:
    """Map handler clip results (`{"id", "videoUrl", "thumbnailUrl"}`) to SnapshotUpdates.

    pulse_fraud_snapshots ids are numeric; a clip id that isn't representable
    as a Long (e.g. a UUID) can't be mapped to a snapshot row. Those are
    returned separately as `skipped` instead of silently dropped or sent as
    garbage ids.
    """
    updates: list[SnapshotUpdate] = []
    skipped: list[Any] = []
    for clip in clips:
        raw_id = clip.get("id")
        try:
            if isinstance(raw_id, bool):
                raise TypeError
            snapshot_id = int(raw_id)
        except (TypeError, ValueError):
            skipped.append(raw_id)
            continue
        updates.append(
            SnapshotUpdate(
                id=snapshot_id,
                video_url=clip.get("videoUrl"),
                thumbnail_url=clip.get("thumbnailUrl"),
            )
        )

    if skipped:
        logger.warning(
            "Skipped clips with non-numeric id when mapping to snapshot updates",
            extra={"extra_fields": {"skipped_ids": skipped}},
        )
    return updates, skipped


class SnapshotPatcher:
    def __init__(self, gateway: GatewayClient, patch_path: str = DEFAULT_PATCH_PATH):
        self._gateway = gateway
        self._patch_path = patch_path

    def patch(self, updates: list[SnapshotUpdate]) -> None:
        """Send a single bulk PATCH for all given updates. No-op if empty."""
        if not updates:
            return

        body = [update.to_payload() for update in updates]
        logger.info("Patching pulse-fraud-snapshots", extra={"extra_fields": {"count": len(body)}})

        try:
            response = self._gateway.request(
                "PATCH",
                self._patch_path,
                json=body,
                headers={"content-type": "application/json"},
            )
        except requests.RequestException as exc:
            raise SnapshotPatchError(f"PATCH {self._patch_path} request failed: {exc}") from exc

        if response.status_code == 204:
            logger.info("Snapshot patch succeeded", extra={"extra_fields": {"count": len(body)}})
            return

        raise SnapshotPatchError(
            f"PATCH {self._patch_path} returned {response.status_code}: {response.text[:2000]}"
        )

    def patch_clips(self, clips: list[dict[str, Any]]) -> list[Any]:
        """Convenience wrapper: map clip results and patch them in one call.

        Returns the list of clip ids that were skipped for not being numeric.
        """
        updates, skipped = updates_from_clips(clips)
        self.patch(updates)
        return skipped
