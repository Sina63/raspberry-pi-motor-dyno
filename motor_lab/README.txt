MOTOR LAB 2.0 — RASPBERRY PI LOCAL DASHBOARD

WHAT CHANGED
One local browser application: live tests, persistent results, charts and
optional downloads. Includes the corrected 10 g at 74 mm (7.4 cm) torque
scale, same-direction baseline capture, a new-test action and a sensor
reconnect action. Your original data remain in ~/dyno/auto_runs.

ONE-TIME INSTALLATION
1. Motor and relay supplies OFF. Stop old GPIO programs with Ctrl+C, or q
   for the old relay control. Do not run old GPIO programs alongside this app.
2. Copy motor_lab_dashboard.zip to ~/dyno on the Pi.
3. In Pi SSH:
   python3 -m zipfile -e ~/dyno/motor_lab_dashboard.zip ~/dyno
   bash ~/dyno/motor_lab/install.sh
4. Open http://raspberrypi.local:8766 on Windows, on the same local network.
   No SSH tunnel is needed. The installer prints the Pi IP as a fallback:
   http://PI_IP_ADDRESS:8766
5. After installation, the terminal can be closed. The service starts at
   boot and opens no generator load or motor sweep until explicitly armed.
   Motor supply remains a physical manual control. Autostart does not mean
   motor or automatic sweep autostart.

The app uses the already-installed gpiozero, lgpio and smbus packages.
Installer installs missing packages through apt only if necessary. It installs
a motor-lab systemd service as the current normal Pi user. GPIO and I2C must
already be enabled/accessible as in the existing working project. It does not
alter the resistor bank, GPIO mapping, I2C address or motor wiring.

NORMAL TEST
1. Pi on. Motor power OFF. Select voltage (start at 6 V), maximum load
   (default 5) and intended direction as viewed from the same shaft end.
2. Confirm motor OFF and stopped; click New test.
3. Click Capture slow-turn baseline. Turn slowly in the intended direction
   for 45 s, aiming for several full revolutions. Do not push the reaction arm.
   If PAUSED appears, stop turning until the normal instruction returns.
4. Stop turning and release shaft for the 10 s settling period.
5. Set motor supply to selected voltage, current limit 2 A, output OFF.
   Relay coil supply ON. Clear hands. Confirm motor OFF checkbox, click
   Arm sweep, then turn motor output ON. Leave this controlling browser tab
   open and visible, with the power supply within reach.
6. Generator load switches 0 -> selected maximum -> 1 -> 0. Loads 1–5:
   5 s each; 6: 3 s; 7: 2 s. Break-before-make: K8 opens, waits 0.2 s,
   routing changes, waits 0.2 s, K8 closes. These are software commands,
   not physical contact feedback.
7. On FINISHED or ABORTED, turn motor power OFF. Click View results.
8. Results remain available after restart. To repeat, motor OFF/stopped,
   confirm checkbox, click New test. Each new test receives a new folder.

SENSOR FAULTS
Capture remains disabled while readings are stale or a reader has stopped.
Keep motor OFF. When possible, use Reconnect sensors with the OFF checkbox
confirmed, then New test. Reconnect never bypasses powered-test checks. A
real loose connection, inadequate power adapter or module failure still
requires repair. The HX711 runs in a separate process using the working
standalone reader protocol; timing retries discard samples. Motor-OFF pauses
are bounded to 3 s. Powered stale data over 0.9 s aborts load switching.

RESULTS
Saved results lists all valid run folders in ~/dyno/auto_runs, including the
older program's runs. Open any to view curves, averaged points, baseline,
time traces and original metadata. No download required.
- Download corrected CSV: derived averaged table with torque in mN·m,
  apparent mechanical W and apparent efficiency, plus correction factor.
- Download all: ZIP with unchanged original files, derived corrected CSV,
  an offline HTML report containing SVG graphs, and correction notes.
- Download offline report: standalone table, charts, warnings and metadata.
- Original-file links deliver the raw files exactly as recorded.

Old records with the explicitly recognised 7.4 mm torque coefficient are
displayed at 10x torque, corresponding to the corrected 74 mm distance.
Already-corrected records are not scaled twice. Unknown scales are flagged
and left unchanged. No historical file is rewritten.

INTERPRETATION AND LIMITS
Torque reference: 10 g, 74 mm perpendicular horizontal lever distance,
170499-count span. Coefficient = -0.007256921/170499 N m per count.
This remains a provisional calibration. Same-direction peaks/valleys centre
the baseline but can include friction. Shaft reaction path is not verified.
Mechanical estimate = mean torque * mean RPM * 2*pi/60. Apparent efficiency
= estimated mechanical W / measured electrical input W * 100. It is NOT
certified motor efficiency. Negative torque or stopped/low-power points have
no displayed efficiency. Values above 100% are flagged, not clamped.
Sensor windows overlap and are asynchronous. Row counts are not independent
sample counts. Short holds do not establish thermal or steady-state ratings.

CURRENT / SPEED PROTECTION
Fixed measured current abort: 1.9 A; calibration checked only through 2 A.
ADC saturation, sensor stale/error, very low RPM (<300 after startup),
RPM below 40% of unloaded reference, excessive voltage/RPM, or missing
controlling-browser heartbeat for 3 s requests K8 OPEN. Averaged current
readings introduce approximately 0.53 s delay; protection is not immediate.
Default maximum load is 5. Previous 6 V load 7 reached about 1.897 A.
Do not increase voltage, dwell or load without reviewing motor, sensor,
relay, wire, shunt and resistor limits. There is no temperature measurement.

STOP opens generator load only. It DOES NOT turn off motor power and can
allow speed to rise. Be beside the physical motor supply switch. Software
cannot protect against a hung Pi, welded contacts or failed GPIO. The
interface needs hardware bias keeping K8 OFF during boot/loss of control.

LOCAL NETWORK
Direct access is intended for your trusted local network. No cloud account
or internet access is used. Do not forward this hardware-control port from
your router to the internet. Commands require same-origin requests and an
application token; a test has one owning browser tab. STOP is available to
other local viewers. Results can be read from other browser tabs.

SERVICE / TROUBLESHOOTING (only when needed)
   systemctl status motor-lab --no-pager
   journalctl -u motor-lab -n 40 --no-pager
   sudo systemctl restart motor-lab
   sudo systemctl stop motor-lab
   sudo systemctl disable --now motor-lab
Stopping the service cannot turn the motor power supply off.

DEVELOPMENT / SOFTWARE CHECKS (no hardware)
   cd ~/dyno/motor_lab
   python3 -m unittest -v test_app.py
   python3 app.py --demo --host 127.0.0.1 --port 8877
   python3 app.py --results-only --host 127.0.0.1 --port 8877
Demo values are simulated and labelled. Hardware validation must be done
attended at 6 V; software tests cannot verify real relay contacts or wiring.
