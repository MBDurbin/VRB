"""
Bus-bar temperature sensors, end to end: the config, the temperature board's
"B" line, the trips, the thermal map, the recording and the sketch.

One DS18B20 on each bus bar that joins one group of parallel cells to the
next: eleven on a 12S module. None are fitted as shipped (busbar_sensor_count
is 0), and everything here has to leave that rig exactly as it was.
"""
import math
import os
import queue
import re
import sys
import threading
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pytest
from PyQt6 import QtWidgets

import control_logic as cl
import gui_layout
import hardware_manager
from gui_layout import HeatmapWindow, TelemetryGUI, busbar_places, record_headers, record_row
from hardware_manager import parse_busbar_line, parse_sensor_field
from rig_config import DS18B20_MAX_C, PackConfig, RigConfig, SafetyLimits
from sil_simulator import SILSimulatorWindow
from test_logic_loop import Rig, read_trip_log


def fitted(count=11):
    """The default config with bus-bar sensors on the module."""
    cfg = RigConfig.defaults()
    cfg.daq.busbar_sensor_count = count
    return cfg


# ================= CONFIG =================

class TestConfig:
    def test_none_are_fitted_as_shipped(self):
        for cfg in (RigConfig.defaults(), RigConfig.load()):
            assert cfg.daq.busbar_sensor_count == 0
            assert cfg.limits.busbar_max_temp_c == 80.0
            assert cfg.arm_blockers() == []
            assert not any("bus-bar" in p for p in cfg.advisories())

    def test_a_module_has_one_bus_bar_between_each_pair_of_groups(self):
        assert PackConfig().bus_bar_count == 11                 # 12S
        assert PackConfig(series_count=14).bus_bar_count == 13
        assert PackConfig(series_count=1).bus_bar_count == 0

    def test_one_per_bus_bar_is_clean(self):
        cfg = fitted(11)
        assert cfg.arm_blockers() == [] and cfg.advisories() == []

    def test_any_other_count_is_pointed_out_but_still_arms(self):
        cfg = fitted(13)
        assert cfg.arm_blockers() == []
        (note,) = [p for p in cfg.advisories() if "bus-bar" in p]
        assert "13 bus-bar sensors configured" in note and "has 11 bus bars" in note

    def test_a_count_that_is_not_a_count_blocks_arm(self):
        for bad in (-1, 2.5, True, "11"):
            cfg = fitted(bad)
            assert any("Bus-bar sensor count" in p for p in cfg.arm_blockers()), bad

    def test_a_trip_the_sensor_cannot_reach_blocks_arm(self):
        cfg = fitted()
        cfg.limits.busbar_max_temp_c = DS18B20_MAX_C + 1
        assert any("Bus-bar trip 126 C" in p and "never fire" in p for p in cfg.arm_blockers())
        cfg.limits.busbar_max_temp_c = DS18B20_MAX_C
        assert cfg.arm_blockers() == []

    def test_a_trip_that_is_not_a_number_blocks_arm(self):
        cfg = fitted()
        cfg.limits.busbar_max_temp_c = float("nan")
        assert any("busbar_max_temp" in p for p in cfg.arm_blockers())

    def test_the_trip_is_not_moved_by_pack_derivation(self):
        limits = SafetyLimits(busbar_max_temp_c=70.0).apply_pack_derivation(PackConfig())
        assert limits.busbar_max_temp_c == 70.0
        assert limits.to_command_dict()['busbar_max_temp'] == 70.0

    def test_both_settings_survive_a_save_and_load(self, tmp_path):
        cfg = fitted(11)
        cfg.limits.busbar_max_temp_c = 72.5
        loaded = RigConfig.load(cfg.save(str(tmp_path / "rig_config.json")))
        assert loaded.daq.busbar_sensor_count == 11
        assert loaded.limits.busbar_max_temp_c == 72.5

    def test_a_config_from_before_bus_bars_still_loads(self):
        old = RigConfig.defaults().to_dict()
        del old['daq']['busbar_sensor_count']
        del old['limits']['busbar_max_temp_c']
        cfg = RigConfig.from_dict(old)
        assert cfg.daq.busbar_sensor_count == 0 and cfg.limits.busbar_max_temp_c == 80.0

    def test_fitting_them_changes_the_settings_fingerprint(self):
        assert fitted(11).settings_fingerprint() != RigConfig.defaults().settings_fingerprint()


