"""paho-mqtt wrapper: connects, resubscribes on reconnect, and publishes results."""

from __future__ import annotations

import logging
import ssl
from typing import Callable

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
        logger.info(
            "Received message",
            extra={"extra_fields": {"topic": message.topic, "qos": message.qos, "mid": message.mid}},
        )
        try:
            self._on_request(message.payload)
        except Exception:
            logger.exception("Unhandled error while processing message")

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
        self._client.loop_forever(retry_first_connection=True)

    def loop_start(self) -> None:
        self._client.loop_start()

    def loop_once(self, timeout: float = 1.0) -> None:
        self._client.loop(timeout=timeout)

    def disconnect(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
