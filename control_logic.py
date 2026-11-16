import csv
import math
import os
import threading
import time
import serial
import serial.tools.list_ports
import pandas as pd
from multiprocessing import Queue, Event
from queue import Empty

# VehicleParams lives in rig_config so every user-tunable value sits in one
# place. Re-exported here because callers and tests import it from this module.
from rig_config import RigConfig, VehicleParams, PackConfig, SafetyLimits  # noqa: F401
from rig_config import arm_blockers
from queue_util import put_latest

# ================= CONFIGURATION =================
RESISTOR_BAUD_RATE = 9600

# Resolved relative to this file so the rig runs from any checkout on any
# machine. An absolute path here would break for every future team.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# Lap profiles live in profiles/. The GUI's Load CSV dialog opens there too.
PROFILES_DIR = os.path.join(PROJECT_DIR, "profiles")
DEFAULT_LAP_CSV = "FSAE - ETS - Speed and Time 1 Lap.csv"
CSV_FILENAME = os.path.join(PROFILES_DIR, DEFAULT_LAP_CSV)

# A bench profile names the resistance itself, in this column, instead of a
# speed for the car model to turn into power. It is for running the rig on bench
# supplies, where there is no car to model, and for constant-resistance
# discharges. Every row is held to the same floors and ratings as a lap row, so
# it can ask for no more load than a lap at full demand can.
RESISTANCE_COLUMN = "Resistance (ohm)"

# Every stop and fault the logic process latches, one row each, appended across
# sessions. logs/ is ignored by git, like the GUI's recordings.
TRIP_LOG_PATH = os.path.join(PROJECT_DIR, "logs", "trip_log.csv")
TRIP_LOG_FIELDS = ("Time", "State", "Reason", "Detail", "Amps", "Module_V", "Min_Cell_V",
                   "Max_Cell_Temp_C", "Resistor_Temps_C", "Ladder_Ohm",
                   "Sample_To_Kill_ms", "Kill_Sent")

MAX_RESISTANCE = 63.75
RESISTOR_RESOLUTION = 0.25
RESISTOR_SCAN_COOLDOWN = 3.0

# The resistor bank's power rating. At the 0.25 ohm floor only bank 1 -- four
# 2 kW elements in parallel -- is in circuit, so it carries the peak duty
# (docs/hardware_topology.md). A property of the hardware, like the two ladder
# values above, and deliberately NOT a rig_config field: every field there can
# be edited from the GUI. The cells may support more current; the bank does not.
VRB_MAX_POWER_W = 8000.0

# Each bank's resistance and continuous rating, bank 1 first, from the bank
# table in docs/hardware_topology.md. Hardware, like VRB_MAX_POWER_W.
#
# The ladder is in series and each bank's relay bypasses it, so a command of N
# steps puts in circuit exactly the banks whose bits are set in N (bank 1 is
# bit 0), and every one of them carries the same current. The 8 kW total does
# not protect them: 0.5 ohm is bank 2 alone, 5.1 kW at 50.4 V against its 4 kW.
BANK_RESISTANCE_OHM = tuple(RESISTOR_RESOLUTION * 2 ** k for k in range(8))
BANK_RATED_POWER_W = (8000.0, 4000.0, 2000.0, 1000.0, 500.0, 300.0, 200.0, 400.0)

# Which bank each character of a resistance command switches, and the Arduino
# pin that switches each bank. The one place the wire format is defined:
# encode_steps() builds every command from it, tools/bench_relay_check.py prints
# it, and tests/test_firmware_bank_map.py runs the real firmware to prove it
# matches resistor_bank_controller.ino for every one of the 255 settings.
#
# The word is written most significant bank first, so it reads as the binary
# number of 0.25 ohm steps: 32 ohm is 10000000, 0.25 ohm is 00000001. The
# firmware drives characters 0-6 onto otherRelayPins[] = {12, 10, 11, 9, 8, 7, 6}
# and the last character onto BANK_1_RELAY, pin 5, which it labels as the
# 0.25 ohm relay and opens first on every change. The host used to send the word
# reversed, least significant first, so 32 ohm arrived as 00000001 and switched
# in bank 1 alone: about 202 A at 50.4 V instead of 1.6 A.
#
# Which pin's relay bypasses which bank is the relay board's wiring, and only a
# meter proves it: `python tools/bench_relay_check.py` and `walk`, battery
# disconnected, before a run and after any rewiring. Metered in October 2026:
# pin 10 bypasses the 16 ohm bank (7) and pin 11 the 8 ohm bank (6), the other
# way round from the binary order, so the firmware and this table swap them.
COMMAND_BANKS = (8, 7, 6, 5, 4, 3, 2, 1)
BANK_RELAY_PIN = (5, 6, 7, 8, 9, 11, 10, 12)    # bank 1 first

# How far past the ratings above, and past VRB_MAX_POWER_W, the bank may run
# unless SafetyLimits.bank_rated_power_only holds it to 100%. Fixed here, so the
# config can choose between the two figures but not raise either.
#
# 140% so that no setting is ever refused at a full 50.4 V module: each of
# banks 1-5 alone at 50.4 V needs 133.7% with its elements at the low end of
# tolerance (127% at nominal resistance). The ratings are continuous figures at
# 70 C ambient in free air, a lap holds full power for about 30 s at most against
# element time constants of minutes, and the resistor thermocouple trips, not
# this figure, are what bound a longer overload. Banks 5 and 6 have no
# thermocouple, and bank 4's trip is a placeholder.
BANK_OVERLOAD_FACTOR = 1.40

# The per-bank overpower trip ignores a new ladder setting for this long. The
# firmware takes ~100 ms to step the relays and the DAQ samples every 100 ms, so
# until then a current reading belongs to the previous setting, and pairing it
# with the new one would mis-share the power between banks. The 8 kW total trip
# needs no setting and is never held off.
BANK_SETTLE_S = 0.5

# Bank 1's elements are +/-5% parts (TE2000B1R0J, the J). One 5% under its
# nominal resistance draws 5% more power at the same voltage, so the resistance
# floor assumes that worst case rather than tripping BANK OVERPOWER mid-run.
RESISTOR_TOLERANCE = 0.05

# The resistor Arduino sheds load after 2 s without an "alive" or a valid
# command. 0.5 s gives four chances before it fires.
HEARTBEAT_INTERVAL_S = 0.5

# A write the OS cannot take within this long fails rather than blocking the
# safety loop: with pyserial's default of no timeout, a controller that stopped
# reading would leave write() waiting forever. A healthy write takes
# milliseconds; the Arduino's watchdog is 2 s.
RESISTOR_WRITE_TIMEOUT_S = 0.25


# Module-level default. Mutating a field here retunes every call that does not
# pass an explicit params object.
DEFAULT_VEHICLE = VehicleParams()


def auto_detect_resistor(discovery_lock=None):
    """Sweep COM ports for the resistor controller. Blocks ~2 s per port.

    `discovery_lock` serialises probing against hardware_manager, which sweeps
    the same ports looking for the temperature Arduino. Without it the two
    processes collide: whichever opens a port second gets an access denial, and
    each open/close toggles DTR and resets whatever Arduino is on the far end.
    Held for the whole sweep rather than per port, so a probe cannot land between
    another process's open and its close.

    NEVER call this from the main loop -- it is slow enough to stall the safety
    checks and the E-STOP path. run_logic_process runs it on a worker thread.
    """
    if discovery_lock is not None:
        with discovery_lock:
            return _sweep_for_resistor()
    return _sweep_for_resistor()


def _sweep_for_resistor():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        try:
            ser = serial.Serial(port.device, RESISTOR_BAUD_RATE, timeout=2)
            time.sleep(2)
            ser.reset_input_buffer()
            ser.write(b"?WHOAMI\n")
            response = ser.readline().decode('utf-8').strip()
            if response == "RESISTOR_CTRL":
                # Keep the handle we already have. Closing and reopening toggles
                # DTR, which hard-resets the Arduino into its bootloader for
                # ~2 s, and every command sent in that window -- including the
                # heartbeat that stops its watchdog shedding load -- is lost.
                # hardware_manager's watchdog already avoids this for the temp
                # sensor; this is the same trap.
                ser.timeout = 0.1
                ser.write_timeout = RESISTOR_WRITE_TIMEOUT_S
                print(f"[LOGIC] Resistor Bank secured on {port.device}")
                return ser
            ser.close()
        except Exception:
            pass
    return None


def write_resistor(ser, payload):
    """Write one line to the resistor controller. True if the port took it.

    False, never an exception, when there is no handle, the port has gone, or
    the write timed out. Every write the logic process makes goes through here,
    and the loop treats False as a lost link: it closes the handle, re-arms
    discovery, and faults a loaded rig. Failures used to be swallowed, leaving
    the GUI showing a connected controller and RUNNING while nothing reached the
    Arduino, and the KILL writes were not guarded at all.

    True only means the OS accepted the bytes. The firmware does not answer
    "alive", so a controller that has hung while still enumerated is not caught
    here; its own 2 s watchdog sheds the load then.
    """
    if ser is None:
        return False
    try:
        ser.write(payload)
        return True
    except (serial.SerialException, OSError):
        return False


# What the resistor controller prints that the host acts on, as the start of the
# line (arduino/resistor_bank_controller). The rest of what it prints is
# progress chatter: "New State Received", "Action: ...".
#
# The host used to write to the controller and never read it. A board that
# restarted mid-run dropped every relay, came back two seconds later, took the
# next row's command and carried on, with nothing on record.
CONTROLLER_LINES = (
    ("Arduino Ready", "reset"),     # setup() ran: the board restarted
    ("No Signal", "shed"),          # shedAllLoad(): its watchdog fired, or a KILL
    ("Error:", "rejected"),         # a line it would not act on
)

# Opening the port restarts the board, and its start-up banner can arrive after
# the handshake has already claimed the port. A banner this soon after is that
# start, not a restart.
CONTROLLER_BOOT_S = 5.0

# Longest part-line kept between passes; a real line is under 60 bytes.
MAX_CONTROLLER_PENDING = 512


def read_controller_lines(ser, pending):
    """Complete lines the controller has printed, without waiting: (lines, pending).

    None, not the pair, if the port failed: the loop treats that as a lost
    link, like a failed write. Only what has already arrived is read, so this
    never holds the loop; a line still arriving stays in `pending`. A port with
    nothing to read from (the test fakes) reads as silent.
    """
    try:
        waiting = getattr(ser, 'in_waiting', 0)
        if waiting:
            pending += ser.read(waiting)
    except (serial.SerialException, OSError):
        return None
    *raw, pending = pending.split(b"\n")
    if len(pending) > MAX_CONTROLLER_PENDING:
        pending = b""
    return [line.decode('utf-8', errors='ignore').strip() for line in raw], pending


def classify_controller_line(line):
    """'reset', 'shed' or 'rejected' for a line the host acts on, else None."""
    for start, kind in CONTROLLER_LINES:
        if line.startswith(start):
            return kind
    return None


def encode_steps(steps):
    """The command word for a ladder setting: one character per bank, COMMAND_BANKS order.

    '1' puts that bank in circuit. Zero steps becomes bank 1 alone (0.25 ohm), as
    the firmware's own all-zero interlock would, so the bank is never commanded
    to zero resistance.
    """
    if steps == 0:
        steps = 1
    return "".join("1" if (steps >> (bank - 1)) & 1 else "0" for bank in COMMAND_BANKS)


def decode_word(word):
    """The ladder setting, in steps, that a command word puts in circuit."""
    return sum(2 ** (bank - 1) for bank, bit in zip(COMMAND_BANKS, word) if bit == "1")


def send_binary_command(ser, steps):
    """Send a ladder setting. False if the write failed; see write_resistor()."""
    return write_resistor(ser, (encode_steps(steps) + '\n').encode('utf-8'))


# ================= PURE SAFETY / PHYSICS LOGIC =================
# Extracted so this logic can be unit-tested without a running GUI,
# Arduino, or DAQ.
#Double Checked
def check_thermal_and_current(max_temp, amps, max_safe_temp, max_safe_current, current_buffer):
    """The two trips that apply in every FSM state, armed or not.

    Returns (is_fault, reason) where reason is 'OVERTEMP', 'OVERCURRENT' or None.
    Temperature outranks current so the reported cause is deterministic when both
    trip at once.

    Undervoltage deliberately lives in evaluate_safety() rather than here: it is
    only meaningful once the rig is armed, and combining the three in one
    function meant two places decided the same thresholds.
    """
    if max_temp >= max_safe_temp:
        return True, "OVERTEMP"
    if amps >= (max_safe_current + current_buffer):
        return True, "OVERCURRENT"
    return False, None


