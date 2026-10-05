"""
Tests for the resistor bank thermal map in gui_layout.

The tile logic is plain Python. The window itself is rendered off-screen, because
a mistake in a paintEvent only surfaces when Qt actually draws -- an import or a
constructor call would not catch it.
"""
import os
import sys

# Off-screen before Qt loads, so this runs without a display.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6 import QtWidgets

import theme
from gui_layout import (BANK_COUNT, BANK_PARTS, FLAT_BAR_BANKS, MIDDLE_ROW_BANKS,
                        ResistorMapWindow, bank_tile)


class TestBankTile:
    def test_cool_bank_is_ok(self):
        assert bank_tile(40.0, 0.1, 225.0, 3.0)[1] == "OK"

    def test_at_trip_is_over(self):
        assert bank_tile(225.0, 0.1, 225.0, 3.0)[1] == "OVER"

    def test_colour_runs_to_red_at_that_banks_own_trip(self):
        # 150 C is the top of bank 4's ramp but only part way up a TE bank's.
        assert bank_tile(150.0, 0.1, 150.0, 3.0)[0] == theme.HEAT_HOT
        assert bank_tile(150.0, 0.1, 225.0, 3.0)[0] != theme.HEAT_HOT

    def test_old_reading_is_stale(self):
        assert bank_tile(40.0, 10.0, 225.0, 3.0)[1] == "STALE"

    def test_over_outranks_stale(self):
        assert bank_tile(230.0, 10.0, 225.0, 3.0)[1] == "OVER"

    def test_thermocoupled_bank_without_a_reading(self):
        assert bank_tile(None, float('inf'), 225.0, 3.0)[1] == "NO DATA"

    def test_bank_without_a_thermocouple(self):
        assert bank_tile(None, float('inf'), None, 3.0)[1] == "NO SENSOR"


class TestTopology:
    """The drawing must hold every element the bank table says exists."""

    def test_middle_row_is_banks_2_and_3(self):
        # Bank 2 is two 1 ohm elements, bank 3 one.
        assert sorted(MIDDLE_ROW_BANKS) == [2, 2, 3]

    def test_flat_bars_carry_banks_4_to_8(self):
        # Bank 4 is 2 x 4 ohm, bank 8 is 2 x 16 ohm; 5-7 are single parts.
        assert sorted(FLAT_BAR_BANKS) == [4, 4, 5, 6, 7, 8, 8]

    def test_every_bank_is_described(self):
        assert sorted(BANK_PARTS) == list(range(1, BANK_COUNT + 1))


class TestResistorMapWindow:
    def _app(self):
        return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_renders_live_data_without_error(self):
        app = self._app()
        win = ResistorMapWindow()
        win.update_banks([231.0, 120.0, None, 95.0], [0.1, 9.0, float('inf'), 0.1],
                         [225.0, 225.0, 225.0, 150.0], 3.0)
        image = win.grab()              # forces a full paint off-screen
        assert not image.isNull()
        statuses = {bank: win.rows[bank][4].text() for bank in range(1, BANK_COUNT + 1)}
        assert statuses == {1: "OVER", 2: "STALE", 3: "NO DATA", 4: "OK",
                            5: "NO SENSOR", 6: "NO SENSOR", 7: "NO SENSOR", 8: "NO SENSOR"}
        win.close()
        app.processEvents()

    def test_renders_before_any_data(self):
        app = self._app()
        win = ResistorMapWindow()
        win.update_banks([], [], [225.0, 225.0, 225.0, 150.0], 3.0)
        assert not win.grab().isNull()
        win.close()
        app.processEvents()
