"""Bench check: does each relay put the resistor bank you expect into circuit?

Run with the BATTERY DISCONNECTED and the 12 V relay supply ON (without it the
Arduino still echoes every command, but nothing clicks). Type a resistance, then
measure across the resistor ladder's two terminals with a multimeter. If the
meter disagrees with what was commanded, a relay is wired to the wrong bank, or
the bit order in control_logic.send_binary_command is wrong.

The Arduino's echo ("New State Received ...") only proves it parsed the string.
It says nothing about which resistor moved. Only the meter tells you that.

For the small banks (0.25, 0.5 ohm), short the meter probes together first and
subtract that reading. Lead resistance is the same size as what you're measuring.

    python tools/bench_relay_check.py              # auto-detect the resistor Arduino
    python tools/bench_relay_check.py --port COM5

At the prompt:
    8               command 8 ohms, encoded exactly as the rig would
    raw 00010000    send a raw 8-bit pattern (raw 00000000 hits the firmware interlock)
    walk            one relay at a time, pausing so you can meter each
    status          heartbeat and reader health, plus resets and watchdog trips seen
    hbtest          pause the heartbeat to prove it's what holds the watchdog off
    kill            send KILL: every relay open, main contactor open
    q               KILL and quit

The heartbeat is silent -- the firmware never answers "alive". It is working
when no WATCHDOG FIRED warning appears between commands; hbtest proves it.
The warnings, and what each one means:
    !! WATCHDOG FIRED   nothing reached the Arduino for 2 s: a heartbeat problem
    !! ARDUINO RESET    the board rebooted: power, brownout or EMI, not the heartbeat
    !! not applied      the Arduino never said "New State Received" for a command
"""

import argparse
import os
import sys
import threading
import time
import serial

# The rig's modules live one folder up.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control_logic import (
    MAX_RESISTANCE,
    RESISTOR_BAUD_RATE,
    RESISTOR_RESOLUTION,
    auto_detect_resistor,
    resistance_to_steps,
    send_binary_command,
)

# Arduino pin driven by each character of the command, left to right. Mirrors
# arduino/resistor_bank_controller: otherRelayPins[] = {12, 11, 10, 9, 8, 7, 6}
# take characters 0-6, and the last character drives BANK_1_RELAY on pin 5.
PIN_FOR_CHAR = [12, 11, 10, 9, 8, 7, 6, 5]

HEARTBEAT_S = 0.5      # the firmware watchdog sheds load after 2 s of silence
APPLY_TIMEOUT_S = 1.5  # how long to wait for "New State Received" after a command
HBTEST_PAUSE_S = 3.5   # long enough to clear the 2 s watchdog with margin

# Lowercase on purpose. Arduino_Slave_Code_v2 only recognises "kill"; v3 and the
# repo firmware accept "KILL" or "kill". Uppercase would be silently ignored by a
# v2 board, leaving the contactor closed while the heartbeat keeps it alive.
KILL = b"kill\n"


class _Capture:
    """Stands in for the serial port so we can read what the rig would send"""

    data = b""

    def write(self, data):
        self.data = data


class Monitor:
    """State shared by the prompt, heartbeat and reader threads."""

    def __init__(self):
        self.lock = threading.Lock()        # one writer on the port at a time
        self.stop = threading.Event()
        self.paused = threading.Event()     # set by hbtest to starve the watchdog
        self.applied = threading.Event()    # set when the Arduino echoes a new state
        self.heartbeats = 0
        self.last_heartbeat = None
        self.commands = 0
        self.last_kill = 0.0
        self.no_signal = 0                  # every "No Signal" line, expected or not
        self.resets = 0                     # unexpected ones only
        self.watchdogs = 0                  # unexpected ones only


def rig_encoding(steps):
    """The exact string control_logic sends for `steps`.

    Borrowed from the rig rather than re-implemented, so this tool always tests
    what the rig actually sends -- including after send_binary_command is edited.
    """
    cap = _Capture()
    send_binary_command(cap, steps)
    return cap.data.decode().strip()


def software_weights():
    """Ohms the software believes each character position switches."""
    weights = [None] * len(PIN_FOR_CHAR) #creates empty list same length as VRB relays
    for k in range(len(PIN_FOR_CHAR)):
        ones = [i for i, bit in enumerate(rig_encoding(2 ** k)) if bit == "1"]
        if len(ones) == 1 and ones[0] < len(weights):
            weights[ones[0]] = RESISTOR_RESOLUTION * 2 ** k
    return weights