def bank_power_factor(limits):
    """1.0 when the limits hold the bank to rated power, else BANK_OVERLOAD_FACTOR.

    A missing key reads as rated power: fail safe.
    """
    return 1.0 if limits.get('bank_rated_only', True) else BANK_OVERLOAD_FACTOR


def banks_in_circuit(steps):
    """0-based indices of the banks a command of `steps` puts in circuit."""
    return [k for k in range(len(BANK_RESISTANCE_OHM)) if (steps >> k) & 1]


def bank_power_shares(steps, total_watts):
    """Split `total_watts` across the banks in circuit, as (bank index, watts).

    Series, so every bank carries the same current and takes the fraction of the
    power that it is of the resistance. A tolerance shared by every element
    cancels out of that fraction.
    """
    r_total = steps * RESISTOR_RESOLUTION
    return [(k, total_watts * BANK_RESISTANCE_OHM[k] / r_total) for k in banks_in_circuit(steps)]


def overpowered_banks(voltage, amps, steps, power_factor=1.0):
    """(bank, watts, limit) for every bank in circuit past its rating; banks 1-based."""
    return [(k + 1, watts, BANK_RATED_POWER_W[k] * power_factor)
            for k, watts in bank_power_shares(steps, voltage * amps)
            if watts > BANK_RATED_POWER_W[k] * power_factor]


def check_bank_power(voltage, amps, steps=None, power_factor=1.0):
    """Resistor bank overload. Returns (is_fault, reason).

    Two checks, both against fixed hardware ratings scaled by power_factor (1.0,
    or BANK_OVERLOAD_FACTOR when not held to rated power), never against the
    configurable current limit: that derives from the cells, and with high-rate
    cells it sits well above anything the bank can absorb.

      * The whole ladder against VRB_MAX_POWER_W. Module voltage times current is
        never less than what the bank itself dissipates, since wiring and
        contact drops come out of the same total. Needs no ladder setting, so
        it applies in every state.
      * Each bank against its own rating, which needs `steps`, the setting in
        circuit. The 8 kW total let bank 2 alone run at 5.1 kW against its 4 kW.

    The command selection (see command_steps) keeps a healthy rig under both at
    steady state, so a trip here means something it could not see: an element
    out of tolerance, a relay stuck, or a bad reading.
    """
    if voltage * amps > VRB_MAX_POWER_W * power_factor:
        return True, "BANK OVERPOWER"
    if steps and overpowered_banks(voltage, amps, steps, power_factor):
        return True, "BANK OVERPOWER"
    return False, None


# The load check: whether the current measured fits the ladder setting commanded.
#
# Nothing else compares the two. Both wiring faults found so far were of this
# kind, and both were found with a meter: the command word sent in reverse
# (202 A where 1.6 A was asked for) and the 8 and 16 ohm relays crossed. It
# also catches a controller that restarted, a contactor or relay that did not
# move, and a current sensor that has stopped reading, which leaves the
# over-current and bank-power trips blind.
#
# More current than the setting allows has no innocent cause: the module
# voltage is measured at the taps, so every element at the low end of its
# tolerance is +5% at most. Less has several: cable, contactor and relay
# contacts in series with the ladder, elements at the top of their tolerance,
# and their resistance rising as they heat. About -23% at the 0.25 ohm step
# with 40 mOhm of wiring, so the low side is the looser.
#
# The floor in amps is for the top of the ladder, where an amp or two is the
# 400 A sensor's own offset rather than the bank. It is low enough to see the
# crossed 8 and 16 ohm relays (3 A against 6 A at 48 V), and it means a wrong
# bank above about 16 ohm, where a volt or two moves less than 2 A, is not seen.
LOAD_CHECK_HIGH = 0.15
LOAD_CHECK_LOW = 0.35
LOAD_CHECK_ABS_A = 2.0
# Readings in a row that must disagree before it faults, each from a new DAQ
# sample taken after the setting had settled (BANK_SETTLE_S). 0.3 s at 10 Hz.
LOAD_CHECK_SAMPLES = 3


def expected_load_amps(voltage, steps):
    """The current a ladder setting draws at this module voltage. None if unknown."""
    if not steps or not _finite(voltage):
        return None
    return voltage / (steps * RESISTOR_RESOLUTION)


def load_mismatch(voltage, amps, steps):
    """How one reading disagrees with the ladder setting, in words. None if it fits.

    `steps` is the setting in circuit once it has settled. None also when there
    is nothing to judge: no setting, or a reading that is not a number, which
    INVALID READING reports.
    """
    expected = expected_load_amps(voltage, steps)
    if expected is None or not _finite(amps):
        return None
    ohms = steps * RESISTOR_RESOLUTION
    line = (f"Ladder at {ohms:.2f} ohm and {voltage:.1f} V should draw {expected:.1f} A; "
            f"measured {amps:.1f} A")
    if amps - expected > max(LOAD_CHECK_ABS_A, LOAD_CHECK_HIGH * expected):
        return line + ". More: a bank that should be in circuit is bypassed"
    if expected - amps > max(LOAD_CHECK_ABS_A, LOAD_CHECK_LOW * expected):
        return line + (". Less: the contactor or a relay is open, the controller "
                       "restarted, or the current sensor is not reading")
    return None


def check_cell_safety(cell_voltages, min_cell_voltage, sense_floor=0.5):
    """Per-cell undervoltage. Returns (is_fault, reason).

    A module-total trip cannot see a single weak cell: eleven cells at 3.40 V
    plus one at 1.00 V totals 38.4 V, above a 36.0 V module trip, while that one
    cell is destroyed.

    Readings below sense_floor are reported as CELL SENSE FAULT rather than
    undervoltage. A genuinely flat cell and an unplugged sense lead look
    identical from here, so neither is ever treated as healthy -- but naming them
    differently tells the operator where to look.
    """
    if not cell_voltages:
        # Fail closed. An empty array means the DAQ produced no per-cell data at
        # all -- a broken harness, an empty channel list, or a parse failure --
        # and running a high-power profile blind to cell voltage is exactly what
        # this check exists to prevent.
        return True, "NO CELL DATA"

    lowest = min(cell_voltages)
    if lowest < sense_floor:
        return True, "CELL SENSE FAULT"
    if lowest <= min_cell_voltage:
        return True, "CELL UNDERVOLTAGE"
    return False, None


def check_daq_health(daq_age_s, stale_timeout_s):
    """Telemetry freshness. Returns (is_fault, reason).

    The logic loop keeps running on the last packet when the DAQ stops feeding
    it, so without this a hung or crashed DAQ leaves every other check
    evaluating frozen values that still look plausible.
    """
    if stale_timeout_s > 0 and daq_age_s > stale_timeout_s:
        return True, "DAQ DATA STALE"
    return False, None


def check_ni_daq(hardware_status):
    """Whether current and voltage are being measured. Returns (is_fault, reason).

    Every current and voltage reading comes from the NI-DAQ. hardware_manager
    used to fill in a simulated healthy pack when the DAQ failed to start, so
    the overcurrent, undervoltage and cell trips watched made-up numbers while
    the bank loaded the real module. It now publishes zero readings with 'ni_daq'
    False instead, and ARM is refused on them. A DAQ lost after arming faults
    here on the next packet rather than waiting for DAQ DATA STALE.

    SIL mode reports True from its plant model, where simulated data is the
    point, and False when its hardware fault toggle is ticked.
    """
    if not hardware_status.get('ni_daq', False):
        return True, "NI-DAQ OFFLINE"
    return False, None


def check_sensor_health(temp_age_s, temp_link_ok, stale_timeout_s):
    """Temperature data integrity. Returns (is_fault, reason).

    The DAQ republishes its last temperature array when the sensor link drops,
    so a dead sensor is indistinguishable from a steady pack. Without this check
    a link lost at 52 C freezes the reading at 52 C and the overtemp trip can
    never fire while the cells keep heating.
    """
    if not temp_link_ok:
        return True, "TEMP LINK LOST"
    if stale_timeout_s > 0 and temp_age_s > stale_timeout_s:
        return True, "TEMP DATA STALE"
    return False, None


def stale_temp_sensors(sensor_ages, stale_timeout_s):
    """1-based (bus, sensor) positions with no valid reading within the timeout.

    Numbered the way the Arduino prints them, so the operator can match the
    console message against the bus lines and the sketch's address tables.
    """
    if stale_timeout_s <= 0:
        return []
    return [(b + 1, s + 1)
            for b, bus in enumerate(sensor_ages)
            for s, age in enumerate(bus)
            if age > stale_timeout_s]


def check_temp_sensors(sensor_ages, stale_timeout_s):
    """Per-sensor temperature integrity. Returns (is_fault, reason).

    The Arduino prints ERR for a DS18B20 it cannot read, and the DAQ keeps that
    sensor's last value rather than writing a zero. The other sensors keep the
    stream as a whole fresh, so neither TEMP LINK LOST nor TEMP DATA STALE
    fires -- while one cell's reading sits frozen at a safe-looking value and
    the cell itself keeps heating.

    sensor_ages is the DAQ's [bus][sensor] array of seconds since each sensor's
    last valid reading. The timeout is the same one the stream-wide check uses,
    so an occasional ERR from OneWire noise does not trip the rig, but a sensor
    that stays dead does.
    """
    if not any(sensor_ages):
        # Fail closed, as with NO CELL DATA: an empty array means per-sensor
        # validity is not being reported at all, so no sensor can be trusted.
        return True, "NO TEMP SENSOR DATA"
    if stale_temp_sensors(sensor_ages, stale_timeout_s):
        return True, "TEMP SENSOR FAULT"
    return False, None


def resistor_limit(max_temps, bank_idx):
    """Trip temperature for the bank at 0-based bank_idx.

    A bank with a thermocouple but no trip of its own gets the strictest one
    configured, and with none configured at all, -inf: any reading trips.
    Mismatched lists are a config error that RigConfig.validate() reports; this
    keeps it from leaving a bank unprotected meanwhile.
    """
    if bank_idx < len(max_temps):
        return max_temps[bank_idx]
    return min(max_temps, default=float('-inf'))


def hot_resistor_banks(resistor_temps, max_temps):
    """(bank, temperature, trip) for every bank at or over its trip; banks 1-based."""
    return [(i + 1, t, resistor_limit(max_temps, i))
            for i, t in enumerate(resistor_temps)
            if t is not None and t >= resistor_limit(max_temps, i)]


def check_resistor_temps(resistor_temps, max_temps):
    """Resistor bank over-temperature. Returns (is_fault, reason).

    Applies in every FSM state, like the cell over-temperature trip: a hot bank
    is hot whatever the state machine thinks. A bank that has not reported yet
    (None) is not judged here; check_resistor_tc_health() faults on it once armed.
    """
    if hot_resistor_banks(resistor_temps, max_temps):
        return True, "RESISTOR OVERTEMP"
    return False, None


def stale_resistor_banks(tc_ages, stale_timeout_s):
    """1-based banks whose thermocouple has no valid reading within the timeout."""
    if stale_timeout_s <= 0:
        return []
    return [i + 1 for i, age in enumerate(tc_ages) if age > stale_timeout_s]


def check_resistor_tc_health(tc_ages, stale_timeout_s):
    """Resistor thermocouple integrity. Returns (is_fault, reason).

    The over-temperature trip can only see a reading that arrives. An open
    thermocouple, a failed read or a missing module leaves the last value frozen
    while the bank keeps heating, so once armed a bank that stops reporting is a
    fault in its own right. Same timeout as the cell sensors.
    """
    if not tc_ages:
        # Fail closed: no thermocouple data at all means the banks are unwatched.
        return True, "NO RESISTOR TEMP DATA"
    if stale_resistor_banks(tc_ages, stale_timeout_s):
        return True, "RESISTOR TC FAULT"
    return False, None


# Readings past these cannot be real, and each lies in the direction that hides
# a danger, so measurement_problems() refuses them rather than trusting them.
#
# The bench only discharges. A few amps of negative offset is calibration;
# beyond this the transducer is reversed, miswired or saturated, and over-current
# can no longer be seen.
CURRENT_MIN_PLAUSIBLE_A = -20.0
# The DS18B20's measurable range starts here. Below it, a reading would make the
# hottest cell look cooler than it is.
CELL_TEMP_MIN_PLAUSIBLE_C = -55.0
# Above a full module by this fraction, or a full cell by this many volts, the
# tap or divider is wrong, and a reading that high can never trip undervoltage.
# Loose on purpose: per-cell voltages are differences of two divided taps, so an
# honest calibration error can approach half a volt at the top of the stack.
VOLTAGE_PLAUSIBLE_MARGIN = 0.10
CELL_PLAUSIBLE_MARGIN_V = 0.8


