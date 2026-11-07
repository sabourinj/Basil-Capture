"""Zebra/Symbol scanner over SSI (Simple Serial Interface) on USB CDC.

In the default USB HID Keyboard mode the scanner can only talk TO the Pi; the
host has no way to make it beep. Switched to "SSI over USB CDC" (one setup
barcode in the scanner's Product Reference Guide) it shows up as a serial port
(/dev/ttyACM*) and speaks a two-way packet protocol: scans arrive as
DECODE_DATA packets, and the Pi can send BEEP commands back. That is what lets
an error sound different from a success, at the scanner, where you're looking.

SSI packet layout (both directions):

    [len] [opcode] [source] [status] [data ...] [cksum_hi] [cksum_lo]

  len      byte count of everything before the checksum (so len >= 4)
  source   0x00 from the scanner, 0x04 from the host
  status   bit0 retransmit, bit1 continuation (more packets follow),
           bit3 permanent (for PARAM_SEND)
  cksum    16-bit two's complement of the sum of all preceding bytes, high
           byte first

This class presents the same interface as BarcodeReader (grab/ungrab,
read_scan(timeout), is_rejected) plus beep(event), so main.py doesn't care
which one it has.
"""
import re
import sys
import threading
import time

import serial

from .barcode_reader import DEFAULT_REJECT_PATTERNS

# Opcodes (SSI Programmer's Guide).
CMD_ACK = 0xD0
CMD_NAK = 0xD1
PARAM_SEND = 0xC6
BEEP = 0xE6
DECODE_DATA = 0xF3
EVENT = 0xF6

SRC_HOST = 0x04
STAT_RETRANSMIT = 0x01
STAT_CONTINUATION = 0x02
STAT_PERMANENT = 0x08

NAK_RESEND = 0x01
BEEP_NONE = 0xFF  # "no beep" for PARAM_SEND's beep field

# Parameter 0xEE "Decode Data Packet Format": 1 = packeted DECODE_DATA. The
# alternative, raw decode data, arrives with no framing or terminator at all,
# so there'd be no telling where one barcode ends and the next begins.
PARAM_DECODE_PACKET_FORMAT = 0xEE

# SSI BEEP codes, by name. Config may use either a name or a raw integer.
BEEP_CODES = {
    "1_short_high": 0x00, "2_short_high": 0x01, "3_short_high": 0x02,
    "4_short_high": 0x03, "5_short_high": 0x04,
    "1_short_low": 0x05, "2_short_low": 0x06, "3_short_low": 0x07,
    "4_short_low": 0x08, "5_short_low": 0x09,
    "1_long_high": 0x0A, "2_long_high": 0x0B, "3_long_high": 0x0C,
    "4_long_high": 0x0D, "5_long_high": 0x0E,
    "1_long_low": 0x0F, "2_long_low": 0x10, "3_long_low": 0x11,
    "4_long_low": 0x12, "5_long_low": 0x13,
    "fast_warble": 0x14, "slow_warble": 0x15,
    "high_low": 0x16, "low_high": 0x17,
    "high_low_high": 0x18, "low_high_low": 0x19,
    "high_high_low_low": 0x1A,
}

# What plays for each outcome unless config.yaml's scanner.beeps overrides it.
# The scanner already gives its own beep the moment it decodes, so "success"
# defaults to silence - the absence of an error tone IS the success signal, and
# an extra chirp on every good scan just trains you to tune beeps out.
DEFAULT_BEEPS = {
    "ready": "low_high",            # service started, scanner connected
    "success": None,                # consumed
    "error": "3_long_low",          # Grocy refused: unknown barcode, out of stock
    "server_error": "slow_warble",  # Grocy unreachable / timed out / 5xx
    "rejected": "3_long_low",       # not a product code (e.g. a URL QR)
    "undo": "high_low",             # undo succeeded
    "undo_error": "3_long_low",     # undo failed
}


def checksum(data):
    """16-bit two's complement of the byte sum, as (hi, lo)."""
    c = (-sum(data)) & 0xFFFF
    return c >> 8, c & 0xFF


def build_packet(opcode, payload=b"", status=0x00):
    body = bytes([4 + len(payload), opcode, SRC_HOST, status]) + bytes(payload)
    return body + bytes(checksum(body))


def resolve_beep(value):
    """Config value -> SSI beep code, or None for silence. Raises on junk."""
    if value is None or value is False or value == "none":
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        if 0 <= value <= 0x1A:
            return value
        raise ValueError(f"beep code out of range 0-26: {value}")
    key = str(value).strip().lower()
    if key in BEEP_CODES:
        return BEEP_CODES[key]
    raise ValueError(f"unknown beep {value!r}; use one of: "
                     + ", ".join(BEEP_CODES))


