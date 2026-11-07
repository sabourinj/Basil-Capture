#!/usr/bin/env python3
"""Play SSI beep tones on a Zebra/Symbol scanner in "SSI over USB CDC" mode.

Use it to check the scanner is in SSI mode and answering, and to pick the tones
for scanner.beeps in config.yaml. Stop basil-capture first - the service holds
the port exclusively.

Usage (from the pi/ directory):
    sudo systemctl stop basil-capture
    ./venv/bin/python ../tools/test_beep.py /dev/serial/by-id/usb-Zebra...
        # plays every tone, one at a time, printing its name
    ./venv/bin/python ../tools/test_beep.py /dev/serial/by-id/... slow_warble 3_long_low
        # plays just those
"""
import argparse
import os
import sys
import time

# Reuse the service's packet code rather than duplicating it.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "pi"))
import serial  # noqa: E402

from basil_capture.ssi_scanner import (  # noqa: E402
    BEEP, BEEP_CODES, CMD_ACK, build_packet, resolve_beep,
)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device", help="scanner serial port, e.g. /dev/ttyACM0")
    ap.add_argument("tones", nargs="*",
                    help="tone names or codes 0-26 (default: all of them)")
    ap.add_argument("--gap", type=float, default=1.5,
                    help="seconds between tones (default 1.5)")
    args = ap.parse_args()

    tones = args.tones or list(BEEP_CODES)
    with serial.Serial(args.device, 9600, timeout=0.5) as port:
        port.reset_input_buffer()
        for tone in tones:
            code = resolve_beep(int(tone) if tone.isdigit() else tone)
            port.write(build_packet(BEEP, bytes([code])))
            reply = port.read(6)
            acked = len(reply) >= 2 and reply[1] == CMD_ACK
            print(f"0x{code:02X}  {tone:<20} {'ACK' if acked else 'no ACK'}")
            time.sleep(args.gap)


if __name__ == "__main__":
    main()