# ================= THE TEMPERATURE BOARD'S LINE =================

def bus_bar_line(temps):
    return "B," + ",".join(temps)


class TestParsing:
    def test_a_field_is_a_reading_only_inside_the_sensors_range(self):
        assert parse_sensor_field("31.5") == 31.5
        assert parse_sensor_field(" -12.0 ") == -12.0
        for bad in ("ERR", "", "hot", "nan", "inf", "-127.0", "130.0"):
            assert parse_sensor_field(bad) is None, bad

    def test_a_full_line_gives_every_bus_bar(self):
        temps = [f"{30 + i}.5" for i in range(11)]
        assert parse_busbar_line(bus_bar_line(temps), 11) == {i: 30.5 + i for i in range(11)}

    def test_err_sensors_are_skipped_not_zeroed(self):
        temps = ["30.0"] * 11
        temps[3] = "ERR"
        readings = parse_busbar_line(bus_bar_line(temps), 11)
        assert 3 not in readings and len(readings) == 10

    def test_a_line_cut_short_is_refused_whole(self):
        # Ten readings must not be taken as bus bars 1 to 10.
        assert parse_busbar_line(bus_bar_line(["30.0"] * 10), 11) is None
        assert parse_busbar_line(bus_bar_line(["30.0"] * 12), 11) is None

    def test_a_cell_bus_line_is_not_the_bus_bar_line(self):
        assert parse_busbar_line("3," + ",".join(["30.0"] * 11), 11) is None
        assert parse_busbar_line("", 11) is None
        assert parse_busbar_line("TEMP_SENSOR", 11) is None

    def test_the_line_is_ignored_while_none_are_configured(self):
        assert parse_busbar_line(bus_bar_line(["30.0"] * 11), 0) is None

    def test_the_cell_parser_still_throws_the_bus_bar_line_away(self):
        # So a board that sends it to an older host does no harm.
        line = bus_bar_line(["30.0"] * 8)                # same width as a cell bus
        assert hardware_manager.parse_temperature_line(line, 8, 6) is None


class _StreamingBoard:
    """A temperature board streaming six cell buses and one bus-bar line."""

    is_open = True
    BUS_BARS = ["41.0", "42.0", "ERR"] + ["40.0"] * 8

    def __init__(self, port, *args, **kwargs):
        self.port, self.buf, self.n = port, b"", 0
        self.lines = [(f"{bus}," + ",".join(["21.5"] * 8) + "\r\n").encode()
                      for bus in range(1, 7)]
        self.lines.append((bus_bar_line(self.BUS_BARS) + "\r\n").encode())

    def _fill(self, size):
        while len(self.buf) < size:
            self.buf += self.lines[self.n % len(self.lines)]
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

    def close(self):
        pass


def daq_packets(monkeypatch, config, seconds=3.5):
    """Run the real DAQ process against the streaming board, with no NI-DAQ."""
    class Port:
        device = "COM9"

    def no_daq():
        raise RuntimeError("DaqNotFoundError")

    monkeypatch.setattr(hardware_manager.serial.tools.list_ports, "comports", lambda: [Port()])
    monkeypatch.setattr(hardware_manager.nidaqmx, "Task", no_daq)
    monkeypatch.setattr(hardware_manager.serial, "Serial", _StreamingBoard)
    packets, stop = queue.Queue(maxsize=1000), threading.Event()
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
    return out


