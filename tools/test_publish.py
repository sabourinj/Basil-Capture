#!/usr/bin/env python3
"""Publish fake status messages so you can verify the Indicator screen reacts
without needing the scanner or Grocy wired up yet.

Usage:
    pip install paho-mqtt
    python test_publish.py --host 192.168.1.x --user mqtt_scanner --password ...
    # cycles idle -> scanning -> success -> error, 2s apart, then loops.

    # or fire a single status:
    python test_publish.py --host ... --user ... --password ... --once success
"""
import argparse
import json
import time

import paho.mqtt.client as mqtt

STATUS_TOPIC = "basil/capture/status"
AVAIL_TOPIC = "basil/capture/availability"

FRAMES = {
    "idle":     ("Ready", "Scan an item"),
    "scanning": ("Looking up", "0123456789012"),
    "success":  ("Consumed", "Whole Milk\nx 2"),
    "error":    ("Error", "Out of stock"),
}


def publish(client, status):
    title, body = FRAMES[status]
    payload = json.dumps({
        "status": status, "title": title, "body": body, "ts": int(time.time()),
    })
    client.publish(STATUS_TOPIC, payload, qos=1, retain=True)
    print(f"-> {status}: {title} / {body}")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", required=True)
    p.add_argument("--port", type=int, default=1883)
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--once", choices=list(FRAMES), help="publish one status and exit")
    p.add_argument("--delay", type=float, default=2.0)
    args = p.parse_args()

    client = mqtt.Client(client_id="basil-capture-tester", protocol=mqtt.MQTTv311)
    if args.user:
        client.username_pw_set(args.user, args.password or "")
    client.connect(args.host, args.port, keepalive=30)
    client.loop_start()
    client.publish(AVAIL_TOPIC, "online", qos=1, retain=True)

    try:
        if args.once:
            publish(client, args.once)
            time.sleep(1)
        else:
            print("Cycling statuses. Ctrl-C to stop.")
            while True:
                for status in ("idle", "scanning", "success", "error"):
                    publish(client, status)
                    time.sleep(args.delay)
    except KeyboardInterrupt:
        pass
    finally:
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    main()
