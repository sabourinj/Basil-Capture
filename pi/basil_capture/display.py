"""Publishes scanner status to MQTT for the SenseCAP Indicator to display, and
listens for the two things the Indicator sends back.

The Indicator D1 is a standalone Wi-Fi device that SUBSCRIBES to the status
topic; it is not attached to the Pi. Status goes out on `status_topic`, and the
Indicator talks back over the same broker on two topics:

  undo_topic        plain-text transaction id of the scan to undo. Sent when
                    the Undo button is tapped.
  result_sec_topic  retained integer: how long a result screen stays up. Set by
                    the slider on the Indicator's Settings page. The Undo
                    button is offered for exactly this long, so one setting
                    covers both.

Both topics have defaults (see __init__), so config.yaml needs no new keys.

Messages are retained so the Indicator shows the current state immediately on
(re)connect, and a Last-Will (LWT) marks the Pi offline if the service dies.

The one exception is `ready_topic`: a NOT-retained "ready", sent once the
scanner is open (and again whenever the Indicator connects). A retained
"online" can be stale after a power cut, so the Indicator waits for this live
message before leaving its Starting Up screen.

Threading: paho runs its network loop on its own thread, and an undo request
needs a Grocy round-trip, so each one is handled on a short-lived worker
thread. Everything that decides "what is on screen right now" (the generation
counter, the undo offer, the post-undo hold) is guarded by one lock so a new
scan and an undo that finishes at the same moment can't publish out of order.
"""
import json
import threading
import time
from enum import Enum

import paho.mqtt.client as mqtt

# Bounds for the on-device slider. The Indicator enforces the same range; the
# Pi clamps again so a stray retained value can't set a zero or absurd timeout.
MIN_RESULT_SEC = 2   # matches the Indicator slider and the Basil app
MAX_RESULT_SEC = 10

# The Indicator hides the Undo button the instant its countdown ends, so a tap
# can reach the Pi a moment after the window closes. Honor taps for this long.
UNDO_GRACE_SEC = 1.0


class Status(Enum):
    IDLE = "idle"
    SCANNING = "scanning"
    SUCCESS = "success"
    ERROR = "error"


