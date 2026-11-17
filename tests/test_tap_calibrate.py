"""
Per-tap calibration: the gains tools/tap_calibrate.py works out, how the rig
applies them, and what blocks ARM. No DAQ.
"""
import importlib.util
import math
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from hardware_manager import derive_cell_voltages, scale_taps
from rig_config import DEFAULT_VOLTAGE_CHANNELS, DaqConfig, PackConfig, RigConfig

spec = importlib.util.spec_from_file_location(
    "tap_calibrate", os.path.join(ROOT, "tools", "tap_calibrate.py"))
tap_calibrate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tap_calibrate)


def module_taps(cell_volts=4.0, cells=12):
    """What a meter reads at each tap of an evenly charged module."""
    return [cell_volts * (k + 1) for k in range(cells)]


# Each divider a little off its 10:1, the way 1% resistors leave them.
DIVIDER_ERROR = [1.004, 0.997, 1.009, 0.992, 1.001, 1.006, 0.995, 1.003, 0.991, 1.008,
                 0.990, 1.010]


def rig_reads(true_taps):
    """Input volts times the multiplier alone: what the rig shows uncalibrated."""
    return [v * e for v, e in zip(true_taps, DIVIDER_ERROR)]


class TestWhyItMatters:
    def test_divider_error_grows_up_the_stack(self):
        cells, _ = derive_cell_voltages(rig_reads(module_taps()))
        # Cell 1 is 16 mV out. Cell 12, the difference of two taps near 48 V
        # with errors of opposite sign, is nearly a volt out.
        assert abs(cells[0] - 4.0) < 0.02
        assert abs(cells[11] - 4.0) > 0.9

    def test_the_gains_put_every_cell_back(self):
        true = module_taps()
        gains = tap_calibrate.tap_gains(true, rig_reads(true))
        daq = DaqConfig(voltage_tap_gains=gains)
        raw_at_the_input = [v / daq.voltage_multiplier for v in rig_reads(true)]

        cells, total = derive_cell_voltages(scale_taps(raw_at_the_input, daq.tap_scales()))
        assert all(math.isclose(c, 4.0, abs_tol=1e-9) for c in cells)
        assert math.isclose(total, 48.0)

    def test_they_hold_at_another_state_of_charge(self):
        # A gain is the divider's ratio, so it does not depend on the voltage.
        gains = tap_calibrate.tap_gains(module_taps(4.0), rig_reads(module_taps(4.0)))
        daq = DaqConfig(voltage_tap_gains=gains)
        low = module_taps(3.1)
        raw = [v / daq.voltage_multiplier for v in rig_reads(low)]
        cells, _ = derive_cell_voltages(scale_taps(raw, daq.tap_scales()))
        assert all(math.isclose(c, 3.1, abs_tol=1e-9) for c in cells)


class TestTheTool:
    def test_gain_is_meter_over_rig(self):
        assert tap_calibrate.tap_gains([4.0, 8.08], [4.0, 8.0]) == [1.0, 1.01]

    def test_one_reading_per_tap(self):
        with pytest.raises(ValueError, match="11 meter readings for 12 taps"):
            tap_calibrate.tap_gains([4.0] * 11, [4.0] * 12)

    def test_an_unpowered_tap_gives_no_gain(self):
        with pytest.raises(ValueError, match="tap 2 reads 0.000 V"):
            tap_calibrate.tap_gains([4.0, 8.0], [4.0, 0.0])

    def test_the_report_gives_the_line_to_paste(self):
        true = module_taps()
        text = tap_calibrate.report(true, rig_reads(true))
        assert '"voltage_tap_gains": [' in text
        pasted = text.split('"voltage_tap_gains": [')[1].split("]")[0]
        gains = [float(g) for g in pasted.split(",")]
        assert len(gains) == 12
        assert all(math.isclose(g, 1 / e, abs_tol=1e-5) for g, e in zip(gains, DIVIDER_ERROR))
        assert "Largest cell correction: cell 12" in text
        assert "will not arm" not in text

    def test_a_gain_no_divider_explains_is_called_out(self):
        true = module_taps()
        rig = rig_reads(true)
        rig[4] *= 0.8                       # a wrong resistor on tap 5
        text = tap_calibrate.report(true, rig)
        assert "Tap(s) 5 are outside 0.9 to 1.1" in text and "will not arm" in text

    def test_cells_from_taps(self):
        assert tap_calibrate.cells_from_taps([4.0, 8.1, 12.0]) == pytest.approx([4.0, 4.1, 3.9])


class TestTheConfig:
    def test_no_gains_is_the_multiplier_alone(self):
        daq = DaqConfig()
        assert daq.voltage_tap_gains == []
        assert daq.tap_scales() == [11.0] * 12
        assert daq.safety_problems(PackConfig()) == []

    def test_a_full_set_scales_each_tap(self):
        gains = [1.0 + 0.001 * k for k in range(12)]
        scales = DaqConfig(voltage_tap_gains=gains).tap_scales()
        assert scales == pytest.approx([11.0 * g for g in gains])

    def test_a_set_of_the_wrong_length_blocks_arm_and_is_not_applied(self):
        daq = DaqConfig(voltage_tap_gains=[1.01] * 11)
        assert any("11 tap gains for 12 voltage channels" in p
                   for p in daq.safety_problems(PackConfig()))
        # Which tap each belonged to is unknown, so none is used.
        assert daq.tap_scales() == [11.0] * 12

    def test_a_gain_past_a_dividers_tolerance_blocks_arm(self):
        for bad in (0.5, 1.5, 0.0, -1.0, float("nan")):
            gains = [1.0] * 12
            gains[6] = bad
            problems = DaqConfig(voltage_tap_gains=gains).safety_problems(PackConfig())
            assert any("Tap gain(s) for tap 7" in p for p in problems), bad

    def test_gains_survive_a_save_and_load(self, tmp_path):
        cfg = RigConfig.defaults()
        cfg.daq.voltage_tap_gains = [1.004] * 12
        path = cfg.save(str(tmp_path / "rig_config.json"))
        assert RigConfig.load(path).daq.voltage_tap_gains == [1.004] * 12

    def test_a_quoted_gain_in_the_file_does_not_reach_the_daq_as_text(self):
        daq = DaqConfig(voltage_tap_gains=["1.004"] * 12)
        assert daq.voltage_tap_gains == [1.004] * 12
        with pytest.raises(ValueError):
            DaqConfig(voltage_tap_gains=["high"] * 12)

    def test_a_config_from_before_the_field_existed_still_loads(self):
        old = RigConfig.defaults().to_dict()
        del old['daq']['voltage_tap_gains']
        assert RigConfig.from_dict(old).daq.voltage_tap_gains == []

    def test_changed_gains_change_the_settings_fingerprint(self):
        # So the window can tell the controller is still judging the old wiring.
        a, b = RigConfig.defaults(), RigConfig.defaults()
        b.daq.voltage_tap_gains = [1.004] * 12
        assert a.settings_fingerprint() != b.settings_fingerprint()

    def test_the_shipped_channel_list_and_gains_agree(self):
        shipped = RigConfig.load()
        assert len(shipped.daq.voltage_channels) == len(DEFAULT_VOLTAGE_CHANNELS)
        assert len(shipped.daq.voltage_tap_gains) in (0, len(shipped.daq.voltage_channels))
        assert shipped.daq.safety_problems(shipped.pack) == []
