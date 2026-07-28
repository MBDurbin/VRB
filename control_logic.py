import os
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
DEFAULT_LAP_CSV = "FSAE - ETS - Speed and Time 1 Lap.csv"
CSV_FILENAME = os.path.join(PROJECT_DIR, DEFAULT_LAP_CSV)

MAX_RESISTANCE = 63.75
RESISTOR_RESOLUTION = 0.25
RESISTOR_SCAN_COOLDOWN = 3.0


# Module-level default. Mutating a field here retunes every call that does not
# pass an explicit params object.
DEFAULT_VEHICLE = VehicleParams()


def auto_detect_resistor():
    ports = serial.tools.list_ports.comports()
    for port in ports:
        try:
            ser = serial.Serial(port.device, RESISTOR_BAUD_RATE, timeout=2)
            time.sleep(2)
            ser.reset_input_buffer()
            ser.write(b"?WHOAMI\n")
            response = ser.readline().decode('utf-8').strip()
            if response == "RESISTOR_CTRL":
                ser.close()
                print(f"[LOGIC] Resistor Bank secured on {port.device}")
                return serial.Serial(port.device, RESISTOR_BAUD_RATE, timeout=0.1)
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
def check_safety_trip(max_temp, amps, voltage, max_safe_temp, max_safe_current,
                      current_buffer, min_safe_voltage):
    """Returns (is_fault, reason).

    reason is 'OVERTEMP', 'OVERCURRENT', 'UNDERVOLTAGE', or None. Checked in that
    priority order so the reported cause is deterministic when several trip at once.

    Undervoltage is measured under load, so min_safe_voltage should sit above the
    cell's absolute cutoff (2.5 V/cell = 30.0 V for 12S) to leave room for IR sag.
    """
    over_temp = max_temp >= max_safe_temp
    over_current = amps >= (max_safe_current + current_buffer)
    under_voltage = voltage <= min_safe_voltage

    if over_temp:
        return True, "OVERTEMP"
    if over_current:
        return True, "OVERCURRENT"
    if under_voltage:
        return True, "UNDERVOLTAGE"
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
        return False, None

    lowest = min(cell_voltages)
    if lowest < sense_floor:
        return True, "CELL SENSE FAULT"
    if lowest <= min_cell_voltage:
        return True, "CELL UNDERVOLTAGE"
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


def evaluate_safety(data, limits, check_sensors=True):
    """All safety checks against one telemetry packet. Returns (is_fault, reason).

    Measured dangers are reported ahead of data-integrity faults: if the pack is
    genuinely over-current AND the temperature link has dropped, the operator
    needs to hear about the current first.

    check_sensors is False outside the armed/running states, where a missing
    temperature link is an ordinary not-connected-yet condition rather than a
    fault to latch.
    """
    fault, reason = check_safety_trip(
        data.get('max_temp', 0.0), data.get('amps', 0.0), data.get('voltage', 0.0),
        limits['max_temp'], limits['max_amps'], limits['amp_buffer'], limits['min_volts'],
    )
    if fault:
        return True, reason

    fault, reason = check_cell_safety(
        data.get('cell_voltages', []), limits['min_cell_volts'], limits['cell_sense_floor'])
    if fault:
        return True, reason

    if check_sensors:
        fault, reason = check_sensor_health(
            data.get('temp_age_s', 0.0),
            data.get('hardware_status', {}).get('temp_arduino', False),
            limits['temp_stale_timeout'],
        )
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


def compute_lap_physics(row, prev_velocity_ms, prev_time_s, is_first_row, row_idx):
    """Derives velocity/acceleration for the current lap-profile row."""
    speed_mph = float(row.get('Speed (mph)', 0))
    velocity_ms = speed_mph * 0.44704
    current_time_s = float(row.get('Time (s)', row_idx))

    if is_first_row:
        dt_physics = 1.0
        dv = 0.0
    else:
        dt_physics = current_time_s - prev_time_s
        if dt_physics <= 0:
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

    Both current clamps below divide the MODULE voltage, because that is what is
    actually across the bank.

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

    clamp_min_r = voltage / max(max_safe_current, 1.0)
    if req_r < clamp_min_r:
        req_r = clamp_min_r

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
        derate_min_r = voltage / max(active_current_limit, 1.0)

        if req_r < derate_min_r:
            req_r = min(MAX_RESISTANCE, derate_min_r)

    return req_r


