#!/usr/bin/env bash
set -euo pipefail

if [ "$(id -u)" -eq 0 ]; then
    echo "Run this as your normal Pi user: bash ~/dyno/motor_lab/install.sh"
    exit 1
fi

lab_user="$(id -un)"
lab_group="$(id -gn)"
lab_source="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
lab_target="$HOME/dyno/motor_lab"

echo "Motor and relay supplies must be OFF. Stop old relay/monitor programs first."
echo "Installing Motor Lab for $lab_user at $lab_target"
command -v systemctl >/dev/null || { echo "This installer requires Raspberry Pi OS with systemd."; exit 1; }

if ! python3 -c 'import gpiozero, lgpio, smbus' >/dev/null 2>&1; then
    sudo apt-get update
    sudo apt-get install -y python3-gpiozero python3-lgpio python3-smbus
fi

mkdir -p "$lab_target" "$HOME/dyno/auto_runs"
sudo systemctl stop motor-lab.service 2>/dev/null || true
if [ "$lab_source" != "$lab_target" ]; then
    cp "$lab_source/app.py" "$lab_source/dyno_core.py" "$lab_source/dashboard.html" "$lab_source/README.txt" "$lab_source/test_app.py" "$lab_target/"
fi
python3 -m py_compile "$lab_target/app.py" "$lab_target/dyno_core.py"

# Old programs are not killed automatically. Refuse installation startup if
# another server or GPIO controller is still running.
if pgrep -u "$lab_user" -f '[p]ython3 .*dyno_auto.py|[p]ython3 .*dyno_full.py|[p]ython3 .*relay_control.*py|[p]ython3 .*loadcell_test.py|[p]ython3 .*rpm_reader.py|[p]ython3 .*torque_monitor.py' >/dev/null; then
    echo "An old GPIO program is still running. Stop it with Ctrl+C (or q for relay control), then run this installer again."
    exit 1
fi
if ! python3 - <<'PY'
import socket
s=socket.socket()
try:
    s.bind(('0.0.0.0',8766))
except OSError as e:
    print('Port 8766 is occupied:',e)
    raise SystemExit(1)
finally:
    s.close()
PY
then
    echo "Close the old SSH forwarding tab for port 8766 if it runs ON the Pi, or stop the old server. Then run this installer again."
    exit 1
fi

lab_unit="$(mktemp "$lab_target/.motor-lab-unit.XXXXXX")"
trap 'rm -f "$lab_unit"' EXIT
cat > "$lab_unit" <<UNIT
[Unit]
Description=Motor Lab local dashboard and attended dyno
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=3

[Service]
Type=simple
User=$lab_user
Group=$lab_group
WorkingDirectory=$lab_target
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 "$lab_target/app.py" --hardware --host 0.0.0.0 --port 8766 --output "$HOME/dyno/auto_runs"
Restart=on-failure
RestartSec=5
TimeoutStopSec=15
KillSignal=SIGTERM
KillMode=mixed

[Install]
WantedBy=multi-user.target
UNIT

sudo install -m 644 "$lab_unit" /etc/systemd/system/motor-lab.service
sudo systemctl daemon-reload
sudo systemctl enable --now motor-lab.service
sleep 2
if ! systemctl is-active --quiet motor-lab.service; then
    sudo journalctl -u motor-lab.service -n 25 --no-pager
    exit 1
fi
echo
echo "INSTALLED. You may close SSH. No motor or test starts automatically."
echo "Open on your Windows browser: http://raspberrypi.local:8766"
echo "If the hostname does not resolve, use one of these Pi IP addresses:"
hostname -I
echo "Saved results: $HOME/dyno/auto_runs"