def _is_number(x):
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _finite(x):
    return _is_number(x) and math.isfinite(x)


def expected_measurements(daq_cfg, pack):
    """What a complete, believable packet holds for this wiring and pack.

    Counts come from the wiring the DAQ is running, except the cells, which come
    from the pack: they are what must be watched, and a channel count that
    disagrees is already a config error. None if the config cannot say, in which
    case config_blockers() is already refusing ARM.
    """
    try:
        return {
            'cells': int(pack.series_count),
            'temp_sensors': int(daq_cfg.sensor_count),
            'resistor_tcs': len(daq_cfg.resistor_tc_channels),
            'max_volts': float(pack.max_voltage) * (1.0 + VOLTAGE_PLAUSIBLE_MARGIN),
            'max_cell_volts': float(pack.cell_max_voltage) + CELL_PLAUSIBLE_MARGIN_V,
        }
    except Exception:
        return None


def measurement_problems(data, expected=None):
    """Readings in one packet that cannot be trusted, as (reason, detail) pairs.

    INVALID READING: not a number, NaN or infinite, or past what the hardware can
    really read. NaN loses every comparison, so a NaN current, voltage or cell
    used to pass every trip as safe. INCOMPLETE DATA: a different number of
    readings from what the wiring delivers, so some cells or banks are not being
    checked at all; a packet with one cell, one sensor and one thermocouple used
    to pass. Empty arrays are left to NO CELL DATA and its siblings, which name
    them. `expected` is expected_measurements(); without it only the checks that
    need no configuration run.
    """
    problems = []

    def invalid(detail):
        problems.append(("INVALID READING", detail))

    amps = data.get('amps', 0.0)
    volts = data.get('voltage', 0.0)
    max_temp = data.get('max_temp', 0.0)
    cells = data.get('cell_voltages', [])

    for name, value in (("current", amps), ("module voltage", volts),
                        ("max cell temperature", max_temp)):
        if not _finite(value):
            invalid(f"{name} reads {value!r}")
    bad = [i + 1 for i, v in enumerate(cells) if not _finite(v)]
    if bad:
        invalid("cell(s) " + ", ".join(map(str, bad)) + " are not a number")
    bad = [i + 1 for i, t in enumerate(data.get('resistor_temps', []))
           if t is not None and not _finite(t)]
    if bad:
        invalid("bank(s) " + ", ".join(map(str, bad)) + " temperature is not a number")

    # Ages may be inf -- never read -- but a NaN age passes every staleness test.
    ages = ([data.get('daq_age_s', 0.0), data.get('temp_age_s', 0.0)]
            + [a for bus in data.get('temp_sensor_ages_s', []) for a in bus]
            + list(data.get('resistor_temp_ages_s', [])))
    if any(not _is_number(a) or math.isnan(a) for a in ages):
        invalid("a reading's age is not a number")

    if _finite(amps) and amps < CURRENT_MIN_PLAUSIBLE_A:
        invalid(f"current {amps:.1f} A is below {CURRENT_MIN_PLAUSIBLE_A:.0f} A; "
                f"check the transducer's direction and wiring")
    if _finite(max_temp) and max_temp < CELL_TEMP_MIN_PLAUSIBLE_C:
        invalid(f"max cell temperature {max_temp:.1f} C is below what the sensors can read")

    if expected:
        if _finite(volts) and volts > expected['max_volts']:
            invalid(f"module voltage {volts:.1f} V is above {expected['max_volts']:.1f} V, "
                    f"more than a full module can read")
        high = [i + 1 for i, v in enumerate(cells)
                if _finite(v) and v > expected['max_cell_volts']]
        if high:
            invalid("cell(s) " + ", ".join(map(str, high))
                    + f" above {expected['max_cell_volts']:.2f} V, more than a full cell")

        n_sensors = sum(len(bus) for bus in data.get('temp_sensor_ages_s', []))
        for name, have, want in (
                ("cell voltages", len(cells), expected['cells']),
                ("temperature sensors", n_sensors, expected['temp_sensors']),
                ("resistor thermocouples", len(data.get('resistor_temp_ages_s', [])),
                 expected['resistor_tcs'])):
            if have and have != want:
                problems.append(("INCOMPLETE DATA", f"{have} {name} reported, {want} wired"))
    return problems


def check_measurements(data, expected=None):
    """measurement_problems() as (is_fault, reason): the first problem's reason."""
    problems = measurement_problems(data, expected)
    if problems:
        return True, problems[0][0]
    return False, None


def arm_refusals(hardware_status, config_problems):
    """Why ARM would be refused right now, one line each. Empty means it would arm.

    `config_problems` is arm_blockers() for the running wiring and the live pack
    and limits. Forwarded to the GUI as well, so the operator sees why ARM does
    nothing without reading the console.
    """
    reasons = []
    no_daq, reason = check_ni_daq(hardware_status)
    if no_daq:
        reasons.append(f"{reason}: current and voltage are not being measured.")
    return reasons + list(config_problems)


def config_blockers(daq_cfg, pack, limits):
    """rig_config.arm_blockers(), failing closed.

    The config is hand-edited JSON. A value of the wrong type (a quoted number,
    say) would raise inside the checks, and a configuration that cannot be
    checked must block ARM rather than take down the logic process.
    """
    try:
        return arm_blockers(daq_cfg, pack, limits)
    except Exception as exc:
        return [f"The configuration could not be checked ({exc!r}). Fix rig_config.json."]


def report_config_problems(problems):
    for problem in problems:
        print(f"[LOGIC CONFIG ERROR] {problem}")
    if problems:
        print("[LOGIC] The rig will not arm until the configuration errors above are fixed.")


def evaluate_safety(data, limits, armed=True, config_problems=(), bank_steps=None,
                    expected=None, load_problem=None):
    """All safety checks against one telemetry packet. Returns (is_fault, reason).

    Measured dangers are reported ahead of data-integrity faults: if the pack is
    genuinely over-current AND the temperature link has dropped, the operator
    needs to hear about the current first.

    `armed` should be True only in ARMED/RUNNING. Cell over-temperature,
    over-current, bank overpower and resistor over-temperature are always
    checked -- any of them means something is wrong no matter what state the FSM
    thinks it is in. Everything else is gated,
    because before the rig is armed those readings describe a bench that is not
    loaded yet:

      * Voltage checks. A rig powered up before the battery is plugged in reads
        0.0 V, which is below any sane undervoltage trip. Checking it in IDLE
        latches a FAULT the operator cannot clear -- RESET returns to IDLE and it
        immediately re-trips -- locking them out of software bring-up entirely.
      * NI-DAQ, cell and sensor data integrity. Missing data before the DAQ
        has connected is an ordinary not-connected-yet condition, not a fault.
        ARM itself is refused without the NI-DAQ (see check_ni_daq).

    Once armed, all of them apply, and missing data is a fault rather than a
    reason to skip a check.

    `config_problems` is arm_blockers() for the running configuration. ARM is
    refused while it is non-empty, so it only faults here when a config or limits
    change lands while already armed.

    `bank_steps` is the ladder setting in circuit, once it has settled (see
    BANK_SETTLE_S), for the per-bank overpower check. None skips that check and
    keeps the whole-ladder one.

    `expected` is expected_measurements() for the running wiring and pack, for
    the count and range checks in measurement_problems(). Like the other
    integrity checks they apply once armed.

    `load_problem` is load_mismatch()'s description once the measured current
    has disagreed with the ladder setting for LOAD_CHECK_SAMPLES readings in a
    row; the loop does the counting. Reported last: every other fault names a
    cause, and this one only says the bank is not doing what it was told.
    """
    fault, reason = check_thermal_and_current(
        data.get('max_temp', 0.0), data.get('amps', 0.0),
        limits['max_temp'], limits['max_amps'], limits['amp_buffer'],
    )
    if fault:
        return True, reason

    fault, reason = check_bank_power(data.get('voltage', 0.0), data.get('amps', 0.0),
                                     steps=bank_steps, power_factor=bank_power_factor(limits))
    if fault:
        return True, reason

    fault, reason = check_resistor_temps(
        data.get('resistor_temps', []), limits['resistor_max_temp'])
    if fault:
        return True, reason

    if not armed:
        return False, None

    # Ahead of the voltage checks: with the DAQ offline the module reads 0.0 V,
    # and UNDERVOLTAGE would send the operator to the battery instead.
    fault, reason = check_ni_daq(data.get('hardware_status', {}))
    if fault:
        return True, reason

    # Also ahead of the readings: a miswired channel list skews the module
    # voltage and the cells, and the cause is the useful thing to report.
    if config_problems:
        return True, "CONFIG FAULT"

    # Before any check reads a value: NaN would pass all of them, and a missing
    # cell can make the module read low and look like UNDERVOLTAGE.
    fault, reason = check_measurements(data, expected)
    if fault:
        return True, reason

    if data.get('voltage', 0.0) <= limits['min_volts']:
        return True, "UNDERVOLTAGE"

    fault, reason = check_cell_safety(
        data.get('cell_voltages', []), limits['min_cell_volts'], limits['cell_sense_floor'])
    if fault:
        return True, reason

    fault, reason = check_daq_health(
        data.get('daq_age_s', 0.0), limits['daq_stale_timeout'])
    if fault:
        return True, reason

    fault, reason = check_sensor_health(
        data.get('temp_age_s', 0.0),
        data.get('hardware_status', {}).get('temp_arduino', False),
        limits['temp_stale_timeout'],
    )
    if fault:
        return True, reason

    # After the link checks: a dropped link makes every sensor stale too, and
    # TEMP LINK LOST is the more useful thing to tell the operator.
    fault, reason = check_temp_sensors(
        data.get('temp_sensor_ages_s', []), limits['temp_stale_timeout'])
    if fault:
        return True, reason

    fault, reason = check_resistor_tc_health(
        data.get('resistor_temp_ages_s', []), limits['temp_stale_timeout'])
    if fault:
        return True, reason

    if load_problem:
        return True, "LOAD MISMATCH"

    return False, None


def trip_details(reason, data, limits, config_problems=(), expected=None, bank_steps=None):
    """What tripped and the limit it crossed, as lines for the console and the trip log.

    So an operator testing a trip can see it fired at the value it should, and
    one meeting a real trip knows where to look. `bank_steps` is the settled
    ladder setting, as evaluate_safety() took it. Empty for a reason with
    nothing to add.
    """
    amps = data.get('amps', 0.0)
    volts = data.get('voltage', 0.0)
    cells = data.get('cell_voltages', [])

    if reason == "OVERTEMP":
        return [f"Hottest cell at {data.get('max_temp', 0.0):.1f} C "
                f"(trip {limits['max_temp']:.1f} C)"]
    if reason == "OVERCURRENT":
        return [f"Current {amps:.1f} A (trip {limits['max_amps'] + limits['amp_buffer']:.1f} A)"]
    if reason == "BANK OVERPOWER":
        factor = bank_power_factor(limits)
        lines = [f"Ladder at {volts * amps:.0f} W (limit {VRB_MAX_POWER_W * factor:.0f} W)"]
        if bank_steps:
            lines += [f"Bank {b} at {w:.0f} W (limit {lim:.0f} W)"
                      for b, w, lim in overpowered_banks(volts, amps, bank_steps, factor)]
        return lines
    if reason == "RESISTOR OVERTEMP":
        hot = hot_resistor_banks(data.get('resistor_temps', []), limits['resistor_max_temp'])
        return [", ".join(f"Bank {b} at {t:.0f} C (trip {lim:.0f} C)" for b, t, lim in hot)]
    if reason == "UNDERVOLTAGE":
        return [f"Module at {volts:.2f} V (trip {limits['min_volts']:.2f} V)"]
    if reason == "CELL UNDERVOLTAGE":
        low = [(i + 1, v) for i, v in enumerate(cells)
               if _finite(v) and v <= limits['min_cell_volts']]
        return [", ".join(f"Cell {i} at {v:.3f} V" for i, v in low)
                + f" (trip {limits['min_cell_volts']:.2f} V)"]
    if reason == "CELL SENSE FAULT":
        low = [(i + 1, v) for i, v in enumerate(cells)
               if _finite(v) and v < limits['cell_sense_floor']]
        return [", ".join(f"Cell {i} reads {v:.3f} V" for i, v in low)
                + f" (below {limits['cell_sense_floor']:.2f} V: check its sense lead)"]
    if reason == "DAQ DATA STALE":
        return [f"Last DAQ packet {data.get('daq_age_s', 0.0):.2f} s old "
                f"(limit {limits['daq_stale_timeout']:.2f} s)"]
    if reason == "TEMP DATA STALE":
        return [f"Last temperature reading {data.get('temp_age_s', 0.0):.1f} s old "
                f"(limit {limits['temp_stale_timeout']:.1f} s)"]
    if reason == "TEMP SENSOR FAULT":
        dead = stale_temp_sensors(data.get('temp_sensor_ages_s', []), limits['temp_stale_timeout'])
        return ["No valid reading from: " + ", ".join(f"bus {b} sensor {s}" for b, s in dead)]
    if reason == "RESISTOR TC FAULT":
        dead = stale_resistor_banks(data.get('resistor_temp_ages_s', []),
                                    limits['temp_stale_timeout'])
        return ["No valid thermocouple reading from: " + ", ".join(f"bank {b}" for b in dead)]
    if reason == "CONFIG FAULT":
        return [f"  - {problem}" for problem in config_problems]
    if reason in ("INVALID READING", "INCOMPLETE DATA"):
        return [f"  - {detail}" for _, detail in measurement_problems(data, expected)]
    if reason == "LOAD MISMATCH":
        problem = load_mismatch(volts, amps, bank_steps)
        return [problem] if problem else []
    return []


