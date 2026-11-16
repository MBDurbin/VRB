"""
The GUI's settings must match what the controller is using.

Covers the Configure dialog leaving the live limits alone until saved, which
sidebar edits switch off pack derivation, Configure being refused mid-run, and
the warning shown when the controller's settings in force differ from the
window's. Runs the real widgets on Qt's offscreen platform; no window appears.
"""
import os
import queue
import sys
import threading
import math
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PyQt6 import QtWidgets

import gui_layout
from control_logic import MAX_RESISTANCE
from gui_layout import ConfigDialog, TelemetryGUI, fault_summary, settings_mismatch
from rig_config import RigConfig


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def gui(app, monkeypatch):
    # The built-in defaults, not whatever rig_config.json holds.
    monkeypatch.setattr(RigConfig, "load", staticmethod(lambda *a, **k: RigConfig.defaults()))
    window = TelemetryGUI(queue.Queue(), queue.Queue(maxsize=10), threading.Event())
    yield window
    window.timer.stop()
    window.deleteLater()


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


# ================= CONFIGURE DIALOG =================

def test_preview_and_cancel_leave_the_live_limits_alone(app):
    cfg = RigConfig.defaults()
    before = cfg.limits.to_command_dict()

    dialog = ConfigDialog(cfg)
    # A different cell: the preview re-derives the limits on every change.
    for name, value in (('cell_max_continuous_a', 40), ('cell_max_temp_c', 60)):
        box = dialog.pack_widgets[name]
        dialog._write_widget(box, type(box.value())(value))
    previewed = dialog.collect()
    dialog.reject()

    assert previewed.limits.max_temp == 60.0         # the preview saw the new cell
    assert cfg.limits.to_command_dict() == before    # the live limits did not


def test_configure_keeps_a_baud_rate_over_three_digits(app):
    # Every whole-number box used to be 1-999, so a Save turned the
    # temperature board's 115200 into 999 and the rig never found it.
    cfg = RigConfig.defaults()
    cfg.daq.temp_baud_rate = 115200
    dialog = ConfigDialog(cfg)
    assert dialog.daq_widgets['temp_baud_rate'].value() == 115200
    assert dialog.collect().daq.temp_baud_rate == 115200

    dialog.daq_widgets['temp_baud_rate'].setValue(250000)
    assert dialog.collect().daq.temp_baud_rate == 250000
    dialog.reject()


def test_configure_never_clamps_a_loaded_number(app):
    cfg = RigConfig.defaults()
    cfg.pack.series_count = 1200            # past the counts' 1-999
    dialog = ConfigDialog(cfg)
    assert dialog.collect().pack.series_count == 1200
    dialog.reject()


# ================= SIDEBAR =================

def test_warn_line_edit_keeps_derivation_and_sends_nothing(gui):
    gui.sb_v_warn.setValue(40.0)
    assert gui.config.limits.warn_volts == 40.0
    assert gui.config.limits.derive_from_pack is True
    assert drain(gui.gui_cmd_queue) == []           # the controller never uses it


def test_derate_switch_keeps_derivation(gui):
    gui.chk_derate.setChecked(True)
    assert gui.config.limits.derive_from_pack is True
    (cmd,) = drain(gui.gui_cmd_queue)
    assert cmd[0] == "SET_LIMITS" and cmd[1]['derate_en'] is True


def test_buffer_edit_keeps_the_trip_on_the_cell_rating(gui):
    rating = gui.config.pack.max_current_a
    gui.sb_c_buffer.setValue(10.0)

    limits = gui.config.limits
    assert limits.derive_from_pack is True
    assert limits.max_amps == rating - 10.0         # re-derived from the new buffer
    assert gui.sb_c_crit.value() == rating - 10.0   # and shown
    (cmd,) = drain(gui.gui_cmd_queue)
    assert cmd[1]['max_amps'] + cmd[1]['amp_buffer'] == rating
    assert gui.config.arm_blockers() == []