class TestDaqProcess:
    def test_bus_bar_readings_reach_the_packet(self, monkeypatch):
        last = daq_packets(monkeypatch, fitted(11))[-1]
        assert last['hardware_status']['temp_arduino'] is True
        temps, ages = last['busbar_temps'], last['busbar_temp_ages_s']
        assert len(temps) == 11 and len(ages) == 11
        assert temps[:2] == [41.0, 42.0] and temps[3:] == [40.0] * 8
        # The one printing ERR has never given a reading: no value, infinite age.
        assert temps[2] is None and ages[2] == float("inf")
        assert all(age < 1.0 for i, age in enumerate(ages) if i != 2)
        # The cell buses are read as before, beside it.
        assert last['temperatures'] == [[21.5] * 8 for _ in range(6)]

    def test_with_none_configured_the_packet_carries_none(self, monkeypatch):
        last = daq_packets(monkeypatch, RigConfig.defaults())[-1]
        assert last['busbar_temps'] == [] and last['busbar_temp_ages_s'] == []
        assert last['temperatures'] == [[21.5] * 8 for _ in range(6)]


# ================= TRIPS =================

def limits(**changes):
    out = SafetyLimits().to_command_dict()
    out.update(changes)
    return out


def packet(**changes):
    data = {
        'max_temp': 30.0, 'amps': 0.0, 'voltage': 46.0, 'cell_voltages': [3.83] * 12,
        'temp_age_s': 0.1, 'daq_age_s': 0.05,
        'temp_sensor_ages_s': [[0.1] * 8 for _ in range(6)],
        'resistor_temps': [40.0] * 4, 'resistor_temp_ages_s': [0.1] * 4,
        'busbar_temps': [35.0] * 11, 'busbar_temp_ages_s': [0.1] * 11,
        'hardware_status': {'temp_arduino': True, 'ni_daq': True},
    }
    data.update(changes)
    return data


EXPECTED = cl.expected_measurements(fitted(11).daq, PackConfig())
NONE_FITTED = cl.expected_measurements(RigConfig.defaults().daq, PackConfig())


