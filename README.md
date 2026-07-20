# Basil Capture

Quick-capture barcode consumption for [Grocy](https://grocy.info/). Scan an item
on a Raspberry Pi with a USB barcode scanner, automatically **consume** it from
Grocy stock, and show the result on a
[SenseCAP Indicator D1](https://www.seeedstudio.com/SenseCAP-Indicator-D1-p-5643.html)
screen — over MQTT, with no physical connection between the Pi and the display.

Basil Capture is a standalone device/service for keeping inventory accurate as
things are consumed or discarded. It works alongside **Basil** (an Android
warehouse-PDA app) but runs entirely on its own — its only dependencies are
Grocy and an MQTT broker.

## How it works

```
[USB barcode scanner]
        | HID keyboard
        v
[Raspberry Pi Zero 2 W] --- reads scan (evdev)
        |                    consumes item (Grocy REST API)
        | publishes status
        v
[MQTT broker (e.g. HA Mosquitto)]
        | status topic (retained)
        v
[SenseCAP Indicator D1] --- subscribes, shows status on 4" LVGL screen
```

The Pi and the Indicator are fully decoupled: the Pi only *publishes* status,
the Indicator only *subscribes*. Neither needs the other's address. If the Pi
goes offline, an MQTT Last-Will flips the Indicator to an "offline" screen.

Status colors: **green** consumed · **red** error · **blue** looking up ·
**grey** idle.

## Repo layout

```
pi/                     Raspberry Pi capture service (Python)
  basil_capture/
    barcode_reader.py   reads the USB HID scanner via evdev
    grocy_client.py     Grocy REST client (verified vs Grocy 4.6.0)
    display.py          publishes status to MQTT
  main.py               entrypoint / main loop
  config.example.yaml   copy to config.yaml and fill in
  requirements.txt
  setup.sh              installs deps, venv, input-group membership
  basil-capture.service systemd unit
indicator/
  indicator-d1.yaml     ESPHome firmware for the SenseCAP Indicator D1
  secrets.example.yaml  wifi + mqtt secrets template
tools/
  test_publish.py       fake status publisher to test the screen alone
```

## MQTT topics

| Topic | Payload | Notes |
|-------|---------|-------|
| `basil/capture/status` | `{"status","title","body","ts"}` | retained |
| `basil/capture/availability` | `online` / `offline` | Pi LWT, retained |
| `basil/capture/indicator/availability` | `online` / `offline` | Indicator LWT |

`status` is one of `idle`, `scanning`, `success`, `error`.

## Prerequisites

- Raspberry Pi (Zero 2 W or better) with Raspberry Pi OS
- USB barcode scanner in HID-keyboard mode (the default for most)
- A running Grocy instance + an API key (Grocy -> Settings -> Manage API keys)
- An MQTT broker. These instructions assume the Home Assistant **Mosquitto
  broker** add-on, which authenticates against Home Assistant user accounts.
- A SenseCAP Indicator D1

## Setup

### 1. Broker / credentials (Home Assistant Mosquitto add-on)

The Mosquitto add-on delegates auth to Home Assistant users (its `logins:` list
is empty by default). Create a dedicated local HA user for this project:

- Settings -> People -> Users -> Add user
- Name e.g. `basil-capture`, enable **Local access only**, set a strong password.

Use those credentials in **both** the Pi and Indicator configs below. Use the
broker's **LAN IP** (e.g. `192.168.1.x`), not a public hostname — MQTT traffic
should stay on the local network, on port `1883`.

### 2. Raspberry Pi

```bash
git clone https://github.com/<you>/basil-capture.git
cd basil-capture/pi
./setup.sh                 # installs deps, venv, adds you to 'input' group
nano config.yaml           # fill in Grocy key, MQTT host/creds, scanner path
```

Find the scanner's stable device path:

```bash
ls -l /dev/input/by-id/
# use the entry ending in -event-kbd
```

Log out and back in (so `input` group membership applies), then test:

```bash
./venv/bin/python main.py
```

Scan something — you should see it consume in Grocy and update the Indicator.

Install as a service so it runs on boot:

```bash
sudo cp basil-capture.service /etc/systemd/system/
# edit the User= / paths in the unit if you're not using the 'pi' user
sudo systemctl daemon-reload
sudo systemctl enable --now basil-capture
journalctl -u basil-capture -f     # watch logs
```

### 3. SenseCAP Indicator D1 (ESPHome)

1. In the ESPHome dashboard (or CLI), add `indicator/indicator-d1.yaml`.
2. Merge `indicator/secrets.example.yaml` into your ESPHome `secrets.yaml`
   with real Wi-Fi + MQTT values (same broker/creds as the Pi).
3. **First flash over USB-C** (the port on the *bottom* of the unit). After
   that, updates go over the air.

## Testing the screen without a scanner

Verify the display reacts before wiring up the scanner or Grocy:

```bash
pip install paho-mqtt
python tools/test_publish.py --host 192.168.1.x --user basil-capture --password '...'
# cycles idle -> scanning -> success -> error every 2s

# or one shot:
python tools/test_publish.py --host ... --user ... --password ... --once success
```

## Troubleshooting

**Indicator screen is black / no backlight.** Almost always PSRAM config. The
`psram: octal` block and the four `sdkconfig_options` in the YAML are required.
If it's still dark, some board revisions gate the backlight through the PCA9554
I/O expander rather than GPIO45 directly — see the comment near `output:` in the
YAML.

**Scans do nothing / appear in the console.** The service must be able to
`grab()` the input device — confirm you're in the `input` group (`groups`) and
that `device_path` points at the `-event-kbd` entry.

**Grocy returns "Unknown barcode".** The barcode isn't linked to a product yet.
Add it in Grocy (product -> barcodes) first.

**Nothing on the Indicator but Pi logs look fine.** Check both are connected to
the same broker/creds, and that the broker is running. Use an MQTT client (e.g.
MQTT Explorer) to watch `basil/capture/status`.

## Notes

- Grocy REST calls are verified against the **Grocy 4.6.0** OpenAPI spec:
  `POST /stock/products/by-barcode/{barcode}/consume` with body
  `{"amount","transaction_type":"consume","spoiled"}`; a 200 returns a JSON
  array of booking objects.
- The `spoil_on_consume` config option maps to Grocy's `spoiled` flag.

## License

MIT — see [LICENSE](LICENSE).
