"""
Unit tests for the DAQ layer's pure data handling.

Covers cumulative-tap differencing and temperature line parsing, both of which
previously assumed a fixed 12S / 6x8 layout and would raise or silently corrupt
readings on any other pack. No NI-DAQ or serial hardware required.
"""
import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hardware_manager import derive_cell_voltages, parse_temperature_line


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
