import math
import serial
import serial.tools.list_ports
import nidaqmx
from nidaqmx.constants import CJCSource, TemperatureUnits, ThermocoupleType
import time
import threading
from multiprocessing import Queue, Event

from rig_config import RigConfig
from queue_util import put_latest

# ================= CONFIGURATION =================
# Channel mapping, scaling and sensor layout all come from rig_config.json --
# see DaqConfig in rig_config.py.
#
# Nothing about the resistor controller belongs in this module. This process
# owns the NI-DAQ and the temperature Arduino; control_logic owns the resistor
# controller, including its baud rate and its port discovery.

# Resistor thermocouples are read on their own thread, this often. The banks
# heat over minutes, and some NI thermocouple modules (the 4-channel 9211) take
# ~0.3 s for one on-demand read, which inline would cut the voltage and current
# loop to ~3 Hz.
RESISTOR_TC_PERIOD_S = 0.25

# Readings outside this band are not temperatures. An open or broken
# thermocouple typically reads far past the top of it, or as NaN. Also passed to
# DAQmx as the channel's expected range: its default of 0-100 C is below what
# the banks reach.
TC_MIN_C = -40.0
TC_MAX_C = 600.0

# What a DS18B20 can measure. A cell reading outside it is a sensor fault, and
# is skipped like an ERR so that sensor's age grows.
DS18B20_MIN_C = -55.0
DS18B20_MAX_C = 125.0


def derive_cell_voltages(cumulative_voltages):
    """Difference cumulative tap readings into per-cell voltages.

    Channel i reads the sum of cells 1..i+1, so cell i is tap i minus tap i-1.
    Driven by the length of the input rather than a fixed count, so a pack with
    a different series count cannot raise an IndexError here.
    """
    if not cumulative_voltages: #if the list is empty
        return [], 0.0 #return an empty list and 0 voltage

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
        return None #if the bus ID and temps dont add up then something is wrong and this data should be dicarded

    try:
        bus_idx = int(parts[0]) - 1 #convert the bus number into an index
    except ValueError: #if its not an int thats fine just pass it since its likely noise
        return None

    if not (0 <= bus_idx < bus_count): #if the bus number is not in the ranges of expected bus numbers get rid of it
        return None

    readings = {} #create a dictionary
    for i in range(sensors_per_bus): #i = sensor index
        raw = parts[i + 1]
        if raw == "ERR":
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        # float() accepts "nan" and "inf". Neither is a temperature, and a NaN
        # loses every comparison, so it would hide from max() and the overtemp
        # trip while still counting as a fresh reading for that sensor. Nor is
        # anything outside the DS18B20's range: -127 is the library's
        # "disconnected" value, and as a fresh reading it would make that cell
        # look cold. The sketch already sends ERR for anything at or below
        # -100 C; this holds if it ever stops. 85 C, the power-on value, is
        # within range and reads hot, so it fails closed on its own.
        if math.isfinite(value) and DS18B20_MIN_C <= value <= DS18B20_MAX_C:
            readings[i] = value

    return bus_idx, readings


def temperature_sensor_ages(last_valid_rx, now):
    """Seconds since each sensor last delivered a valid reading.

    Same [bus][sensor] shape as the temperature array. The stream-wide
    temp_age_s cannot see a single failed sensor: when one DS18B20 reports ERR,
    the other sensors on the bus keep the stream fresh while that sensor's last
    value sits frozen in the array. This is what lets the logic process fault on
    it.

    A sensor that has never delivered a number -- ERR since power-up -- reads
    inf, so it cannot pass as fresh.
    """
    return [reading_ages(bus, now) for bus in last_valid_rx]


def reading_ages(last_valid_rx, now):
    """Flat version of temperature_sensor_ages(): one age per timestamp, inf for None."""
    return [now - t if t is not None else float('inf') for t in last_valid_rx]


def apply_thermocouple_readings(raw, temps, last_rx, now):
    """Store each plausible reading from one thermocouple read, and stamp its time.

    `raw` is what task.read() returned: a list, or a bare float when only one
    channel is configured. Readings that are not finite or fall outside
    TC_MIN_C..TC_MAX_C are skipped. They leave that bank's last value and
    timestamp alone, so its age grows and the logic process faults on it once
    armed -- the same treatment as a DS18B20 reporting ERR.
    """
    if not isinstance(raw, (list, tuple)):
        raw = [raw]
    for i, value in enumerate(raw[:len(temps)]):
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and TC_MIN_C <= value <= TC_MAX_C:
            temps[i] = value
            last_rx[i] = now


