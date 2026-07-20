"""Publishes scanner status to MQTT for the SenseCAP Indicator to display.

The Indicator D1 is a standalone Wi-Fi device that SUBSCRIBES to the status
topic; it is not attached to the Pi. This class only publishes, so it never
needs to know the Indicator's address.

Messages are retained so the Indicator shows the current state immediately on
(re)connect, and a Last-Will (LWT) marks the Pi offline if the service dies.
"""
import json
import time
from enum import Enum

import paho.mqtt.client as mqtt


class Status(Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    SUCCESS = "success"
    ERROR = "error"


class Display:
    def __init__(self, cfg):
        self.topic = cfg["status_topic"]
        self.avail = cfg["availability_topic"]

        self.client = mqtt.Client(
            client_id=cfg.get("client_id", "basil-capture-pi"),
            protocol=mqtt.MQTTv311,
        )
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password", ""))

        # LWT: broker publishes "offline" if the Pi drops unexpectedly.
        self.client.will_set(self.avail, "offline", qos=1, retain=True)

        self.client.connect(cfg["host"], cfg.get("port", 1883), keepalive=30)
        self.client.loop_start()
        self.client.publish(self.avail, "online", qos=1, retain=True)

    def _publish(self, status, title, body):
        payload = json.dumps({
            "status": status.value,
            "title": title,
            "body": body,
            "ts": int(time.time()),
        })
        self.client.publish(self.topic, payload, qos=1, retain=True)

    def show_idle(self):
        self._publish(Status.IDLE, "Ready", "Scan an item")

    def show_scanning(self, barcode):
        self._publish(Status.SCANNING, "Looking up", barcode)

    def show_success(self, product, amount):
        self._publish(Status.SUCCESS, "Consumed", f"{product}  -{amount}")

    def show_error(self, message):
        self._publish(Status.ERROR, "Error", message)

    def close(self):
        try:
            self.client.publish(self.avail, "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
