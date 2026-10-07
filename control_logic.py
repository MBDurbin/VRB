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

# ================= CONFIGURATION =================
RESISTOR_BAUD_RATE = 9600

# Resolved relative to this file so the rig runs from any checkout on any
# machine. An absolute path here would break for every future team.
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
# Lap profiles live in profiles/. The GUI's Load CSV dialog opens there too.
PROFILES_DIR = os.path.join(PROJECT_DIR, "profiles")
DEFAULT_LAP_CSV = "FSAE - ETS - Speed and Time 1 Lap.csv"
CSV_FILENAME = os.path.join(PROFILES_DIR, DEFAULT_LAP_CSV)

MAX_RESISTANCE = 63.75
RESISTOR_RESOLUTION = 0.25
RESISTOR_SCAN_COOLDOWN = 3.0

# The resistor bank's power rating. At the 0.25 ohm floor only bank 1 -- four
# 2 kW elements in parallel -- is in circuit, so it carries the peak duty
# (docs/hardware_topology.md). A property of the hardware, like the two ladder
# values above, and deliberately NOT a rig_config field: every field there can
# be edited from the GUI. The cells may support more current; the bank does not.
VRB_MAX_POWER_W = 8000.0

# Bank 1's elements are +/-5% parts (TE2000B1R0J, the J). One 5% under its
# nominal resistance draws 5% more power at the same voltage, so the resistance
# floor assumes that worst case rather than tripping BANK OVERPOWER mid-run.
RESISTOR_TOLERANCE = 0.05

# The resistor Arduino sheds load after 2 s without an "alive" or a valid
# command. 0.5 s gives four chances before it fires.
HEARTBEAT_INTERVAL_S = 0.5


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
                print(f"[LOGIC] Resistor Bank secured on {port.device}")
                return ser
            ser.close()
        except Exception:
            pass
    return None


def send_binary_command(ser, steps):
    bin_str = format(steps, '08b')[::-1]
    if bin_str == "00000000":
        bin_str = "00000001"
    try:
        ser.write((bin_str + '\n').encode('utf-8'))
    except serial.SerialException:
        pass


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


def check_bank_power(voltage, amps):
    """Resistor bank overload. Returns (is_fault, reason).

    Checked against the fixed VRB_MAX_POWER_W, never against the configurable
    limits: the current limit derives from the cells, and with high-rate cells
    it sits well above anything the bank can absorb. Module voltage times
    current is never less than what the bank itself dissipates, since wiring
    and contact drops come out of the same total.

    The resistance floor (see bank_power_floor) keeps a healthy rig under this
    at steady state, so a trip here means something the floor could not see: a
    bank element out of tolerance, a relay stuck closed, or a bad reading.
    """
    if voltage * amps > VRB_MAX_POWER_W:
        return True, "BANK OVERPOWER"
    return False, None


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