def trip_record(reason, state, details, data=None, ladder_ohm=None, kill_sent=None,
                killed_at=None):
    """One row of the trip log, keyed by TRIP_LOG_FIELDS.

    `state` is the FSM state the rig tripped from. `data` is the packet that
    tripped it, for a measured trip; None for an operator stop or a lost link.
    `kill_sent` is whether KILL reached the controller, None if none was
    connected. `killed_at` is time.time() just after that write.

    Sample_To_Kill_ms is how long the host took from the DAQ sampling the
    packet to the KILL leaving it: the queue, the loop and the write. It leaves
    out the sensor's own response and the contactor opening, which happen
    outside the host, so time those on the bench.
    """
    now = killed_at if killed_at is not None else time.time()
    record = dict.fromkeys(TRIP_LOG_FIELDS, '')
    record.update({
        'Time': (time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now))
                 + f".{int(now % 1 * 1000):03d}"),
        'State': state,
        'Reason': reason,
        'Detail': "; ".join(line.strip().removeprefix("- ") for line in details),
        'Kill_Sent': {True: 'yes', False: 'NO'}.get(kill_sent, ''),
    })
    if ladder_ohm:
        record['Ladder_Ohm'] = f"{ladder_ohm:.2f}"
    if data is not None:
        def number(value, spec):
            return format(value, spec) if _finite(value) else ''
        cells = [v for v in data.get('cell_voltages', []) if _finite(v)]
        record.update({
            'Amps': number(data.get('amps'), '.1f'),
            'Module_V': number(data.get('voltage'), '.2f'),
            'Min_Cell_V': number(min(cells), '.3f') if cells else '',
            'Max_Cell_Temp_C': number(data.get('max_temp'), '.1f'),
            'Resistor_Temps_C': "/".join(number(t, '.0f')
                                         for t in data.get('resistor_temps', [])),
        })
        sampled_at = data.get('sampled_at')
        if kill_sent and killed_at is not None and _finite(sampled_at):
            record['Sample_To_Kill_ms'] = f"{(killed_at - sampled_at) * 1000.0:.0f}"
    return record


def append_trip_log(record, path=None):
    """Append one trip_record() to the trip log. True if it was written.

    Never raises. Called after the KILL has gone out, so a slow disk delays
    nothing that matters, and a log that cannot be written -- open in Excel,
    which locks it on Windows, or a read-only folder -- is reported and
    skipped rather than taking down the logic process. `path` defaults to
    TRIP_LOG_PATH, looked up at call time so tests can point it elsewhere.
    """
    path = path or TRIP_LOG_PATH
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        new = not os.path.exists(path) or os.path.getsize(path) == 0
        with open(path, 'a', newline='', encoding='utf-8') as fh:
            writer = csv.DictWriter(fh, fieldnames=TRIP_LOG_FIELDS)
            if new:
                writer.writeheader()
            writer.writerow(record)
        return True
    except Exception as exc:
        print(f"[LOGIC WARNING] Trip not written to {path}: {exc}")
        return False

#Double Checked
def coulomb_step(amps, dt, remaining_ah, total_capacity_ah):
    """Advances coulomb counting by dt seconds. Returns (new_remaining_ah, true_soc_pct).

    A sample that is not a finite number is skipped: one NaN would otherwise make
    the remaining charge NaN for good, and min() then pins the SOC at 100%.
    """
    if not (_finite(amps) and _finite(dt)):
        amps, dt = 0.0, 0.0
    ah_consumed = (amps * dt) / 3600.0
    new_remaining_ah = remaining_ah - ah_consumed
    true_soc = max(0.0, min(100.0, (new_remaining_ah / total_capacity_ah) * 100.0))
    return new_remaining_ah, true_soc


def compute_lap_physics(row, prev_velocity_ms, prev_time_s, is_first_row, row_idx,
                        lap_wrap_dt=None):
    """Derives velocity/acceleration for the current lap-profile row.

    `is_first_row` means the standing start of the RUN, not the start of a lap.
    It must be true only for lap 1 row 0. Tying it to `row_idx == 0` alone made
    every lap after the first begin as a standing start: the car's carried
    velocity was discarded, dv forced to zero, and the acceleration term dropped
    out of the power demand for that frame.

    `lap_wrap_dt` covers the frame where a lap repeats. The profile's `Time (s)`
    restarts at zero while `prev_time_s` still holds the end of the previous lap,
    so the raw difference is large and negative (0.0 - 60.0 = -60.0 s). The
    non-positive guard below would catch that and substitute 1.0 s, which is not
    a failsafe so much as a silently wrong number -- it divides a real velocity
    change by a fabricated interval. Pass the profile's sampling interval for
    that one frame instead; the next row differences normally again.
    """
    speed_mph = float(row.get('Speed (mph)', 0))
    velocity_ms = speed_mph * 0.44704
    current_time_s = float(row.get('Time (s)', row_idx))

    if is_first_row:
        # Standing start: nothing to difference against.
        return velocity_ms, 0.0, current_time_s

    if lap_wrap_dt is not None and math.isfinite(lap_wrap_dt) and lap_wrap_dt > 0:
        dt_physics = lap_wrap_dt
    else:
        dt_physics = current_time_s - prev_time_s
        if not math.isfinite(dt_physics) or dt_physics <= 0:
            dt_physics = 1.0

    dv = velocity_ms - prev_velocity_ms

    acceleration = dv / dt_physics
    return velocity_ms, acceleration, current_time_s


def compute_road_load_forces(velocity_ms, acceleration, params=None):
    """Longitudinal force breakdown at the contact patch, in newtons.

    Returned separately from power so the individual terms can be inspected and
    tested. Positive force opposes motion / must be overcome.
    """
    p = params if params is not None else DEFAULT_VEHICLE

    total_mass = p.total_mass_kg
    dynamic_pressure = 0.5 * p.air_density_kgm3 * (velocity_ms ** 2)

    f_downforce = dynamic_pressure * abs(p.lift_coefficient) * p.downforce_area_m2
    f_drag = dynamic_pressure * p.drag_coefficient * p.drag_area_m2

    # Downforce presses the tyres harder into the track, so rolling resistance
    # rises with speed rather than staying at the static-weight value.
    normal_force = (total_mass * p.gravity_ms2) + f_downforce
    f_roll = p.rolling_resistance_coeff * normal_force

    # Rotational inertia resists speed changes only -- hence the factor applies
    # here and deliberately NOT to normal_force above.
    f_accel = (total_mass * p.rotational_mass_factor) * acceleration

    return {
        'drag': f_drag,
        'downforce': f_downforce,
        'rolling': f_roll,
        'accel': f_accel,
        'total': f_roll + f_drag + f_accel,
    }


def compute_required_power(velocity_ms, acceleration, params=None):
    """Powertrain-side power for the profile's speed/acceleration, in watts.

    This is power at the motor input, not at the contact patch: the drivetrain
    efficiency correction is applied last. Positive means the pack is driving
    the car; negative means braking.
    """
    p = params if params is not None else DEFAULT_VEHICLE

    forces = compute_road_load_forces(velocity_ms, acceleration, p)
    power_at_wheels = forces['total'] * velocity_ms

    # Clamp to a sane range so a mistyped efficiency cannot divide by zero and
    # take down the safety-critical logic process.
    eta = min(1.0, max(0.01, p.drivetrain_efficiency))

    if power_at_wheels > 0:
        # Driving: the powertrain must produce more than reaches the road.
        return power_at_wheels / eta

    # Braking: friction eats part of what would otherwise come back.
    return power_at_wheels * eta


def bank_power_floor(voltage, power_factor=1.0):
    """Lowest resistance that keeps the whole ladder within VRB_MAX_POWER_W.

    Bank power is V^2 / R. `voltage` is the measured module voltage, which is
    never below the voltage across the bank. Stepping to a lower resistance sags
    the module further, so V^2 / R from the reading taken before the step
    over-estimates the power after it. Stepping to a higher resistance always
    lowers the power. Either way the bank stays inside its rating.

    Sized for an element at the low end of its tolerance, so the floor rather
    than the BANK OVERPOWER trip is what holds the limit.

    In practice this only ever removes the bottom step: at a full 50.4 V module
    the 0.25 ohm step would dissipate about 10 kW, and it comes back once the
    module is below sqrt(0.25 * 0.95 * 8000) = 43.6 V (51.6 V at 140%). It is a
    floor because total power only falls as resistance rises. Each bank's own
    rating is not like that, so command_steps() checks those per setting.

    `power_factor` is 1.0, or BANK_OVERLOAD_FACTOR when not held to rated power.
    """
    return voltage ** 2 / (VRB_MAX_POWER_W * power_factor * (1.0 - RESISTOR_TOLERANCE))


def steps_within_rating(steps, voltage, power_factor=1.0):
    """Whether a ladder setting keeps the whole ladder and every bank in it in rating.

    Worst case, as bank_power_floor(): the measured module voltage, with the
    elements at the low end of their tolerance. A tolerance shared by every
    element cancels out of each bank's share (bank_power_shares), so this bounds
    what the per-bank trip will estimate as well as what the elements dissipate.
    """
    if steps <= 0:
        return False
    total = voltage ** 2 / (steps * RESISTOR_RESOLUTION * (1.0 - RESISTOR_TOLERANCE))
    if not total <= VRB_MAX_POWER_W * power_factor:
        return False
    return all(watts <= BANK_RATED_POWER_W[k] * power_factor
               for k, watts in bank_power_shares(steps, total))


def resistance_floor(voltage, max_safe_current, current_max_temp, derate_enabled,
                     derate_start_temp, max_safe_temp, power_factor=1.0):
    """Lowest resistance the bank may be set to right now.

    The highest of three floors, each dividing the MODULE voltage because that is
    what is across the bank:
      * the configured current limit, which comes from the cells;
      * the whole ladder's power rating, which configuration can only choose
        100% or 140% of;
      * the thermal derate, when enabled and the cells are past its start.
    """
    floor_r = max(voltage / max(max_safe_current, 1.0), bank_power_floor(voltage, power_factor))

    if derate_enabled and (current_max_temp > derate_start_temp):
        derate_range = max_safe_temp - derate_start_temp
        if derate_range <= 0:
            # Misconfigured thresholds (derate start >= max temp) -- fail safe to full derate
            # instead of a ZeroDivisionError that would crash the safety-critical logic process.
            derate_pct = 1.0
        else:
            derate_pct = (current_max_temp - derate_start_temp) / derate_range
            derate_pct = max(0.0, min(1.0, derate_pct))

        active_current_limit = max_safe_current * (1.0 - derate_pct)
        floor_r = max(floor_r, voltage / max(active_current_limit, 1.0))

    return min(MAX_RESISTANCE, floor_r)


