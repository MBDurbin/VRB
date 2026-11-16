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

def test_configure_is_refused_while_running(gui, monkeypatch):
    told = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "information",
                        lambda *a, **k: told.append(a[1]))
    monkeypatch.setattr(ConfigDialog, "exec",
                        lambda self: pytest.fail("the dialog opened mid-run"))
    before = gui.config.to_dict()

    gui.last_fsm_state = "RUNNING"
    gui.open_config_dialog()

    assert told == ["Run in progress"]
    assert gui.config.to_dict() == before
    assert drain(gui.gui_cmd_queue) == []


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