class TestTrips:
    def test_healthy_bus_bars_pass(self):
        assert cl.evaluate_safety(packet(), limits(), expected=EXPECTED) == (False, None)

    def test_one_hot_bus_bar_trips_in_every_state(self):
        temps = [35.0] * 11
        temps[6] = 80.0                                  # at the trip
        hot = packet(busbar_temps=temps)
        assert cl.evaluate_safety(hot, limits(), armed=False) == (True, "BUSBAR OVERTEMP")
        assert cl.evaluate_safety(hot, limits(), expected=EXPECTED) == (True, "BUSBAR OVERTEMP")
        temps[6] = 79.9
        assert cl.evaluate_safety(packet(busbar_temps=temps), limits(), armed=False) \
            == (False, None)

    def test_the_trip_names_the_bus_bar_and_the_groups_it_joins(self):
        temps = [35.0] * 11
        temps[6], temps[10] = 84.25, 81.0
        (line,) = cl.trip_details("BUSBAR OVERTEMP", packet(busbar_temps=temps), limits(),
                                  expected=EXPECTED)
        assert line == ("Bus bar 7 (S7-S8) at 84.2 C, bus bar 11 (S11-S12) at 81.0 C "
                        "(trip 80.0 C)")

    def test_a_lowered_trip_is_the_one_used(self):
        warm = packet(busbar_temps=[61.0] * 11)
        assert cl.evaluate_safety(warm, limits(), armed=False) == (False, None)
        assert cl.evaluate_safety(warm, limits(busbar_max_temp=60.0), armed=False) \
            == (True, "BUSBAR OVERTEMP")

    def test_cell_and_current_trips_are_reported_ahead_of_it(self):
        hot = packet(busbar_temps=[90.0] * 11)
        assert cl.evaluate_safety({**hot, 'max_temp': 95.0}, limits())[1] == "OVERTEMP"
        assert cl.evaluate_safety({**hot, 'amps': 400.0}, limits())[1] == "OVERCURRENT"

    def test_a_sensor_that_has_not_reported_is_not_a_cold_bus_bar(self):
        temps = [35.0] * 11
        temps[2] = None
        ages = [0.1] * 11
        ages[2] = float("inf")
        silent = packet(busbar_temps=temps, busbar_temp_ages_s=ages)
        # No over-temperature trip from a missing reading, armed or not...
        assert cl.evaluate_safety(silent, limits(), armed=False) == (False, None)
        # ...but once armed, a bus bar nobody is watching is a fault.
        assert cl.evaluate_safety(silent, limits(), expected=EXPECTED) \
            == (True, "BUSBAR SENSOR FAULT")
        (line,) = cl.trip_details("BUSBAR SENSOR FAULT", silent, limits(), expected=EXPECTED)
        assert line == "No valid reading from: bus bar 3 (S3-S4)"

    def test_a_frozen_reading_faults_once_armed(self):
        # ERR from the sensor: its last 35 C stays in the array while the bar heats.
        ages = [0.1] * 11
        ages[8] = 9.0
        frozen = packet(busbar_temp_ages_s=ages)
        assert cl.evaluate_safety(frozen, limits(), armed=False) == (False, None)
        assert cl.evaluate_safety(frozen, limits(), expected=EXPECTED) \
            == (True, "BUSBAR SENSOR FAULT")

    def test_configured_and_never_heard_from_faults_once_armed(self):
        nothing = packet(busbar_temps=[], busbar_temp_ages_s=[])
        assert cl.evaluate_safety(nothing, limits(), armed=False) == (False, None)
        assert cl.evaluate_safety(nothing, limits(), expected=EXPECTED) \
            == (True, "NO BUSBAR DATA")
        (line,) = cl.trip_details("NO BUSBAR DATA", nothing, limits(), expected=EXPECTED)
        assert "11 bus-bar sensor(s) are configured and none has reported" in line

    def test_a_module_without_them_runs_as_it_always_did(self):
        without = packet(busbar_temps=[], busbar_temp_ages_s=[])
        assert cl.evaluate_safety(without, limits(), expected=NONE_FITTED) == (False, None)
        # A packet from before the fields existed, too.
        del without['busbar_temps'], without['busbar_temp_ages_s']
        assert cl.evaluate_safety(without, limits(), expected=NONE_FITTED) == (False, None)

    def test_fewer_readings_than_sensors_is_incomplete_data(self):
        short = packet(busbar_temps=[35.0] * 10, busbar_temp_ages_s=[0.1] * 10)
        assert cl.evaluate_safety(short, limits(), expected=EXPECTED) == (True, "INCOMPLETE DATA")
        details = cl.trip_details("INCOMPLETE DATA", short, limits(), expected=EXPECTED)
        assert any("10 bus-bar sensors reported, 11 wired" in d for d in details)

    def test_a_reading_that_is_not_a_number_is_refused(self):
        temps = [35.0] * 11
        temps[4] = float("nan")
        bad = packet(busbar_temps=temps)
        assert cl.evaluate_safety(bad, limits(), expected=EXPECTED) == (True, "INVALID READING")

    def test_sensors_that_cannot_be_placed_are_named_by_number(self):
        thirteen = cl.expected_measurements(fitted(13).daq, PackConfig())
        assert cl.busbar_label(4, thirteen) == "bus bar 4"
        assert cl.busbar_label(4, EXPECTED) == "bus bar 4 (S4-S5)"
        assert cl.busbar_label(4) == "bus bar 4"

    def test_no_packet_yet_is_not_a_fault_before_arming(self):
        assert cl.evaluate_safety(cl.NEUTRAL_PACKET(), limits(), armed=False) == (False, None)


# ================= THE LOGIC LOOP =================

@pytest.fixture
def rig(monkeypatch, tmp_path):
    monkeypatch.setattr(cl, "TRIP_LOG_PATH", str(tmp_path / "trip_log.csv"))
    r = Rig(monkeypatch)
    r.trip_log = tmp_path / "trip_log.csv"
    r.config = fitted(11)
    r.readings = {'busbar_temps': [35.0] * 11, 'busbar_temp_ages_s': [0.1] * 11}
    yield r
    r.close()