def evaluate_safety(data, limits, armed=True):
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
      * Cell and sensor data integrity. Missing data before the DAQ has
        connected is an ordinary not-connected-yet condition, not a fault.

    Once armed, all of them apply, and missing data is a fault rather than a
    reason to skip a check.
    """
    fault, reason = check_thermal_and_current(
        data.get('max_temp', 0.0), data.get('amps', 0.0),
        limits['max_temp'], limits['max_amps'], limits['amp_buffer'],
    )
    if fault:
        return True, reason

    fault, reason = check_bank_power(data.get('voltage', 0.0), data.get('amps', 0.0))
    if fault:
        return True, reason

    fault, reason = check_resistor_temps(
        data.get('resistor_temps', []), limits['resistor_max_temp'])
    if fault:
        return True, reason

    if not armed:
        return False, None

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

    return False, None

#Double Checked
def coulomb_step(amps, dt, remaining_ah, total_capacity_ah):
    """Advances coulomb counting by dt seconds. Returns (new_remaining_ah, true_soc_pct)."""
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


def bank_power_floor(voltage):
    """Lowest resistance that keeps the bank within VRB_MAX_POWER_W.

    Bank power is V^2 / R. `voltage` is the measured module voltage, which is
    never below the voltage across the bank. Stepping to a lower resistance sags
    the module further, so V^2 / R from the reading taken before the step
    over-estimates the power after it. Stepping to a higher resistance always
    lowers the power. Either way the bank stays inside its rating.

    Sized for an element at the low end of its tolerance, so the floor rather
    than the BANK OVERPOWER trip is what holds the limit.

    In practice this only ever removes the bottom step. At a full 50.4 V module
    the 0.25 ohm step would dissipate about 10 kW, while the next step, 0.5 ohm,
    cannot exceed about 5 kW at any charge. 0.25 ohm comes back once the module
    is below sqrt(0.25 * 0.95 * 8000) = 43.6 V.
    """
    return voltage ** 2 / (VRB_MAX_POWER_W * (1.0 - RESISTOR_TOLERANCE))


def resistance_floor(voltage, max_safe_current, current_max_temp, derate_enabled,
                     derate_start_temp, max_safe_temp):
    """Lowest resistance the bank may be set to right now.

    The highest of three floors, each dividing the MODULE voltage because that is
    what is across the bank:
      * the configured current limit, which comes from the cells;
      * the bank's own power rating, which no configuration can change;
      * the thermal derate, when enabled and the cells are past its start.
    """
    floor_r = max(voltage / max(max_safe_current, 1.0), bank_power_floor(voltage))

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
                               modules_in_series=1):
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
                                       derate_enabled, derate_start_temp, max_safe_temp))


def command_steps(voltage, req_power, max_safe_current, current_max_temp,
                  derate_enabled, derate_start_temp, max_safe_temp, modules_in_series=1):
    """Ladder steps to send for one profile row.

    The request rounds to the nearest 0.25 ohm step, but the floor rounds UP.
    Rounding everything to nearest used to undercut the floor at the bottom of
    the ladder: a 0.29 ohm current-limit clamp became the 0.25 ohm step, which
    draws 192 A from a full module.
    """
    req_r = compute_target_resistance(voltage, req_power, max_safe_current, current_max_temp,
                                      derate_enabled, derate_start_temp, max_safe_temp,
                                      modules_in_series=modules_in_series)
    floor_r = resistance_floor(voltage, max_safe_current, current_max_temp,
                               derate_enabled, derate_start_temp, max_safe_temp)
    return resistance_to_steps(req_r, min_r=floor_r)


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
    """
    df = pd.read_csv(path)
    rows = df.to_dict('records')

    usable = []
    for row in rows:
        try:
            speed = float(row.get('Speed (mph)'))
            stamp = float(row.get('Time (s)'))
        except (TypeError, ValueError):
            continue
        if math.isfinite(speed) and math.isfinite(stamp):
            usable.append(row)

    return usable, len(rows) - len(usable)


def lap_row_interval(lap_data, idx, default=1.0):
    """Real seconds to hold row `idx`, taken from the profile's own timestamps.

    The playback used to advance one row per wall-clock second regardless of what
    the CSV said. That happens to match the shipped 1 Hz profile, but a 10 Hz log
    would run ten times too slow AND hold each power demand ten times too long,
    over-draining the pack by the same factor. Since compute_lap_physics()
    already derives acceleration from these timestamps, playback ignoring them
    also made the two disagree about how much time a row represents.
    """
    try:
        here = float(lap_data[idx].get('Time (s)'))
        nxt = float(lap_data[idx + 1].get('Time (s)'))
    except (IndexError, KeyError, TypeError, ValueError, AttributeError):
        return default

    dt = nxt - here
    if math.isfinite(dt) and dt > 0:
        return dt
    return default


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
    """Whether the resistor controller should be sent an "alive" now.

    RUNNING is included deliberately. The heartbeat used to stop once a run
    began, leaving the per-row resistance commands as the only thing feeding the
    Arduino's 2 s watchdog. With the shipped 1 s rows that left about 0.9 s of
    margin; a profile with rows ~2 s apart, or a single command lost on the
    wire, let the watchdog fire mid-run. That opens the main contactor under full
    load, and the next command re-closes it a few milliseconds later.

    "alive" moves no relay, so this holds the bank at its current resistance
    between rows. The watchdog still protects against a stalled host, because
    the heartbeat comes from this same loop.

    Not in FAULT: the load has been killed, and letting the watchdog lapse there
    is a second, independent guarantee that it stays shed.
    """
    return (fsm_state in ("IDLE", "ARMED", "RUNNING")
            and since_last_s > HEARTBEAT_INTERVAL_S)


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


