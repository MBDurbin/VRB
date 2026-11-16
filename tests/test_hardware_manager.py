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
    apply_thermocouple_readings, reading_ages, TC_MAX_C, await_identity,
    read_available_lines, MAX_PENDING_BYTES)
from rig_config import RigConfig


# ================= READING THE TEMPERATURE STREAM =================

class WaitingBytes:
    """A port holding exactly `data`; it refuses any call that could wait."""

    def __init__(self, data):
        self.data = data

    @property
    def in_waiting(self):
        return len(self.data)

    def read(self, n):
        assert n <= len(self.data), "asked for more than was waiting: that read would block"
        chunk, self.data = self.data[:n], self.data[n:]
        return chunk

    def readlines(self):
        raise AssertionError("readlines() waits for the port to go quiet")


class TestReadAvailableLines:
    def test_complete_lines_now_and_the_partial_one_kept(self):
        port = WaitingBytes(b"1,20.0,20.5\r\n2,19.5,19.")
        lines, pending = read_available_lines(port, b"")
        assert lines == [b"1,20.0,20.5\r"]
        assert pending == b"2,19.5,19."
        port.data = b"5\r\n"
        lines, pending = read_available_lines(port, pending)
        assert lines == [b"2,19.5,19.5\r"] and pending == b""

    def test_nothing_waiting_reads_nothing(self):
        lines, pending = read_available_lines(WaitingBytes(b""), b"3,20")
        assert lines == [] and pending == b"3,20"

    def test_a_runaway_part_line_is_dropped(self):
        _, pending = read_available_lines(WaitingBytes(b"x" * (MAX_PENDING_BYTES + 1)), b"")
        assert pending == b""


# ================= FINDING THE TEMPERATURE BOARD =================

class BootingBoard:
    """A port to an Arduino that hears nothing until it has booted, then
    answers each ?WHOAMI behind a line of streamed readings."""

    def __init__(self, boot_s, identity=b"TEMP_SENSOR"):
        self.ready_at = time.monotonic() + boot_s
        self.identity = identity
        self.out = b""

    def write(self, data):
        if time.monotonic() >= self.ready_at and data == b"?WHOAMI\n":
            self.out += b"1,19.5,20.0,19.5\r\n" + self.identity + b"\r\n"

    @property
    def in_waiting(self):
        return len(self.out)

    def read(self, n=1):
        if not self.out:
            time.sleep(0.01)            # the port's short read timeout
            return b""
        chunk, self.out = self.out[:n], self.out[n:]
        return chunk


