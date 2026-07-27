import serial
import serial.tools.list_ports
import nidaqmx
import time
import threading
from multiprocessing import Queue, Event

from rig_config import RigConfig

# ================= CONFIGURATION =================
# Channel mapping, scaling and sensor layout all come from rig_config.json --
# see DaqConfig in rig_config.py. Only the resistor-bank baud rate stays here
# because it is fixed by the Arduino sketch, not by the pack.
RESISTOR_BAUD_RATE = 9600


def derive_cell_voltages(cumulative_voltages):
    """Difference cumulative tap readings into per-cell voltages.

    Channel i reads the sum of cells 1..i+1, so cell i is tap i minus tap i-1.
    Driven by the length of the input rather than a fixed count, so a pack with
    a different series count cannot raise an IndexError here.
    """
    if not cumulative_voltages:
        return [], 0.0

    cells = [cumulative_voltages[0]]
    for i in range(1, len(cumulative_voltages)):
        cells.append(cumulative_voltages[i] - cumulative_voltages[i - 1])
    return cells, cumulative_voltages[-1]


def parse_temperature_line(line, sensors_per_bus, bus_count):
    """Parse one CSV line from the temperature Arduino.

    Format is "<bus number>,<t1>,...,<tN>". Returns (bus_index, {i: temp}) or
    None if the line is malformed, truncated, or names a bus outside the
    configured layout -- serial noise must never take down the DAQ loop.
    """
    parts = line.split(',')
    if len(parts) != sensors_per_bus + 1:
        return None

    try:
        bus_idx = int(parts[0]) - 1
    except ValueError:
        return None

    if not (0 <= bus_idx < bus_count):
        return None

    readings = {}
    for i in range(sensors_per_bus):
        raw = parts[i + 1]
        if raw == "ERR":
            continue
        try:
            readings[i] = float(raw)
        except ValueError:
            continue

    return bus_idx, readings


