#!/usr/bin/env python3
import fcntl
import json
import paho.mqtt.client as mqtt
import serial
import signal
import struct
import sys
import threading
import time

# Version 4: persistent port, bus recovery and controlled write retries
# --- Settings ---
PORT = "/dev/ttyUSB0"
MQTT_BROKER = "192.0.2.10"  # placeholder: your MQTT broker address
MQTT_PORT = 1883
MQTT_USER = "your_mqtt_user"
MQTT_PASS = "your_mqtt_password"
POLL_INTERVAL = 15
WB_POLL_MULTIPLIER = 1  # Full wallbox reads happen every 1 cycle (15 seconds)
REG_CABLE_STATUS = b"\x00\x29"

# --- Fail-Safe (Watchdog) Settings ---
STARTUP_TIMEOUT = 60   # Seconds of grace period exclusive to startup
WATCHDOG_TIMEOUT = 300 # Normal operating limit in seconds if communication is lost
SAFE_AMPS = 6.0        # Safe current (amps) applied by the Watchdog

# Device mapping
DEVICES = {
    "1": {"name": "wallbox_a", "addr": "AAAA0001", "type": "wallbox"},
    "2": {"name": "wallbox_b", "addr": "AAAA0002", "type": "wallbox"},
    "3": {"name": "site",      "addr": "AAAA0003", "type": "ct"},
}

# --- Availability MQTT ---
AVAILABILITY_ON = "online"
AVAILABILITY_OFF = "offline"

# Global state variables
last_hass_contact = time.time()  # Start the real clock at startup
watchdog_triggered = False       # Starts inert to allow external control
startup_phase = True             # Starts in temporary tolerance mode

def availability_topic(target_id):
    dev = DEVICES[target_id]
    return f"ecohub/{dev['type']}/{target_id}/availability"

GLOBAL_LWT_TOPIC = availability_topic("3")
shutdown_requested = threading.Event()

def handle_shutdown_signal(signum, frame):
    print(f"Received shutdown signal: {signum}", file=sys.stderr)
    shutdown_requested.set()

def publish_availability(client, target_id, online, wait=False):
    payload = AVAILABILITY_ON if online else AVAILABILITY_OFF
    info = client.publish(
        availability_topic(target_id),
        payload,
        qos=1,
        retain=True,
    )
    if wait:
        info.wait_for_publish()

def publish_all_availability(client, online, wait=False):
    for target_id in DEVICES:
        publish_availability(client, target_id, online, wait=wait)

def publish_heartbeat_alive(client, is_alive):
    topic = "ecohub/status/heartbeat_alive"
    payload = "ON" if is_alive else "OFF"
    client.publish(topic, payload, retain=True)

# --- RS485 Protocol ---
PREAMBLE = b"UU"
REG_WRITE_AMPS = b"\x81\x0f"
REG_READ_AMPS = b"\x01\x0f"
REG_WRITE_PAUSE = b"\x81\x01"
REG_READ_PAUSE = b"\x01\x01"
REG_STATE = b"\x00\x0f"
REG_TELEMETRY = {"1": b"\x00\x13", "2": b"\x00\x14", "3": b"\x00\x15"}

VAL_PAUSE = 21474830
VAL_UNPAUSE = 0

serial_lock = threading.Lock()

# --- Persistent RS485 session ---
# A single serial port is shared by ALL devices on the same bus:
# wallbox A, wallbox B and CT/site.
serial_port = None
SERIAL_REOPEN_DELAY = 1.0       # Wait with the port closed before reopening
SERIAL_POST_OPEN_DELAY = 0.5    # Settling time after opening the FTDI
INTER_TRANSACTION_DELAY = 0.10  # Interval between transactions on the ALM/RS485 bus
WRITE_MAX_ATTEMPTS = 2       # Initial attempt + one retry after bus recovery