def resistance_to_steps(req_r):
    return int(round(max(RESISTOR_RESOLUTION, req_r) / RESISTOR_RESOLUTION))


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


def run_logic_process(daq_queue: Queue, telemetry_queue: Queue, gui_cmd_queue: Queue, stop_event: Event):
    res_ser = auto_detect_resistor()
    fsm_state = "DISCONNECTED"
    target_res = 0.0

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
          f"temp stale > {limits['temp_stale_timeout']:.1f} s")
    for warning in config.limits.exceedances(pack):
        print(f"[LOGIC WARNING] {warning}")

    last_heartbeat = time.time()
    last_physics_time = time.time()
    last_resistor_scan = time.time()

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
        df = pd.read_csv(CSV_FILENAME)
        lap_data = df.to_dict('records')
        total_rows = len(lap_data)
        print(f"[LOGIC] Loaded default lap profile: {total_rows} rows.")
    except Exception as e:
        print(f"[LOGIC WARNING] Failed to load default CSV: {e}")

    while not stop_event.is_set():
        try:
            data = daq_queue.get(timeout=0.5)
        except Empty:
            continue

        if res_ser is None or not res_ser.is_open:
            if fsm_state != "FAULT":
                fsm_state = "DISCONNECTED"
            if time.time() - last_resistor_scan >= RESISTOR_SCAN_COOLDOWN:
                last_resistor_scan = time.time()
                res_ser = auto_detect_resistor()
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
                        df = pd.read_csv(filepath)
                        lap_data = df.to_dict('records')
                        total_rows = len(lap_data)
                        current_row_idx = 0
                        print(f"\n[LOGIC] Successfully loaded new lap profile: {filepath}")
                        print(f"[LOGIC] Total rows: {total_rows}")
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

                        # Reset Capacity for new run
                        remaining_ah = total_capacity_ah
                        last_coulomb_time = time.time()

                        print(f"[LOGIC] Lap Simulation STARTED. Target: {total_laps} Laps.")
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
            data, limits, check_sensors=fsm_state in ("ARMED", "RUNNING"))

        if is_fault and fsm_state not in ["FAULT", "DISCONNECTED"]:
            fsm_state = "FAULT"
            print(f"\n[LOGIC] {trigger_reason} ALARM! Killing Load.")
            if res_ser:
                res_ser.write(b"KILL\n")

        # --- E. Coulomb Counting Math ---
        current_time = time.time()
        dt = current_time - last_coulomb_time
        last_coulomb_time = current_time

        remaining_ah, true_soc = coulomb_step(data['amps'], dt, remaining_ah, total_capacity_ah)

        # --- F. Finite State Machine Actions ---
        if res_ser and res_ser.is_open:
            if fsm_state in ["IDLE", "ARMED"]:
                if time.time() - last_heartbeat > 0.5:
                    try:
                        res_ser.write(b"alive\n")
                    except:
                        pass
                    last_heartbeat = time.time()

            elif fsm_state == "RUNNING":
                if time.time() - last_physics_time >= 1.0:
                    if current_row_idx < total_rows:
                        row = lap_data[current_row_idx]

                        velocity_ms, acceleration, current_time_s = compute_lap_physics(
                            row, prev_velocity_ms, prev_time_s, current_row_idx == 0, current_row_idx
                        )
                        prev_velocity_ms = velocity_ms
                        prev_time_s = current_time_s

                        req_power = compute_required_power(velocity_ms, acceleration, vehicle)

                        voltage = data['voltage']
                        current_max_temp = data['max_temp']

                        req_r = compute_target_resistance(
                            voltage, req_power, limits['max_amps'], current_max_temp,
                            limits['derate_en'], limits['derate_start'], limits['max_temp'],
                            modules_in_series=pack.modules_in_series
                        )

                        target_res = req_r
                        steps = resistance_to_steps(req_r)

                        send_binary_command(res_ser, steps)

                        current_row_idx += 1
                        last_physics_time = time.time()
                    else:
                        if current_lap < total_laps:
                            current_lap += 1
                            current_row_idx = 0
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

    if res_ser and res_ser.is_open:
        res_ser.write(b"KILL\n")
        res_ser.close()
    print("[LOGIC] Process cleanly shutdown.")