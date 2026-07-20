"""Basil Capture - barcode consume service package.

Captures barcode scans on a Raspberry Pi, consumes the matching item from
Grocy, and publishes status over MQTT for the SenseCAP Indicator to display.
Works alongside Basil (the Android warehouse-PDA app) but runs independently.
"""
__version__ = "1.0.0"