def open_serial_port():
    """Opens /dev/ttyUSB0 once and keeps the session open."""
    global serial_port

    if serial_port is not None and serial_port.is_open:
        return True

    try:
        ser = serial.Serial(
            PORT,
            baudrate=9600,
            timeout=0.5,
            write_timeout=1.0,
        )

        # The flock is acquired only once and held for the whole lifetime
        # of this file descriptor. serial_lock remains the main protection
        # between transactions within this process.
        fcntl.flock(ser.fileno(), fcntl.LOCK_EX)
        serial_port = ser
        print(f"RS485 port {PORT} opened and kept persistent.", file=sys.stderr)
        return True
    except Exception as e:
        print(f"Error opening RS485 port {PORT}: {e}", file=sys.stderr)
        try:
            ser.close()
        except Exception:
            pass
        return False

def close_serial_port():
    """Closes the single serial session during shutdown."""
    global serial_port

    ser = serial_port
    serial_port = None

    if ser is None:
        return

    try:
        try:
            fcntl.flock(ser.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        ser.close()
        print(f"RS485 port {PORT} closed.", file=sys.stderr)
    except Exception as e:
        print(f"Error closing RS485 port {PORT}: {e}", file=sys.stderr)

def recover_serial_port(reason="unspecified cause"):
    """Recovers the session of the ENTIRE bus after a timeout or I/O failure.

    Must be called with serial_lock held. Does not repeat the transaction
    that failed, especially writes that may have been executed even though
    the confirmation was lost.
    """
    global serial_port
    old_ser = serial_port
    serial_port = None

    if old_ser is not None:
        try:
            try:
                fcntl.flock(old_ser.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            old_ser.close()
        except Exception:
            pass

    if shutdown_requested.is_set():
        return False

    print(
        f"Recovering the RS485 bus session ({reason}). "
        f"Waiting {SERIAL_REOPEN_DELAY:.1f}s before reopening...",
        file=sys.stderr,
        flush=True,
    )
    shutdown_requested.wait(SERIAL_REOPEN_DELAY)
    if shutdown_requested.is_set():
        return False

    if not open_serial_port():
        return False

    # Opening/configuring the FTDI may electrically disturb the bus.
    # Do not start a new transaction until the interface settles.
    shutdown_requested.wait(SERIAL_POST_OPEN_DELAY)
    return not shutdown_requested.is_set()

def get_serial_port():
    """Ensures an open serial session exists. Must be called with serial_lock held."""
    if serial_port is None or not serial_port.is_open:
        return open_serial_port()
    return True


def calc_crc(packet_body):
    body_padded = packet_body + b"\x00\x00"
    total = sum(
        int.from_bytes(body_padded[i:i + 2], "big")
        for i in range(0, len(body_padded) - 2, 2)
    )
    return (total & 0xFFFF).to_bytes(2, "big")

def build_packet(address_hex, register, value):
    body = PREAMBLE + bytes.fromhex(address_hex) + register + struct.pack(">I", value)
    return body + calc_crc(body)

def send_and_receive(ser, address, packet, read_timeout=0.5):
    """Sends a transaction and waits for the expected 14-byte response.

    read_timeout preserves the distinct timeouts that existed in the
    original code, even though the serial port is now persistent.
    """
    previous_timeout = ser.timeout
    try:
        ser.timeout = read_timeout
        ser.reset_input_buffer()
        ser.write(packet)
        ser.flush()
        response = ser.read(14)
        if len(response) != 14:
            raise TimeoutError(f"Timeout: Expected 14 bytes, received {len(response)}")

        recv_crc = response[-2:]
        # The CT/site device may answer with a zero checksum; accept it
        is_ct = any(
            d["type"] == "ct" and d["addr"].lower() == address.lower()
            for d in DEVICES.values()
        )
        if is_ct and recv_crc == b"\x00\x00":
            return response

        expected_crc = calc_crc(response[:-2])
        if expected_crc != recv_crc:
            raise ValueError("Invalid CRC")
        return response
    finally:
        ser.timeout = previous_timeout
        # The persistent port removes the delays previously introduced by
        # close/open. Restoring an explicit interval protects the bus timing.
        if not shutdown_requested.is_set():
            time.sleep(INTER_TRANSACTION_DELAY)

# --- Safe Operations (Thread-Safe and Process-Safe) ---
def read_cable_status(target_id):
    dev = DEVICES[target_id]
    addr = dev["addr"]

    with serial_lock:
        try:
            if not get_serial_port():
                return None

            resp_cable = send_and_receive(
                serial_port,
                addr,
                build_packet(addr, REG_CABLE_STATUS, 0),
                read_timeout=0.5,
            )
            val_cable = struct.unpack(">I", resp_cable[8:12])[0]
            return "true" if val_cable > 0 else "false"
        except TimeoutError as e:
            print(
                f"Timeout reading cable status of {dev['name']}: {e}. "
                "Recovering the RS485 bus.",
                file=sys.stderr,
            )
            recover_serial_port(f"timeout on cable status of {dev['name']}")
            return None
        except ValueError as e:
            # Invalid CRC / protocol error does not imply a TTY/USB failure.
            print(f"Protocol error reading cable status of {dev['name']}: {e}", file=sys.stderr)
            return None
        except (serial.SerialException, OSError) as e:
            print(
                f"I/O failure reading cable status of {dev['name']}: {e}",
                file=sys.stderr,
            )
            recover_serial_port(f"I/O failure on cable status of {dev['name']}: {e}")
            return None
        except Exception as e:
            print(f"Unexpected error reading cable status of {dev['name']}: {e}", file=sys.stderr)
            return None

def read_device(target_id, current_cable_status=None):
    dev = DEVICES[target_id]
    addr = dev["addr"]
    data = {}

    with serial_lock:
        try:
            if not get_serial_port():
                return None

            ser = serial_port

            # Read phases (L or CT)
            for phase in ["1", "2", "3"]:
                resp = send_and_receive(
                    ser,
                    addr,
                    build_packet(addr, REG_TELEMETRY[phase], 0),
                    read_timeout=0.5,
                )
                val = struct.unpack(">I", resp[8:12])[0]
                data[f"f{phase}"] = round(val / 1000.0, 3)
                time.sleep(0.05)

            # Read wallbox-only parameters
            if dev["type"] == "wallbox":
                # Configured current
                resp_amps = send_and_receive(
                    ser,
                    addr,
                    build_packet(addr, REG_READ_AMPS, 0),
                    read_timeout=0.5,
                )
                val_amps = struct.unpack(">I", resp_amps[8:12])[0]
                data["amps_limit"] = round(val_amps / 1000.0, 1)

                # Pause state
                resp_pause = send_and_receive(
                    ser,
                    addr,
                    build_packet(addr, REG_READ_PAUSE, 0),
                    read_timeout=0.5,
                )
                val_pause = struct.unpack(">I", resp_pause[8:12])[0]
                data["paused"] = True if val_pause > 0 else False

                # Mask the limit as 0.0A if the wallbox is paused
                if data["paused"]:
                    data["amps_limit"] = 0.0

                # Inject the pre-read cable state
                if current_cable_status is not None:
                    data["cable_connected"] = current_cable_status

        except TimeoutError as e:
            print(
                f"Timeout reading {dev['name']}: {e}. "
                "Recovering the RS485 bus.",
                file=sys.stderr,
            )
            recover_serial_port(f"timeout reading {dev['name']}")
            return None
        except ValueError as e:
            # Invalid CRC / protocol error does not trigger a TTY close/open.
            print(f"Protocol error reading {dev['name']}: {e}", file=sys.stderr)
            return None
        except (serial.SerialException, OSError) as e:
            # A real TTY/USB failure invalidates the session for the ENTIRE bus.
            recover_serial_port(f"I/O failure reading {dev['name']}: {e}")
            print(
                f"I/O failure reading {dev['name']}: {e}",
                file=sys.stderr,
            )
            return None
        except Exception as e:
            print(f"Unexpected error reading {dev['name']}: {e}", file=sys.stderr)
            return None
    return data

def write_amps(target_id, amps):
    """Writes the current limit and retries once after any failure.

    In ALM, losing the command is worse than sending the same value again.
    Each retry is preceded by a full session recovery.
    """
    dev = DEVICES[target_id]
    addr = dev["addr"]
    val_ma = int(float(amps) * 1000)

    with serial_lock:
        for attempt in range(1, WRITE_MAX_ATTEMPTS + 1):
            try:
                if not get_serial_port():
                    error = RuntimeError(f"Could not open {PORT}")
                    if attempt < WRITE_MAX_ATTEMPTS:
                        recover_serial_port(
                            f"port unavailable before writing current limit on {dev['name']}"
                        )
                        continue
                    print(
                        f"Final failure writing current limit on {dev['name']}: {error}",
                        file=sys.stderr,
                    )
                    return False

                send_and_receive(
                    serial_port,
                    addr,
                    build_packet(addr, REG_WRITE_AMPS, val_ma),
                    read_timeout=1.0,
                )
                suffix = "" if attempt == 1 else f" after {attempt} attempts"
                print(f"Write confirmed: {dev['name']} -> {amps}A{suffix}")
                return True

            except (TimeoutError, ValueError, serial.SerialException, OSError) as e:
                if attempt < WRITE_MAX_ATTEMPTS:
                    print(
                        f"Attempt {attempt}/{WRITE_MAX_ATTEMPTS} failed writing "
                        f"current limit on {dev['name']}: {type(e).__name__}: {e}. "
                        "Recovering the bus and retrying the command.",
                        file=sys.stderr,
                    )
                    recover_serial_port(
                        f"failure writing current limit on {dev['name']}: {e}"
                    )
                    continue

                print(
                    f"Final failure after {WRITE_MAX_ATTEMPTS} attempts writing "
                    f"current limit on {dev['name']}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return False

            except Exception as e:
                print(
                    f"Unexpected error writing current limit on {dev['name']}: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return False

    return False

def write_pause(target_id, do_pause):
    """Changes the pause state and retries once after any handled failure."""
    dev = DEVICES[target_id]
    addr = dev["addr"]
    val = VAL_PAUSE if do_pause else VAL_UNPAUSE
    action = "Enabled" if do_pause else "Disabled"

    with serial_lock:
        for attempt in range(1, WRITE_MAX_ATTEMPTS + 1):
            try:
                if not get_serial_port():
                    error = RuntimeError(f"Could not open {PORT}")
                    if attempt < WRITE_MAX_ATTEMPTS:
                        recover_serial_port(
                            f"port unavailable before writing pause on {dev['name']}"
                        )
                        continue
                    print(
                        f"Final failure writing pause on {dev['name']}: {error}",
                        file=sys.stderr,
                    )
                    return False

                send_and_receive(
                    serial_port,
                    addr,
                    build_packet(addr, REG_WRITE_PAUSE, val),
                    read_timeout=1.0,
                )
                suffix = "" if attempt == 1 else f" after {attempt} attempts"
                print(f"Pause changed: {dev['name']} -> {action}{suffix}")
                return True

            except (TimeoutError, ValueError, serial.SerialException, OSError) as e:
                if attempt < WRITE_MAX_ATTEMPTS:
                    print(
                        f"Attempt {attempt}/{WRITE_MAX_ATTEMPTS} failed writing "
                        f"pause on {dev['name']}: {type(e).__name__}: {e}. "
                        "Recovering the bus and retrying the command.",
                        file=sys.stderr,
                    )
                    recover_serial_port(
                        f"failure writing pause on {dev['name']}: {e}"
                    )
                    continue

                print(
                    f"Final failure after {WRITE_MAX_ATTEMPTS} attempts writing "
                    f"pause on {dev['name']}: {type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return False

            except Exception as e:
                print(
                    f"Unexpected error writing pause on {dev['name']}: "
                    f"{type(e).__name__}: {e}",
                    file=sys.stderr,
                )
                return False

    return False

def wait_and_pause(target_id, timeout=60):
    """Waits for the wallbox to respond on the bus before applying the pause."""
    print(f"Waiting for wallbox {target_id} to respond before applying safety pause...", file=sys.stderr)
    start = time.time()
    while time.time() - start < timeout:
        # If we can read data, the wallbox RS485 bus is operational
        if read_device(target_id) is not None:
            print(f"Wallbox {target_id} online. Applying pause...", file=sys.stderr)
            return write_pause(target_id, True)
        time.sleep(2) # Interval between boot checks
    print(f"Timeout: Wallbox {target_id} did not respond to the initial handshake.", file=sys.stderr)
    return False

# --- MQTT Callbacks ---
def on_connect(client, userdata, flags, reason_code, properties=None):
    if reason_code == 0:
        print("Connected to MQTT broker successfully.", file=sys.stderr)
        publish_all_availability(client, True)
        publish_heartbeat_alive(client, False)
        
        # ALM, State and Heartbeat subscriptions
        client.subscribe("ecohub/wallbox/+/set_amps")
        client.subscribe("ecohub/wallbox/+/set_pause")
        client.subscribe("ecohub/control/heartbeat")
    else:
        print(f"MQTT connection failed. Code: {reason_code}", file=sys.stderr)

def on_message(client, userdata, msg):
    global last_hass_contact, watchdog_triggered, startup_phase
    
    # 1. Heartbeat-only handling
    if msg.topic == "ecohub/control/heartbeat":
        last_hass_contact = time.time()
        startup_phase = False # First heartbeat ends the startup phase
        
        # Publish ON whenever a valid heartbeat is received, regardless of the watchdog state
        publish_heartbeat_alive(client, True)
        
        # Only report recovery if the system was previously in failure
        if watchdog_triggered:
            watchdog_triggered = False
            print("INFO: Contact with HASS restored. Watchdog disarmed.", file=sys.stderr)
            for target_id, dev in DEVICES.items():
                if dev["type"] == "wallbox":
                    # Initially pause as a precaution; the ALM on the HASS side takes over afterwards
                    wait_and_pause(target_id)
        return
    
    # 2. Command handling (does not reset the Watchdog)
    payload = msg.payload.decode()
    try:
        parts = msg.topic.split("/")
        target_id = parts[2]
        command = parts[3]

        if target_id in DEVICES and DEVICES[target_id]["type"] == "wallbox":
            need_publish = False

            if command == "set_pause":
                do_pause = payload.lower() in ("true", "1", "on")
                if write_pause(target_id, do_pause):
                    need_publish = True

            elif command == "set_amps":
                new_amps = float(payload)
                if new_amps == 0.0:
                    if write_pause(target_id, True):
                        need_publish = True
                elif 6.0 <= new_amps <= 32.0:
                    if write_amps(target_id, new_amps):
                        need_publish = True
                else:
                    print(f"Value rejected: {new_amps}A", file=sys.stderr)

            # State publishing (kept)
            if need_publish:
                current_cable = read_cable_status(target_id)
                topic = f"ecohub/{DEVICES[target_id]['type']}/{target_id}/state"
                
                if current_cable is not None:
                    data = read_device(target_id, current_cable_status=current_cable)
                    if data:
                        client.publish(topic, json.dumps(data), retain=True)
                        publish_availability(client, target_id, True)

    except Exception as e:
        print(f"Error processing MQTT message: {e}", file=sys.stderr)
        
# --- Main Loop ---
if __name__ == "__main__":
    signal.signal(signal.SIGTERM, handle_shutdown_signal)
    signal.signal(signal.SIGINT, handle_shutdown_signal)

    try:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    except AttributeError:
        client = mqtt.Client()

    client.username_pw_set(MQTT_USER, MQTT_PASS)
    client.on_connect = on_connect
    client.on_message = on_message

    # Single LWT
    client.will_set(
        GLOBAL_LWT_TOPIC,
        payload=AVAILABILITY_OFF,
        qos=1,
        retain=True,
    )

    try:
        client.connect_async(MQTT_BROKER, MQTT_PORT, 60)
    except Exception as e:
        print(f"Initial MQTT socket error: {e}", file=sys.stderr)

    if shutdown_requested.is_set():
        sys.exit(0)

    client.loop_start()

    # Open the RS485 bus only once at startup.
    # Both wallboxes and the CT/site share this same /dev/ttyUSB0.
    with serial_lock:
        open_serial_port()

    # Strict precaution against wallbox hardware reset when the RS485 port is opened
    print("Re-arming mechanical safety on the wallboxes...", file=sys.stderr)
    for target_id, dev in DEVICES.items():
        if dev["type"] == "wallbox":
            # Replaces the direct write_pause with wait_and_pause
            wait_and_pause(target_id)

    print("MQTT-RS485 bridge started safely...")

    last_cable_states = {k: None for k, v in DEVICES.items() if v["type"] == "wallbox"}
    loop_counter = 0

    try:
        while not shutdown_requested.is_set():
            
            # Dynamic Fail-Safe / Watchdog evaluation
            timeout_limit = STARTUP_TIMEOUT if startup_phase else WATCHDOG_TIMEOUT
            
            if (time.time() - last_hass_contact > timeout_limit) and not watchdog_triggered:
                print(f"CRITICAL ALERT: HASS has not communicated for more than {timeout_limit}s. Watchdog triggered.", file=sys.stderr)
                for target_id, dev in DEVICES.items():
                    if dev["type"] == "wallbox":
                        print(f"Watchdog: Forcing {SAFE_AMPS}A protection on wallbox {dev['name']}", file=sys.stderr)
                        write_amps(target_id, SAFE_AMPS)
                        write_pause(target_id, False)
                watchdog_triggered = True
                startup_phase = False
                publish_heartbeat_alive(client, False)
                
            for target_id in DEVICES:
                if shutdown_requested.is_set():
                    break

                dev_type = DEVICES[target_id]["type"]
                avail_topic = availability_topic(target_id)
                topic = f"ecohub/{dev_type}/{target_id}/state"

                if dev_type == "wallbox":
                    current_cable = read_cable_status(target_id)

                    if current_cable is None:
                        client.publish(avail_topic, AVAILABILITY_OFF, qos=1, retain=True)
                        continue

                    cable_changed = False
                    if last_cable_states[target_id] is not None:
                        if current_cable != last_cable_states[target_id]:
                            cable_changed = True

                    last_cable_states[target_id] = current_cable
                    is_full_cycle = (loop_counter % WB_POLL_MULTIPLIER == 0)

                    if is_full_cycle or cable_changed:
                        data = read_device(target_id, current_cable_status=current_cable)
                        if data:
                            client.publish(topic, json.dumps(data), retain=True)
                            client.publish(avail_topic, AVAILABILITY_ON, qos=1, retain=True)
                        else:
                            client.publish(avail_topic, AVAILABILITY_OFF, qos=1, retain=True)
                else:
                    data = read_device(target_id)
                    if data:
                        client.publish(topic, json.dumps(data), retain=True)
                        client.publish(avail_topic, AVAILABILITY_ON, qos=1, retain=True)
                    else:
                        client.publish(avail_topic, AVAILABILITY_OFF, qos=1, retain=True)

            loop_counter += 1
            shutdown_requested.wait(POLL_INTERVAL)

    finally:
        print("Shutting down: publishing offline availability...", file=sys.stderr)
        try:
            with serial_lock:
                close_serial_port()
        except Exception as e:
            print(f"Error closing RS485 port on shutdown: {e}", file=sys.stderr)
        try:
            publish_all_availability(client, False, wait=True)
        except Exception as e:
            print(f"Error publishing offline availability: {e}", file=sys.stderr)
        try:
            client.loop_stop()
        except Exception:
            pass
        try:
            client.disconnect()
        except Exception:
            pass