def test_threshold_edit_is_an_override(gui):
    gui.sb_t_crit.setValue(70.0)
    assert gui.config.limits.derive_from_pack is False
    assert gui.config.limits.max_temp == 70.0
    (cmd,) = drain(gui.gui_cmd_queue)
    assert cmd[1]['max_temp'] == 70.0


def test_one_box_does_not_round_another(gui):
    # A derived limit with more decimals than the boxes show must survive an
    # edit to a different box untouched.
    gui.config.limits.derive_from_pack = False
    gui.config.limits.min_volts = 36.123
    gui.apply_config_to_widgets()                   # the box now shows 36.12
    gui.sb_t_crit.setValue(70.0)
    assert gui.config.limits.min_volts == 36.123


# ================= CONFIGURE MID-RUN =================

def no_dialog_boxes(monkeypatch):
    """Fail the test if anything modal opens: it would put E-STOP out of reach."""
    for name in ("information", "warning", "critical", "question"):
        monkeypatch.setattr(QtWidgets.QMessageBox, name,
                            lambda *a, **k: pytest.fail("a dialog box opened mid-run"))
    monkeypatch.setattr(ConfigDialog, "exec",
                        lambda self: pytest.fail("the Configure dialog opened mid-run"))
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        lambda *a, **k: pytest.fail("the file dialog opened mid-run"))


def test_configure_is_refused_while_running(gui, monkeypatch):
    no_dialog_boxes(monkeypatch)
    before = gui.config.to_dict()

    gui.last_fsm_state = "RUNNING"
    gui.open_config_dialog()

    # Said in the window, not in a box that has to be clicked away first.
    assert not gui.lbl_notice.isHidden()
    assert gui.lbl_notice.text() == "Run in progress: stop it before changing the configuration."
    assert gui.config.to_dict() == before
    assert drain(gui.gui_cmd_queue) == []


def test_a_profile_cannot_be_picked_while_running(gui, monkeypatch):
    # The file dialog is modal: while it is open E-STOP takes no click.
    no_dialog_boxes(monkeypatch)
    gui.last_fsm_state = "RUNNING"
    gui.load_csv_dialog()
    assert gui.lbl_notice.text() == "Run in progress: stop it before loading a profile."
    assert drain(gui.gui_cmd_queue) == []


READING = {'voltage': 44.0, 'amps': 0.0, 'max_temp': 24.0, 'cell_voltages': [3.67] * 12}


def test_the_profile_shown_is_the_one_the_controller_holds(gui, monkeypatch):
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        lambda *a, **k: ("C:/somewhere/Broken.csv", ""))
    gui.load_csv_dialog()
    assert drain(gui.gui_cmd_queue) == [("LOAD_CSV", "C:/somewhere/Broken.csv")]

    # The controller could not read it and still reports the old one.
    held = {**READING, 'fsm_state': "IDLE", 'profile_name': "Lap.csv", 'profile_rows': 68}
    gui.queue.put(held)
    gui.update_gui()
    assert gui.lbl_active_csv.text() == "Profile: Lap.csv  (68 rows)"
    assert gui.lbl_notice.isHidden()                # still inside the grace

    gui.profile_wanted = ("Broken.csv", time.time() - gui_layout.PROFILE_ACK_S - 0.1)
    gui.queue.put(held)
    gui.update_gui()
    assert "Broken.csv was not loaded" in gui.lbl_notice.text()
    assert gui.lbl_active_csv.text() == "Profile: Lap.csv  (68 rows)"


def test_a_profile_that_loads_raises_no_notice(gui, monkeypatch):
    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName",
                        lambda *a, **k: ("C:/somewhere/Bench - Hold 1 ohm.csv", ""))
    gui.load_csv_dialog()
    gui.queue.put({**READING, 'fsm_state': "IDLE",
                   'profile_name': "Bench - Hold 1 ohm.csv", 'profile_rows': 3})
    gui.update_gui()
    assert gui.profile_wanted is None and gui.lbl_notice.isHidden()
    assert gui.lbl_active_csv.text() == "Profile: Bench - Hold 1 ohm.csv  (3 rows)"


# ================= E-STOP FROM THE KEYBOARD =================

