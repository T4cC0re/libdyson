"""Dyson device."""
from abc import abstractmethod
from enum import Enum
import json
import logging
import threading
from typing import Any, List, Optional

import paho.mqtt.client as mqtt

from libdyson.const import MessageType

from .exceptions import (
    DysonConnectionRefused,
    DysonConnectTimeout,
    DysonInvalidCredential,
    DysonNotConnected,
)
from .utils import mqtt_time

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 10


class DysonDevice:
    """Base class for dyson devices."""

    def __init__(self, serial: str, credential: str):
        """Initialize the device."""
        self._serial = serial
        self._credential = credential
        self._mqtt_client = None
        self._connected = threading.Event()
        self._disconnected = threading.Event()
        self._state_data_available = threading.Event()
        self._callbacks = []

    @property
    def serial(self) -> str:
        """Return the serial number of the device."""
        return self._serial

    @property
    def is_connected(self) -> bool:
        """Whether MQTT connection is active."""
        return self._connected.is_set()

    @property
    @abstractmethod
    def device_type(self) -> str:
        """Device type."""

    @property
    @abstractmethod
    def _status_topic(self) -> str:
        """MQTT status topic."""

    @property
    def _command_topic(self) -> str:
        """MQTT command topic."""
        return f"{self.device_type}/{self._serial}/command"

    @property
    @abstractmethod
    def _state_types(self) -> List[str]:
        """MQTT message types that represents a state message."""

    def connect(self, host: str) -> None:
        """Connect to the device MQTT broker."""
        self._disconnected.clear()
        self._mqtt_client = mqtt.Client(protocol=mqtt.MQTTv31)
        self._mqtt_client.username_pw_set(self._serial, self._credential)
        error = None

        def _on_connect(client: mqtt.Client, userdata: Any, flags, rc):
            _LOGGER.debug("Connected with result code %d", rc)
            nonlocal error
            if rc == 4:
                error = DysonInvalidCredential
            elif rc != 0:
                error = DysonConnectionRefused
            else:
                client.subscribe(self._status_topic)
            self._connected.set()

        self._mqtt_client.on_connect = _on_connect
        self._mqtt_client.on_disconnect = self._on_disconnect
        self._mqtt_client.on_message = self._on_message
        self._mqtt_client.connect_async(host)
        self._mqtt_client.loop_start()
        if self._connected.wait(timeout=CONNECT_TIMEOUT):
            if error is not None:
                self._mqtt_client.loop_stop()
                self._connected.clear()
                raise error

            _LOGGER.info("Connected to device %s", self._serial)
            self.request_current_state()

            # Wait for first data
            if self._state_data_available.wait(timeout=CONNECT_TIMEOUT):
                return
            else:
                self.disconnect()

        self._mqtt_client.loop_stop()
        raise DysonConnectTimeout

    def disconnect(self) -> None:
        """Disconnect from the device."""
        self._connected.clear()
        self._mqtt_client.disconnect()
        if not self._disconnected.wait(timeout=10):
            _LOGGER.warning("Disconnect timed out")
        self._mqtt_client.loop_stop()

    def add_message_listener(self, callback) -> None:
        """Add a callback to receive update notification."""
        self._callbacks.append(callback)

    def remove_message_listener(self, callback) -> None:
        """Remove an existed callback."""
        if callback in self._callbacks:
            self._callbacks.remove(callback)

    def _on_disconnect(self, client, userdata, rc):
        _LOGGER.debug(f"Disconnected with result code {str(rc)}")
        self._disconnected.set()

    def _on_message(self, client, userdata: Any, msg: mqtt.MQTTMessage):
        payload = json.loads(msg.payload.decode("utf-8"))
        self._handle_message(payload)

    def _handle_message(self, payload: dict) -> None:
        if payload["msg"] in ["CURRENT-STATE", "STATE-CHANGE"]:
            _LOGGER.debug("New state: %s", payload)
            self._update_state(payload)
            if not self._state_data_available.is_set():
                self._state_data_available.set()
            for callback in self._callbacks:
                callback(MessageType.STATE)

    @abstractmethod
    def _update_state(self, payload: dict) -> None:
        """Update the device state."""

    def _set_enum_attr(self, value: str, attr: str, enum: Enum) -> None:
        """Update state based on enum."""
        try:
            setattr(self, f"_{attr}", enum(value))
        except ValueError:
            _LOGGER.error("Unknown %s value %s", attr, value)

    def _send_command(self, command: str, data: Optional[dict] = None):
        if not self.is_connected:
            raise DysonNotConnected
        if data is None:
            data = {}
        payload = {
            "msg": command,
            "time": mqtt_time(),
        }
        payload.update(data)
        self._mqtt_client.publish(self._command_topic, json.dumps(payload))

    def request_current_state(self):
        """Request new state message."""
        if not self.is_connected:
            raise DysonNotConnected
        payload = {
            "msg": "REQUEST-CURRENT-STATE",
            "time": mqtt_time(),
        }
        self._mqtt_client.publish(self._command_topic, json.dumps(payload))
