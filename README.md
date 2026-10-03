# alm-mqtt-bridge

An RS485 ⇄ MQTT bridge for EO Genius wallboxes and their CT/site meter, with a built-in fail-safe watchdog.

The bridge reads the phase currents, cable state and current limit of each wallbox over the ALM RS485 bus and publishes them to MQTT. It also accepts MQTT commands to set the current limit or to pause/resume charging. It is meant to be driven by a load-management controller such as a Home Assistant automation: the bridge itself never decides anything, it only reads, writes and protects.

> **Unofficial project.** Not affiliated with, or endorsed by, EO Charging. See the [Disclaimer](#disclaimer).

## Features

- One persistent serial session shared by every device on the bus (wallboxes and CT/site), protected by an exclusive `flock` and an in-process lock between transactions.
- Automatic bus recovery after a timeout or I/O failure.
- Write commands (current limit, pause) are retried once after the bus has been recovered.
- Retained state and availability topics, plus an MQTT Last Will.
- Heartbeat watchdog: if the controller stops sending heartbeats, the wallboxes fall back to a safe current.
- Works with paho-mqtt 1.x and 2.x.

## Requirements

- Linux (the script uses `fcntl`). A Raspberry Pi is fine.
- Python 3.
- An RS485 adapter that shows up as a serial port (an FTDI-based USB–RS485 adapter on `/dev/ttyUSB0` by default).
- An MQTT broker, for example Mosquitto.
- The Python packages in `requirements.txt` (`paho-mqtt`, `pyserial`).

## Installation

```bash
git clone https://github.com/jprates/alm-mqtt-bridge.git
cd alm-mqtt-bridge
python3 -m venv venv
. venv/bin/activate
pip install -r requirements.txt
```

The user running the script needs access to the serial port. On most distributions:

```bash
sudo usermod -aG dialout $USER   # then log out and back in
```

## Configuration

All settings are constants at the top of `alm_mqtt_bridge.py`.

| Constant | Default | Meaning |
|---|---|---|
| `PORT` | `/dev/ttyUSB0` | Serial port of the RS485 adapter (fixed at 9600 baud). |
| `MQTT_BROKER` | `192.0.2.10` | **Placeholder.** Address of your MQTT broker. |
| `MQTT_PORT` | `1883` | MQTT broker port. |
| `MQTT_USER` / `MQTT_PASS` | placeholders | **Placeholders.** Your MQTT credentials. |
| `POLL_INTERVAL` | `15` | Seconds between polling cycles. |
| `WB_POLL_MULTIPLIER` | `1` | Full wallbox reads happen every N cycles (the cable state is checked every cycle). |
| `STARTUP_TIMEOUT` | `60` | Seconds to wait for the first heartbeat after start-up. |
| `WATCHDOG_TIMEOUT` | `300` | Seconds without a heartbeat before the watchdog fires (after the first heartbeat). |
| `SAFE_AMPS` | `6.0` | Current limit (A) applied by the watchdog. |
| `DEVICES` | see below | The devices on the bus. |

### Devices

```python
DEVICES = {
    "1": {"name": "wallbox_a", "addr": "AAAA0001", "type": "wallbox"},
    "2": {"name": "wallbox_b", "addr": "AAAA0002", "type": "wallbox"},
    "3": {"name": "site",      "addr": "AAAA0003", "type": "ct"},
}
```

- The **key** (`"1"`, `"2"`, `"3"`) is the ID used in the MQTT topics.
- `addr` is the 8-hex-digit address of the device on the RS485 bus. **The values above are placeholders; replace them with the addresses of your own devices.**
- `type` is either `wallbox` or `ct`. The `ct` device only reports phase currents.
- `name` is only used in log messages.

Do not commit your real credentials to a public repository.

## Running

```bash
python3 alm_mqtt_bridge.py
```

Stop it with `Ctrl+C` or `SIGTERM`: the bridge closes the serial port and publishes every device as `offline` before exiting. Log messages go to stderr (and a few to stdout).

### As a systemd service

```ini
[Unit]
Description=ALM MQTT bridge
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/opt/alm-mqtt-bridge/venv/bin/python /opt/alm-mqtt-bridge/alm_mqtt_bridge.py
Restart=always
RestartSec=5
User=<user-in-the-dialout-group>

[Install]
WantedBy=multi-user.target
```

## MQTT topics

Topics follow the layout `ecohub/<type>/<id>/<suffix>`, where `<type>` (`wallbox` or `ct`) and `<id>` come from `DEVICES`. With the default configuration, the wallboxes are IDs `1` and `2` and the CT/site device is ID `3`.

### Published by the bridge

| Topic | Retained | Payload |
|---|---|---|
| `ecohub/<type>/<id>/state` | yes | JSON, see below. |
| `ecohub/<type>/<id>/availability` | yes (QoS 1) | `online` or `offline`. |
| `ecohub/status/heartbeat_alive` | yes | `ON` while heartbeats are being received, `OFF` otherwise. |

Availability is `offline` when a device does not answer on the bus. If the bridge disconnects unexpectedly, the MQTT Last Will sets the availability of the `ct` device (ID `3` by default) to `offline`.

### Subscribed by the bridge

| Topic | Payload | Effect |
|---|---|---|
| `ecohub/wallbox/<id>/set_amps` | number | `0` pauses the wallbox. A value from `6` to `32` sets the current limit (A). Any other value is rejected and logged. |
| `ecohub/wallbox/<id>/set_pause` | `true`, `1` or `on` (case-insensitive) | Pauses the wallbox. Any other payload resumes it. |
| `ecohub/control/heartbeat` | anything | Resets the watchdog (see [Fail-safe watchdog](#fail-safe-watchdog)). |

Things to know:

- Setting a current limit does **not** resume a paused wallbox, and `set_amps` = `0` does not change the stored limit. To resume charging, send `set_pause` = `false`.
- After every successful command, the bridge immediately publishes a fresh `state` message.
- Commands are ignored for devices of type `ct`.

### State payloads

Wallbox:

```json
{"f1": 6.012, "f2": 0.0, "f3": 0.0, "amps_limit": 16.0, "paused": false, "cable_connected": "true"}
```

CT/site:

```json
{"f1": 3.2, "f2": 1.1, "f3": 0.4}
```

| Field | Meaning |
|---|---|
| `f1`, `f2`, `f3` | Current on phases 1–3, in A. |
| `amps_limit` | Configured current limit in A. Reported as `0.0` while the wallbox is paused. |
| `paused` | `true` if charging is paused. |
| `cable_connected` | The **string** `"true"` or `"false"` (not a JSON boolean). Omitted if the cable state could not be read. |

## Fail-safe watchdog

A load-management controller that stops working must not leave the chargers in an unknown state, so the bridge runs a watchdog based on a heartbeat topic.

1. **Start-up.** After opening the bus, the bridge waits for each wallbox to answer (up to 60 s) and then **pauses** it. Opening the RS485 port can reset the wallbox hardware, so this puts everything in a known state.
2. **Grace period.** Until the first message arrives on `ecohub/control/heartbeat`, the watchdog limit is `STARTUP_TIMEOUT` (60 s). After the first heartbeat it becomes `WATCHDOG_TIMEOUT` (300 s).
3. **Watchdog fires.** If no heartbeat arrives within the limit, every wallbox is set to `SAFE_AMPS` and **resumed**, and `ecohub/status/heartbeat_alive` goes to `OFF`. This happens once, until heartbeats return.
4. **Recovery.** When a heartbeat arrives again, `heartbeat_alive` goes to `ON` and every wallbox is **paused**, so that the controller starts from a clean state and takes over.

> **Your controller must publish a heartbeat** to `ecohub/control/heartbeat` at least every 30 seconds or so (the first one must arrive within `STARTUP_TIMEOUT` of the bridge starting). Otherwise the watchdog will force the wallboxes to `SAFE_AMPS` and resume them.

Note that the fallback is to *keep charging at a safe current*, not to stop. Check that this is the behaviour you want for your installation and adjust `SAFE_AMPS` or the code if needed.

## Home Assistant

Two example files are provided in `examples/`:

- `homeassistant_mqtt.yaml`: the MQTT entities for the topics above. It has no top-level `mqtt:` key, so include it from `configuration.yaml` with `mqtt: !include homeassistant_mqtt.yaml` (or merge it into your existing MQTT YAML).
- `homeassistant_heartbeat_automation.yaml`: an automation that publishes the heartbeat every 30 seconds. **You need something like this**, see [Fail-safe watchdog](#fail-safe-watchdog).

Entity names follow the bridge: `wallbox_a`, `wallbox_b` and `site` are the `name` values in `DEVICES`, and the suffixes are the field names of the JSON state.

| Entity | Type | Topic / field |
|---|---|---|
| `wallbox_a_f1`, `_f2`, `_f3` (same for `wallbox_b`, `site`) | sensor | `state` → `f1`, `f2`, `f3` (A) |
| `wallbox_a_amps_limit` (same for `wallbox_b`) | sensor | `state` → `amps_limit` (A) |
| `wallbox_a_set_amps` (same for `wallbox_b`) | number (slider, 0–32 A) | reads `amps_limit`, writes `set_amps` |
| `wallbox_a_paused` (same for `wallbox_b`) | switch | reads `paused`, writes `set_pause` |
| `wallbox_a_cable_connected` (same for `wallbox_b`) | binary sensor | `state` → `cable_connected` |
| `ecohub_online` | binary sensor | availability of the CT/site device, which is also the bridge's Last Will topic |
| `heartbeat_alive` | binary sensor | `ecohub/status/heartbeat_alive` |

Notes:

- The slider also allows 1–5 A, which the bridge rejects. Use `0` (pause) or `6`–`32`.
- Moving the slider does not resume a paused wallbox. Turn the `*_paused` switch off.
- Entity IDs are derived from these names. If you rename entities in an existing installation, update your dashboards and automations too.
- The load-management logic itself (deciding which current to set) is not part of this project; it depends on your installation.

## Bus protocol notes

These are the frame and register details as used by the script.

- Serial settings: 9600 baud, pyserial defaults for the rest.
- Every request and response is 14 bytes: `UU` preamble (2 bytes), device address (4), register (2), value (4, big-endian unsigned) and a checksum (2).
- The checksum is the sum of the six 16-bit big-endian words of the first 12 bytes, modulo 65536. The CT/site device may reply with a zero checksum, which the script accepts.
- Currents are exchanged in mA.

| Register | Access | Meaning |
|---|---|---|
| `0x0013`, `0x0014`, `0x0015` | read | Current on phase 1, 2, 3 (mA). |
| `0x0029` | read | Cable status (non-zero = connected). |
| `0x010F` / `0x810F` | read / write | Current limit (mA). |
| `0x0101` / `0x8101` | read / write | Pause state. Write `21474830` to pause and `0` to resume. |

## Troubleshooting

- **"Error opening RS485 port"**: check that the adapter is present (`ls /dev/ttyUSB*`), that your user is in the `dialout` group, and that no other program is using the port (the bridge takes an exclusive lock).
- **Repeated "Recovering the RS485 bus" messages or devices stuck `offline`**: check the A/B wiring and termination, the device addresses in `DEVICES`, and that the adapter is connected to the right bus.
- **Wallboxes drop to `SAFE_AMPS` shortly after start**: the controller is not sending heartbeats in time. See [Fail-safe watchdog](#fail-safe-watchdog).

## Credits

Based on knowledge about RS-485 communication shared by Mike Scott [@minceheid] on his project https://github.com/minceheid/openeo
Written by **joao prates** ([@jprates](https://github.com/jprates))

## License

MIT Licence.
Free to use and share as long as credits are kept and no commercial use or profit is obtained from it.

## Disclaimer

This software controls EV charging equipment. It is provided as is, without warranty of any kind, and you use it at your own risk. It is an independent project and is not affiliated with EO Charging. Test carefully, and make sure the fail-safe behaviour is acceptable for your installation before relying on it.
