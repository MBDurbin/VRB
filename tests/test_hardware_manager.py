"""
Unit tests for the DAQ layer's pure data handling.

Covers cumulative-tap differencing and temperature line parsing, both of which
previously assumed a fixed 12S / 6x8 layout and would raise or silently corrupt
readings on any other pack, and what run_daq_process publishes when its NI-DAQ
is missing or lost (nidaqmx faked). No NI-DAQ or serial hardware required.
"""
import math
import os
import queue
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hardware_manager
from hardware_manager import (
    derive_cell_voltages, parse_temperature_line, temperature_sensor_ages,
    apply_thermocouple_readings, reading_ages, TC_MAX_C)
from rig_config import RigConfig


# ================= CELL VOLTAGE DIFFERENCING =================

class TestDeriveCellVoltages:
    def test_uniform_pack_differences_correctly(self):
        # 12 taps at 4.1 V per cell -> every cell reads 4.1 V.
        cumulative = [4.1 * (i + 1) for i in range(12)]
        cells, total = derive_cell_voltages(cumulative)

        assert len(cells) == 12
        for v in cells:
            assert math.isclose(v, 4.1, abs_tol=1e-9)
        assert math.isclose(total, 49.2, abs_tol=1e-9)

    def test_follows_input_length_not_a_fixed_count(self):
        # The old code hardcoded 12 and would IndexError on anything shorter.
        for series in [4, 12, 14, 20]:
            cumulative = [3.7 * (i + 1) for i in range(series)]
            cells, total = derive_cell_voltages(cumulative)
            assert len(cells) == series
            assert math.isclose(total, 3.7 * series, rel_tol=1e-9)

    def test_detects_a_weak_cell(self):
        # Cell 3 is 0.5 V low; differencing must localise it.
        cumulative = [4.0, 8.0, 11.5, 15.5]
        cells, total = derive_cell_voltages(cumulative)

        assert math.isclose(cells[2], 3.5, abs_tol=1e-9)
        assert math.isclose(total, 15.5, abs_tol=1e-9)

    def test_single_cell_pack(self):
        cells, total = derive_cell_voltages([4.2])
        assert cells == [4.2]
        assert math.isclose(total, 4.2)

    def test_empty_input_does_not_raise(self):
        cells, total = derive_cell_voltages([])
        assert cells == []
        assert total == 0.0


# ================= TEMPERATURE LINE PARSING =================

class TestParseTemperatureLine:
    def test_well_formed_line(self):
        line = "1,25.0,25.5,26.0,26.5,27.0,27.5,28.0,28.5"
        parsed = parse_temperature_line(line, sensors_per_bus=8, bus_count=6)

        assert parsed is not None
        bus_idx, readings = parsed
        assert bus_idx == 0                      # bus numbers are 1-based on the wire
        assert len(readings) == 8
        assert math.isclose(readings[0], 25.0)
        assert math.isclose(readings[7], 28.5)

    def test_err_sensors_are_skipped_not_zeroed(self):
        # A disconnected sensor must leave the previous reading in place rather
        # than writing a bogus 0.0 that would drag the max-temp calculation down.
        line = "2,25.0,ERR,26.0,ERR,27.0,27.5,28.0,28.5"
        bus_idx, readings = parse_temperature_line(line, 8, 6)

        assert bus_idx == 1
        assert 1 not in readings
        assert 3 not in readings
        assert len(readings) == 6

    def test_wrong_field_count_is_rejected(self):
        # Truncated serial line -- must not partially apply.
        assert parse_temperature_line("1,25.0,25.5", 8, 6) is None

    def test_non_numeric_bus_is_rejected(self):
        assert parse_temperature_line("x,1,2,3,4,5,6,7,8", 8, 6) is None

    def test_out_of_range_bus_is_rejected(self):
        # Bus 9 on a 6-bus rig would have written past the end of the array.
        assert parse_temperature_line("9,1,2,3,4,5,6,7,8", 8, 6) is None
        assert parse_temperature_line("0,1,2,3,4,5,6,7,8", 8, 6) is None

    def test_garbage_temperature_field_is_skipped(self):
        bus_idx, readings = parse_temperature_line("1,25.0,junk,26.0,4,5,6,7,8", 8, 6)
        assert 1 not in readings
        assert math.isclose(readings[0], 25.0)

    def test_adapts_to_a_different_sensor_count(self):
        line = "3," + ",".join(str(20.0 + i) for i in range(4))
        bus_idx, readings = parse_temperature_line(line, sensors_per_bus=4, bus_count=8)

        assert bus_idx == 2
        assert len(readings) == 4

    def test_empty_line_is_rejected(self):
        assert parse_temperature_line("", 8, 6) is None

    def test_negative_temperatures_are_accepted(self):
        # The cells are rated to -40 C; sub-zero readings are legitimate.
        line = "1," + ",".join(["-15.5"] * 8)
        _, readings = parse_temperature_line(line, 8, 6)
        assert all(math.isclose(v, -15.5) for v in readings.values())

    def test_non_finite_values_are_not_readings(self):
        # float() accepts these. A NaN would hide from max() and the overtemp
        # trip while still refreshing that sensor's age.
        _, readings = parse_temperature_line("1,nan,inf,-inf,4,5,6,7,8", 8, 6)
        assert 0 not in readings
        assert 1 not in readings
        assert 2 not in readings
        assert len(readings) == 5


# ================= PER-SENSOR AGES =================

