"""Reads a USB HID barcode scanner via evdev and yields decoded strings.

Most USB barcode scanners present as HID keyboards: they "type" the barcode
digits and finish with Enter. We grab the device exclusively so those
keystrokes never leak into the Pi's console/TTY.

read_scan() takes an optional timeout so the caller never has to stop reading
the device. That matters more than it looks: while nothing reads the fd, the
kernel buffers the scanner's keystrokes and replays them later, so scans made
during a pause are not dropped - they pile up and fire afterwards. Worse, if
that buffer overflows mid-barcode the Enter can be lost, leaving partial digits
that the next scan appends to, producing a barcode nobody scanned.
"""
import select
import time

import evdev
from evdev import categorize, ecodes

# USB HID keyboard scancode -> character map (digits, letters, common symbols).
KEYMAP = {
    ecodes.KEY_1: "1", ecodes.KEY_2: "2", ecodes.KEY_3: "3", ecodes.KEY_4: "4",
    ecodes.KEY_5: "5", ecodes.KEY_6: "6", ecodes.KEY_7: "7", ecodes.KEY_8: "8",
    ecodes.KEY_9: "9", ecodes.KEY_0: "0",
    ecodes.KEY_A: "a", ecodes.KEY_B: "b", ecodes.KEY_C: "c", ecodes.KEY_D: "d",
    ecodes.KEY_E: "e", ecodes.KEY_F: "f", ecodes.KEY_G: "g", ecodes.KEY_H: "h",
    ecodes.KEY_I: "i", ecodes.KEY_J: "j", ecodes.KEY_K: "k", ecodes.KEY_L: "l",
    ecodes.KEY_M: "m", ecodes.KEY_N: "n", ecodes.KEY_O: "o", ecodes.KEY_P: "p",
    ecodes.KEY_Q: "q", ecodes.KEY_R: "r", ecodes.KEY_S: "s", ecodes.KEY_T: "t",
    ecodes.KEY_U: "u", ecodes.KEY_V: "v", ecodes.KEY_W: "w", ecodes.KEY_X: "x",
    ecodes.KEY_Y: "y", ecodes.KEY_Z: "z",
    ecodes.KEY_MINUS: "-", ecodes.KEY_DOT: ".", ecodes.KEY_SPACE: " ",
}
SHIFT_KEYS = {ecodes.KEY_LEFTSHIFT, ecodes.KEY_RIGHTSHIFT}


class BarcodeReader:
    def __init__(self, device_path, min_length=4):
        self.device = evdev.InputDevice(device_path)
        self.min_length = min_length
        # Decoder state lives on the instance, not in a local, so a scan that
        # straddles a read_scan() timeout still assembles correctly.
        self._buffer = []
        self._shift = False

    def grab(self):
        """Take exclusive control so scans don't reach the console."""
        self.device.grab()

    def ungrab(self):
        try:
            self.device.ungrab()
        except Exception:
            pass

    def read_scan(self, timeout=None):
        """Return the next decoded barcode, or None if `timeout` seconds pass.

        timeout=None blocks until a scan arrives. A timeout returning None does
        not discard a half-typed barcode - those digits stay buffered and the
        rest of the scan completes on a later call.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None

            readable, _, _ = select.select([self.device.fd], [], [], remaining)
            if not readable:
                return None

            for event in self.device.read():
                code_str = self._feed(event)
                if code_str is not None:
                    return code_str

    def scans(self):
        """Generator yielding one decoded barcode per Enter keypress."""
        while True:
            yield self.read_scan()

    def _feed(self, event):
        """Fold one event into the decoder. Returns a barcode when complete."""
        if event.type != ecodes.EV_KEY:
            return None
        key = categorize(event)
        code = key.scancode

        if code in SHIFT_KEYS:
            self._shift = key.keystate != key.key_up
            return None

        if key.keystate != key.key_down:
            return None

        if code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER):
            code_str = "".join(self._buffer)
            self._buffer = []
            return code_str if len(code_str) >= self.min_length else None

        if code in KEYMAP:
            ch = KEYMAP[code]
            self._buffer.append(ch.upper() if self._shift else ch)
        return None
