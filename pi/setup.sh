#!/usr/bin/env bash
# One-shot setup for the Pi side. Run from the pi/ directory.
set -euo pipefail

echo ">> Installing system deps..."
sudo apt update
sudo apt install -y python3-venv python3-pip

echo ">> Adding $USER to the 'input' group (needed to read the scanner)..."
sudo usermod -aG input "$USER"

echo ">> Creating virtualenv + installing Python deps..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

if [ ! -f config.yaml ]; then
  cp config.example.yaml config.yaml
  echo ">> Created config.yaml from example - EDIT IT before running."
fi

echo
echo "Done. Next steps:"
echo "  1. Edit config.yaml (Grocy key, MQTT host/creds, scanner device_path)."
echo "  2. Find your scanner path:  ls -l /dev/input/by-id/"
echo "  3. Log out/in (or reboot) so the 'input' group membership applies."
echo "  4. Test:  ./venv/bin/python main.py"
echo "  5. Install service:  see README (systemd section)."