def compute_target_resistance(voltage, req_power, max_safe_current, current_max_temp,
                               derate_enabled, derate_start_temp, max_safe_temp,
                               modules_in_series=1, power_factor=1.0):
    """Resistance needed to reproduce the car's duty on ONE module.

    The bench loads a single module (`voltage` is the measured module voltage,
    ~50 V), but `req_power` is the whole car's demand, drawn from all
    `modules_in_series` modules in series (~450 V). Matching the module's real
    duty means matching its CURRENT, since series modules all carry the same one:

        I_car = req_power / V_battery = req_power / (N * voltage)
        R     = voltage / I_car       = N * voltage^2 / req_power

    Equivalently, this is 1/N of the resistance a bank across the whole battery
    would need -- the bank sees a ninth of the voltage, so it needs a ninth of
    the resistance to pull the same current.

    The result never goes below resistance_floor(): the current limit, the
    bank's power rating and the thermal derate. It is continuous; command_steps()
    turns it into a ladder setting without rounding under that floor.

    This previously read `(voltage * 9) ** 2`, i.e. N^2 rather than N. That is
    the resistance for a bank spanning the entire battery, so on a single-module
    bench it under-loaded by a factor of N: an 81 kW lap demand drew 20 A instead
    of 180 A, and no plausible car power could reach the ladder's 0.25 ohm step.
    """
    modules = max(1, modules_in_series)

    if req_power <= 0:
        req_r = MAX_RESISTANCE
    else:
        req_r = min(MAX_RESISTANCE, (modules * voltage ** 2) / req_power)

    return max(req_r, resistance_floor(voltage, max_safe_current, current_max_temp,
                                       derate_enabled, derate_start_temp, max_safe_temp,
                                       power_factor))


def command_steps(voltage, req_power, max_safe_current, current_max_temp,
                  derate_enabled, derate_start_temp, max_safe_temp, modules_in_series=1,
                  power_factor=1.0):
    """Ladder steps to send for one lap-profile row: the car's power demand,
    turned into a resistance and then into a setting by steps_for_resistance().
    """
    req_r = compute_target_resistance(voltage, req_power, max_safe_current, current_max_temp,
                                      derate_enabled, derate_start_temp, max_safe_temp,
                                      modules_in_series=modules_in_series,
                                      power_factor=power_factor)
    return steps_for_resistance(voltage, req_r, max_safe_current, current_max_temp,
                                derate_enabled, derate_start_temp, max_safe_temp,
                                power_factor=power_factor)


def steps_for_resistance(voltage, req_r, max_safe_current, current_max_temp,
                         derate_enabled, derate_start_temp, max_safe_temp, power_factor=1.0):
    """Ladder steps for a requested resistance, held to every floor and rating.

    A lap row's request comes from command_steps(); a bench profile's is the
    resistance it names (RESISTANCE_COLUMN). Both pass through here, so neither
    can command more load than the other.

    The request is raised to resistance_floor() and rounds to the nearest
    0.25 ohm step, but the floor rounds UP. Rounding everything to nearest used
    to undercut the floor at the bottom of the ladder: a 0.29 ohm current-limit
    clamp became the 0.25 ohm step, which draws 192 A from a full module.

    Then every bank the setting puts in circuit must be within its own rating
    (steps_within_rating). That is not a floor: a bank alone carries the whole
    current, so 0.5 ohm (bank 2 alone, 5.1 kW at 50.4 V against 4 kW) can be out
    of rating while 0.75 ohm (banks 1 and 2 sharing 3.4 kW) is fine. A setting
    that fails moves to the nearest one at or above the floor that passes, the
    higher resistance on a tie.
    """
    floor_r = resistance_floor(voltage, max_safe_current, current_max_temp,
                               derate_enabled, derate_start_temp, max_safe_temp, power_factor)
    if not math.isfinite(req_r):
        # NaN rounds to the bottom step in resistance_to_steps(). Least load.
        req_r = MAX_RESISTANCE
    req_r = max(req_r, floor_r)
    steps = resistance_to_steps(req_r, min_r=floor_r)
    if steps_within_rating(steps, voltage, power_factor):
        return steps

    max_steps = int(round(MAX_RESISTANCE / RESISTOR_RESOLUTION))
    floor_steps = max(1, math.ceil(floor_r / RESISTOR_RESOLUTION - 1e-9))
    allowed = [s for s in range(floor_steps, max_steps + 1)
               if steps_within_rating(s, voltage, power_factor)]
    if not allowed:
        # Only an unreadable voltage gets here. Least load the ladder has.
        return max_steps
    return min(allowed, key=lambda s: (abs(s * RESISTOR_RESOLUTION - req_r), -s))


def NEUTRAL_PACKET():
    """Stand-in used before the first DAQ packet arrives.

    Deliberately reads as "no data" rather than as a healthy pack: zero volts and
    no cell readings trip the voltage and cell checks the moment the rig is
    armed, so the FSM cannot be driven on a packet that never existed.
    """
    return {
        'amps': 0.0, 'voltage': 0.0, 'max_temp': 0.0,
        'cell_voltages': [], 'temperatures': [], 'power_kw': 0.0,
        'temp_age_s': float('inf'), 'temp_sensor_ages_s': [],
        'resistor_temps': [], 'resistor_temp_ages_s': [],
        'hardware_status': {},
    }


def load_lap_profile(path):
    """Read a lap CSV, dropping rows that cannot be used. Returns (rows, dropped).

    Trailing blank rows are common in exported telemetry -- the shipped profile
    has seven. Left in place they yield NaN speed and time, which propagates
    through the physics to a NaN power demand. NaN then loses every comparison
    in compute_target_resistance(), so `min(MAX_RESISTANCE, nan)` quietly returns
    MAX_RESISTANCE and the bank sits at minimum load for the tail of every lap
    without anything being reported.

    A bench profile (a RESISTANCE_COLUMN instead of, or as well as, a speed)
    is read the same way, by its resistance. It is refused outright if a row
    asks for zero or less: the ladder's lightest load is MAX_RESISTANCE, and a
    0 ohm row, meant as "off", would command its heaviest.
    """
    df = pd.read_csv(path)
    rows = df.to_dict('records')
    demand = RESISTANCE_COLUMN if RESISTANCE_COLUMN in df.columns else 'Speed (mph)'

    usable = []
    for line, row in enumerate(rows, start=2):      # line 1 is the header
        try:
            value = float(row.get(demand))
            stamp = float(row.get('Time (s)'))
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and math.isfinite(stamp):
            if demand == RESISTANCE_COLUMN and value <= 0:
                raise ValueError(
                    f"line {line} asks for {value:g} ohm. The lightest load the ladder has "
                    f"is {MAX_RESISTANCE} ohm; use that for no load.")
            usable.append(row)

    return usable, len(rows) - len(usable)


def profile_resistance(row):
    """The resistance a bench-profile row asks for, in ohms; None for a lap row."""
    if RESISTANCE_COLUMN not in row:
        return None
    return float(row[RESISTANCE_COLUMN])


# ================= LOAD OVERRIDE =================
# The operator can take the load off the profile and hold a resistance or a power
# of their own, from the GUI sidebar, before or during a run. The run still
# follows the profile's clock and laps and ends with them; only the load each
# moment changes. Like a bench-profile row, the override passes through
# steps_for_resistance(), so the current limit, the bank's ratings and the
# thermal derate hold it exactly as they hold a lap.
OVERRIDE_MODES = ("RESISTANCE", "POWER")

# While running, a changed override reaches the bank on the next loop pass, and
# a power override is re-solved this often against the module voltage, which
# falls as the module discharges.
OVERRIDE_REFRESH_S = 0.5


def parse_override(cmd):
    """The override a SET_OVERRIDE command asks for: None, or (mode, value).

    ("SET_OVERRIDE", None) hands the load back to the profile.
    ("SET_OVERRIDE", "RESISTANCE", ohms) and ("SET_OVERRIDE", "POWER", watts)
    hold that value. Anything else raises ValueError, so a malformed command
    changes nothing.
    """
    if isinstance(cmd, tuple) and len(cmd) == 2 and cmd[1] is None:
        return None
    if not (isinstance(cmd, tuple) and len(cmd) == 3 and cmd[1] in OVERRIDE_MODES):
        raise ValueError(f"not an override: {cmd!r}")
    mode, value = cmd[1], cmd[2]
    if not (_finite(value) and value > 0):
        raise ValueError(f"{mode.lower()} must be a positive number, not {value!r}")
    return mode, float(value)


def override_resistance(override, voltage):
    """The resistance an override asks for at this module voltage, before any floor.

    POWER is power at the bank, V^2 / R, on the measured module voltage: what is
    across the bank, and what the GUI's Power card shows as V x I. Without a
    usable voltage it asks for the least load.
    """
    mode, value = override
    if mode == "RESISTANCE":
        return value
    if not (_finite(voltage) and voltage > 0):
        return MAX_RESISTANCE
    return voltage ** 2 / value


def describe_override(override):
    """The override in words, for the console."""
    if override is None:
        return "off: the profile sets the load"
    mode, value = override
    if mode == "RESISTANCE":
        return f"holding {value:.2f} ohm"
    return f"holding {value / 1000.0:.2f} kW at the bank"


def lap_row_interval(lap_data, idx, default=1.0):
    """Real seconds to hold row `idx`, taken from the profile's own timestamps.

    The playback used to advance one row per wall-clock second regardless of what
    the CSV said. That happens to match the shipped 1 Hz profile, but a 10 Hz log
    would run ten times too slow AND hold each power demand ten times too long,
    over-draining the pack by the same factor. Since compute_lap_physics()
    already derives acceleration from these timestamps, playback ignoring them
    also made the two disagree about how much time a row represents.

    The last row has no row after it to say how long it lasts: see
    last_row_interval().
    """
    if lap_data and idx == len(lap_data) - 1:
        return last_row_interval(lap_data, default)
    return _row_gap(lap_data, idx, default)


def _row_gap(lap_data, idx, default):
    """Seconds from row idx to the row after it, or `default` if that is unusable."""
    try:
        here = float(lap_data[idx].get('Time (s)'))
        nxt = float(lap_data[idx + 1].get('Time (s)'))
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return default

    dt = nxt - here
    if math.isfinite(dt) and dt > 0:
        return dt
    return default


def last_row_interval(lap_data, default=1.0):
    """Real seconds to hold a profile's last row.

    Nothing follows it, so its length is taken from the rest of the profile. An
    evenly sampled log holds it for one sample, as every other row. The default
    second was used for all of them, so a 10 Hz lap stood on its final row ten
    times too long, every lap.

    A profile with uneven rows is a bench profile of steps and holds, and its
    last row is the marker that ends the hold before it, not a sample. That one
    keeps the default, or the shortest row if that is shorter still.
    """
    gaps = [_row_gap(lap_data, i, None) for i in range(len(lap_data) - 1)]
    gaps = [g for g in gaps if g is not None]
    if not gaps:
        return default
    shortest, longest = min(gaps), max(gaps)
    if longest - shortest <= 0.01 * longest:
        return longest
    return min(default, shortest)


def resistance_to_steps(req_r, min_r=0.0):
    """Nearest ladder step to req_r, but never below min_r and never past the top.

    min_r rounds up, so the step sent always sits at or above it. The small
    epsilon stops a floor exactly on the grid (0.5 ohm) becoming one step too
    many through float error.
    """
    steps = int(round(max(RESISTOR_RESOLUTION, req_r) / RESISTOR_RESOLUTION))
    floor_steps = math.ceil(min_r / RESISTOR_RESOLUTION - 1e-9)
    max_steps = int(round(MAX_RESISTANCE / RESISTOR_RESOLUTION))
    return min(max_steps, max(steps, floor_steps))


def heartbeat_due(fsm_state, since_last_s):
    """Whether the resistor controller should be sent its keep-alive now.

    In every state, every HEARTBEAT_INTERVAL_S. What is sent depends on the
    state: see keepalive_message().
    """
    return since_last_s > HEARTBEAT_INTERVAL_S


def keepalive_message(fsm_state):
    """The line the controller is sent every HEARTBEAT_INTERVAL_S in this state.

    RUNNING: "alive". It feeds the Arduino's 2 s watchdog and moves no relay, so
    it holds the bank at its resistance between rows. Without it a profile with
    rows ~2 s apart, or one command lost on the wire, let the watchdog open the
    main contactor under full load, with the next command re-closing it
    milliseconds later. The watchdog still catches a stalled host, because the
    heartbeat comes from this same loop.

    Every other state: "KILL". The bank must be disconnected whenever the rig is
    not running, so the host says so again twice a second instead of saying
    "alive". KILL does not feed the watchdog, so in FAULT the watchdog still
    lapses as a second, independent guarantee. The heartbeat here used to be
    "alive" in IDLE and ARMED: when a run ended, one KILL went out and the rig
    went back to feeding the watchdog, so had that one line been lost the
    contactor stayed closed at the last setting with nothing to open it. A failed
    write still shows a dead link, as "alive" did.
    """
    return b"alive\n" if fsm_state == "RUNNING" else b"KILL\n"


def is_valid_transition(current_state, command):
    """Pure FSM guard mirroring the legality checks in the GUI-command handler below."""
    if command == "ARM":
        return current_state == "IDLE"
    if command == "RUN":
        return current_state == "ARMED"
    if command == "RESET":
        return current_state == "FAULT"
    if command == "STOP":
        return True
    return False


