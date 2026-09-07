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
    def __init__(self, cfg, result_display_sec=5.0):
        self.topic = cfg["status_topic"]
        self.avail = cfg["availability_topic"]
        # How long main.py will leave a result on screen before publishing idle.
        # Sent with every result so the Indicator's countdown bar drains over
        # exactly that span - the Pi's config stays the single source of truth
        # and changing it needs no reflash.
        self.result_ms = int(result_display_sec * 1000)

        self.client = mqtt.Client(
            client_id=cfg.get("client_id", "basil-capture-pi"),
            protocol=mqtt.MQTTv311,
        )
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password", ""))

        # LWT: broker publishes "offline" if the Pi drops unexpectedly.
        self.client.will_set(self.avail, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect

        self.client.connect(cfg["host"], cfg.get("port", 1883), keepalive=30)
        self.client.loop_start()

    def _on_connect(self, client, userdata, flags, rc):
        """Announce "online" on EVERY connect, not just the first.

        After an ungraceful drop (wifi blip, broker restart, missed keepalive)
        the broker has already published our retained LWT "offline", and paho
        reconnects silently underneath us. Publishing the birth message only
        once at startup would leave that retained "offline" in place forever -
        status messages keep flowing but the Indicator stays stuck on its
        offline screen until the service is restarted.
        """
        if rc == 0:
            client.publish(self.avail, "online", qos=1, retain=True)

    def _publish(self, status, title, body, badge="", timeout_ms=0):
        # "badge" and "timeout_ms" are always present, even when empty/zero. The
        # Indicator reads them with defaulting accessors anyway, but emitting
        # them unconditionally keeps retained messages from older builds from
        # being the only shape the firmware ever has to cope with.
        #
        # timeout_ms is 0 on screens that don't time out (idle, scanning), which
        # is what hides the countdown bar rather than a separate flag.
        payload = json.dumps({
            "status": status.value,
            "title": title,
            "body": body,
            "badge": badge,
            "timeout_ms": timeout_ms,
            "ts": int(time.time()),
        })
        self.client.publish(self.topic, payload, qos=1, retain=True)

    def show_idle(self):
        self._publish(Status.IDLE, "Ready", "Scan an item")

    def show_scanning(self, barcode):
        # The barcode is deliberately not shown - the screen just states what
        # is happening, matching the wording used elsewhere in the app. It is
        # still logged by main.py, which is where you'd want it for debugging.
        self._publish(Status.SCANNING, "Identifying product...", "")

    def show_success(self, product, amount, remaining=None):
        amount = int(amount) if amount == int(amount) else amount
        body = product if amount == 1 else f"{product}\nx {amount}"
        self._publish(Status.SUCCESS, "Consumed", body,
                      badge=self._stock_badge(remaining),
                      timeout_ms=self.result_ms)

    @staticmethod
    def _stock_badge(remaining):
        """Text for the stock pill: a count, or the emptied-shelf note.

        The pill sizes to its content, so both cases fit in the one widget and
        there is no separate note row to keep aligned.

        "" when the lookup failed, which hides the pill entirely - claiming a
        count we don't have is worse than showing nothing. That is why None and
        0 stay distinct all the way from grocy_client to here.
        """
        if remaining is None:
            return ""
        if remaining <= 0:
            return "Last available"
        remaining = int(remaining) if remaining == int(remaining) else remaining
        return f"{remaining} remaining"

    def show_error(self, message):
        self._publish(Status.ERROR, "Error", message, timeout_ms=self.result_ms)

    def close(self):
        try:
            self.client.publish(self.avail, "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
