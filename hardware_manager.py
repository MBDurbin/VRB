import serial
import serial.tools.list_ports
import nidaqmx
import time
import threading
from multiprocessing import Queue, Event

# ================= CONFIGURATION =================
TEMP_BAUD_RATE = 115200
RESISTOR_BAUD_RATE = 9600

CURRENT_CHANNEL = "cDAQ1Mod8/ai0"
VOLTAGE_CHANNELS = [
    "cDAQ1Mod8/ai1", "cDAQ1Mod8/ai2", "cDAQ1Mod8/ai3",
    "cDAQ1Mod7/ai0", "cDAQ1Mod7/ai1", "cDAQ1Mod7/ai2", "cDAQ1Mod7/ai3",
    "cDAQ1Mod6/ai0", "cDAQ1Mod6/ai1", "cDAQ1Mod6/ai2", "cDAQ1Mod6/ai3",
    "cDAQ1Mod5/ai0"
]
VOLTAGE_MULTIPLIER = 11.0


def run_daq_process(telemetry_queue: Queue, stop_event: Event):
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
                    for baud in [115200, 9600]:
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

    battery_temps = [[0.0] * 8 for _ in range(6)]

    # --- FIX 3: NI-DAQ DESK TEST BYPASS ---
    ni_daq_active = False
    task = None
    try:
        task = nidaqmx.Task()
        task.ai_channels.add_ai_voltage_chan(CURRENT_CHANNEL, min_val=-10.0, max_val=10.0)
        for ch in VOLTAGE_CHANNELS:
            task.ai_channels.add_ai_voltage_chan(ch, min_val=-10.0, max_val=10.0)
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
                current = daq_data[0] * 100.0
                raw_daq_voltages = daq_data[1:]
                actual_cumulative_voltages = [v * VOLTAGE_MULTIPLIER for v in raw_daq_voltages]
            else:
                # SIMULATION DATA (Desk Test Mode)
                current = 15.0
                actual_cumulative_voltages = [4.1 * (i + 1) for i in range(12)]  # Perfect 4.1V cells

            cell_voltages = [0.0] * 12
            cell_voltages[0] = actual_cumulative_voltages[0]
            for i in range(1, 12):
                cell_voltages[i] = actual_cumulative_voltages[i] - actual_cumulative_voltages[i - 1]
            total_pack_voltage = actual_cumulative_voltages[-1]

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
                            data = decoded_line.split(',')
                            if len(data) == 9:
                                try:
                                    bus_idx = int(data[0]) - 1
                                    for i in range(8):
                                        if data[i + 1] != "ERR":
                                            battery_temps[bus_idx][i] = float(data[i + 1])
                                except ValueError:
                                    pass
                except serial.SerialException:
                    print("\n[DAQ ERROR] Temperature Sensor LOST! Watchdog engaging...")
                    current_temp_ser.close()
                    with state_lock:
                        hardware_state['temp_ser'] = None

            max_t = max(max(bus) for bus in battery_temps)

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
            if elapsed < 0.1:
                time.sleep(0.1 - elapsed)

    except Exception as e:
        print(f"\n[DAQ CRITICAL ERROR]: {e}")
    finally:
        with state_lock:
            if hardware_state['temp_ser'] and hardware_state['temp_ser'].is_open:
                hardware_state['temp_ser'].close()
        if ni_daq_active and task:
            task.close()
        print("[DAQ] Process cleanly shutdown.")