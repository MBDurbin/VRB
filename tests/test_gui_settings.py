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
from gui_layout import ConfigDialog, TelemetryGUI, settings_mismatch
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
