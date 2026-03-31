"""
MQTT pub/sub bus for The Dude fleet.

Connects to mosquitto broker on rat (Tailscale).
Subscribes to dude/message for fleet-wide text messages.
Any machine on the tailnet can publish to dude/message to
broadcast a message to all Dude instances.
"""

import json
import logging
import time
from typing import Callable, Optional

import paho.mqtt.client as mqtt

log = logging.getLogger("dude-mqtt")

# Tailscale IP of rat (broker host)
DEFAULT_BROKER = "100.77.205.27"
DEFAULT_PORT = 1883

# Topics
TOPIC_MESSAGE = "dude/message"       # fleet-wide text messages
TOPIC_PRESENCE = "dude/presence"     # online/offline (retained)
TOPIC_SPOTIFY = "dude/spotify"       # now-playing updates


class DudeMQTT:
    """MQTT client for The Dude fleet."""

    def __init__(
        self,
        name: str = "rat",
        broker: str = DEFAULT_BROKER,
        port: int = DEFAULT_PORT,
        on_message_received: Optional[Callable] = None,
    ):
        self.name = name
        self.broker = broker
        self.port = port
        self.on_message_received = on_message_received  # callback(dict) when message received
        self._client: Optional[mqtt.Client] = None
        self._connected = False

    def start(self):
        """Connect to broker and start listening in background thread."""
        try:
            self._client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=f"dude-{self.name}-{int(time.time())}",
                clean_session=True,
            )
            self._client.on_connect = self._on_connect
            self._client.on_disconnect = self._on_disconnect
            self._client.on_message = self._on_message

            # Last will: announce offline when we disconnect
            self._client.will_set(
                TOPIC_PRESENCE,
                json.dumps({"name": self.name, "status": "offline"}),
                qos=1,
                retain=True,
            )

            self._client.connect_async(self.broker, self.port, keepalive=60)
            self._client.loop_start()
            log.info("MQTT connecting to %s:%d as '%s'", self.broker, self.port, self.name)
        except Exception as e:
            log.warning("MQTT start failed: %s", e)

    def stop(self):
        """Disconnect and stop background thread."""
        if self._client:
            self._client.publish(
                TOPIC_PRESENCE,
                json.dumps({"name": self.name, "status": "offline"}),
                qos=1,
                retain=True,
            )
            self._client.loop_stop()
            self._client.disconnect()
            self._connected = False
            log.info("MQTT disconnected")

    def publish_message(self, text: str, source: Optional[str] = None, duration: float = 1.0):
        """Broadcast a text message to all Dudes."""
        payload = {
            "text": text,
            "source": source or self.name,
            "duration": duration,
            "ts": int(time.time()),
        }
        self._publish(TOPIC_MESSAGE, payload, qos=1)

    def publish_spotify(self, track_info: dict):
        """Publish now-playing info for all Dudes."""
        self._publish(TOPIC_SPOTIFY, track_info, qos=0)

    @property
    def connected(self) -> bool:
        return self._connected

    # ── Internal ──

    def _publish(self, topic: str, payload: dict, qos: int = 0):
        if not self._client or not self._connected:
            log.warning("MQTT not connected, can't publish to %s", topic)
            return
        try:
            msg = json.dumps(payload)
            self._client.publish(topic, msg, qos=qos)
            log.debug("MQTT published to %s: %s", topic, msg[:80])
        except Exception as e:
            log.warning("MQTT publish error: %s", e)

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self._connected = True
            log.info("MQTT connected to broker")

            client.subscribe(TOPIC_MESSAGE, qos=1)
            client.subscribe(TOPIC_SPOTIFY, qos=0)
            log.info("MQTT subscribed to %s, %s", TOPIC_MESSAGE, TOPIC_SPOTIFY)

            # Announce online (retained)
            client.publish(
                TOPIC_PRESENCE,
                json.dumps({"name": self.name, "status": "online", "ts": int(time.time())}),
                qos=1,
                retain=True,
            )
        else:
            log.warning("MQTT connect failed with rc=%d", rc)

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0:
            log.warning("MQTT unexpected disconnect (rc=%d), will auto-reconnect", rc)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            log.warning("MQTT bad payload on %s", msg.topic)
            return

        topic = msg.topic
        log.info("MQTT [%s]: %s", topic, str(payload)[:80])

        if topic == TOPIC_MESSAGE and self.on_message_received:
            self.on_message_received(payload)


# ── Standalone test ──
if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1 and sys.argv[1] == "pub":
        text = " ".join(sys.argv[2:]) or "Test message from CLI"
        bus = DudeMQTT(name="cli")
        bus.start()
        time.sleep(1)
        bus.publish_message(text, source="cli")
        print(f"Published: {text}")
        time.sleep(0.5)
        bus.stop()
    else:
        def on_msg(payload):
            print(f"GOT: {payload}")

        bus = DudeMQTT(name="test", on_message_received=on_msg)
        bus.start()
        print("Listening... (Ctrl+C to stop)")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            bus.stop()