def run_daq_process(telemetry_queue: Queue, stop_event: Event, config: RigConfig = None):
    config = config if config is not None else RigConfig.load()
    daq_cfg = config.daq
    pack = config.pack

    print(f"[DAQ] Pack {pack.series_count}S{pack.parallel_count}P | "
          f"{daq_cfg.channel_count} voltage channels | "
          f"{daq_cfg.temp_bus_count}x{daq_cfg.sensors_per_bus} = "
          f"{daq_cfg.sensor_count} thermistors")
    for problem in daq_cfg.validate(pack):
        print(f"[DAQ WARNING] {problem}")

    hardware_state = {
        'temp_ser': None,
        'res_port': None
    }
    state_lock = threading.Lock()

    def connection_watchdog():
        """Hunts for missing Arduinos and verifies existing ones across multiple baud rates."""
        while not stop_event.is_set():
            with state_lock:
                current_active_ports = [p.device for p in serial.tools.list_ports.comports()]

                if hardware_state['res_port'] and hardware_state['res_port'] not in current_active_ports:
                    print("[WATCHDOG] Resistor Controller LOST! Device unplugged.")
                    hardware_state['res_port'] = None

                needs_temp = hardware_state['temp_ser'] is None
                needs_res = hardware_state['res_port'] is None

            if needs_temp or needs_res:
                ports = serial.tools.list_ports.comports()
                for port in ports:
                    if stop_event.is_set(): break

                    # Skip ports we already actively own
                    with state_lock:
                        if (hardware_state['temp_ser'] and hardware_state['temp_ser'].port == port.device) or \
                                (hardware_state['res_port'] == port.device):
                            continue

                    found = False
                    # FIX 1: Try both baud rates to catch both Arduinos
                    for baud in [daq_cfg.temp_baud_rate, RESISTOR_BAUD_RATE]:
                        if found: break
                        try:
                            ser = serial.Serial(port.device, baud, timeout=2)
                            time.sleep(2)  # Wait for Arduino to clear bootloader
                            ser.reset_input_buffer()

                            ser.write(b"?WHOAMI\n")
                            response = ser.readline().decode('utf-8').strip()

                            with state_lock:
                                if response == "TEMP_SENSOR" and hardware_state['temp_ser'] is None:
                                    # FIX 2: Do NOT close the port! Prevent double-reset.
                                    ser.timeout = 0.1
                                    hardware_state['temp_ser'] = ser
                                    print(f"[WATCHDOG] RECONNECTED: Temp Sensor on {port.device}")
                                    found = True

                                elif response == "RESISTOR_CTRL" and hardware_state['res_port'] is None:
                                    hardware_state['res_port'] = port.device
                                    print(f"[WATCHDOG] RECONNECTED: Resistor Controller on {port.device}")
                                    ser.close()  # We close this because logic script uses it, not DAQ
                                    found = True
                                else:
                                    ser.close()
                        except Exception:
                            pass

            time.sleep(3)

    print("[DAQ] Booting Self-Healing Watchdog...")
    watchdog = threading.Thread(target=connection_watchdog, daemon=True)
    watchdog.start()

    battery_temps = [[0.0] * daq_cfg.sensors_per_bus for _ in range(daq_cfg.temp_bus_count)]

    # --- FIX 3: NI-DAQ DESK TEST BYPASS ---
    ni_daq_active = False
    task = None
    try:
        task = nidaqmx.Task()
        task.ai_channels.add_ai_voltage_chan(
            daq_cfg.current_channel,
            min_val=daq_cfg.ai_min_volts, max_val=daq_cfg.ai_max_volts)
        for ch in daq_cfg.voltage_channels:
            task.ai_channels.add_ai_voltage_chan(
                ch, min_val=daq_cfg.ai_min_volts, max_val=daq_cfg.ai_max_volts)
        ni_daq_active = True
        print("[DAQ] Hardware NI-DAQ initialized.")
    except Exception as e:
        print(f"\n[WARNING] NI-DAQ Hardware not found: {e}")
        print("[WARNING] Running in DESK TEST / SIMULATION MODE.")
        if task: task.close()

    try:
        while not stop_event.is_set():
            loop_start = time.time()

            # --- A. Read Voltages and Current ---
            if ni_daq_active:
                daq_data = task.read()
                current = daq_data[0] * daq_cfg.current_amps_per_volt
                raw_daq_voltages = daq_data[1:]
                actual_cumulative_voltages = [
                    v * daq_cfg.voltage_multiplier for v in raw_daq_voltages]
            else:
                # SIMULATION DATA (Desk Test Mode): a healthy pack at rest, sized
                # to the configured series count rather than a fixed 12S.
                current = 15.0
                sim_cell_v = pack.cell_nominal_voltage + 0.5
                actual_cumulative_voltages = [
                    sim_cell_v * (i + 1) for i in range(pack.series_count)]

            cell_voltages, total_pack_voltage = derive_cell_voltages(actual_cumulative_voltages)

            # --- B. Read Temperatures ---
            with state_lock:
                current_temp_ser = hardware_state['temp_ser']
                res_status = hardware_state['res_port'] is not None

            if current_temp_ser:
                try:
                    if current_temp_ser.in_waiting > 0:
                        lines = current_temp_ser.readlines()
                        for line in lines:
                            decoded_line = line.decode('utf-8', errors='ignore').strip()
                            parsed = parse_temperature_line(
                                decoded_line, daq_cfg.sensors_per_bus, daq_cfg.temp_bus_count)
                            if parsed is None:
                                continue
                            bus_idx, readings = parsed
                            for i, temp_c in readings.items():
                                battery_temps[bus_idx][i] = temp_c
                except serial.SerialException:
                    print("\n[DAQ ERROR] Temperature Sensor LOST! Watchdog engaging...")
                    current_temp_ser.close()
                    with state_lock:
                        hardware_state['temp_ser'] = None

            # Guard the empty case: a misconfigured zero-bus layout must not
            # raise on max() of an empty sequence.
            max_t = max((max(bus) for bus in battery_temps if bus), default=0.0)

            # --- C. Package Data and Send to Queue ---
            data_packet = {
                'amps': current,
                'voltage': total_pack_voltage,
                'cell_voltages': cell_voltages,
                'power_kw': (current * total_pack_voltage) / 1000.0,
                'temperatures': battery_temps,
                'max_temp': max_t,
                'hardware_status': {
                    'temp_arduino': current_temp_ser is not None,
                    'res_arduino': res_status,
                    'ni_daq': ni_daq_active
                }
            }

            if telemetry_queue.full():
                telemetry_queue.get()
            telemetry_queue.put(data_packet)

            elapsed = time.time() - loop_start
            if elapsed < daq_cfg.sample_period_s:
                time.sleep(daq_cfg.sample_period_s - elapsed)

    except Exception as e:
        print(f"\n[DAQ CRITICAL ERROR]: {e}")
    finally:
        with state_lock:
            if hardware_state['temp_ser'] and hardware_state['temp_ser'].is_open:
                hardware_state['temp_ser'].close()
        if ni_daq_active and task:
            task.close()
        print("[DAQ] Process cleanly shutdown.")