def test_esc_raises_the_estop_from_any_window(app, monkeypatch):
    from PyQt6 import QtCore
    monkeypatch.setattr(RigConfig, "load", staticmethod(lambda *a, **k: RigConfig.defaults()))
    estop = threading.Event()
    window = TelemetryGUI(queue.Queue(), queue.Queue(maxsize=10), threading.Event(), estop)
    try:
        shortcut = window.estop_shortcut
        assert shortcut.key().toString() == "Esc"
        assert shortcut.context() == QtCore.Qt.ShortcutContext.ApplicationShortcut
        shortcut.activated.emit()
        assert estop.is_set()
        assert "(Esc)" in window.btn_stop.text()
    finally:
        window.timer.stop()
        window.deleteLater()


# ================= NO TELEMETRY =================

def test_telemetry_is_lost_after_a_short_silence_but_not_at_startup():
    lost = gui_layout.telemetry_lost_for
    # Before the first packet the logic process is still loading.
    assert lost(now=105.0, opened_at=100.0, last_packet_at=None) is None
    assert lost(now=100.0 + gui_layout.TELEMETRY_STARTUP_S + 1, opened_at=100.0,
                last_packet_at=None) is not None
    # Once packets have flowed, ten a second, a second and a half is a long time.
    assert lost(now=200.5, opened_at=100.0, last_packet_at=200.0) is None
    assert math.isclose(lost(now=203.0, opened_at=100.0, last_packet_at=200.0), 3.0)


def test_a_silent_controller_is_not_shown_as_running(gui):
    # The window used to keep showing the last state it was sent, for ever.
    gui.queue.put({**READING, 'fsm_state': "RUNNING"})
    gui.update_gui()
    assert gui.lbl_fsm_state.text() == "RUNNING"

    gui.last_packet_at = time.time() - 4.0          # nothing for four seconds
    gui.update_gui()
    assert gui.lbl_fsm_state.text().startswith("NO TELEMETRY")
    assert "not a live one" in gui.lbl_fsm_state.toolTip()

    gui.queue.put({**READING, 'fsm_state': "FAULT"})
    gui.update_gui()
    assert gui.lbl_fsm_state.text() == "FAULT" and gui.lbl_fsm_state.toolTip() == ""


# ================= RECORDING =================

def recording_packet(**changes):
    packet = {
        'sampled_at': 1000.25, 'voltage': 46.1234, 'amps': 45.678, 'power_kw': 2.1057,
        'max_temp': 31.26, 'true_soc': 87.654, 'fsm_state': "RUNNING",
        'target_resistance': 1.0, 'cell_voltages': [3.84125] * 12,
        'temperatures': [[30.04] * 8 for _ in range(6)],
        'current_lap': 2, 'profile_row': 17, 'requested_resistance': 0.9412,
        'expected_amps': 46.12, 'remaining_ah': 17.5309, 'daq_age_s': 0.0123,
        'resistor_temps': [61.23, 44.0, None, 30.0],
    }
    packet.update(changes)
    return packet


def test_the_recording_keeps_the_columns_older_tools_read():
    headers = gui_layout.record_headers(12, 4, 4)
    # The first 68 are as they always were; tools/dcir_from_log.py reads by name.
    assert headers[:8] == ["Timestamp", "Pack_V", "Amps", "Power_kW", "Max_Temp", "SOC_Est",
                           "FSM_State", "Target_Res"]
    assert headers[8:20] == [f"Cell_{i}_V" for i in range(1, 13)]
    assert headers[20] == "S1_T1" and headers[67] == "S12_T4"
    assert headers[68:] == ["Lap", "Row", "Asked_Res", "Expected_A", "Remaining_Ah",
                            "DAQ_Age_s", "Bank_1_T", "Bank_2_T", "Bank_3_T", "Bank_4_T"]