class TestLogicLoop:
    def test_a_rig_with_bus_bar_sensors_arms_and_runs(self, rig):
        rig.start()
        rig.wait_for("IDLE")
        rig.cmd_q.put(cl.transition_command("ARM"))
        rig.wait_for("ARMED")
        rig.cmd_q.put(cl.transition_command("RUN", 1))
        rig.wait_for("RUNNING")
        time.sleep(0.8)
        assert rig.state == "RUNNING"

    def test_a_hot_bus_bar_trips_an_idle_rig_and_is_logged(self, rig, capsys):
        rig.start()
        rig.wait_for("IDLE")
        temps = [35.0] * 11
        temps[4] = 86.0
        rig.readings = {'busbar_temps': temps, 'busbar_temp_ages_s': [0.1] * 11}
        rig.wait_for("FAULT", timeout=1.0)

        time.sleep(0.2)
        assert rig.last['fault_reason'] == "BUSBAR OVERTEMP"
        assert rig.last['fault_detail'] == ["Bus bar 5 (S5-S6) at 86.0 C (trip 80.0 C)"]
        (row,) = read_trip_log(rig)
        assert (row['Reason'], row['State']) == ("BUSBAR OVERTEMP", "IDLE")
        assert "Bus bar 5 (S5-S6) at 86.0 C" in row['Detail']
        assert "BUSBAR OVERTEMP ALARM! Killing Load." in capsys.readouterr().out

    def test_a_bus_bar_sensor_that_stops_reporting_faults_an_armed_rig(self, rig):
        rig.start()
        rig.wait_for("IDLE")
        rig.cmd_q.put(cl.transition_command("ARM"))
        rig.wait_for("ARMED")
        ages = [0.1] * 11
        ages[9] = 12.0
        rig.readings = {'busbar_temps': [35.0] * 11, 'busbar_temp_ages_s': ages}
        rig.wait_for("FAULT", timeout=1.0)
        time.sleep(0.2)
        assert rig.last['fault_reason'] == "BUSBAR SENSOR FAULT"
        assert rig.last['fault_detail'] == ["No valid reading from: bus bar 10 (S10-S11)"]

    def test_sensors_configured_but_never_heard_from_fault_on_arming(self, rig):
        # The host set to 11 and the sketch still at 0: nothing must run blind.
        rig.readings = {'busbar_temps': [], 'busbar_temp_ages_s': []}
        rig.start()
        rig.wait_for("IDLE")
        time.sleep(0.3)
        assert rig.state == "IDLE"                      # not a fault before arming
        rig.cmd_q.put(cl.transition_command("ARM"))
        rig.wait_for("FAULT", timeout=1.5)
        time.sleep(0.2)
        assert rig.last['fault_reason'] == "NO BUSBAR DATA"

    def test_the_trip_in_force_is_reported_to_the_window(self, rig):
        rig.start()
        rig.wait_for("IDLE")
        assert rig.last['active_limits']['busbar_max_temp'] == 80.0


# ================= THE WINDOW =================

@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def window_for(config, monkeypatch):
    monkeypatch.setattr(RigConfig, "load", staticmethod(lambda *a, **k: config))
    return TelemetryGUI(queue.Queue(), queue.Queue(maxsize=10), threading.Event())


READING = {'voltage': 44.0, 'amps': 0.0, 'max_temp': 24.0, 'cell_voltages': [3.67] * 12,
           'temperatures': [[24.0] * 8 for _ in range(6)], 'fsm_state': "IDLE"}


