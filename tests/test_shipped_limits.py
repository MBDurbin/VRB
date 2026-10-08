"""
The shipped rig_config.json against the two limits that bound the load.

The cells' current rating and the resistor bank's power ratings are separate
limits, and both have to hold for every setting the rig can command, at any
state of charge and with either bank power setting. Generic in the pack: if the
rig is retargeted, these still check the new config rather than the RS50.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from control_logic import (
    BANK_OVERLOAD_FACTOR, RESISTOR_RESOLUTION, RESISTOR_TOLERANCE,
    check_bank_power, check_thermal_and_current, command_steps, steps_within_rating,
)
from rig_config import CONFIG_PATH, RigConfig


def shipped():
    return RigConfig.load(CONFIG_PATH)


def test_over_current_trip_sits_on_the_pack_rating():
    cfg = shipped()
    trip = cfg.limits.max_amps + cfg.limits.amp_buffer
    assert trip <= cfg.pack.max_current_a
    assert cfg.arm_blockers() == []


def test_the_file_says_what_is_in_force():
    # It used to say 180 A while pack derivation replaced it with 275 A on every
    # load, behind a "hand-edited limits were replaced" dialog.
    assert getattr(shipped(), 'discarded_limits', []) == []


def test_every_command_respects_the_current_limit_and_every_bank_rating():
    cfg = shipped()
    pack, limits = cfg.pack, cfg.limits.to_command_dict()
    lo, hi = pack.min_voltage, pack.max_voltage
    volts = [lo + (hi - lo) * i / 20 for i in range(21)]

    for v in volts:
        for factor in (1.0, BANK_OVERLOAD_FACTOR):
            for target in list(range(1, 256)) + [None]:
                # Every ladder setting as the request, plus flat-out demand.
                demand = 1e12 if target is None else \
                    pack.modules_in_series * v ** 2 / (target * RESISTOR_RESOLUTION)
                steps = command_steps(v, demand, limits['max_amps'], 25.0, False,
                                      limits['derate_start'], limits['max_temp'],
                                      modules_in_series=pack.modules_in_series,
                                      power_factor=factor)
                ohms = steps * RESISTOR_RESOLUTION
                worst_amps = v / (ohms * (1.0 - RESISTOR_TOLERANCE))

                # The cells: commanded current within the operating limit, and
                # even worst-case tolerance never reaches the over-current trip.
                assert v / ohms <= limits['max_amps'], (v, factor, steps)
                assert check_thermal_and_current(
                    25.0, worst_amps, limits['max_temp'], limits['max_amps'],
                    limits['amp_buffer']) == (False, None), (v, factor, steps)
                # The bank: the ladder total and every bank in circuit.
                assert steps_within_rating(steps, v, factor), (v, factor, steps)
                assert check_bank_power(v, worst_amps, steps, factor) == (False, None)


def test_with_these_cells_the_ladder_caps_current_below_the_cell_rating():
    # The 0.25 ohm bottom step limits a full module to ~200 A, under the RS50's
    # 280 A, so the bank's power ratings bind before the cells' current does.
    # The over-current trip stays as the backstop for a fault, such as a
    # shorted ladder.
    cfg = shipped()
    flat_out = command_steps(cfg.pack.max_voltage, 1e12, cfg.limits.max_amps, 25.0, False,
                             cfg.limits.derate_start, cfg.limits.max_temp,
                             modules_in_series=cfg.pack.modules_in_series,
                             power_factor=BANK_OVERLOAD_FACTOR)
    amps = cfg.pack.max_voltage / (flat_out * RESISTOR_RESOLUTION)
    assert amps < cfg.limits.max_amps