def test_a_row_is_stamped_with_when_the_daq_sampled_it():
    headers = gui_layout.record_headers(12, 4, 4)
    row = dict(zip(headers, gui_layout.record_row(recording_packet(), 990.0, 12, 4, 4)))
    assert len(row) == len(headers)
    assert row['Timestamp'] == 10.25                # the sample, not the redraw
    assert (row['Pack_V'], row['Amps'], row['Cell_1_V']) == (46.123, 45.68, 3.841)
    assert (row['Lap'], row['Row']) == (2, 17)
    assert (row['Asked_Res'], row['Expected_A']) == (0.941, 46.12)
    # Bank temperatures were not recorded at all. A bank with no reading yet
    # is an empty field, not a zero.
    assert (row['Bank_1_T'], row['Bank_3_T'], row['Bank_4_T']) == (61.2, '', 30.0)


def test_a_missing_reading_is_an_empty_field_never_a_zero():
    headers = gui_layout.record_headers(12, 4, 4)
    packet = recording_packet(cell_voltages=[3.8] * 11, amps=float('nan'),
                              requested_resistance=None, expected_amps=None,
                              temperatures=[], resistor_temps=[])
    row = dict(zip(headers, gui_layout.record_row(packet, 990.0, 12, 4, 4)))
    assert len(row) == len(headers)
    assert row['Cell_12_V'] == '' and row['Amps'] == '' and row['S1_T1'] == ''
    assert row['Asked_Res'] == '' and row['Bank_1_T'] == ''


def test_a_packet_with_no_sample_time_uses_when_it_arrived():
    packet = recording_packet()
    del packet['sampled_at']
    assert gui_layout.record_row(packet, 990.0, 12, 4, 4, received_at=995.5)[0] == 5.5


def test_every_sample_is_recorded_once(gui, tmp_path, monkeypatch):
    monkeypatch.setattr(gui_layout, "PROJECT_DIR", str(tmp_path))
    gui.btn_record.setChecked(True)
    gui.toggle_logging()
    try:
        # Two samples inside one redraw: both are written. Then the second
        # again, carried forward by the logic process: not written twice.
        gui.queue.put(recording_packet(sampled_at=gui.start_time + 1.0))
        gui.queue.put(recording_packet(sampled_at=gui.start_time + 1.1, amps=50.0))
        gui.update_gui()
        gui.queue.put(recording_packet(sampled_at=gui.start_time + 1.1, amps=50.0,
                                       daq_age_s=0.2))
        gui.update_gui()
    finally:
        gui.btn_record.setChecked(False)
        gui.toggle_logging()

    (log,) = (tmp_path / "logs").glob("telemetry_*.csv")
    lines = log.read_text().splitlines()
    assert len(lines) == 3                          # header and two samples
    assert [line.split(",")[0] for line in lines[1:]] == ["1.0", "1.1"]


# ================= SETTINGS IN FORCE =================

def packet_from(config):
    """What the controller reports when it is using `config`."""
    return {'active_limits': config.limits.to_command_dict(),
            'active_settings': config.settings_fingerprint()}


def test_matching_settings_show_nothing():
    cfg = RigConfig.defaults()
    assert settings_mismatch(cfg, packet_from(cfg)) == []
    assert settings_mismatch(cfg, {}) == []          # an older packet says nothing


def test_a_limit_not_in_force_is_named():
    shown = RigConfig.defaults()
    shown.limits.max_temp = 60.0
    (line,) = settings_mismatch(shown, packet_from(RigConfig.defaults()))
    assert line.startswith("Max temp: shown 60.0, in force 80.0")


def test_a_config_not_in_force_is_named():
    in_force = RigConfig.defaults()
    shown = RigConfig.defaults()
    shown.pack.cell_model = "Some other cell"
    lines = settings_mismatch(shown, packet_from(in_force))
    assert any("previous configuration" in line for line in lines)


def test_warning_waits_out_the_acknowledgement_grace(gui):
    stale = RigConfig.defaults()
    stale.limits.max_temp = 60.0
    packet = packet_from(stale)

    gui.update_settings_mismatch(packet)
    assert gui.lbl_settings_mismatch.isHidden()      # a change may be in flight

    gui.mismatch_since = time.time() - gui_layout.SETTINGS_ACK_GRACE_S - 0.1
    gui.update_settings_mismatch(packet)
    assert not gui.lbl_settings_mismatch.isHidden()
    assert "NOT IN FORCE ON THE CONTROLLER: Max temp" in gui.lbl_settings_mismatch.text()

    gui.update_settings_mismatch(packet_from(gui.config))
    assert gui.lbl_settings_mismatch.isHidden()