class Display:
    def __init__(self, cfg, result_display_sec=5.0):
        self.topic = cfg["status_topic"]
        self.avail = cfg["availability_topic"]
        self.undo_topic = cfg.get("undo_topic", "basil/capture/undo")
        self.setting_topic = cfg.get("result_sec_topic",
                                     "basil/capture/settings/result_sec")
        # Live "I'm ready" signal: NOT retained, so receiving it proves the Pi
        # is running right now with its scanner open. The retained
        # availability topic can't prove that - after a power cut it still
        # says "online" until the broker's keepalive check expires.
        self.ready_topic = cfg.get("ready_topic", "basil/capture/ready")
        # The Indicator's birth/LWT topic. When it (re)connects we answer with
        # a fresh ready so a freshly booted Indicator doesn't have to wait.
        self.indicator_avail = cfg.get("indicator_availability_topic",
                                       "basil/capture/indicator/availability")
        # Set by mark_ready() once the scanner is open. Until then the Pi does
        # not announce itself "online" at all.
        self._ready = False

        # How long a result is left on screen before idle. config.yaml's
        # result_display_sec is only the starting value: once the Indicator
        # publishes its slider setting, that wins (see _apply_setting). It is
        # also how long Undo stays available, and it is sent with every result
        # so the Indicator's countdown bar drains over exactly that span.
        self.result_sec = float(result_display_sec)

        self._lock = threading.Lock()
        # Bumped whenever a new scan takes over the screen. Anything scheduled
        # for an earlier screen (an undo that finishes late, the timer that
        # returns to idle after "Undone") checks it and stands down.
        self._gen = 0
        # (transaction_id, product, expires_monotonic) for the one scan that can
        # currently be undone, or None.
        self._undo = None
        # While a timed result that main.py doesn't know about ("Undone") is
        # up, show_idle() is a no-op until this passes; the timer below ends it.
        self._hold_until = 0.0
        self._undo_handler = None

        self.client = mqtt.Client(
            client_id=cfg.get("client_id", "basil-capture-pi"),
            protocol=mqtt.MQTTv311,
        )
        if cfg.get("username"):
            self.client.username_pw_set(cfg["username"], cfg.get("password", ""))

        # LWT: broker publishes "offline" if the Pi drops unexpectedly.
        self.client.will_set(self.avail, "offline", qos=1, retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        self.client.connect(cfg["host"], cfg.get("port", 1883), keepalive=30)
        self.client.loop_start()

    @property
    def result_ms(self):
        return int(self.result_sec * 1000)

    def set_undo_handler(self, handler):
        """handler(transaction_id) performs the Grocy undo.

        It should raise on failure; if the exception has a `user_message`
        (GrocyError does) that text is shown on the Indicator.
        """
        self._undo_handler = handler

    # ------------------------------------------------------------------ MQTT

    def _on_connect(self, client, userdata, flags, rc):
        """Announce "online" and (re)subscribe on EVERY connect, not just the first.

        After an ungraceful drop (wifi blip, broker restart, missed keepalive)
        the broker has already published our retained LWT "offline", and paho
        reconnects silently underneath us. Publishing the birth message only
        once at startup would leave that retained "offline" in place forever -
        status messages keep flowing but the Indicator stays stuck on its
        offline screen until the service is restarted.

        Subscriptions need the same treatment: with a clean session the broker
        forgets them on disconnect.

        "online" and ready are only sent once the scanner is open (see
        mark_ready). Connecting to the broker alone doesn't mean scans work.
        """
        if rc == 0:
            client.subscribe([(self.undo_topic, 1), (self.setting_topic, 1),
                              (self.indicator_avail, 1)])
            if self._ready:
                self._announce_ready()

    def mark_ready(self):
        """The scanner is open and listening: tell the Indicator.

        Call once at startup, after the scanner is initialized. Reconnects
        re-announce on their own (see _on_connect).
        """
        self._ready = True
        self._announce_ready()

    def _announce_ready(self):
        self.client.publish(self.avail, "online", qos=1, retain=True)
        self.client.publish(self.ready_topic, "ready", qos=1, retain=False)

    def _on_message(self, client, userdata, msg):
        payload = msg.payload.decode("utf-8", errors="replace").strip()
        if msg.topic == self.indicator_avail:
            # The Indicator just (re)booted or reconnected and is waiting on
            # its Starting Up screen. The retained copy of its last birth
            # message arrives here too; answering it is harmless.
            if payload == "online" and self._ready:
                self.client.publish(self.ready_topic, "ready", qos=1,
                                    retain=False)
        elif msg.topic == self.setting_topic:
            self._apply_setting(payload)
        elif msg.topic == self.undo_topic:
            # An undo is an action, never state. A retained one would replay on
            # every reconnect, so refuse it outright.
            if msg.retain:
                print("Ignoring retained undo message")
                return
            threading.Thread(target=self._handle_undo, args=(payload,),
                             daemon=True).start()

    def _apply_setting(self, payload):
        try:
            sec = float(payload)
        except ValueError:
            print(f"Ignoring bad result_sec setting: {payload!r}")
            return
        sec = max(MIN_RESULT_SEC, min(MAX_RESULT_SEC, sec))
        self.result_sec = sec
        print(f"Result screen time is now {sec:g}s (undo window matches)")

    def _publish(self, status, title, body, badge="", timeout_ms=0, undo_id=""):
        # "badge", "timeout_ms" and "undo_id" are always present, even when
        # empty/zero. The Indicator reads them with defaulting accessors anyway,
        # but emitting them unconditionally keeps retained messages from older
        # builds from being the only shape the firmware ever has to cope with.
        #
        # timeout_ms is 0 on screens that don't time out (idle, scanning), which
        # is what hides the countdown bar rather than a separate flag.
        #
        # undo_id is a STRING ("" when nothing can be undone): Grocy's
        # transaction ids are strings, and the firmware's `| ""` accessor only
        # returns string fields.
        payload = json.dumps({
            "status": status.value,
            "title": title,
            "body": body,
            "badge": badge,
            "timeout_ms": timeout_ms,
            "undo_id": undo_id,
            "ts": int(time.time()),
        })
        self.client.publish(self.topic, payload, qos=1, retain=True)

    # ------------------------------------------------------------ screens
    # Callers of _publish below hold self._lock, so the check-then-publish in
    # each is atomic with respect to the undo worker thread.

    def _supersede(self):
        """A new scan began: drop any undo offer and any pending "Undone" hold."""
        self._gen += 1
        self._undo = None
        self._hold_until = 0.0

    def show_idle(self):
        with self._lock:
            if time.monotonic() < self._hold_until:
                # An "Undone" (or undo error) result is still up. main.py's own
                # result timer ran out first; the hold timer ends it instead.
                return
            self._publish(Status.IDLE, "Ready", "Scan an item")

    def show_scanning(self, barcode):
        # The barcode is deliberately not shown - the screen just states what
        # is happening, matching the wording used elsewhere in the app. It is
        # still logged by main.py, which is where you'd want it for debugging.
        with self._lock:
            self._supersede()
            self._publish(Status.SCANNING, "Identifying product...", "")

    def show_success(self, product, amount, remaining=None, undo_id=None):
        amount = int(amount) if amount == int(amount) else amount
        body = product if amount == 1 else f"{product}\nx {amount}"
        with self._lock:
            self._undo = None
            if undo_id:
                expires = time.monotonic() + self.result_sec + UNDO_GRACE_SEC
                self._undo = (str(undo_id), product, expires)
            self._publish(Status.SUCCESS, "Consumed", body,
                          badge=self._stock_badge(remaining),
                          timeout_ms=self.result_ms,
                          undo_id=str(undo_id) if undo_id else "")

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
        with self._lock:
            self._supersede()
            self._publish(Status.ERROR, "Error", message,
                          timeout_ms=self.result_ms)

    # --------------------------------------------------------------- undo

    def _handle_undo(self, tid):
        with self._lock:
            cur = self._undo
            if cur is None or cur[0] != tid or time.monotonic() > cur[2]:
                # Stale tap: a newer scan replaced it, the window closed, or it
                # was already used. Never undo something the user can't see.
                print(f"Undo ignored (stale or expired): {tid!r}")
                return
            # Claim it before the slow Grocy call so a second tap can't undo
            # the same transaction twice.
            self._undo = None
            product = cur[1]
            gen = self._gen

        error = None
        handler = self._undo_handler
        if handler is None:
            error = "Undo unavailable"
        else:
            try:
                handler(tid)
                print(f"Undid transaction {tid} ({product})")
            except Exception as e:  # GrocyError carries a display-safe message
                error = getattr(e, "user_message", None) or "Undo failed"
                print(f"Undo failed for {tid}: {e}")

        with self._lock:
            if gen != self._gen:
                # A new scan took over the screen while Grocy was working. The
                # undo itself went through (or failed) and is logged above, but
                # showing it now would clobber the scan in progress.
                return
            if error:
                self._show_timed_locked(Status.ERROR, "Undo failed", error, gen)
            else:
                self._show_timed_locked(Status.SUCCESS, "Undone", product, gen)

    def _show_timed_locked(self, status, title, body, gen):
        """Show a result main.py doesn't know about, then return to idle.

        main.py is usually parked in read_scan() by the time an undo finishes,
        so it can't be the one to publish idle afterwards. This screen owns its
        own timer; show_idle() stays quiet until the hold ends.
        """
        self._hold_until = time.monotonic() + self.result_sec
        self._publish(status, title, body, timeout_ms=self.result_ms)
        timer = threading.Timer(self.result_sec, self._finish_hold, args=(gen,))
        timer.daemon = True
        timer.start()

    def _finish_hold(self, gen):
        with self._lock:
            if gen != self._gen:
                return  # a newer scan owns the screen now
            self._hold_until = 0.0
            self._publish(Status.IDLE, "Ready", "Scan an item")

    def close(self):
        try:
            self.client.publish(self.avail, "offline", qos=1, retain=True)
            self.client.loop_stop()
            self.client.disconnect()
        except Exception:
            pass