class SsiScanner:
    def __init__(self, device_path, min_length=4, reject_patterns=None,
                 beeps=None):
        self.min_length = min_length
        if reject_patterns is None:
            reject_patterns = DEFAULT_REJECT_PATTERNS
        self.reject = [re.compile(p, re.IGNORECASE) for p in reject_patterns]

        merged = dict(DEFAULT_BEEPS)
        merged.update(beeps or {})
        unknown = set(merged) - set(DEFAULT_BEEPS)
        if unknown:
            raise ValueError(f"unknown beep event(s) in config: "
                             f"{', '.join(sorted(unknown))}")
        # Resolved up front so a typo in config.yaml fails at startup, not on
        # the first error scan.
        self.beeps = {event: resolve_beep(v) for event, v in merged.items()}

        # Baud rate is meaningless on USB CDC but pyserial wants one.
        # exclusive=True is the serial-port equivalent of the HID reader's
        # grab(): nothing else (a getty, ModemManager) can open it under us.
        self.port = serial.Serial(device_path, 9600, timeout=None,
                                  exclusive=True)
        self._write_lock = threading.Lock()
        self._rx = bytearray()
        self._parts = []        # data from a multipacket DECODE_DATA so far
        self._last_packet = None

        self.port.reset_input_buffer()
        # Packeted decode data is required to delimit scans. Sent as a
        # temporary change on every start so the scanner's stored settings
        # aren't rewritten each boot; it still sticks until power-cycle, and a
        # power-cycle means a new /dev/ttyACM and a service restart anyway.
        self._send(build_packet(
            PARAM_SEND, bytes([BEEP_NONE, PARAM_DECODE_PACKET_FORMAT, 0x01])))

    # -------------------------------------------------- BarcodeReader API

    def grab(self):
        """No-op: the port is already opened exclusively."""

    def ungrab(self):
        try:
            self.port.close()
        except Exception:
            pass

    def is_rejected(self, code_str):
        return any(p.search(code_str) for p in self.reject)

    def read_scan(self, timeout=None):
        """Return the next decoded barcode, or None if `timeout` seconds pass.

        Like BarcodeReader, a timeout never discards a partial packet: the
        bytes stay in self._rx and the packet completes on a later call.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            # Drain anything already buffered before waiting for more.
            code = self._next_scan_from_buffer()
            if code is not None:
                return code

            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
            self.port.timeout = remaining
            chunk = self.port.read(1)
            if not chunk:
                return None
            chunk += self.port.read(self.port.in_waiting)
            self._rx.extend(chunk)

    # ------------------------------------------------------------ beeps

    def beep(self, event):
        """Play the configured tone for `event` (see DEFAULT_BEEPS).

        Never raises: feedback is a nicety, and a hiccup sending it must not
        take down the scan loop. Safe to call from any thread.
        """
        code = self.beeps.get(event)
        if code is None:
            return
        try:
            self._send(build_packet(BEEP, bytes([code])))
        except Exception as e:
            print(f"  warning: could not beep scanner ({event}): {e}",
                  file=sys.stderr)

    # --------------------------------------------------------- internals

    def _send(self, packet):
        with self._write_lock:
            self.port.write(packet)
            self.port.flush()

    def _next_scan_from_buffer(self):
        """Consume whole packets from self._rx; return a barcode if one completes."""
        while self._rx:
            length = self._rx[0]
            if length < 4:
                # Not a valid length byte - we're out of sync (or this is the
                # stray ENQ some scanners emit). Drop a byte and look again.
                del self._rx[0]
                continue
            total = length + 2
            if len(self._rx) < total:
                return None  # wait for the rest of the packet
            packet = bytes(self._rx[:total])
            del self._rx[:total]

            if bytes(checksum(packet[:length])) != packet[length:]:
                print("  warning: bad SSI checksum, asking scanner to resend",
                      file=sys.stderr)
                self._send(build_packet(CMD_NAK, bytes([NAK_RESEND])))
                continue

            code = self._handle_packet(packet[:length])
            if code is not None:
                return code
        return None

    def _handle_packet(self, body):
        opcode, status, data = body[1], body[3], body[4:]

        if opcode == CMD_ACK:
            return None  # scanner confirming one of our commands
        if opcode == CMD_NAK:
            cause = data[0] if data else None
            print(f"  warning: scanner NAK'd a command (cause {cause})",
                  file=sys.stderr)
            return None

        # Everything else the scanner sends expects an ACK; without one it
        # retries and eventually sounds its own transmit-error beep.
        self._send(build_packet(CMD_ACK))

        if status & STAT_RETRANSMIT and body == self._last_packet:
            return None  # our earlier ACK was late; already handled this one
        self._last_packet = body

        if opcode != DECODE_DATA:
            return None  # EVENT etc. - acknowledged, nothing to do

        # data[0] is the barcode symbology type; the rest is the barcode.
        self._parts.append(data[1:])
        if status & STAT_CONTINUATION:
            return None
        raw = b"".join(self._parts)
        self._parts = []
        code_str = raw.decode("utf-8", errors="replace").strip()
        return code_str if len(code_str) >= self.min_length else None
