"""paho-mqtt wrapper: connects, resubscribes on reconnect, and publishes results."""

from __future__ import annotations

import logging
import queue
import ssl
import threading
from typing import Callable, Optional

import paho.mqtt.client as mqtt

from src.config import MqttConfig

logger = logging.getLogger(__name__)

OnRequestCallback = Callable[[bytes], None]


class MqttClientError(Exception):
    """Raised when the MQTT client cannot connect or publish."""


class MqttClient:
    def __init__(self, config: MqttConfig, on_request: OnRequestCallback):
        self._config = config
        self._on_request = on_request
        self._client = mqtt.Client(client_id=config.client_id, clean_session=False)

        # on_message must return quickly: it runs on paho's own network I/O
        # thread, and request handling (ffmpeg + S3 + gateway PATCH) can take
        # tens of seconds. Blocking that thread starves PINGREQ, which can
        # get the broker to drop the connection mid-request; combined with
        # clean_session=False, the dropped, not-yet-acked QoS-1 message then
        # gets redelivered on reconnect. Handing payloads off to a separate
        # worker thread keeps the network thread free to service keepalive.
        self._request_queue: "queue.Queue[bytes]" = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._stop_worker = threading.Event()

        if config.username:
            self._client.username_pw_set(config.username, config.password)
        if config.tls:
            self._client.tls_set(cert_reqs=ssl.CERT_REQUIRED)

        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message

    def _handle_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            logger.error("MQTT connect failed", extra={"extra_fields": {"rc": rc}})
            return
        logger.info("MQTT connected", extra={"extra_fields": {"host": self._config.host}})
        client.subscribe(self._config.topic_request, qos=self._config.qos)
        logger.info("Subscribed", extra={"extra_fields": {"topic": self._config.topic_request}})

    def _handle_disconnect(self, client, userdata, rc) -> None:
        level = logging.WARNING if rc != 0 else logging.INFO
        logger.log(level, "MQTT disconnected", extra={"extra_fields": {"rc": rc}})

    def _handle_message(self, client, userdata, message) -> None:
        if message.retain:
            logger.info(
                "Ignoring retained message",
                extra={"extra_fields": {"topic": message.topic, "mid": message.mid}},
            )
            return

        logger.info(
            "Received message",
            extra={"extra_fields": {"topic": message.topic, "qos": message.qos, "mid": message.mid}},
        )
        self._request_queue.put(message.payload)

    def _worker_loop(self) -> None:
        while not self._stop_worker.is_set():
            try:
                payload = self._request_queue.get(timeout=1.0)
            except queue.Empty:
                continue
            try:
                self._on_request(payload)
            except Exception:
                logger.exception("Unhandled error while processing message")
            finally:
                self._request_queue.task_done()

    def connect(self) -> None:
        try:
            self._client.connect(self._config.host, self._config.port, keepalive=self._config.keepalive)
        except OSError as exc:
            raise MqttClientError(f"Could not connect to MQTT broker {self._config.host}:{self._config.port}: {exc}") from exc

    def publish(self, topic: str, payload: str) -> None:
        info = self._client.publish(topic, payload, qos=self._config.qos)
        info.wait_for_publish(timeout=30)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            raise MqttClientError(f"Publish to {topic} failed with rc={info.rc}")
        logger.info("Published response", extra={"extra_fields": {"topic": topic}})

    def loop_forever(self) -> None:
        self._start_worker()
        self._client.loop_forever(retry_first_connection=True)

    def loop_start(self) -> None:
        self._start_worker()
        self._client.loop_start()

    def loop_once(self, timeout: float = 1.0) -> None:
        self._client.loop(timeout=timeout)

    def _start_worker(self) -> None:
        self._stop_worker.clear()
        self._worker_thread = threading.Thread(
            target=self._worker_loop, name="mqtt-request-worker", daemon=True
        )
        self._worker_thread.start()

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
        self._stop_worker.set()
        if self._worker_thread is not None:
            self._worker_thread.join(timeout=30)