class TestThermalMap:
    def test_one_tile_per_bus_bar_between_the_groups(self):
        assert busbar_places(12, 11) == (11, True)
        assert busbar_places(14, 13) == (13, True)
        # None fitted: the same places, drawn empty, so the map shows where they go.
        assert busbar_places(12, 0) == (11, True)
        # A count that is not one per bus bar cannot be placed.
        assert busbar_places(12, 13) == (13, False)

    def test_the_places_are_drawn_empty_while_none_are_fitted(self, app):
        win = HeatmapWindow(12, 4, 0)
        win.update_temps([[30.0] * 8 for _ in range(6)], 65.0)
        win.update_busbars([], [], 80.0, 3.0)
        assert not win.grab().isNull()                  # forces a full paint
        assert len(win.busbar_labels) == 11
        assert all(lbl.text() == "--" for lbl in win.busbar_labels)
        assert "NO SENSORS FITTED" in win.lbl_busbar_caption.text()
        assert win.busbar_labels[3].toolTip() == "Bus bar 4, between S4 and S5"
        win.close()
        app.processEvents()

    def test_fitted_bus_bars_show_their_temperatures(self, app):
        win = HeatmapWindow(12, 4, 11)
        temps = [35.0 + i for i in range(11)]
        temps[5] = None                                  # not reported yet
        ages = [0.1] * 11
        ages[5], ages[8] = float("inf"), 9.0             # and one gone stale
        win.update_busbars(temps, ages, 80.0, 3.0)
        assert not win.grab().isNull()

        texts = [lbl.text() for lbl in win.busbar_labels]
        assert texts[0] == "35" and texts[10] == "45" and texts[5] == "--"
        assert "dashed" in win.busbar_labels[8].styleSheet()
        assert "dashed" not in win.busbar_labels[0].styleSheet()
        assert win.lbl_busbar_caption.text() == "BUS-BAR TEMPERATURES · 11 SENSORS · TRIP 80 °C"
        win.close()
        app.processEvents()

    def test_a_bus_bar_at_its_trip_is_drawn_hot(self, app):
        import theme
        win = HeatmapWindow(12, 4, 11)
        win.update_busbars([80.0] + [30.0] * 10, [0.1] * 11, 80.0, 3.0)
        assert theme.HEAT_HOT in win.busbar_labels[0].styleSheet()
        assert theme.HEAT_HOT not in win.busbar_labels[1].styleSheet()
        win.close()
        app.processEvents()

    def test_an_unplaceable_count_is_drawn_as_a_plain_row(self, app):
        win = HeatmapWindow(12, 4, 13)
        assert len(win.busbar_labels) == 13
        assert win.busbar_labels[0].toolTip() == "Bus-bar sensor 1"
        win.update_busbars([30.0] * 13, [0.1] * 13, 80.0, 3.0)
        assert not win.grab().isNull()
        win.close()
        app.processEvents()


class TestMainWindow:
    def test_no_bus_bar_card_while_none_are_fitted(self, app, monkeypatch):
        window = window_for(RigConfig.defaults(), monkeypatch)
        try:
            assert window.card_busbar is None
            window.queue.put(dict(READING))
            window.update_gui()                         # and nothing trips over it
        finally:
            window.timer.stop()
            window.deleteLater()

    def test_the_hottest_bus_bar_has_a_card_once_fitted(self, app, monkeypatch):
        window = window_for(fitted(11), monkeypatch)
        try:
            assert window.card_busbar is not None
            window.queue.put({**READING, 'busbar_temps': [35.0, 61.5, None] + [30.0] * 8,
                              'busbar_temp_ages_s': [0.1] * 11})
            window.update_gui()
            assert window.card_busbar.value.text() == "61.5 °C"

            # Nothing reported yet reads as dashes, never as 0.0.
            window.queue.put({**READING, 'busbar_temps': [None] * 11,
                              'busbar_temp_ages_s': [float("inf")] * 11})
            window.update_gui()
            assert window.card_busbar.value.text() == "--- °C"
        finally:
            window.timer.stop()
            window.deleteLater()

    def test_the_open_map_follows_the_packets(self, app, monkeypatch):
        window = window_for(fitted(11), monkeypatch)
        try:
            window.toggle_heatmap()
            window.queue.put({**READING, 'busbar_temps': [41.0] * 11,
                              'busbar_temp_ages_s': [0.1] * 11})
            window.update_gui()
            assert [lbl.text() for lbl in window.heatmap_window.busbar_labels] == ["41"] * 11
        finally:
            window.timer.stop()
            window.heatmap_window.close()
            window.deleteLater()
            app.processEvents()

    def test_the_configure_box_can_go_back_to_none(self, app):
        dialog = gui_layout.ConfigDialog(fitted(11))
        box = dialog.daq_widgets['busbar_sensor_count']
        assert box.value() == 11 and box.minimum() == 0
        box.setValue(0)
        assert dialog.collect().daq.busbar_sensor_count == 0
        assert "no sensors fitted" in dialog.lbl_derived.text()
        dialog.reject()