TRANSITION_COMMANDS = ("ARM", "RUN", "RESET")


def press_clock():
    """The clock every ARM, RUN and RESET press, and every stop and fault, is stamped with.

    time.perf_counter(): one clock for every process on the machine (on Windows
    since Python 3.10) and fine-grained. time.monotonic() is system-wide too,
    but on Windows before Python 3.13 it steps every 15.6 ms, so a RESET pressed
    in the same step as the fault it follows compared equal to it and was
    thrown away as stale. The operator had to press it again, and the tests for
    it failed about one run in two.
    """
    return time.perf_counter()


def transition_command(name, *args):
    """ARM, RUN or RESET as the GUI queues it: (name, *args, issued_at).

    issued_at is press_clock() when the operator pressed the button. The logic
    process acts on the command only if it was issued after the most recent stop
    or fault (stale_transition() below), so a press already waiting in the queue
    cannot undo an E-STOP.
    """
    if name not in TRANSITION_COMMANDS:
        raise ValueError(f"not a state transition: {name!r}")
    return (name, *args, press_clock())


def parse_transition(cmd):
    """(name, args, issued_at) for a transition command, else None.

    issued_at is None when the command carries no usable stamp: a bare "ARM"
    string, or an old ("RUN", laps). The loop refuses those outright.
    """
    name = cmd[0] if isinstance(cmd, tuple) and cmd else cmd
    if not isinstance(name, str) or name not in TRANSITION_COMMANDS:
        return None
    arg_count = 1 if name == "RUN" else 0
    if not isinstance(cmd, tuple) or len(cmd) != arg_count + 2 or not _finite(cmd[-1]):
        return name, (), None
    return name, tuple(cmd[1:-1]), float(cmd[-1])


def run_laps(args):
    """The lap count a RUN command carries, or None if it is not a count.

    The GUI sends a spinbox value, but anything can put a RUN on the queue, and
    a None or a 0 here used to reach the lap comparison mid-run.
    """
    try:
        laps = args[0]
    except (IndexError, TypeError):
        return None
    if isinstance(laps, bool) or not isinstance(laps, (int, float)):
        return None
    if not math.isfinite(laps) or laps != int(laps) or laps < 1:
        return None
    return int(laps)


def stale_transition(issued_at, fault_at):
    """Whether a transition must be ignored: unstamped, or issued before the last stop.

    E-STOP latches FAULT and then the logic loop works through the command queue.
    A RESET, ARM and RUN queued before the stop were then accepted in order, and
    the rig was RUNNING again, reconnecting the load, in the same loop pass as the
    E-STOP. Comparing stamps rather than queue position means a press is judged
    by when it was made, however late the queue delivers it. A press made in the
    instant between the E-STOP and the loop seeing it counts as before: the
    operator presses it again.
    """
    if issued_at is None:
        return True
    return fault_at is not None and issued_at <= fault_at


def kill_load(res_ser):
    """Send KILL to the resistor controller. False only if the link failed.

    True with no controller connected: there is no link to lose, and nothing
    the host could reach. Never raises, so a dead link cannot take down the
    logic process on its way to a fault; the Arduino's own 2 s watchdog sheds
    the load once the host goes quiet.
    """
    return res_ser is None or write_resistor(res_ser, b"KILL\n")


