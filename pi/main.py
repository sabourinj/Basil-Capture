#!/usr/bin/env python3
"""Basil Capture - barcode -> Grocy consume -> MQTT status.

Reads scans from a USB HID barcode scanner, consumes the matching product in
Grocy, and publishes a status message over MQTT that the SenseCAP Indicator D1
subscribes to and displays.
"""
import argparse
import sys
import time

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

    display = Display(cfg["mqtt"])
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

    try:
        for barcode in reader.scans():
            print(f"Scanned: {barcode}")
            display.show_scanning(barcode)
            try:
                product, consumed = grocy.consume_by_barcode(
                    barcode,
                    amount=g["consume_amount"],
                    spoiled=g.get("spoil_on_consume", False),
                )
                print(f"  consumed {consumed} x {product}")
                display.show_success(product, consumed)
            except GrocyError as e:
                print(f"  error: {e}", file=sys.stderr)
                display.show_error(e.user_message)
            time.sleep(b.get("result_display_sec", 1.5))
            display.show_idle()
    except KeyboardInterrupt:
        pass
    finally:
        reader.ungrab()
        display.close()
        print("\nStopped.")


if __name__ == "__main__":
    main()