class TestAwaitIdentity:
    """Opening a port resets the Arduino. The temperature board takes ~4.8 s to
    boot, so one ?WHOAMI at 2 s was always lost and the rig never found it."""

    def test_a_board_still_booting_at_the_first_ask_is_found(self):
        assert await_identity(BootingBoard(boot_s=0.3), "TEMP_SENSOR",
                              timeout_s=1.0, retry_s=0.1)

    def test_gives_up_when_the_board_never_answers(self):
        started = time.monotonic()
        assert not await_identity(BootingBoard(boot_s=60.0), "TEMP_SENSOR",
                                  timeout_s=0.3, retry_s=0.1)
        assert time.monotonic() - started < 0.6

    def test_another_board_is_not_taken_for_it(self):
        for other in (b"RESISTOR_CTRL", b"TEMP_SENSOR_2"):
            assert not await_identity(BootingBoard(boot_s=0.0, identity=other), "TEMP_SENSOR",
                                      timeout_s=0.3, retry_s=0.1)


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

    def test_values_a_ds18b20_cannot_read_are_not_readings(self):
        # -127 is the library's disconnected value: as a fresh reading it would
        # make that cell look cold. The sketch sends ERR for it today.
        _, readings = parse_temperature_line("1,-127.0,-55.1,125.1,-55.0,125.0,85.0,30,30",
                                             8, 6)
        assert sorted(readings) == [3, 4, 5, 6, 7]
        assert readings[5] == 85.0          # the power-on value reads hot: kept


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

    def _collect(self, monkeypatch, task_factory, seconds=1.2, packets=None, ports=()):
        monkeypatch.setattr(hardware_manager.serial.tools.list_ports, "comports",
                            lambda: list(ports))
        monkeypatch.setattr(hardware_manager.nidaqmx, "Task", task_factory)

        config = RigConfig.defaults()
        packets = packets if packets is not None else queue.Queue(maxsize=1000)
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

            def start(self):
                pass

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

    def test_a_temperature_board_that_never_pauses_cannot_stall_the_loop(self, monkeypatch):
        # The board streams nonstop. readlines() waited for it to go quiet, so
        # once it was connected the DAQ loop stopped publishing altogether.
        class Port:
            device = "COM9"

        class NonstopBoard:
            LINES = [(f"{bus}," + ",".join(["21.5"] * 8) + "\r\n").encode()
                     for bus in range(1, 7)]

            is_open = True

            def __init__(self, port, *args, **kwargs):
                self.port, self.buf, self.n = port, b"", 0

            def _fill(self, size):
                while len(self.buf) < size:
                    self.buf += self.LINES[self.n % 6]
                    self.n += 1

            @property
            def in_waiting(self):
                self._fill(300)
                return len(self.buf)

            def read(self, size=1):
                self._fill(size)
                chunk, self.buf = self.buf[:size], self.buf[size:]
                return chunk

            def write(self, data):
                if data == b"?WHOAMI\n":
                    self.buf += b"TEMP_SENSOR\r\n"

            def reset_input_buffer(self):
                self.buf = b""

            def readlines(self):
                raise AssertionError("readlines() never returns on this board")

            def close(self):
                pass

        def no_daq():
            raise RuntimeError("DaqNotFoundError")

        monkeypatch.setattr(hardware_manager.serial, "Serial", NonstopBoard)
        _, packets = self._collect(monkeypatch, no_daq, seconds=3.5, ports=[Port()])

        connected = [p for p in packets if p['hardware_status']['temp_arduino']]
        assert len(connected) >= 5          # still publishing at ~10 Hz after the claim
        assert connected[-1]['temperatures'] == [[21.5] * 8 for _ in range(6)]

    def test_taps_are_read_in_the_configured_mode_and_the_current_is_not(self, monkeypatch):
        # On a 9205 the driver's default can pair two taps as one differential
        # input, so the taps are set explicitly. The current is on a 9215, which
        # is differential only and refuses RSE.
        added, started = [], []

        class Channels(_FakeChannels):
            def add_ai_voltage_chan(self, channel, **kwargs):
                added.append((channel, kwargs.get('terminal_config')))

        class RecordingTask:
            def __init__(self):
                self.ai_channels = Channels()

            def read(self):
                return [0.0] + [0.3 * (i + 1) for i in range(12)]

            def start(self):
                started.append(True)

            def close(self):
                pass

        config, _ = self._collect(monkeypatch, RecordingTask, seconds=0.3)
        rse = hardware_manager.TerminalConfiguration.RSE
        assert added[0] == (config.daq.current_channel, None)
        assert added[1:] == [(ch, rse) for ch in config.daq.voltage_channels]
        # Started once: a task never started is started and stopped around
        # every read, 113 ms a read on the rig against 2 ms.
        assert started == [True]


class DrainedBeforeGet(queue.Queue):
    """Reports full, as it was a moment ago, though its consumer has since
    emptied it: the race between full() and a blocking get()."""
    def full(self):
        return True


def test_daq_keeps_publishing_when_its_queue_is_drained_mid_publish(monkeypatch):
    # The old `if full(): get()` waited forever here and the DAQ went silent.
    def no_daq():
        raise RuntimeError("DaqNotFoundError")

    _, packets = TestNoNiDaq()._collect(monkeypatch, no_daq, seconds=0.6,
                                        packets=DrainedBeforeGet(maxsize=1000))
    assert len(packets) >= 3
