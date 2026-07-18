"""Entry point for the MQTT-driven fraud clip generation worker."""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading

from src.config import ConfigError, load_config
from src.dedup_store import DedupStore
from src.ffmpeg_processor import FfmpegProcessor
from src.gateway import GatewayClient
from src.handler import RequestHandler
from src.logging_setup import setup_logging
from src.mqtt_client import MqttClient, MqttClientError
from src.s3_storage import S3Storage
from src.snapshot_patcher import SnapshotPatcher
from src.source_cache import SourceCache

logger = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="MQTT-driven fraud clip generation worker")
    parser.add_argument("--config", default="config.conf", help="Path to config.conf")
    parser.add_argument(
        "--log-level",
        default=None,
        help="Override app.log_level from config (DEBUG, INFO, WARNING, ERROR)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip ffmpeg/S3 writes; log intended actions and publish predicted URLs",
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Process a single request then exit, instead of running as a daemon",
    )
    parser.add_argument(
        "--one-shot-timeout",
        type=float,
        default=60.0,
        help="Seconds to wait for a message in --one-shot mode before exiting non-zero",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    setup_logging(
        args.log_level or config.app.log_level,
        json_format=config.app.log_json,
        log_dir=config.app.log_dir,
    )

    logger.info(
        "Starting fraud-generator-helper worker",
        extra={"extra_fields": {"dry_run": args.dry_run, "one_shot": args.one_shot}},
    )

    dedup_store = DedupStore(config.dedup.db_path)
    s3_storage = S3Storage(config.aws, config.s3)
    source_cache = SourceCache(s3_storage, config.s3, config.cache)
    ffmpeg_processor = FfmpegProcessor(config.app, config.video)
    gateway_client = GatewayClient(config.gateway)
    snapshot_patcher = SnapshotPatcher(gateway_client, patch_path=config.gateway.snapshot_patch_path)
    handler = RequestHandler(
        source_cache=source_cache,
        ffmpeg_processor=ffmpeg_processor,
        s3_storage=s3_storage,
        dedup_store=dedup_store,
        s3_config=config.s3,
        work_dir=config.app.work_dir,
        dry_run=args.dry_run,
        snapshot_patcher=snapshot_patcher,
    )

    stop_event = threading.Event()
    exit_code = 0
    mqtt_client: MqttClient | None = None

    def on_request(raw_payload: bytes) -> None:
        nonlocal exit_code
        try:
            response = handler.handle_request(raw_payload)
            if response is not None and mqtt_client is not None:
                mqtt_client.publish(config.mqtt.topic_response, json.dumps(response))
        except Exception:
            logger.exception("Unexpected error while handling request")
            exit_code = 1
        finally:
            if args.one_shot:
                stop_event.set()

    mqtt_client = MqttClient(config.mqtt, on_request)

    def handle_signal(signum, frame) -> None:
        logger.info("Received shutdown signal", extra={"extra_fields": {"signum": signum}})
        stop_event.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        mqtt_client.connect()
    except MqttClientError as exc:
        logger.error("Failed to connect to MQTT broker: %s", exc)
        dedup_store.close()
        return 1

    mqtt_client.loop_start()
    try:
        if args.one_shot:
            received = stop_event.wait(timeout=args.one_shot_timeout)
            if not received:
                logger.error("Timed out waiting for a request in --one-shot mode")
                exit_code = 1
        else:
            stop_event.wait()
    finally:
        mqtt_client.disconnect()
        dedup_store.close()

    logger.info("Worker shut down cleanly")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