def describe(pattern, weights):
    """Which pins leave their resistor IN circuit, and what the meter should read.

    Firmware: '1' drives a relay OPEN, which stops bypassing its resistor and puts
    it in circuit. '0' closes the bypass and shorts that resistor out.
    """
    if set(pattern) == {"0"}:
        pattern = pattern[:-1] + "1"   # firmware interlock rewrites the last character
    ones = [i for i, bit in enumerate(pattern) if bit == "1"]
    pins = ", ".join(str(PIN_FOR_CHAR[i]) for i in ones) or "none"
    if ones and all(weights[i] is not None for i in ones):
        expect = f"{sum(weights[i] for i in ones):.2f} ohm"
    else:
        expect = "unknown"
    return pins, expect


def send(ser, mon, pattern, weights, label):
    pins, expect = describe(pattern, weights)
    mon.applied.clear()
    with mon.lock:
        ser.write((pattern + "\n").encode())
    mon.commands += 1
    print(f"  {label}  ->  sent {pattern}")
    print(f"  resistor(s) IN circuit on pin: {pins}")
    print(f"  meter across the ladder should read: {expect}")
    if not mon.applied.wait(APPLY_TIMEOUT_S):
        print(f"  !! not applied: no 'New State Received' within {APPLY_TIMEOUT_S:g} s. Either the\n"
              "     Arduino was already in this state, or it reset / never heard the command.")


def send_kill(ser, mon):
    mon.last_kill = time.time()
    with mon.lock:
        ser.write(KILL)


def walk(ser, mon, weights):
    """Put one resistor in circuit at a time so each relay can be metered alone."""
    for i in range(len(PIN_FOR_CHAR)):
        pattern = "".join("1" if j == i else "0" for j in range(len(PIN_FOR_CHAR)))
        print(f"\n  [{i + 1}/{len(PIN_FOR_CHAR)}]")
        send(ser, mon, pattern, weights, f"relay on pin {PIN_FOR_CHAR[i]}")
        if input("  measure, then Enter for next (s to stop) > ").strip().lower() == "s":
            return


def heartbeat(ser, mon):
    """Send "alive" every HEARTBEAT_S.

    The firmware never answers it. The evidence it arrives is that the 2 s
    watchdog never fires between commands -- which hbtest demonstrates.
    """
    while not mon.stop.is_set():
        if not mon.paused.is_set():
            with mon.lock:
                try:
                    ser.write(b"alive\n")
                except serial.SerialException as exc:
                    # Loud on purpose. A heartbeat that dies quietly lets the
                    # watchdog drop the contactor 2 s later with nothing on
                    # screen to say why.
                    print(f"\n  !! HEARTBEAT STOPPED ({exc}). The Arduino will shed load "
                          "within 2 s. Restart the script.")
                    return
            mon.heartbeats += 1
            mon.last_heartbeat = time.time()
        mon.stop.wait(HEARTBEAT_S)


def reader(ser, mon):
    while not mon.stop.is_set():
        try:
            line = ser.readline()
        except serial.SerialException as exc:
            print(f"\n  !! LOST THE SERIAL PORT ({exc}). USB dropped out -- a board that "
                  "resets hard enough can take its USB link with it. Restart the script.")
            return
        if line:
            classify(line.decode(errors="ignore").strip(), mon)


def classify(text, mon):
    """Print one Arduino line, and call out the ones that explain a dead relay."""
    print(f"    [arduino] {text}")

    if text.startswith("New State Received"):
        mon.applied.set()

    elif text.startswith("Arduino Ready"):
        # setup() prints this, so it appears once at startup when opening the
        # port resets the board. After a command has been sent it means the
        # board rebooted mid-test.
        if mon.commands:
            mon.resets += 1
            print("  !! ARDUINO RESET: the board rebooted, which drops every relay and the main\n"
                  "     contactor. Straight after a command, that's a brownout or EMI when the\n"
                  "     contactor or relay coils energise -- not the heartbeat.")

    elif text.startswith("No Signal"):
        # shedAllLoad() prints this for the watchdog AND for an explicit kill,
        # and once at boot before the first message arrives.
        mon.no_signal += 1
        if mon.paused.is_set():
            print("     (expected: hbtest paused the heartbeat)")
        elif time.time() - mon.last_kill < 1.0 or not mon.commands:
            pass
        else:
            mon.watchdogs += 1
            print("  !! WATCHDOG FIRED: nothing reached the Arduino for 2 s, so it shed load.\n"
                  "     This IS a heartbeat problem.")


def heartbeat_test(mon):
    """Starve the watchdog on purpose, to prove the heartbeat is what holds it off."""
    print(f"  pausing the heartbeat for {HBTEST_PAUSE_S:g} s -- the Arduino should report "
          "'No Signal' about 2 s in and shed load")
    before = mon.no_signal
    mon.paused.set()
    time.sleep(HBTEST_PAUSE_S)
    fired = mon.no_signal > before
    mon.paused.clear()
    if fired:
        print("  PASS: the watchdog fired only once the heartbeat stopped, so the heartbeat\n"
              "        was reaching the Arduino. Load is shed; send a command to re-apply.")
    else:
        print("  FAIL: no 'No Signal' reply. Either the Arduino isn't running the watchdog\n"
              "        firmware, or its replies aren't reaching this PC.")


