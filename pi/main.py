#!/usr/bin/env python3
"""Basil Capture - barcode -> Grocy consume -> MQTT status.

Reads scans from a USB HID barcode scanner, consumes the matching product in
Grocy, and publishes a status message over MQTT that the SenseCAP Indicator D1
subscribes to and displays. The Indicator can send two things back: a tap on its
Undo button (reverts the last scan in Grocy) and a new result-screen time from
its Settings slider. Both are handled inside Display; see display.py.
"""
import argparse
import sys

import yaml

from basil_capture.barcode_reader import BarcodeReader
from basil_capture.grocy_client import GrocyClient, GrocyError
from basil_capture.display import Display


def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def open_scanner(s, b):
    """Build the reader for scanner.mode: 'hid' (default) or 'ssi'.

    hid  USB HID Keyboard mode, read via evdev. One-way: no host beeps.
    ssi  Zebra/Symbol "SSI over USB CDC" mode, read as a serial port. Two-way:
         the Pi sounds distinct beeps for each outcome (see ssi_scanner.py).
    """
    mode = str(s.get("mode", "hid")).lower()
    common = dict(min_length=b["scan_min_length"],
                  reject_patterns=b.get("reject_patterns"))
    if mode == "ssi":
        # Imported here so HID-only installs don't need pyserial.
        from basil_capture.ssi_scanner import SsiScanner
        return SsiScanner(s["device_path"], beeps=s.get("beeps"), **common)
    if mode == "hid":
        return BarcodeReader(s["device_path"], **common)
    raise ValueError(f"scanner.mode must be 'hid' or 'ssi', not {mode!r}")


def main():
    parser = argparse.ArgumentParser(description="Basil Capture barcode consume service")
    parser.add_argument("-c", "--config", default="config.yaml",
                        help="path to config.yaml (default: ./config.yaml)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    g, s, b = cfg["grocy"], cfg["scanner"], cfg["behavior"]

    display = Display(cfg["mqtt"], b.get("result_display_sec", 5.0))
    grocy = GrocyClient(g["base_url"], g["api_key"], timeout=b["consume_timeout_sec"])
    try:
        reader = open_scanner(s, b)
    except OSError as e:  # FileNotFoundError, PermissionError, SerialException
        display.show_error("Scanner not found")
        print(f"ERROR: could not open scanner at {s['device_path']}: {e}",
              file=sys.stderr)
        display.close()
        sys.exit(1)

    def undo(transaction_id):
        # Undo requests arrive on an MQTT worker thread; Display validates them
        # and calls this to do the actual Grocy undo. reader.beep() is
        # thread-safe and a no-op in HID mode.
        try:
            grocy.undo_transaction(transaction_id)
        except Exception:
            reader.beep("undo_error")
            raise
        reader.beep("undo")

    display.set_undo_handler(undo)

    reader.grab()
    reader.beep("ready")
    display.show_idle()
    print("Scanner service started. Waiting for scans...")

    try:
        # The result screen is a WAIT, not a sleep. Scanning during it cuts the
        # screen short and processes the new item immediately.
        #
        # This is not just a nicety. A blocking sleep leaves the scanner's fd
        # unread, and the kernel keeps buffering keystrokes - so scans made
        # during the window were never actually ignored, they replayed
        # afterwards as a backlog, each with its own full-length result screen.
        # Longer result_display_sec made that worse, and an overflowing buffer
        # could drop an Enter and splice two barcodes into one.
        pending = reader.read_scan()
        while pending is not None:
            barcode, pending = pending, None
            print(f"Scanned: {barcode}")
            if reader.is_rejected(barcode):
                # e.g. a URL QR code. Never sent to Grocy, but still shown as an
                # error so the scan doesn't look like it went unnoticed.
                print("  rejected: matches reject_patterns", file=sys.stderr)
                display.show_error("Not a product code")
                reader.beep("rejected")
            else:
                # Show "Identifying product..." BEFORE the Grocy round-trip, not
                # after. The consume POST plus the details GET can take a second
                # or more, and without this the screen sits on "Ready to Scan"
                # the whole time, so a scan looks like it did nothing.
                display.show_scanning(barcode)
                try:
                    product, consumed, remaining, undo_id = grocy.consume_by_barcode(
                        barcode,
                        amount=g["consume_amount"],
                        spoiled=g.get("spoil_on_consume", False),
                    )
                    print(f"  consumed {consumed} x {product} ({remaining} remaining)"
                          f" [transaction {undo_id}]")
                    display.show_success(product, consumed, remaining,
                                         undo_id=undo_id)
                    reader.beep("success")
                except GrocyError as e:
                    print(f"  error: {e}", file=sys.stderr)
                    display.show_error(e.user_message)
                    # Distinct tones: "Grocy said no to this item" vs "Grocy
                    # is unreachable, it's not the item's fault".
                    reader.beep("server_error" if e.server_side else "error")

            # display.result_sec follows the Indicator's Settings slider, so it
            # is read fresh each time rather than once at startup. The Undo
            # button is offered for this same span.
            pending = reader.read_scan(timeout=display.result_sec)
            if pending is None:
                # No-op while an "Undone" result is still up: Display ends that
                # one itself, because we may be parked in the read below by the
                # time an undo finishes.
                display.show_idle()
                pending = reader.read_scan()
    except KeyboardInterrupt:
        pass
    finally:
        reader.ungrab()
        display.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
