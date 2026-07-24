import time
import serial
import serial.tools.list_ports
import pandas as pd
from multiprocessing import Queue, Event
from queue import Empty

# ================= CONFIGURATION =================
RESISTOR_BAUD_RATE = 9600
CSV_FILENAME = r"C:\Users\Durbi\PycharmProjects\Resistor Bank Master\FSAE - ETS - Speed and Time 1 Lap.csv"

MAX_RESISTANCE = 63.75
RESISTOR_RESOLUTION = 0.25
RESISTOR_SCAN_COOLDOWN = 3.0


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

def check_safety_trip(max_temp, amps, max_safe_temp, max_safe_current, current_buffer):
    """Returns (is_fault, reason) where reason is 'OVERTEMP', 'OVERCURRENT', or None."""
    over_temp = max_temp >= max_safe_temp
    over_current = amps >= (max_safe_current + current_buffer)
    if over_temp or over_current:
        return True, ("OVERTEMP" if over_temp else "OVERCURRENT")
    return False, None


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


def compute_required_power(velocity_ms, acceleration, mass_car=260.0, mass_driver=70.0):
    """Road-load power required to hold the profile's speed/acceleration."""
    total_mass = mass_car + mass_driver

    f_downforce = 0.5 * 1.2255 * abs(-1.0) * 2.224 * (velocity_ms ** 2)
    f_drag = 0.5 * 1.2255 * 0.6 * 2.224 * (velocity_ms ** 2)
    normal_force = (total_mass * 9.81) + f_downforce

    f_roll = 0.015 * normal_force
    f_accel = total_mass * acceleration

    f_total = f_roll + f_drag + f_accel
    return f_total * velocity_ms


def compute_target_resistance(voltage, req_power, max_safe_current, current_max_temp,
                               derate_enabled, derate_start_temp, max_safe_temp):
    """Resistance needed to hit req_power, clamped for current limit and thermal derate."""
    if req_power <= 0:
        req_r = MAX_RESISTANCE
    else:
        req_r = min(MAX_RESISTANCE, ((voltage * 9) ** 2) / req_power)

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

    max_safe_current = 182.0
    current_buffer = 5.0
    max_safe_temp = 65.0

    derate_enabled = False
    derate_start_temp = 55.0

    last_heartbeat = time.time()
    last_physics_time = time.time()
    last_resistor_scan = time.time()

    # --- COULOMB COUNTING VARIABLES ---
    # Based on Molicel P45B: 4.5Ah * 4P = 18.0Ah
    total_capacity_ah = 18.0
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
                    limits = cmd[1]
                    max_safe_current = limits.get('max_amps', max_safe_current)
                    current_buffer = limits.get('amp_buffer', current_buffer)
                    max_safe_temp = limits.get('max_temp', max_safe_temp)
                    derate_enabled = limits.get('derate_en', derate_enabled)
                    derate_start_temp = limits.get('derate_start', derate_start_temp)
                    print(
                        f"[LOGIC] Limits Updated -> Max A:{max_safe_current} | Max T:{max_safe_temp} | Derate:{derate_enabled}")

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
        is_fault, trigger_reason = check_safety_trip(
            data['max_temp'], data['amps'], max_safe_temp, max_safe_current, current_buffer
        )

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

                        req_power = compute_required_power(velocity_ms, acceleration)

                        voltage = data['voltage']
                        current_max_temp = data['max_temp']

                        req_r = compute_target_resistance(
                            voltage, req_power, max_safe_current, current_max_temp,
                            derate_enabled, derate_start_temp, max_safe_temp
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