"""
tap_calibrate.py -- a gain for each cell tap, from a multimeter.

Bench script, run directly with the taps powered (a module, or the bench tap
supply into its divider) and no current flowing:

    python tools/tap_calibrate.py

It reads every tap in voltage_channels the way the rig does, then asks for the
multimeter's reading of each one, measured from the module's negative to that
tap. Or give the twelve readings, tap 1 first, and it asks nothing:

    python tools/tap_calibrate.py --meter 4.012,8.021,12.035,...

It prints each tap's gain (meter volts over the rig's reading), what each cell
reads before and after, and the line to paste into rig_config.json as
"voltage_tap_gains". Restart the rig after pasting: the DAQ process takes its
wiring once, at start.

Why. Each tap has its own 10:1 divider and no two are exactly 10:1. A cell's
voltage is the difference of two taps, so the dividers' errors do not cancel,
they grow up the stack: taps 11 and 12 sit near 46 V and 50 V, and 1% on each
can put cell 12 almost a volt out. The per-cell undervoltage trip and each
cell's measured resistance carry the same error.

Use a meter you trust to a few millivolts at 50 V, and take all the readings
in one sitting: the gains are only as good as the meter, and a module drifts.
The rig's reading here is the divider multiplier alone, without any gains
already in the config, so the list printed replaces the old one rather than
stacking on it.

Reads only; it never writes to the DAQ or the config.

Close main_v2.py first. Two programs cannot hold the same DAQ module, and the
rig's own reader does not come back if it loses one.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rig_config import TAP_GAIN_MAX, TAP_GAIN_MIN, RigConfig

# On-demand reads averaged per tap. The 9205's noise is a few hundred microvolts
# at the input, a few millivolts after the divider; fifty brings that under one.
SAMPLES = 50


def tap_gains(meter_volts, uncorrected_volts):
    """The gain for each tap: what the meter read over what the rig read.

    `uncorrected_volts` is the input volts times the divider multiplier alone,
    with no gain applied. Raises ValueError on a reading that cannot give one.
    """
    if len(meter_volts) != len(uncorrected_volts):
        raise ValueError(f"{len(meter_volts)} meter readings for "
                         f"{len(uncorrected_volts)} taps")
    gains = []
    for tap, (meter, rig) in enumerate(zip(meter_volts, uncorrected_volts), start=1):
        if not rig > 0:
            raise ValueError(f"tap {tap} reads {rig:.3f} V on the rig: is it powered?")
        gains.append(meter / rig)
    return gains


def cells_from_taps(tap_volts):
    """Per-cell voltages from cumulative taps: each tap less the one below it."""
    return [v - (tap_volts[i - 1] if i else 0.0) for i, v in enumerate(tap_volts)]


def out_of_range(gains):
    """1-based taps whose gain no divider tolerance explains."""
    return [i + 1 for i, g in enumerate(gains) if not TAP_GAIN_MIN <= g <= TAP_GAIN_MAX]


def report(meter_volts, uncorrected_volts):
    """The table and the line to paste, as text. Raises ValueError as tap_gains()."""
    gains = tap_gains(meter_volts, uncorrected_volts)
    before = cells_from_taps(uncorrected_volts)
    after = cells_from_taps(meter_volts)

    lines = [f"{'Tap':>3} {'Rig V':>9} {'Meter V':>9} {'Gain':>8}   "
             f"{'Cell before':>11} {'Cell after':>10} {'Shift mV':>9}"]
    for i, gain in enumerate(gains):
        lines.append(f"{i + 1:3d} {uncorrected_volts[i]:9.3f} {meter_volts[i]:9.3f} "
                     f"{gain:8.4f}   {before[i]:11.3f} {after[i]:10.3f} "
                     f"{(after[i] - before[i]) * 1000:9.1f}")
    worst = max(range(len(gains)), key=lambda i: abs(after[i] - before[i]))
    lines.append("")
    lines.append(f"Largest cell correction: cell {worst + 1}, "
                 f"{(after[worst] - before[worst]) * 1000:+.0f} mV.")

    bad = out_of_range(gains)
    if bad:
        lines.append(f"Tap(s) {', '.join(map(str, bad))} are outside {TAP_GAIN_MIN:g} to "
                     f"{TAP_GAIN_MAX:g}: more than a divider's tolerance. Check the meter "
                     f"reading and the tap before using these; the rig will not arm on them.")
    lines.append("")
    lines.append('Paste into the "daq" section of rig_config.json, then restart the rig:')
    lines.append('    "voltage_tap_gains": [' + ", ".join(f"{g:.5f}" for g in gains) + "],")
    return "\n".join(lines)


def read_taps(daq, samples=SAMPLES):
    """Mean input volts times the divider multiplier for each tap, tap 1 first."""
    import nidaqmx
    from nidaqmx.constants import TerminalConfiguration

    mode = TerminalConfiguration[daq.voltage_terminal_config]
    with nidaqmx.Task() as task:
        for channel in daq.voltage_channels:
            task.ai_channels.add_ai_voltage_chan(
                channel, terminal_config=mode,
                min_val=daq.ai_min_volts, max_val=daq.ai_max_volts)
        task.start()
        totals = [0.0] * len(daq.voltage_channels)
        for _ in range(samples):
            reading = task.read()
            if not isinstance(reading, list):
                reading = [reading]
            totals = [t + v for t, v in zip(totals, reading)]
    return [t / samples * daq.voltage_multiplier for t in totals]


def ask_meter(uncorrected_volts):
    """Prompt for the multimeter's reading of each tap."""
    print("Multimeter black lead on the module's negative. Type the reading for each tap.\n")
    meter = []
    for tap, rig in enumerate(uncorrected_volts, start=1):
        while True:
            text = input(f"  Tap {tap:2d} (the rig reads {rig:7.3f} V): ").strip()
            try:
                meter.append(float(text))
                break
            except ValueError:
                print("    not a number")
    return meter


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--meter", help="the multimeter's readings, tap 1 first, comma separated")
    args = parser.parse_args(argv)

    daq = RigConfig.load().daq
    print(f"Reading {len(daq.voltage_channels)} taps, {daq.voltage_terminal_config}, "
          f"x{daq.voltage_multiplier:g}, {SAMPLES} samples each...")
    uncorrected = read_taps(daq)

    if args.meter:
        try:
            meter = [float(part) for part in args.meter.split(",") if part.strip()]
        except ValueError:
            sys.exit("--meter takes numbers separated by commas")
    else:
        meter = ask_meter(uncorrected)

    try:
        print("\n" + report(meter, uncorrected))
    except ValueError as exc:
        sys.exit(f"Cannot work out the gains: {exc}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
