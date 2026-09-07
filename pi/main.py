#!/usr/bin/env python3
"""Basil Capture - barcode -> Grocy consume -> MQTT status.

Reads scans from a USB HID barcode scanner, consumes the matching product in
Grocy, and publishes a status message over MQTT that the SenseCAP Indicator D1
subscribes to and displays.
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
        reader = BarcodeReader(s["device_path"], min_length=b["scan_min_length"])
    except (FileNotFoundError, PermissionError) as e:
        display.show_error("Scanner not found")
        print(f"ERROR: could not open scanner at {s['device_path']}: {e}",
              file=sys.stderr)
        display.close()
        sys.exit(1)

    reader.grab()
    display.show_idle()
    print("Scanner service started. Waiting for scans...")

    result_sec = b.get("result_display_sec", 5.0)

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
            # Show "Identifying product..." BEFORE the Grocy round-trip, not
            # after. The consume POST plus the details GET can take a second or
            # more, and without this the screen sits on "Ready to Scan" the
            # whole time, so a scan looks like it did nothing.
            display.show_scanning(barcode)
            try:
                product, consumed, remaining = grocy.consume_by_barcode(
                    barcode,
                    amount=g["consume_amount"],
                    spoiled=g.get("spoil_on_consume", False),
                )
                print(f"  consumed {consumed} x {product} ({remaining} remaining)")
                display.show_success(product, consumed, remaining)
            except GrocyError as e:
                print(f"  error: {e}", file=sys.stderr)
                display.show_error(e.user_message)

            pending = reader.read_scan(timeout=result_sec)
            if pending is None:
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