# ================= LOAD OVERRIDE =================

FIXED_RESISTANCE, FIXED_POWER = 1, 2         # positions in the override menu


def test_override_choices_send_what_the_controller_takes(gui):
    gui.cmb_override.setCurrentIndex(FIXED_RESISTANCE)
    assert drain(gui.gui_cmd_queue) == [("SET_OVERRIDE", "RESISTANCE", MAX_RESISTANCE)]
    gui.sb_override.setValue(1.0)
    assert drain(gui.gui_cmd_queue) == [("SET_OVERRIDE", "RESISTANCE", 1.0)]

    gui.cmb_override.setCurrentIndex(FIXED_POWER)           # shown in kW, sent in W
    gui.sb_override.setValue(2.5)
    assert drain(gui.gui_cmd_queue) == [("SET_OVERRIDE", "POWER", 500.0),
                                        ("SET_OVERRIDE", "POWER", 2500.0)]

    gui.cmb_override.setCurrentIndex(0)                     # back to the lap profile
    assert drain(gui.gui_cmd_queue) == [("SET_OVERRIDE", None)]
    assert not gui.sb_override.isEnabled()


def test_each_override_mode_keeps_its_own_value(gui):
    gui.cmb_override.setCurrentIndex(FIXED_RESISTANCE)
    gui.sb_override.setValue(2.0)
    gui.cmb_override.setCurrentIndex(FIXED_POWER)
    assert (gui.sb_override.suffix(), gui.sb_override.value()) == (" kW", 0.5)
    gui.cmb_override.setCurrentIndex(FIXED_RESISTANCE)
    assert (gui.sb_override.suffix(), gui.sb_override.value()) == (" Ω", 2.0)


def test_override_value_commits_on_enter_not_per_keystroke(gui):
    # Typing 0.5 must not send 0 and 0. on the way, mid-run.
    assert gui.sb_override.keyboardTracking() is False


def test_override_status_shows_what_the_controller_holds(gui):
    gui.cmb_override.setCurrentIndex(FIXED_RESISTANCE)
    gui.sb_override.setValue(1.0)
    gui.update_override_status({'load_override': ("RESISTANCE", 1.0),
                                'fsm_state': "RUNNING", 'target_resistance': 1.25})
    assert gui.lbl_override_status.text() == "Holding 1.00 Ω, ladder at 1.25 Ω"


def test_an_override_the_controller_never_took_is_flagged(gui):
    gui.cmb_override.setCurrentIndex(FIXED_RESISTANCE)
    packet = {'load_override': None, 'fsm_state': "IDLE"}

    gui.update_override_status(packet)
    assert not gui.lbl_override_status.text().startswith("NOT IN FORCE")  # may be in flight

    gui.override_sent_at -= gui_layout.SETTINGS_ACK_GRACE_S + 0.1
    gui.update_override_status(packet)
    assert gui.lbl_override_status.text() == "NOT IN FORCE. Controller: Profile sets the load"


# ================= TRIP REASON =================

def test_fault_summary_names_the_trip_and_its_first_detail():
    assert fault_summary(None, []) == ""
    assert fault_summary("OVERCURRENT", ["Current 291.3 A (trip 280.0 A)"]) \
        == "TRIPPED: OVERCURRENT. Current 291.3 A (trip 280.0 A)"
    assert fault_summary("CONFIG FAULT", ["  - first problem", "  - second"]) \
        == "TRIPPED: CONFIG FAULT. first problem"
    assert fault_summary("NI-DAQ OFFLINE", []) == "TRIPPED: NI-DAQ OFFLINE"


