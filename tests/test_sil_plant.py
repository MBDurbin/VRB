"""
The SIL plant models the configured pack and wiring, not a hardcoded one.

It used to hardcode the Molicel P45B's 45 mOhm module and a 4x12 sensor layout,
so after the move to the RS50 its sag was 3-4x too large, and its packets would
not match a different sensor layout. Runs on Qt's offscreen platform.
"""
import os
import queue
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from PyQt6 import QtWidgets

from control_logic import check_measurements, evaluate_safety, expected_measurements
from rig_config import RigConfig
from sil_simulator import SILSimulatorWindow


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def packet(app, cfg, amps):
    q = queue.Queue(maxsize=5)
    plant = SILSimulatorWindow(q, cfg)
    plant.timer.stop()
    plant.slider_amps.setValue(int(amps * 10))
    plant.inject_telemetry()
    plant.deleteLater()
    return q.get_nowait()


def test_sag_follows_the_configured_pack(app):
    cfg = RigConfig.defaults()              # RS50 12S4P: 12 mOhm module
    data = packet(app, cfg, 250.0)
    assert abs(data['voltage'] - (cfg.pack.max_voltage - 250.0 * cfg.pack.resistance_ohm)) < 1e-9


def test_packets_are_complete_for_the_configured_wiring(app):
    for layout in ((6, 8, 4), (5, 10, 3)):
        cfg = RigConfig.defaults()
        cfg.daq.temp_bus_count, cfg.daq.sensors_per_bus = layout[0], layout[1]
        cfg.daq.resistor_tc_channels = cfg.daq.resistor_tc_channels[:layout[2]]
        data = packet(app, cfg, 50.0)
        expected = expected_measurements(cfg.daq, cfg.pack)
        assert check_measurements(data, expected) == (False, None), layout


def test_the_140_percent_bank_trip_is_reachable_on_the_slider(app):
    # With the real pack resistance, 250 A at a full pack is ~11.9 kW, past the
    # 11.2 kW ladder limit. The P45B model topped out near 9.8 kW.
    cfg = RigConfig.defaults()
    data = packet(app, cfg, 250.0)
    limits = cfg.limits.to_command_dict()
    assert evaluate_safety(data, limits, armed=False) == (True, "BANK OVERPOWER")