class TestTemperatureSensorAges:
    def test_age_is_time_since_last_valid_reading(self):
        ages = temperature_sensor_ages([[100.0, 98.0], [99.5, 90.0]], now=100.0)
        assert ages == [[0.0, 2.0], [0.5, 10.0]]

    def test_never_read_sensor_is_infinitely_old(self):
        # ERR since power-up must not read as fresh.
        ages = temperature_sensor_ages([[100.0, None]], now=100.0)
        assert ages[0][0] == 0.0
        assert ages[0][1] == float('inf')

    def test_keeps_the_bus_layout(self):
        ages = temperature_sensor_ages([[None] * 8 for _ in range(6)], now=0.0)
        assert len(ages) == 6
        assert all(len(bus) == 8 for bus in ages)


# ================= RESISTOR THERMOCOUPLES =================

class TestThermocoupleReadings:
    def _fresh(self, n=4):
        return [None] * n, [None] * n

    def test_valid_readings_are_stored_and_stamped(self):
        temps, rx = self._fresh()
        apply_thermocouple_readings([120.5, 80.0, 60.0, 45.0], temps, rx, now=10.0)
        assert temps == [120.5, 80.0, 60.0, 45.0]
        assert rx == [10.0] * 4

    def test_open_thermocouple_readings_are_skipped(self):
        # An open thermocouple reads far off scale or as NaN. Neither may
        # refresh that bank, so its age grows and the logic faults on it.
        temps, rx = [100.0] * 4, [5.0] * 4
        apply_thermocouple_readings([float('nan'), 1372.0, -270.0, 101.0], temps, rx, now=6.0)
        assert temps[:3] == [100.0, 100.0, 100.0]
        assert rx[:3] == [5.0, 5.0, 5.0]
        assert temps[3] == 101.0 and rx[3] == 6.0

    def test_limit_of_the_valid_band_is_accepted(self):
        temps, rx = self._fresh(1)
        apply_thermocouple_readings([TC_MAX_C], temps, rx, now=1.0)
        assert temps == [TC_MAX_C]

    def test_single_channel_read_returns_a_bare_float(self):
        # nidaqmx returns a float, not a list, when one channel is configured.
        temps, rx = self._fresh(1)
        apply_thermocouple_readings(55.0, temps, rx, now=1.0)
        assert temps == [55.0]

    def test_failed_read_changes_nothing(self):
        temps, rx = [50.0] * 4, [1.0] * 4
        apply_thermocouple_readings([], temps, rx, now=9.0)
        assert temps == [50.0] * 4 and rx == [1.0] * 4

    def test_extra_values_are_ignored(self):
        temps, rx = self._fresh(2)
        apply_thermocouple_readings([30.0, 31.0, 32.0], temps, rx, now=1.0)
        assert temps == [30.0, 31.0]

    def test_ages(self):
        assert reading_ages([9.0, None], now=10.0) == [1.0, float('inf')]


# ================= NO NI-DAQ: NO SIMULATED PACK =================

class _FakeChannels:
    def add_ai_voltage_chan(self, *args, **kwargs):
        pass

    def add_ai_thrmcpl_chan(self, *args, **kwargs):
        raise RuntimeError("no thermocouple module")


class TestNoNiDaq:
    """run_daq_process itself, with nidaqmx faked and no COM ports to sweep.

    Without its NI-DAQ this process used to publish a healthy simulated pack --
    15 A, every cell at nominal + 0.5 V -- that the logic process could not tell
    from a real one.
    """

    def _collect(self, monkeypatch, task_factory, seconds=1.2):
        monkeypatch.setattr(hardware_manager.serial.tools.list_ports, "comports", lambda: [])
        monkeypatch.setattr(hardware_manager.nidaqmx, "Task", task_factory)

        config = RigConfig.defaults()
        packets = queue.Queue(maxsize=1000)
        stop = threading.Event()
        worker = threading.Thread(target=hardware_manager.run_daq_process,
                                  args=(packets, stop, config), daemon=True)
        worker.start()
        time.sleep(seconds)
        stop.set()
        worker.join(timeout=5)
        assert not worker.is_alive()

        out = []
        while not packets.empty():
            out.append(packets.get_nowait())
        return config, out

    def test_missing_daq_publishes_no_data_not_a_simulated_pack(self, monkeypatch):
        def no_daq():
            raise RuntimeError("DaqNotFoundError")

        _, packets = self._collect(monkeypatch, no_daq)
        assert packets
        for p in packets:
            assert p['hardware_status']['ni_daq'] is False
            assert p['amps'] == 0.0
            assert p['voltage'] == 0.0
            assert p['cell_voltages'] == []

    def test_daq_lost_mid_session_keeps_publishing_as_offline(self, monkeypatch):
        tasks = []

        class FlakyTask:
            def __init__(self):
                self.ai_channels = _FakeChannels()
                self.reads = 0
                self.closed = False
                tasks.append(self)

            def read(self):
                self.reads += 1
                if self.reads > 3:
                    raise RuntimeError("device removed")
                return [0.5] + [0.35 * (i + 1) for i in range(12)]

            def close(self):
                self.closed = True

        config, packets = self._collect(monkeypatch, FlakyTask)
        online = [p for p in packets if p['hardware_status']['ni_daq']]
        offline = [p for p in packets if not p['hardware_status']['ni_daq']]

        assert len(online) == 3
        assert online[0]['amps'] == 0.5 * config.daq.current_amps_per_volt
        # Read failures used to end the loop. Now it carries on, offline.
        assert len(offline) >= 3
        assert packets.index(offline[0]) == 3
        for p in offline:
            assert p['amps'] == 0.0 and p['voltage'] == 0.0 and p['cell_voltages'] == []
        assert tasks[0].closed

