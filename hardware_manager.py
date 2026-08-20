import serial
import serial.tools.list_ports
import nidaqmx
import time
import threading
from multiprocessing import Queue, Event

from rig_config import RigConfig

# ================= CONFIGURATION =================
# Channel mapping, scaling and sensor layout all come from rig_config.json --
# see DaqConfig in rig_config.py.
#
# Nothing about the resistor controller belongs in this module. This process
# owns the NI-DAQ and the temperature Arduino; control_logic owns the resistor
# controller, including its baud rate and its port discovery.


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


def run_daq_process(telemetry_queue: Queue, stop_event: Event, config: RigConfig = None,
                    discovery_lock=None):
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
    }
    state_lock = threading.Lock()

    def connection_watchdog():
        """Hunts for the temperature Arduino and reconnects it when it drops.

        This process owns the temperature sensor ONLY. control_logic owns the
        resistor controller and finds it with its own sweep.

        That split matters. This watchdog used to hunt for RESISTOR_CTRL as well,
        which was worse than redundant:

          * control_logic holds that port open, so this process could never
            claim it. res_port therefore stayed None permanently, the
            "do I still need to find it" condition never went false, and the
            sweep ran every 3 s for the life of the process instead of stopping
            once the temperature sensor was connected.
          * Every one of those sweeps blocked 2 s per baud rate per port, and
            opening then closing a port toggles DTR, which hard-resets whatever
            Arduino is on it. When control_logic was between its own reconnect
            attempts the port was briefly free, so this thread could grab the
            resistor controller, identify it, close it -- and reset it out from
            under the process that actually drives the bank.
          * The result was discarded anyway: control_logic overwrites
            hardware_status['res_arduino'] on every packet.
        """
        def sweep():
            """One pass over every COM port looking for TEMP_SENSOR."""
            for port in serial.tools.list_ports.comports():
                if stop_event.is_set():
                    return

                # Skip the port we already actively own
                with state_lock:
                    if hardware_state['temp_ser'] and hardware_state['temp_ser'].port == port.device:
                        continue

                try:
                    ser = serial.Serial(port.device, daq_cfg.temp_baud_rate, timeout=2)
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
                            return
                        ser.close()
                except Exception:
                    pass

        while not stop_event.is_set():
            with state_lock:
                needs_temp = hardware_state['temp_ser'] is None

            if needs_temp:
                # Serialise against control_logic, which sweeps the same ports
                # looking for the resistor controller. Without this the two
                # processes collide: whichever opens a port second gets an access
                # denial, and each open/close toggles DTR and resets whatever
                # Arduino is on the far end -- including the other process's.
                # Held across the whole sweep, so a probe cannot land between
                # another process's open and its close.
                if discovery_lock is not None:
                    with discovery_lock:
                        sweep()
                else:
                    sweep()

            # wait() rather than sleep() so shutdown is not delayed by up to 3 s.
            stop_event.wait(3)

    print("[DAQ] Booting Self-Healing Watchdog...")
    watchdog = threading.Thread(target=connection_watchdog, daemon=True)
    watchdog.start()

    battery_temps = [[0.0] * daq_cfg.sensors_per_bus for _ in range(daq_cfg.temp_bus_count)]

    # When the temperature link drops, battery_temps keeps its last values and
    # would otherwise be republished forever as if fresh. Timestamping it lets
    # the logic process refuse stale readings instead of trusting a frozen
    # temperature while the cells keep heating.
    last_temp_rx = time.time()

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
                            if readings:
                                last_temp_rx = time.time()
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
                'temp_age_s': time.time() - last_temp_rx,
                'hardware_status': {
                    'temp_arduino': current_temp_ser is not None,
                    # Placeholder only. This process does not talk to the
                    # resistor controller and cannot know its state;
                    # control_logic owns that port and overwrites this key on
                    # every packet before the GUI ever sees it. The key is kept
                    # so the dict shape is stable for anything reading it early.
                    'res_arduino': False,
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