class TestRecording:
    def test_no_bus_bar_columns_while_none_are_fitted(self):
        assert not any(h.startswith("BusBar") for h in record_headers(12, 4, 4))
        assert len(record_headers(12, 4, 4)) == len(record_row(dict(READING), 0.0, 12, 4, 4))

    def test_one_column_per_bus_bar_after_everything_else(self):
        headers = record_headers(12, 4, 4, 11)
        assert headers[-11:] == [f"BusBar_{i}_T" for i in range(1, 12)]
        assert headers[:-11] == record_headers(12, 4, 4)     # nothing before them moved

        temps = [35.04 + i for i in range(11)]
        temps[2] = None
        row = dict(zip(headers, record_row({**READING, 'sampled_at': 5.0, 'busbar_temps': temps},
                                           0.0, 12, 4, 4, 11)))
        assert len(row) == len(headers)
        assert row['BusBar_1_T'] == 35.0 and row['BusBar_11_T'] == 45.0
        assert row['BusBar_3_T'] == ''                   # no reading is empty, not zero

    def test_the_window_records_them(self, app, monkeypatch, tmp_path):
        monkeypatch.setattr(gui_layout, "PROJECT_DIR", str(tmp_path))
        window = window_for(fitted(11), monkeypatch)
        try:
            window.btn_record.setChecked(True)
            window.toggle_logging()
            window.queue.put({**READING, 'sampled_at': window.start_time + 2.0,
                              'busbar_temps': [41.0] * 11, 'busbar_temp_ages_s': [0.1] * 11})
            window.update_gui()
            window.btn_record.setChecked(False)
            window.toggle_logging()
        finally:
            window.timer.stop()
            window.deleteLater()
        (log,) = (tmp_path / "logs").glob("telemetry_*.csv")
        header, row = [line.split(",") for line in log.read_text().splitlines()]
        assert header[-1] == "BusBar_11_T" and row[-1] == "41.0" and len(header) == len(row)


class TestSilPlant:
    def test_the_plant_publishes_as_many_bus_bars_as_are_configured(self, app):
        for count in (0, 11):
            out = queue.Queue(maxsize=10)
            plant = SILSimulatorWindow(out, fitted(count))
            plant.timer.stop()
            plant.slider_temp.setValue(415)              # 41.5 C
            plant.inject_telemetry()
            sent = out.get_nowait()
            assert sent['busbar_temps'] == [41.5] * count
            assert sent['busbar_temp_ages_s'] == [0.0] * count
            # A complete packet for the logic's count checks.
            expected = cl.expected_measurements(fitted(count).daq, PackConfig())
            assert cl.measurement_problems(sent, expected) == []
            plant.deleteLater()


# ================= THE SKETCH =================

SKETCH = os.path.join(ROOT, "arduino", "temperature_sensor_array",
                      "temperature_sensor_array.ino")


class TestSketch:
    def source(self):
        with open(SKETCH, encoding="utf-8") as fh:
            return fh.read()

    def test_the_sketch_ships_with_none_fitted_like_the_config(self):
        match = re.search(r"#define\s+BUSBAR_SENSOR_COUNT\s+(\d+)", self.source())
        assert match and int(match.group(1)) == RigConfig.load().daq.busbar_sensor_count == 0

    def test_the_line_starts_the_way_the_host_expects(self):
        assert f'Serial.print("{hardware_manager.BUSBAR_LINE_PREFIX}");' in self.source()

    def test_the_address_table_has_a_row_for_every_bus_bar_of_a_12s_module(self):
        table = self.source().split("busbarSensors[BUSBAR_SENSOR_COUNT] = {")[1].split("};")[0]
        rows = re.findall(r"^\s*\{[01],\s*\{[^}]*\}\},", table, re.M)
        assert len(rows) == PackConfig().bus_bar_count == 11
        # Named in order, so a row can be matched to the bar it is taped to.
        assert "Bus bar 1:  between S1 and S2" in table
        assert "Bus bar 11: between S11 and S12" in table

    def test_a_sensor_it_cannot_read_is_sent_as_err(self):
        bus_bar_state = self.source().split("case 8:")[1]
        assert 'Serial.print("ERR");' in bus_bar_state
        # A row naming a bus that does not exist reads as disconnected too.
        assert "-127.0" in bus_bar_state