def run_logic_process(daq_queue: Queue, telemetry_queue: Queue, gui_cmd_queue: Queue,
                      stop_event: Event, discovery_lock=None, estop_event=None,
                      sil_mode=False):
    """The logic process: state machine, trips, lap playback and the resistor controller.

    `sil_mode` is True when main_v2 found the SIL dongle and the DAQ is the
    desk-test plant model. Its current is a slider the operator drags, not what
    the ladder would draw, so the load check is skipped there.
    """
    fsm_state = "DISCONNECTED"
    target_res = 0.0
    # press_clock() at the most recent stop or fault; see stale_transition().
    fault_at = None

    # --- Resistor controller discovery, off the safety loop ---
    #
    # This used to run inline: the first connect blocked startup, and every
    # reconnect attempt blocked the main loop for ~2 s PER COM PORT. During that
    # stall nothing was serviced -- not the GUI command queue (so a pressed
    # E-STOP sat unread), not the safety checks, not telemetry forwarding, so the
    # display froze too. Relying on the Arduino's watchdog to cover a stall in
    # the safety-critical process is a structural weakness even when it works.
    #
    # Ownership rule that keeps this safe without heavy locking:
    #   the worker thread only ever ASSIGNS the handle when the slot is empty;
    #   the main loop only ever CLEARS it.
    # So the two never contend for the same handle, and the main loop can read
    # it without holding a lock across a serial write.
    resistor_slot = {'ser': None}
    slot_lock = threading.Lock()

    # Set when the main loop ends, however it ends -- shutdown or an unexpected
    # error -- so the worker never outlives it holding a port.
    logic_done = threading.Event()

    def resistor_discovery_worker():
        while not (stop_event.is_set() or logic_done.is_set()):
            with slot_lock:
                have = resistor_slot['ser'] is not None
            if not have:
                found = auto_detect_resistor(discovery_lock)
                if found is not None:
                    with slot_lock:
                        # Re-check: the main loop cannot have filled the slot,
                        # but it may have ended while we probed.
                        if stop_event.is_set() or logic_done.is_set():
                            found.close()
                        else:
                            resistor_slot['ser'] = found
            logic_done.wait(RESISTOR_SCAN_COOLDOWN)

    discovery = threading.Thread(target=resistor_discovery_worker, daemon=True)

    res_ser = None
    # What the controller has printed that is not yet a whole line, and when
    # this handle was taken. See read_controller_lines() and CONTROLLER_BOOT_S.
    res_pending = b""
    res_connected_at = 0.0
    controller_rejects = 0
    # bank_steps_at of the setting last sent a second time after a rejection.
    resent_setting_at = None

    # Why the rig is in FAULT, for the GUI: the reason and trip_details()'s
    # lines. Cleared by RESET.
    fault_reason = None
    fault_detail = []

    def latch_fault(reason, details=()):
        """Enter FAULT, and void every ARM, RUN and RESET issued before now.

        Called for every stop and fault, including a stop while already in
        FAULT: the operator's latest press is a stop, so a RESET made before it
        must not clear it.
        """
        nonlocal fsm_state, fault_at, fault_reason, fault_detail
        fsm_state = "FAULT"
        fault_at = press_clock()
        fault_reason, fault_detail = reason, list(details)

    def operator_stop(reason):
        """E-STOP or a queued STOP: latch, KILL at once, then report and log it."""
        stopped_in = fsm_state
        had_link = res_ser is not None
        latch_fault(reason, ["Operator stop"])
        killed = kill_load(res_ser)
        killed_at = time.time()
        print("[LOGIC] EMERGENCY STOP triggered via GUI.")
        if not killed:
            drop_resistor_link("KILL")
        append_trip_log(trip_record(
            reason, stopped_in, fault_detail,
            ladder_ohm=target_res if stopped_in == "RUNNING" else None,
            kill_sent=killed if had_link else None, killed_at=killed_at))

    def drop_resistor_link(what):
        """The controller stopped taking writes, or its port closed under us.

        Invalidate the handle: close it, and clear the slot so the discovery
        thread looks for the controller again. If the bank could be loaded,
        latch a fault, because commands are no longer reaching it. KILL cannot
        go over a dead link; the Arduino's watchdog sheds the load 2 s after
        the last message that reached it.
        """
        nonlocal res_ser
        lost, res_ser = res_ser, None
        with slot_lock:
            resistor_slot['ser'] = None
        if lost is not None:
            try:
                lost.close()
            except Exception:
                pass
        print(f"\n[LOGIC] Resistor controller link lost ({what}).")
        if fsm_state in ("ARMED", "RUNNING"):
            lost_in = fsm_state
            latch_fault("RESISTOR LINK LOST",
                        [f"Controller link lost ({what}); its watchdog sheds the load"])
            print("[LOGIC] RESISTOR LINK LOST ALARM! Commands are not reaching the "
                  "controller; its watchdog sheds the load 2 s after the last one did.")
            append_trip_log(trip_record(
                "RESISTOR LINK LOST", lost_in, fault_detail,
                ladder_ohm=target_res if lost_in == "RUNNING" else None, kill_sent=False))

    def controller_fault(reason, detail, packet):
        """The controller reported something a loaded rig cannot carry on from.

        KILL first, as for a measured trip: after a restart the board is back
        and listening, and its next valid command would reconnect the bank.
        """
        tripped_in = fsm_state
        latch_fault(reason, [detail])
        killed = kill_load(res_ser)
        killed_at = time.time()
        print(f"\n[LOGIC] {reason} ALARM! Killing Load.")
        print(f"[LOGIC] {detail}")
        if not killed:
            drop_resistor_link("KILL")
        append_trip_log(trip_record(
            reason, tripped_in, fault_detail, packet,
            ladder_ohm=target_res if tripped_in == "RUNNING" else None,
            kill_sent=killed, killed_at=killed_at))

    # All limits, the pack spec and the vehicle model come from rig_config.json
    # (falling back to the RS50 12S4P defaults). Nothing here is hardcoded, so a
    # future team retargets the rig from the GUI rather than from source.
    config = RigConfig.load()
    pack = config.pack
    vehicle = config.vehicle

    # Single dict rather than a row of parallel variables: SET_LIMITS then updates
    # one thing, and there is no way for a new limit to be added to the config and
    # silently never reach the safety checks.
    limits = config.limits.to_command_dict()

    print(f"[LOGIC] Battery: {pack.modules_in_series} x {pack.cell_model} "
          f"{pack.series_count}S{pack.parallel_count}P "
          f"= {pack.battery_cell_count} cells, "
          f"{pack.battery_min_voltage:.0f}-{pack.battery_max_voltage:.0f} V total")
    print(f"[LOGIC] Module: {pack.capacity_ah:.1f} Ah, {pack.max_current_a:.0f} A, "
          f"{pack.min_voltage:.1f}-{pack.max_voltage:.1f} V, "
          f"{pack.resistance_ohm * 1000:.0f} mOhm")
    print(f"[LOGIC] Trips: {limits['max_amps']:.0f} A (+{limits['amp_buffer']:.0f}) | "
          f"{limits['max_temp']:.0f} C | {limits['min_volts']:.1f} V module | "
          f"{limits['min_cell_volts']:.2f} V/cell | "
          f"temp stale > {limits['temp_stale_timeout']:.1f} s | "
          f"bank {VRB_MAX_POWER_W * bank_power_factor(limits) / 1000:.1f} kW total and "
          f"{bank_power_factor(limits) * 100:.0f}% of each bank's rating | resistors "
          + "/".join(f"{t:.0f}" for t in limits['resistor_max_temp']) + " C")
    # Trips past a rating block ARM, and print with the config errors below.
    for warning in config.limits.advisories():
        print(f"[LOGIC WARNING] {warning}")

    # The DAQ process loaded this same file at startup and keeps that wiring
    # until it restarts (see DaqConfig), so this copy -- not whatever a later
    # SET_CONFIG brings -- is what the cells and banks are actually read through.
    # Fixing a channel list in the GUI therefore does not unblock ARM until the
    # DAQ is really reading the fixed list.
    running_daq = config.daq
    config_problems = config_blockers(running_daq, pack, limits)
    report_config_problems(config_problems)
    # What a complete packet holds for that wiring and this pack.
    expected = expected_measurements(running_daq, pack)
    # Reported in every packet so the GUI can see whether this process is using
    # the vehicle, pack and wiring its window shows. See settings_fingerprint().
    settings_id = config.settings_fingerprint()

    last_heartbeat = time.time()
    last_physics_time = time.time()

    # --- COULOMB COUNTING VARIABLES ---
    total_capacity_ah = pack.capacity_ah
    remaining_ah = total_capacity_ah
    last_coulomb_time = time.time()

    # Lap Tracking Variables
    lap_data = []
    total_rows = 0
    current_row_idx = 0
    total_laps = 1
    current_lap = 1

    # Physics Tracking Variables
    prev_velocity_ms = 0.0
    prev_time_s = 0.0

    # The ladder setting in circuit, for the per-bank overpower trip, and when it
    # was sent. None until a run commands one; the contactor is open otherwise.
    bank_steps = None
    bank_steps_at = 0.0

    # The load check: how many settled readings in a row have disagreed with
    # the ladder setting, and the latest one's description. See load_mismatch().
    load_check_run = 0
    load_problem = None
    if sil_mode:
        print("[LOGIC] SIL mode: the load check is off. The plant's current is a slider, "
              "not what the ladder would draw.")

    # The operator's load override (parse_override()), None to follow the
    # profile. override_due sends a change on the next pass rather than at the
    # next row. profile_steps is the profile's own setting for the current row,
    # computed even while overridden so handing back is immediate.
    override = None
    override_due = False
    override_sent_at = 0.0
    profile_steps = None

    def set_ladder(steps):
        """Send a ladder setting while running, and note it for the per-bank trip."""
        nonlocal target_res, bank_steps, bank_steps_at
        # What the ladder is actually set to, not the continuous request. Near
        # the bottom of the ladder the two can differ by a whole step.
        target_res = steps * RESISTOR_RESOLUTION
        if send_binary_command(res_ser, steps):
            # The firmware ignores a repeat of its current setting, so only a
            # change restarts the settle time.
            if steps != bank_steps:
                bank_steps, bank_steps_at = steps, time.time()
        else:
            drop_resistor_link("resistance command")

    # The profile this process actually holds, reported in every packet. The
    # window used to show whichever file the operator last picked, loaded or not.
    profile_name = None
    try:
        lap_data, dropped = load_lap_profile(CSV_FILENAME)
        total_rows = len(lap_data)
        profile_name = DEFAULT_LAP_CSV
        print(f"[LOGIC] Loaded default lap profile: {total_rows} rows"
              + (f" ({dropped} unusable rows dropped)." if dropped else "."))
    except Exception as e:
        print(f"[LOGIC WARNING] Failed to load default CSV: {e}")

    # Last packet seen, and when. The loop body must run whether or not the DAQ
    # is feeding it -- see the comment on the get() below.
    last_data = None
    last_daq_rx = time.time()
    row_interval = 1.0

    # Started here rather than above, so nothing between its start and the
    # finally below can leave it running.
    discovery.start()
    clean_exit = False
    crash = None
    try:
        while not stop_event.is_set():
            # Never `continue` past the loop body on an empty queue. Doing so skipped
            # GUI commands (including E-STOP), every safety check, the resistor
            # heartbeat and telemetry forwarding, so a hung or crashed DAQ left the
            # logic process paralysed: the operator's E-STOP sat unread in the queue
            # and the GUI kept displaying RUNNING. The Arduino's own 2 s watchdog
            # still shed the load, but nothing in software noticed or reported it.
            try:
                data = daq_queue.get(timeout=0.1)
                last_data = data
                last_daq_rx = time.time()
                fresh_sample = True
            except Empty:
                # Carry the last packet forward, tagged with its age so the staleness
                # check can fault on it. Copy it: section G writes FSM fields into
                # the packet before forwarding.
                data = dict(last_data) if last_data is not None else NEUTRAL_PACKET()
                fresh_sample = False

            data['daq_age_s'] = time.time() - last_daq_rx

            # Pick up whatever the discovery thread has found. Never blocks: if the
            # slot is empty the thread is still sweeping, and this loop carries on
            # servicing commands and safety checks in the meantime.
            if res_ser is None:
                with slot_lock:
                    res_ser = resistor_slot['ser']
                if res_ser is not None:
                    res_pending, res_connected_at = b"", time.time()

            # A port that closed under us is a lost link, like a failed write. It
            # used to drop a running rig to DISCONNECTED, and a reconnect then took
            # it straight back to IDLE with no fault on record.
            if res_ser is not None and not res_ser.is_open:
                drop_resistor_link("port closed")

            # --- A2. What the controller has to say ---
            # Read before the commands below, so a line is judged against the
            # state the rig was in when the controller printed it.
            if res_ser is not None:
                heard = read_controller_lines(res_ser, res_pending)
                if heard is None:
                    drop_resistor_link("read")
                else:
                    lines, res_pending = heard
                    for kind in filter(None, map(classify_controller_line, lines)):
                        if kind == "rejected":
                            controller_rejects += 1
                            print(f"[LOGIC WARNING] The resistor controller rejected a line "
                                  f"({controller_rejects} so far). A command may not have "
                                  f"been applied.")
                            # If that was this row's command, the bank is still at
                            # the row before. Say it again, once per setting; the
                            # firmware ignores a repeat of what it already holds.
                            # If it is refused again the load check faults the run.
                            if (fsm_state == "RUNNING" and bank_steps is not None
                                    and resent_setting_at != bank_steps_at):
                                resent_setting_at = bank_steps_at = time.time()
                                if not send_binary_command(res_ser, bank_steps):
                                    drop_resistor_link("resistance command")
                                    break
                        elif kind == "reset":
                            if time.time() - res_connected_at < CONTROLLER_BOOT_S:
                                continue
                            if fsm_state in ("ARMED", "RUNNING"):
                                controller_fault(
                                    "CONTROLLER RESET",
                                    "The resistor controller restarted, which drops every "
                                    "relay. Check its supply: a brownout or noise as the "
                                    "contactor or relay coils switch", data)
                            else:
                                print("[LOGIC WARNING] The resistor controller restarted.")
                        elif kind == "shed" and fsm_state == "RUNNING" and bank_steps is not None:
                            # The host sends no KILL while RUNNING, so this was the
                            # controller's own watchdog: heartbeats did not reach it.
                            controller_fault(
                                "CONTROLLER SHED LOAD",
                                "The resistor controller's watchdog opened the contactor "
                                "mid-run: it went 2 s without hearing from the host", data)

            if res_ser is None:
                if fsm_state != "FAULT":
                    fsm_state = "DISCONNECTED"
            elif fsm_state == "DISCONNECTED" and data['hardware_status'].get('temp_arduino', False):
                fsm_state = "IDLE"

            # --- B. Operator E-STOP, ahead of every queued command ---
            # A latched Event rather than a queued "STOP": the bounded command queue
            # could drop it when full (see gui_layout.request_estop). Cleared as it is
            # taken, so each press is acted on once; a press landing between the
            # check and the clear is the same stop.
            if estop_event is not None and estop_event.is_set():
                estop_event.clear()
                operator_stop("E-STOP")

            # --- C. Process GUI Commands ---
            while not gui_cmd_queue.empty():
                try:
                    cmd = gui_cmd_queue.get_nowait()

                    transition = parse_transition(cmd)
                    if transition is not None:
                        name, args, issued_at = transition
                        if stale_transition(issued_at, fault_at):
                            print(f"[LOGIC] Ignoring {name}: "
                                  + ("issued before the last stop or fault. Press it again."
                                     if issued_at is not None else "no issue time."))
                            continue
                    else:
                        name, args = None, ()

                    if isinstance(cmd, tuple) and cmd[0] == "SET_LIMITS":
                        # Merge rather than replace, so a partial payload cannot drop
                        # a limit and leave that check running on a missing key.
                        limits.update(cmd[1])
                        print(f"[LOGIC] Limits updated -> {limits['max_amps']:.0f} A | "
                              f"{limits['max_temp']:.0f} C | {limits['min_volts']:.1f} V | "
                              f"{limits['min_cell_volts']:.2f} V/cell | "
                              f"derate={limits['derate_en']} | "
                              f"bank power {bank_power_factor(limits) * 100:.0f}% of rating")
                        # Staleness timeouts and resistor trips arrive here too.
                        previous = config_problems
                        config_problems = config_blockers(running_daq, pack, limits)
                        if config_problems != previous:
                            report_config_problems(config_problems)

                    elif isinstance(cmd, tuple) and cmd[0] == "SET_CONFIG":
                        # Full config push from the GUI's Configure dialog: new car,
                        # new cells, or both. Rejected while RUNNING so the physics
                        # model cannot change underneath an in-progress lap.
                        if fsm_state == "RUNNING":
                            print("[LOGIC] Ignoring config change while RUNNING. Stop the run first.")
                        else:
                            try:
                                # Everything that can fail on a bad value is
                                # worked out first, on the side. The swap below
                                # then cannot stop half way: a config that
                                # raised used to be reported as rejected after
                                # its pack and limits were already in force.
                                new_config = RigConfig.from_dict(cmd[1])
                                new_pack, new_vehicle = new_config.pack, new_config.vehicle
                                new_limits = new_config.limits.to_command_dict()
                                new_capacity = float(new_pack.capacity_ah)
                                if not (math.isfinite(new_capacity) and new_capacity > 0):
                                    raise ValueError(f"module capacity {new_capacity!r} Ah")
                                new_settings_id = new_config.settings_fingerprint()
                                summary = (f"[LOGIC] Config updated -> {new_pack.cell_model} "
                                           f"{new_pack.series_count}S{new_pack.parallel_count}P, "
                                           f"{new_capacity:.1f} Ah, "
                                           f"{new_vehicle.total_mass_kg:.0f} kg car+driver")
                                warnings = list(new_config.limits.advisories())
                            except Exception as exc:
                                print(f"[LOGIC ERROR] Rejected bad config, nothing changed: {exc}")
                            else:
                                config, pack, vehicle = new_config, new_pack, new_vehicle
                                limits = new_limits

                                # A new pack is judged against the wiring the DAQ is
                                # running, not the new DAQ section, which waits for a
                                # restart. Neither call can raise: both fail closed.
                                config_problems = config_blockers(running_daq, pack, limits)
                                expected = expected_measurements(running_daq, pack)
                                settings_id = new_settings_id

                                # Capacity change invalidates the running coulomb
                                # count, so rebaseline rather than carry a stale Ah.
                                total_capacity_ah = new_capacity
                                remaining_ah = total_capacity_ah
                                last_coulomb_time = time.time()

                                print(summary)
                                for warning in warnings:
                                    print(f"[LOGIC WARNING] {warning}")

                                report_config_problems(config_problems)
                                if config.daq != running_daq:
                                    print("[LOGIC] DAQ settings changed. They apply on restart; "
                                          "until then ARM is judged against the wiring the DAQ "
                                          "started with.")

                    elif isinstance(cmd, tuple) and cmd[0] == "SET_OVERRIDE":
                        # Taken in any state, RUNNING included: it moves the load
                        # only within the floors a lap row is held to.
                        try:
                            override = parse_override(cmd)
                        except ValueError as exc:
                            print(f"[LOGIC] Ignoring load override: {exc}")
                        else:
                            override_due = True
                            print(f"[LOGIC] Load override {describe_override(override)}.")

                    elif isinstance(cmd, tuple) and cmd[0] == "LOAD_CSV" and fsm_state == "RUNNING":
                        # Like a new config: not under a run. It used to be taken,
                        # sending the run back to row 0 of the new profile with the
                        # old one's speed carried in, and the file was parsed on
                        # this loop while it was holding a load.
                        print("[LOGIC] Ignoring profile change while RUNNING. Stop the run first.")

                    elif isinstance(cmd, tuple) and cmd[0] == "LOAD_CSV":
                        filepath = cmd[1]
                        try:
                            lap_data, dropped = load_lap_profile(filepath)
                            total_rows = len(lap_data)
                            current_row_idx = 0
                            profile_name = os.path.basename(str(filepath))
                            print(f"\n[LOGIC] Successfully loaded new lap profile: {filepath}")
                            print(f"[LOGIC] Total rows: {total_rows}"
                                  + (f" ({dropped} unusable rows dropped)" if dropped else ""))
                            if total_rows > 1:
                                span = lap_row_interval(lap_data, 0)
                                print(f"[LOGIC] Row interval from profile timestamps: {span:.3f} s")
                            if lap_data and profile_resistance(lap_data[0]) is not None:
                                print("[LOGIC] Bench profile: each row sets the resistance, held "
                                      "to the same current, power and derate floors as a lap.")
                        except Exception as e:
                            print(f"\n[LOGIC ERROR] Failed to load new CSV: {e}")

                    elif name == "ARM" and is_valid_transition(fsm_state, "ARM"):
                        # Refused outright rather than armed into an immediate
                        # fault: nothing is wrong with the rig itself, it just
                        # cannot watch all of it, so stay in IDLE.
                        refusals = arm_refusals(data.get('hardware_status', {}), config_problems)
                        for refusal in refusals:
                            print(f"[LOGIC] Cannot ARM: {refusal}")
                        if not refusals:
                            fsm_state = "ARMED"
                            print("[LOGIC] System ARMED.")

                    elif name == "RUN" and is_valid_transition(fsm_state, "RUN"):
                        laps = run_laps(args)
                        if laps is None:
                            print(f"[LOGIC] Cannot RUN: laps must be a whole number of 1 or "
                                  f"more, not {args[0] if args else None!r}.")
                        elif total_rows > 0:
                            total_laps = laps
                            fsm_state = "RUNNING"
                            current_row_idx = 0
                            current_lap = 1
                            prev_velocity_ms = 0.0
                            prev_time_s = 0.0
                            row_interval = lap_row_interval(lap_data, 0)
                            bank_steps = None
                            # An override in force goes out on the first pass;
                            # without one, the first row does.
                            profile_steps = None
                            override_due = False
                            override_sent_at = 0.0

                            # Coulomb count deliberately CARRIES OVER between runs.
                            # Resetting to full here meant two manually-triggered
                            # back-to-back laps both started at 100%, so the counter
                            # forgot everything the first run drew -- overstating
                            # remaining charge, which is the dangerous direction.
                            # It rebaselines on a pack/config change or a restart.
                            last_coulomb_time = time.time()

                            print(f"[LOGIC] Lap Simulation STARTED. Target: {total_laps} Laps | "
                                  f"row interval {row_interval:.3f} s | "
                                  f"starting SOC {(remaining_ah / total_capacity_ah) * 100:.1f}% "
                                  f"({remaining_ah:.2f} Ah)")
                            if override is not None:
                                print(f"[LOGIC] Load override {describe_override(override)}; "
                                      f"the profile sets only the timing and laps.")
                        else:
                            print("[LOGIC] Cannot RUN: No lap data loaded!")

                    elif cmd == "STOP" and is_valid_transition(fsm_state, "STOP"):
                        # Still accepted from the queue, for callers with no E-STOP
                        # event (the bench tools); the GUI uses the event.
                        operator_stop("STOP")

                    elif name == "RESET" and is_valid_transition(fsm_state, "RESET"):
                        fsm_state = "IDLE"
                        target_res = 0.0
                        fault_reason, fault_detail = None, []
                except Empty:
                    break

            # --- D. Safety Monitors ---
            # Sensor-integrity checks only apply once the bank can actually be driven.
            # In IDLE a missing temperature link is a not-connected-yet condition, and
            # latching a fault for it would make the rig impossible to bring up.
            # The per-bank power check needs the ladder setting the current reading
            # belongs to, so only one that has been in circuit for BANK_SETTLE_S.
            settled_steps = None
            if (fsm_state == "RUNNING" and bank_steps is not None
                    and time.time() - bank_steps_at >= BANK_SETTLE_S):
                settled_steps = bank_steps

            # The load check counts new DAQ samples only, each taken after the
            # setting had settled: a packet carried forward is the same reading
            # again, and one sampled before the relays moved belongs to the
            # setting before. A reading that fits clears the count.
            if fsm_state != "RUNNING" or sil_mode or not limits.get('load_check', True):
                load_check_run, load_problem = 0, None
            elif fresh_sample and settled_steps is not None:
                sampled_at = data.get('sampled_at')
                if not _finite(sampled_at) or sampled_at - bank_steps_at >= BANK_SETTLE_S:
                    load_problem = load_mismatch(data.get('voltage'), data.get('amps'),
                                                 settled_steps)
                    load_check_run = load_check_run + 1 if load_problem else 0

            is_fault, trigger_reason = evaluate_safety(
                data, limits, armed=fsm_state in ("ARMED", "RUNNING"),
                config_problems=config_problems, bank_steps=settled_steps,
                expected=expected,
                load_problem=load_problem if load_check_run >= LOAD_CHECK_SAMPLES else None)

            # DISCONNECTED latches too. It used to be skipped along with FAULT, so
            # a rig waiting on its controller or its temperature board showed
            # nothing and logged nothing with every cell at 95 C. Only the trips
            # that apply in every state can fire there: `armed` is False.
            if is_fault and fsm_state != "FAULT":
                tripped_in = fsm_state
                had_link = res_ser is not None
                latch_fault(trigger_reason)
                # KILL first: describing, printing and logging the trip all wait
                # until it has gone out.
                killed = kill_load(res_ser)
                killed_at = time.time()
                print(f"\n[LOGIC] {trigger_reason} ALARM! Killing Load.")
                try:
                    fault_detail = trip_details(trigger_reason, data, limits, config_problems,
                                                expected, settled_steps)
                except Exception as exc:
                    # Describing a trip must never stop the loop that handles it.
                    fault_detail = [f"(could not describe the trip: {exc!r})"]
                for line in fault_detail:
                    print(f"[LOGIC] {line}")
                if not killed:
                    drop_resistor_link("KILL")
                append_trip_log(trip_record(
                    trigger_reason, tripped_in, fault_detail, data,
                    ladder_ohm=target_res if tripped_in == "RUNNING" else None,
                    kill_sent=killed if had_link else None, killed_at=killed_at))

            # --- E. Coulomb Counting Math ---
            current_time = time.time()
            dt = current_time - last_coulomb_time
            last_coulomb_time = current_time

            remaining_ah, true_soc = coulomb_step(data['amps'], dt, remaining_ah, total_capacity_ah)

            # --- F. Finite State Machine Actions ---
            if res_ser and res_ser.is_open:
                if heartbeat_due(fsm_state, time.time() - last_heartbeat):
                    # "alive" while running, KILL in every other state: see
                    # keepalive_message(). A failed one used to be swallowed,
                    # leaving the handle in place and the rig RUNNING with
                    # nothing reaching the Arduino.
                    if not write_resistor(res_ser, keepalive_message(fsm_state)):
                        drop_resistor_link("heartbeat")
                    last_heartbeat = time.time()

                if fsm_state == "RUNNING":
                    # Dwell on each row for the interval the profile itself declares,
                    # not a fixed second. A 10 Hz log played at 1 row/s would run ten
                    # times too slow and hold every power demand ten times too long.
                    if time.time() - last_physics_time >= row_interval:
                        if current_row_idx < total_rows:
                            row = lap_data[current_row_idx]
                            voltage = data['voltage']
                            current_max_temp = data['max_temp']
                            bench_ohms = profile_resistance(row)

                            if bench_ohms is not None:
                                # A bench profile names the resistance. Held to the
                                # same floors and ratings as a lap row.
                                steps = steps_for_resistance(
                                    voltage, bench_ohms, limits['max_amps'], current_max_temp,
                                    limits['derate_en'], limits['derate_start'],
                                    limits['max_temp'], power_factor=bank_power_factor(limits)
                                )
                            else:
                                # Standing start is the start of the RUN, not of a
                                # lap: lap 2 onwards inherits the car's velocity.
                                standing_start = (current_lap == 1 and current_row_idx == 0)

                                # On a lap repeat the profile's clock restarts, so the
                                # raw timestamp difference is large and negative.
                                # Bridge that one frame with the profile's own
                                # sampling interval rather than a literal, so this
                                # stays correct whatever rate the CSV was logged at.
                                wrap_dt = None
                                if current_row_idx == 0 and current_lap > 1:
                                    wrap_dt = lap_row_interval(lap_data, 0)

                                velocity_ms, acceleration, current_time_s = compute_lap_physics(
                                    row, prev_velocity_ms, prev_time_s,
                                    standing_start, current_row_idx, lap_wrap_dt=wrap_dt
                                )
                                prev_velocity_ms = velocity_ms
                                prev_time_s = current_time_s

                                req_power = compute_required_power(velocity_ms, acceleration,
                                                                   vehicle)

                                steps = command_steps(
                                    voltage, req_power, limits['max_amps'], current_max_temp,
                                    limits['derate_en'], limits['derate_start'],
                                    limits['max_temp'],
                                    modules_in_series=pack.modules_in_series,
                                    power_factor=bank_power_factor(limits)
                                )

                            # Worked out even while overridden, so the physics keeps
                            # pace with the lap and handing back is immediate.
                            profile_steps = steps
                            if override is None:
                                set_ladder(steps)

                            row_interval = lap_row_interval(lap_data, current_row_idx)
                            current_row_idx += 1
                            last_physics_time = time.time()
                        else:
                            if current_lap < total_laps:
                                current_lap += 1
                                current_row_idx = 0
                                row_interval = lap_row_interval(lap_data, 0)
                                print(f"[LOGIC] Starting Lap {current_lap} of {total_laps}")
                            else:
                                target_res = 0.0
                                if kill_load(res_ser):
                                    fsm_state = "IDLE"
                                    print(f"[LOGIC] All {total_laps} laps completed. System Idling.")
                                else:
                                    # Still RUNNING here, so this latches a fault:
                                    # the KILL that ends the run never went out.
                                    drop_resistor_link("KILL at the end of the run")
                                last_physics_time = time.time()

                    # The operator's override, in place of the profile's load: sent
                    # as soon as it changes, and re-solved every OVERRIDE_REFRESH_S
                    # because a power override follows the module voltage. When it
                    # is handed back, the current row's own setting goes out at once.
                    if fsm_state == "RUNNING" and (override is not None or override_due):
                        if override_due or time.time() - override_sent_at >= OVERRIDE_REFRESH_S:
                            steps = profile_steps
                            if override is not None:
                                voltage = data['voltage']
                                steps = steps_for_resistance(
                                    voltage, override_resistance(override, voltage),
                                    limits['max_amps'], data['max_temp'],
                                    limits['derate_en'], limits['derate_start'],
                                    limits['max_temp'], power_factor=bank_power_factor(limits)
                                )
                            override_due = False
                            override_sent_at = time.time()
                            if steps is not None:
                                set_ladder(steps)

            # --- G. Pipeline Forwarding ---
            data['fsm_state'] = fsm_state
            data['target_resistance'] = target_res
            data['current_lap'] = current_lap
            data['total_laps'] = total_laps
            # The profile in force and the row last sent, for the window and
            # the recording. Rows count from 1; 0 before the first is sent.
            data['profile_name'] = profile_name
            data['profile_rows'] = total_rows
            data['profile_row'] = current_row_idx if fsm_state == "RUNNING" else 0
            # What the ladder setting in circuit should draw at this voltage, for
            # the recording: the load check's own figure. None when not running.
            data['expected_amps'] = (expected_load_amps(data.get('voltage'), bank_steps)
                                     if fsm_state == "RUNNING" else None)
            data['remaining_ah'] = remaining_ah
            data['true_soc'] = true_soc
            data['hardware_status']['res_arduino'] = (res_ser is not None)
            data['arm_refusals'] = arm_refusals(data['hardware_status'], config_problems)
            # The settings actually in force, as the acknowledgement for
            # SET_LIMITS and SET_CONFIG: either can be rejected (a config while
            # RUNNING) or dropped by the bounded command queue, and the GUI
            # compares these against what it shows. Copies, so the GUI never
            # holds this process's own objects.
            data['active_limits'] = {k: (list(v) if isinstance(v, list) else v)
                                     for k, v in limits.items()}
            data['active_settings'] = settings_id
            # The override in force, so the window can show it took.
            data['load_override'] = override
            # Why it faulted, so the window can say so rather than only FAULT.
            in_fault = fsm_state == "FAULT"
            data['fault_reason'] = fault_reason if in_fault else None
            data['fault_detail'] = list(fault_detail) if in_fault else []

            # Never blocks: a blocking get or put here would stall every trip, the
            # E-STOP and the heartbeat behind the GUI's queue. See put_latest().
            put_latest(telemetry_queue, data)
        clean_exit = True
    except Exception as exc:
        # Unexpected, and fatal to this process. Tell the GUI before going, so it
        # does not keep showing the last state it was sent -- RUNNING, say.
        print(f"\n[LOGIC CRITICAL ERROR] {exc!r}. Killing the load and stopping the "
              f"logic process.")
        crash = exc
        crashed = NEUTRAL_PACKET()
        crashed['fsm_state'] = "FAULT"
        crashed['fault_reason'] = "LOGIC ERROR"
        crashed['fault_detail'] = [repr(exc)]
        crashed['hardware_status'] = {'res_arduino': False}
        put_latest(telemetry_queue, crashed)
        raise
    finally:
        # However the loop ended, the load is killed and the port released. An
        # error used to skip all of this, leaving the bank to the watchdog.
        logic_done.set()

        # Take the handle out of the slot before closing it, so a discovery sweep
        # that finishes during shutdown cannot hand back a port we are tearing down.
        with slot_lock:
            final_ser = resistor_slot['ser'] or res_ser
            resistor_slot['ser'] = None

        killed = None
        if final_ser is not None:
            killed = kill_load(final_ser)
            try:
                final_ser.close()
            except Exception:
                pass

        # Logged after the KILL above, like every other trip.
        if crash is not None:
            append_trip_log(trip_record("LOGIC ERROR", fsm_state, [repr(crash)],
                                        kill_sent=killed, killed_at=time.time()))

        # The worker is a daemon and waits on logic_done, so it exits on its
        # own; join briefly so its port handles are released before the process
        # ends.
        discovery.join(timeout=2.0)
        if clean_exit:
            print("[LOGIC] Process cleanly shutdown.")
