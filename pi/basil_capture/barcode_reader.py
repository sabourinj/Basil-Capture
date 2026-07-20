"""Reads a USB HID barcode scanner via evdev and yields decoded strings.

Most USB barcode scanners present as HID keyboards: they "type" the barcode
digits and finish with Enter. We grab the device exclusively so those
keystrokes never leak into the Pi's console/TTY.
"""
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

    def grab(self):
        """Take exclusive control so scans don't reach the console."""
        self.device.grab()

    def ungrab(self):
        try:
            self.device.ungrab()
        except Exception:
            pass

    def scans(self):
        """Generator yielding one decoded barcode per Enter keypress."""
        buffer = []
        shift = False
        for event in self.device.read_loop():
            if event.type != ecodes.EV_KEY:
                continue
            key = categorize(event)
            code = key.scancode

            if code in SHIFT_KEYS:
                shift = key.keystate != key.key_up
                continue

            if key.keystate != key.key_down:
                continue

            if code in (ecodes.KEY_ENTER, ecodes.KEY_KPENTER):
                code_str = "".join(buffer)
                buffer = []
                if len(code_str) >= self.min_length:
                    yield code_str
            elif code in KEYMAP:
                ch = KEYMAP[code]
                buffer.append(ch.upper() if shift else ch)