def run_daq_process(telemetry_queue: Queue, stop_event: Event, config: RigConfig = None,
                    discovery_lock=None):
    config = config if config is not None else RigConfig.load()
    daq_cfg = config.daq
    pack = config.pack

    print(f"[DAQ] Pack {pack.series_count}S{pack.parallel_count}P | " #print current config file loaded
          f"{daq_cfg.channel_count} voltage channels | "
          f"{daq_cfg.temp_bus_count}x{daq_cfg.sensors_per_bus} = "
          f"{daq_cfg.sensor_count} thermistors")
    # The logic process refuses ARM on the first group; see rig_config.arm_blockers.
    for problem in daq_cfg.safety_problems(pack):
        print(f"[DAQ CONFIG ERROR] {problem}")
    for problem in daq_cfg.advisories(pack):
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

    # The same timestamp per sensor. Stream-wide freshness says nothing about a
    # single sensor reporting ERR while the rest of its bus keeps arriving.
    # None means no valid reading yet.
    sensor_last_rx = [[None] * daq_cfg.sensors_per_bus for _ in range(daq_cfg.temp_bus_count)]

    # --- NI-DAQ: current and cumulative cell voltages ---
    # No simulated fallback. A DAQ that failed to start used to be replaced by a
    # healthy pack at rest (15 A, every cell at nominal + 0.5 V), so a rig
    # started without its DAQ drove the real bank while the overcurrent,
    # undervoltage and cell trips watched made-up numbers. Desk testing is what
    # the SIL dongle is for. Without the DAQ this process publishes zero current,
    # zero volts and no cells with hardware_status['ni_daq'] False, and the logic
    # process refuses to arm on that.
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
        print("[WARNING] Current and voltage are NOT being measured. The rig will not arm.")
        if task: task.close()
        task = None

    # --- Resistor bank thermocouples ---
    # A task of their own, so a slow or missing thermocouple module cannot stall
    # or break the voltage and current reads. Based on Bryce Marshall's bench
    # test (tools/thermocouple_check.py), with two settings it left at
    # nidaqmx's defaults made explicit: the cold junction (default a fixed 25 C)
    # and the expected range (default 0-100 C, below what the banks reach).
    #
    # Never simulated. With no thermocouple data the logic process refuses to
    # arm, rather than running the bank on made-up resistor temperatures.
    n_tc = len(daq_cfg.resistor_tc_channels)
    resistor_temps = [None] * n_tc          # last valid reading per bank, C
    resistor_last_rx = [None] * n_tc        # when it arrived; None = never
    tc_lock = threading.Lock()
    tc_task = None
    tc_thread = None
    # Its own stop signal: the shared stop_event is not set if this process
    # exits on an error, and the task must not be closed under a running read.
    tc_stop = threading.Event()
    try:
        tc_task = nidaqmx.Task()
        for ch in daq_cfg.resistor_tc_channels:
            tc_task.ai_channels.add_ai_thrmcpl_chan(
                ch, min_val=TC_MIN_C, max_val=TC_MAX_C,
                units=TemperatureUnits.DEG_C,
                thermocouple_type=ThermocoupleType[daq_cfg.thermocouple_type],
                cjc_source=CJCSource[daq_cfg.resistor_tc_cjc_source])
        print(f"[DAQ] Resistor thermocouples initialized: {n_tc} bank(s), "
              f"type {daq_cfg.thermocouple_type}, CJC {daq_cfg.resistor_tc_cjc_source}.")
    except Exception as e:
        print(f"\n[WARNING] Resistor thermocouples unavailable: {e}")
        print("[WARNING] The rig will not arm without resistor temperatures.")
        if tc_task: tc_task.close()
        tc_task = None

    def resistor_tc_reader():
        """Reads every bank thermocouple once per RESISTOR_TC_PERIOD_S."""
        reported = False
        while not (stop_event.is_set() or tc_stop.is_set()):
            started = time.time()
            try:
                raw = tc_task.read()
                reported = False
            except Exception as exc:
                # Leave the timestamps alone so the ages grow and the logic
                # process faults. Say so once, not ten times a second.
                if not reported:
                    print(f"\n[DAQ ERROR] Resistor thermocouple read failed: {exc}")
                    reported = True
                raw = []
            with tc_lock:
                apply_thermocouple_readings(raw, resistor_temps, resistor_last_rx, time.time())
            tc_stop.wait(max(0.0, RESISTOR_TC_PERIOD_S - (time.time() - started)))

    if tc_task is not None:
        tc_thread = threading.Thread(target=resistor_tc_reader, daemon=True)
        tc_thread.start()

    try:
        while not stop_event.is_set():
            loop_start = time.time()

            # --- A. Read Voltages and Current ---
            # Zero and empty unless the DAQ delivers: the same "no data" reading
            # as control_logic.NEUTRAL_PACKET, never a plausible healthy pack.
            current = 0.0
            actual_cumulative_voltages = []
            if ni_daq_active:
                try:
                    daq_data = task.read()
                    current = daq_data[0] * daq_cfg.current_amps_per_volt
                    raw_daq_voltages = daq_data[1:]
                    actual_cumulative_voltages = [
                        v * daq_cfg.voltage_multiplier for v in raw_daq_voltages]
                except Exception as exc:
                    # Lost mid-session: USB pulled, chassis powered down. This
                    # used to end the loop, so packets stopped and the logic
                    # process only noticed once DAQ DATA STALE timed out.
                    # Carrying on with ni_daq False faults it on the next packet
                    # instead, and keeps the temperatures reaching the GUI. Not
                    # retried: restart the program once the DAQ is back.
                    print(f"\n[DAQ ERROR] NI-DAQ read failed: {exc}")
                    print("[DAQ ERROR] Current and voltage are no longer measured.")
                    ni_daq_active = False
                    try:
                        task.close()
                    except Exception:
                        pass
                    task = None

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
                            rx_time = time.time()
                            for i, temp_c in readings.items():
                                battery_temps[bus_idx][i] = temp_c
                                sensor_last_rx[bus_idx][i] = rx_time
                            if readings:
                                last_temp_rx = rx_time
                except serial.SerialException:
                    print("\n[DAQ ERROR] Temperature Sensor LOST! Watchdog engaging...")
                    current_temp_ser.close()
                    with state_lock:
                        hardware_state['temp_ser'] = None

            # Guard the empty case: a misconfigured zero-bus layout must not
            # raise on max() of an empty sequence.
            max_t = max((max(bus) for bus in battery_temps if bus), default=0.0)

            # --- C. Package Data and Send to Queue ---
            now = time.time()
            with tc_lock:
                bank_temps = list(resistor_temps)
                bank_ages = reading_ages(resistor_last_rx, now)
            data_packet = {
                'amps': current,
                'voltage': total_pack_voltage,
                'cell_voltages': cell_voltages,
                'power_kw': (current * total_pack_voltage) / 1000.0,
                'temperatures': battery_temps,
                'max_temp': max_t,
                'temp_age_s': now - last_temp_rx,
                'temp_sensor_ages_s': temperature_sensor_ages(sensor_last_rx, now),
                # Bank 1 first. None until a bank's first valid reading.
                'resistor_temps': bank_temps,
                'resistor_temp_ages_s': bank_ages,
                'hardware_status': {
                    'temp_arduino': current_temp_ser is not None,
                    'resistor_tc': tc_task is not None,
                    # Placeholder only. This process does not talk to the
                    # resistor controller and cannot know its state;
                    # control_logic owns that port and overwrites this key on
                    # every packet before the GUI ever sees it. The key is kept
                    # so the dict shape is stable for anything reading it early.
                    'res_arduino': False,
                    'ni_daq': ni_daq_active
                }
            }

            # Never blocks; see put_latest(). A blocking get here could wait
            # forever once the logic process drained the queue, and the DAQ
            # would stop publishing altogether.
            put_latest(telemetry_queue, data_packet)

            elapsed = time.time() - loop_start
            if elapsed < daq_cfg.sample_period_s:
                time.sleep(daq_cfg.sample_period_s - elapsed)

    except Exception as e:
        print(f"\n[DAQ CRITICAL ERROR]: {e}")
    finally:
        with state_lock:
            if hardware_state['temp_ser'] and hardware_state['temp_ser'].is_open:
                hardware_state['temp_ser'].close()
        if task is not None:
            task.close()
        tc_stop.set()
        if tc_thread is not None:
            tc_thread.join(timeout=2.0)
        if tc_task is not None:
            tc_task.close()
        print("[DAQ] Process cleanly shutdown.")