def test_the_trip_reason_shows_only_while_faulted(gui):
    reading = {'voltage': 44.0, 'amps': 0.0, 'max_temp': 24.0, 'cell_voltages': [3.67] * 12}
    gui.queue.put({**reading, 'fsm_state': "FAULT", 'fault_reason': "OVERCURRENT",
                   'fault_detail': ["Current 291.3 A (trip 280.0 A)"]})
    gui.update_gui()
    assert not gui.lbl_fault_reason.isHidden()
    assert gui.lbl_fault_reason.text() == "TRIPPED: OVERCURRENT. Current 291.3 A (trip 280.0 A)"

    gui.queue.put({**reading, 'fsm_state': "IDLE", 'fault_reason': None, 'fault_detail': []})
    gui.update_gui()
    assert gui.lbl_fault_reason.isHidden()


# ================= BENCH SETTINGS THAT MUST NOT BE FORGOTTEN =================

def test_a_threshold_edit_unticks_derivation_and_says_so(gui):
    assert gui.chk_derive.isChecked() and gui.lbl_setup_notices.isHidden()
    gui.sb_c_crit.setValue(60.0)                    # the bench supply's limit

    assert not gui.chk_derive.isChecked()
    assert not gui.lbl_setup_notices.isHidden()
    assert gui.lbl_setup_notices.text() == "LIMITS HAND-SET"
    assert "60 A" in gui.lbl_setup_notices.toolTip()
    assert "The pack gives 275 A (+5)" in gui.lbl_setup_notices.toolTip()


def test_ticking_derivation_puts_the_pack_limits_back(gui):
    # The way back used to be editing rig_config.json by hand.
    gui.sb_c_crit.setValue(60.0)
    gui.sb_cell_min.setValue(2.5)
    drain(gui.gui_cmd_queue)

    gui.chk_derive.setChecked(True)
    limits = gui.config.limits
    assert limits.derive_from_pack is True
    assert (limits.max_amps, limits.min_cell_volts) == (275.0, 2.7)
    assert (gui.sb_c_crit.value(), gui.sb_cell_min.value()) == (275.0, 2.7)
    assert gui.lbl_setup_notices.isHidden()
    (cmd,) = drain(gui.gui_cmd_queue)
    assert cmd[0] == "SET_LIMITS" and cmd[1]['max_amps'] == 275.0


def test_unticking_derivation_keeps_the_values(gui):
    gui.chk_derive.setChecked(False)
    assert gui.config.limits.derive_from_pack is False
    assert gui.config.limits.max_amps == 275.0
    assert gui.lbl_setup_notices.text() == "LIMITS HAND-SET"


def test_the_load_check_switch_reaches_the_controller_and_warns_while_off(gui):
    assert gui.chk_load_check.isChecked()
    gui.chk_load_check.setChecked(False)

    assert gui.config.limits.load_check_enabled is False
    assert gui.config.limits.derive_from_pack is True      # no override of the pack limits
    (cmd,) = drain(gui.gui_cmd_queue)
    assert cmd[0] == "SET_LIMITS" and cmd[1]['load_check'] is False
    assert gui.lbl_setup_notices.text() == "LOAD CHECK OFF"

    gui.chk_load_check.setChecked(True)
    assert gui.lbl_setup_notices.isHidden()


def test_both_notices_show_together():
    cfg = RigConfig.defaults()
    assert gui_layout.setup_notices(cfg) == []
    cfg.limits.derive_from_pack = False
    cfg.limits.load_check_enabled = False
    assert [short for short, _ in gui_layout.setup_notices(cfg)] \
        == ["LIMITS HAND-SET", "LOAD CHECK OFF"]


def test_a_window_opened_on_bench_settings_says_so_at_once(app, monkeypatch):
    bench = RigConfig.defaults()
    bench.limits.derive_from_pack = False
    bench.limits.max_amps = 60.0
    monkeypatch.setattr(RigConfig, "load", staticmethod(lambda *a, **k: bench))
    window = TelemetryGUI(queue.Queue(), queue.Queue(maxsize=10), threading.Event())
    try:
        assert window.lbl_setup_notices.text() == "LIMITS HAND-SET"
        assert not window.chk_derive.isChecked()
    finally:
        window.timer.stop()
        window.deleteLater()