def run_logic_process(daq_queue: Queue, telemetry_queue: Queue, gui_cmd_queue: Queue,
                      stop_event: Event, discovery_lock=None):
    fsm_state = "DISCONNECTED"
    target_res = 0.0

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

    def resistor_discovery_worker():
        while not stop_event.is_set():
            with slot_lock:
                have = resistor_slot['ser'] is not None
            if not have:
                found = auto_detect_resistor(discovery_lock)
                if found is not None:
                    with slot_lock:
                        # Re-check: the main loop cannot have filled the slot,
                        # but it may have signalled shutdown while we probed.
                        if stop_event.is_set():
                            found.close()
                        else:
                            resistor_slot['ser'] = found
            stop_event.wait(RESISTOR_SCAN_COOLDOWN)

    discovery = threading.Thread(target=resistor_discovery_worker, daemon=True)
    discovery.start()

    res_ser = None

    # All limits, the pack spec and the vehicle model come from rig_config.json
    # (falling back to the P45B 12S4P defaults). Nothing here is hardcoded, so a
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
          f"bank {VRB_MAX_POWER_W / 1000:.1f} kW (fixed) | resistors "
          + "/".join(f"{t:.0f}" for t in limits['resistor_max_temp']) + " C")
    for warning in config.limits.exceedances(pack):
        print(f"[LOGIC WARNING] {warning}")

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

    try:
        lap_data, dropped = load_lap_profile(CSV_FILENAME)
        total_rows = len(lap_data)
        print(f"[LOGIC] Loaded default lap profile: {total_rows} rows"
              + (f" ({dropped} unusable rows dropped)." if dropped else "."))
    except Exception as e:
        print(f"[LOGIC WARNING] Failed to load default CSV: {e}")

    # Last packet seen, and when. The loop body must run whether or not the DAQ
    # is feeding it -- see the comment on the get() below.
    last_data = None
    last_daq_rx = time.time()
    row_interval = 1.0

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
        except Empty:
            # Carry the last packet forward, tagged with its age so the staleness
            # check can fault on it. Copy it: section G writes FSM fields into
            # the packet before forwarding.
            data = dict(last_data) if last_data is not None else NEUTRAL_PACKET()

        data['daq_age_s'] = time.time() - last_daq_rx

        # Pick up whatever the discovery thread has found. Never blocks: if the
        # slot is empty the thread is still sweeping, and this loop carries on
        # servicing commands and safety checks in the meantime.
        if res_ser is None:
            with slot_lock:
                res_ser = resistor_slot['ser']

        if res_ser is None or not res_ser.is_open:
            if fsm_state != "FAULT":
                fsm_state = "DISCONNECTED"
            if res_ser is not None:
                # Link died. Clearing the slot is what re-arms the discovery
                # thread -- it only assigns when the slot is empty.
                with slot_lock:
                    resistor_slot['ser'] = None
                res_ser = None
        else:
            if fsm_state == "DISCONNECTED" and data['hardware_status'].get('temp_arduino', False):
                fsm_state = "IDLE"

        # --- C. Process GUI Commands ---
        while not gui_cmd_queue.empty():
            try:
                cmd = gui_cmd_queue.get_nowait()

                if isinstance(cmd, tuple) and cmd[0] == "SET_LIMITS":
                    # Merge rather than replace, so a partial payload cannot drop
                    # a limit and leave that check running on a missing key.
                    limits.update(cmd[1])
                    print(f"[LOGIC] Limits updated -> {limits['max_amps']:.0f} A | "
                          f"{limits['max_temp']:.0f} C | {limits['min_volts']:.1f} V | "
                          f"{limits['min_cell_volts']:.2f} V/cell | "
                          f"derate={limits['derate_en']}")

                elif isinstance(cmd, tuple) and cmd[0] == "SET_CONFIG":
                    # Full config push from the GUI's Configure dialog: new car,
                    # new cells, or both. Rejected while RUNNING so the physics
                    # model cannot change underneath an in-progress lap.
                    if fsm_state == "RUNNING":
                        print("[LOGIC] Ignoring config change while RUNNING. Stop the run first.")
                    else:
                        try:
                            new_config = RigConfig.from_dict(cmd[1])
                            config = new_config
                            pack = config.pack
                            vehicle = config.vehicle

                            limits = config.limits.to_command_dict()

                            # Capacity change invalidates the running coulomb
                            # count, so rebaseline rather than carry a stale Ah.
                            total_capacity_ah = pack.capacity_ah
                            remaining_ah = total_capacity_ah
                            last_coulomb_time = time.time()

                            print(f"[LOGIC] Config updated -> {pack.cell_model} "
                                  f"{pack.series_count}S{pack.parallel_count}P, "
                                  f"{pack.capacity_ah:.1f} Ah, "
                                  f"{vehicle.total_mass_kg:.0f} kg car+driver")
                            for warning in config.limits.exceedances(pack):
                                print(f"[LOGIC WARNING] {warning}")
                        except Exception as exc:
                            print(f"[LOGIC ERROR] Rejected bad config: {exc}")

                elif isinstance(cmd, tuple) and cmd[0] == "LOAD_CSV":
                    filepath = cmd[1]
                    try:
                        lap_data, dropped = load_lap_profile(filepath)
                        total_rows = len(lap_data)
                        current_row_idx = 0
                        print(f"\n[LOGIC] Successfully loaded new lap profile: {filepath}")
                        print(f"[LOGIC] Total rows: {total_rows}"
                              + (f" ({dropped} unusable rows dropped)" if dropped else ""))
                        if total_rows > 1:
                            span = lap_row_interval(lap_data, 0)
                            print(f"[LOGIC] Row interval from profile timestamps: {span:.3f} s")
                    except Exception as e:
                        print(f"\n[LOGIC ERROR] Failed to load new CSV: {e}")

                elif cmd == "ARM" and is_valid_transition(fsm_state, "ARM"):
                    fsm_state = "ARMED"
                    print("[LOGIC] System ARMED.")

                elif isinstance(cmd, tuple) and cmd[0] == "RUN" and is_valid_transition(fsm_state, "RUN"):
                    if total_rows > 0:
                        total_laps = cmd[1]
                        fsm_state = "RUNNING"
                        current_row_idx = 0
                        current_lap = 1
                        prev_velocity_ms = 0.0
                        prev_time_s = 0.0
                        row_interval = lap_row_interval(lap_data, 0)

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
                    else:
                        print("[LOGIC] Cannot RUN: No lap data loaded!")

                elif cmd == "STOP" and is_valid_transition(fsm_state, "STOP"):
                    fsm_state = "FAULT"
                    if res_ser: res_ser.write(b"KILL\n")
                    print("[LOGIC] EMERGENCY STOP triggered via GUI.")

                elif cmd == "RESET" and is_valid_transition(fsm_state, "RESET"):
                    fsm_state = "IDLE"
                    target_res = 0.0
            except Empty:
                break

        # --- D. Safety Monitors ---
        # Sensor-integrity checks only apply once the bank can actually be driven.
        # In IDLE a missing temperature link is a not-connected-yet condition, and
        # latching a fault for it would make the rig impossible to bring up.
        is_fault, trigger_reason = evaluate_safety(
            data, limits, armed=fsm_state in ("ARMED", "RUNNING"))

        if is_fault and fsm_state not in ["FAULT", "DISCONNECTED"]:
            fsm_state = "FAULT"
            print(f"\n[LOGIC] {trigger_reason} ALARM! Killing Load.")
            if trigger_reason == "TEMP SENSOR FAULT":
                dead = stale_temp_sensors(data.get('temp_sensor_ages_s', []),
                                          limits['temp_stale_timeout'])
                print("[LOGIC] No valid reading from: "
                      + ", ".join(f"bus {b} sensor {s}" for b, s in dead))
            elif trigger_reason == "RESISTOR OVERTEMP":
                hot = hot_resistor_banks(data.get('resistor_temps', []),
                                         limits['resistor_max_temp'])
                print("[LOGIC] " + ", ".join(f"Bank {b} at {t:.0f} C (trip {lim:.0f} C)"
                                             for b, t, lim in hot))
            elif trigger_reason == "RESISTOR TC FAULT":
                dead = stale_resistor_banks(data.get('resistor_temp_ages_s', []),
                                            limits['temp_stale_timeout'])
                print("[LOGIC] No valid thermocouple reading from: "
                      + ", ".join(f"bank {b}" for b in dead))
            if res_ser:
                res_ser.write(b"KILL\n")

        # --- E. Coulomb Counting Math ---
        current_time = time.time()
        dt = current_time - last_coulomb_time
        last_coulomb_time = current_time

        remaining_ah, true_soc = coulomb_step(data['amps'], dt, remaining_ah, total_capacity_ah)

        # --- F. Finite State Machine Actions ---
        if res_ser and res_ser.is_open:
            if heartbeat_due(fsm_state, time.time() - last_heartbeat):
                try:
                    res_ser.write(b"alive\n")
                except serial.SerialException:
                    # Link dropped. Swallowed deliberately: the port check at
                    # the top of the loop handles reconnection, and the
                    # Arduino's own 2 s watchdog sheds the load meanwhile.
                    # Narrowed from a bare except, which also caught
                    # KeyboardInterrupt and SystemExit and would have made
                    # this process ignore a shutdown request.
                    pass
                last_heartbeat = time.time()

            if fsm_state == "RUNNING":
                # Dwell on each row for the interval the profile itself declares,
                # not a fixed second. A 10 Hz log played at 1 row/s would run ten
                # times too slow and hold every power demand ten times too long.
                if time.time() - last_physics_time >= row_interval:
                    if current_row_idx < total_rows:
                        row = lap_data[current_row_idx]

                        # Standing start is the start of the RUN, not of a lap:
                        # lap 2 onwards inherits the car's carried velocity.
                        standing_start = (current_lap == 1 and current_row_idx == 0)

                        # On a lap repeat the profile's clock restarts, so the
                        # raw timestamp difference is large and negative. Bridge
                        # that one frame with the profile's own sampling
                        # interval rather than a literal, so this stays correct
                        # whatever rate the loaded CSV was logged at.
                        wrap_dt = None
                        if current_row_idx == 0 and current_lap > 1:
                            wrap_dt = lap_row_interval(lap_data, 0)

                        velocity_ms, acceleration, current_time_s = compute_lap_physics(
                            row, prev_velocity_ms, prev_time_s,
                            standing_start, current_row_idx, lap_wrap_dt=wrap_dt
                        )
                        prev_velocity_ms = velocity_ms
                        prev_time_s = current_time_s

                        req_power = compute_required_power(velocity_ms, acceleration, vehicle)

                        voltage = data['voltage']
                        current_max_temp = data['max_temp']

                        steps = command_steps(
                            voltage, req_power, limits['max_amps'], current_max_temp,
                            limits['derate_en'], limits['derate_start'], limits['max_temp'],
                            modules_in_series=pack.modules_in_series
                        )

                        # What the ladder is actually set to, not the continuous
                        # request. Near the bottom of the ladder the two can
                        # differ by a whole step.
                        target_res = steps * RESISTOR_RESOLUTION

                        send_binary_command(res_ser, steps)

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
                            fsm_state = "IDLE"
                            target_res = 0.0
                            res_ser.write(b"KILL\n")
                            print(f"[LOGIC] All {total_laps} laps completed. System Idling.")
                            last_physics_time = time.time()

        # --- G. Pipeline Forwarding ---
        data['fsm_state'] = fsm_state
        data['target_resistance'] = target_res
        data['current_lap'] = current_lap
        data['total_laps'] = total_laps
        data['remaining_ah'] = remaining_ah
        data['true_soc'] = true_soc
        data['hardware_status']['res_arduino'] = (res_ser is not None)

        if telemetry_queue.full():
            telemetry_queue.get()
        telemetry_queue.put(data)

    # Take the handle out of the slot before closing it, so a discovery sweep
    # that finishes during shutdown cannot hand back a port we are tearing down.
    with slot_lock:
        final_ser = resistor_slot['ser'] or res_ser
        resistor_slot['ser'] = None

    if final_ser and final_ser.is_open:
        try:
            final_ser.write(b"KILL\n")
        except serial.SerialException:
            pass
        final_ser.close()

    # The worker is a daemon and waits on stop_event, so it exits on its own;
    # join briefly so its port handles are released before the process ends.
    discovery.join(timeout=2.0)
    print("[LOGIC] Process cleanly shutdown.")