def print_status(mon, hb_thread, rd_thread):
    ago = f"{time.time() - mon.last_heartbeat:.1f} s ago" if mon.last_heartbeat else "never"
    print(f"  heartbeat thread {'running' if hb_thread.is_alive() else 'DEAD'}: "
          f"{mon.heartbeats} sent, last {ago}")
    print(f"  reader thread    {'running' if rd_thread.is_alive() else 'DEAD'}")
    print(f"  since the first command: {mon.resets} Arduino reset(s), "
          f"{mon.watchdogs} watchdog trip(s)")
    print("  (the firmware never answers 'alive' -- hbtest proves it's arriving)")


def open_port(port):
    if port is None:
        print("Scanning COM ports for RESISTOR_CTRL (about 2 s per port)...")
        ser = auto_detect_resistor()
        if ser is None:
            raise SystemExit("Resistor Arduino not found. Plug it in, or pass --port "
                             "(Arduino_Slave_Code_v2 doesn't answer ?WHOAMI, so it can't be "
                             "auto-detected).")
        return ser

    ser = serial.Serial(port, RESISTOR_BAUD_RATE, timeout=2)
    time.sleep(2)              # opening the port resets the Arduino; wait out the bootloader
    ser.reset_input_buffer()
    ser.write(b"?WHOAMI\n")
    reply = ser.readline().decode(errors="ignore").strip()
    if reply != "RESISTOR_CTRL":
        print(f"  ! {port} answered {reply!r}, not RESISTOR_CTRL -- is this the right board?")
    ser.timeout = 0.1
    return ser


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", help="e.g. COM5; omit to auto-detect")
    args = parser.parse_args()

    print("Battery DISCONNECTED?  12 V relay supply ON?")
    print("The first command closes the main contactor.\n")

    weights = software_weights()
    print("What the software believes each relay switches (from send_binary_command):")
    for i, w in enumerate(weights):
        print(f"  char {i}  pin {PIN_FOR_CHAR[i]:>2}  ->  {f'{w:g} ohm' if w is not None else '?'}")
    print()

    ser = open_port(args.port)
    mon = Monitor()
    hb_thread = threading.Thread(target=heartbeat, args=(ser, mon), daemon=True)
    rd_thread = threading.Thread(target=reader, args=(ser, mon), daemon=True)
    hb_thread.start()
    rd_thread.start()
    print(f"Heartbeat running ('alive' every {HEARTBEAT_S:g} s). It's silent: no WATCHDOG FIRED "
          "warning means it's working.")

    try:
        while True:
            line = input("\nohms | raw XXXXXXXX | walk | status | hbtest | kill | q > ").strip().lower()
            if not line:
                continue
            if line == "q":
                break
            if line == "kill":
                send_kill(ser, mon)
                print("  kill sent: every relay open, main contactor open")
                continue
            if line == "walk":
                walk(ser, mon, weights)
                continue
            if line == "status":
                print_status(mon, hb_thread, rd_thread)
                continue
            if line == "hbtest":
                heartbeat_test(mon)
                continue
            if line.startswith("raw"):
                pattern = line[3:].strip()
                if len(pattern) != len(PIN_FOR_CHAR) or set(pattern) - {"0", "1"}:
                    print("  raw needs exactly 8 characters of 0/1, e.g. raw 00010000")
                    continue
                if set(pattern) == {"0"}:
                    print("  expect the Arduino to reply 'SAFETY ACTION' and apply 00000001")
                send(ser, mon, pattern, weights, f"raw {pattern}")
                continue

            try:
                ohms = float(line)
            except ValueError:
                print("  not a number or a command")
                continue
            steps = resistance_to_steps(min(MAX_RESISTANCE, ohms))
            actual = steps * RESISTOR_RESOLUTION
            label = f"commanded {actual:.2f} ohm"
            if abs(actual - ohms) > 1e-9:
                label += f" (nearest step to {ohms:g})"
            send(ser, mon, rig_encoding(steps), weights, label)

    except (KeyboardInterrupt, EOFError):
        print()
    finally:
        # KILL only. Never follow it with 00000000: that is a valid command (the
        # firmware interlock turns it into 00000001), so it re-closes the main
        # contactor with a resistor in circuit until the 2 s watchdog fires.
        # Isolated Master Sender.py and Master_Code_v1.py both do that.
        try:
            send_kill(ser, mon)
            time.sleep(0.3)    # let the Arduino's reply print
        except serial.SerialException:
            pass
        mon.stop.set()
        ser.close()
        print("kill sent, port closed.")


if __name__ == "__main__":
    main()
