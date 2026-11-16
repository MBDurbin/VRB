"""
tap_map_check.py -- which input carries which cell tap, read off the hardware.

Bench script, run directly with the taps powered (the bench tap supply into its
divider, or a module) and no current flowing:

    python tools/tap_map_check.py 44.0

The argument is the top-of-stack voltage: the tap supply's setting, or the
module voltage measured with a multimeter. Every input of the module the taps
are configured on is read on its own, the way the rig reads the taps
(voltage_terminal_config, against COM for RSE), and scaled by the divider
multiplier. Tap k of an N-cell stack sits at k/N of the top, so each input is
matched to the tap it reads. The result is checked against voltage_channels in
rig_config.json; if they differ, the list to paste in is printed.

Reads only; it never writes to the DAQ or the config.

A floating input on a multiplexed module such as the 9205 holds the voltage of
whatever was read before it, so an unwired input can look like a tap. Every
input is therefore read twice, in ascending and then descending order: a wired
tap reads the same both times, a floating input copies a different neighbour
and is set aside. A tap that more than one input still matches is flagged, not
guessed.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rig_config import RigConfig

# How close to tap k an input must read, as a fraction of one cell's voltage.
# Under half, so a reading can match only one tap. Wide because a divider's
# error grows up the stack: 3% low is a third of a cell at tap 12.
MATCH_TOLERANCE = 0.4

# An input whose two readings differ by more than this fraction of a cell is
# floating: it reads whatever was read before it.
FLOATING_SPREAD = 0.1


def floating_inputs(first, second, step, spread=FLOATING_SPREAD):
    """Inputs whose readings in two different orders disagree: wired to nothing."""
    return {ch for ch in first if ch in second and abs(first[ch] - second[ch]) > spread * step}


def match_taps(readings, top_volts, series_count, tolerance=MATCH_TOLERANCE):
    """Which tap each input reads. Returns ({channel: tap or 0 or None}, {tap: [channels]}).

    `readings` maps channel name to its scaled voltage. Tap 0 is the stack's
    negative, the ground; None is an input that matches nothing.
    """
    step = top_volts / series_count
    per_channel, per_tap = {}, {k: [] for k in range(1, series_count + 1)}
    for channel, volts in readings.items():
        k = volts / step
        nearest = round(k)
        if 0 <= nearest <= series_count and abs(k - nearest) <= tolerance:
            per_channel[channel] = nearest
            if nearest:
                per_tap[nearest].append(channel)
        else:
            per_channel[channel] = None
    return per_channel, per_tap


def suggested_channels(per_tap):
    """The voltage_channels list, tap 1 first, if every tap has exactly one input."""
    if all(len(chans) == 1 for chans in per_tap.values()):
        return [per_tap[k][0] for k in sorted(per_tap)]
    return None


def read_input(nidaqmx, mode, channel, samples=10):
    """Mean of `samples` on-demand reads of one input, in volts at the terminal."""
    with nidaqmx.Task() as task:
        task.ai_channels.add_ai_voltage_chan(channel, terminal_config=mode,
                                             min_val=-10.0, max_val=10.0)
        values = [task.read() for _ in range(samples)]
    return sum(values) / len(values)


def main(top_volts):
    import nidaqmx
    import nidaqmx.system
    from nidaqmx.constants import TerminalConfiguration

    config = RigConfig.load()
    daq, series = config.daq, config.pack.series_count
    module = daq.voltage_channels[0].split("/")[0]
    mode = TerminalConfiguration[daq.voltage_terminal_config]
    inputs = [c.name for c in nidaqmx.system.Device(module).ai_physical_chans]

    step = top_volts / series
    print(f"{module}: {len(inputs)} inputs, read {daq.voltage_terminal_config}, "
          f"x{daq.voltage_multiplier:g}. Top of stack {top_volts:g} V, "
          f"{step:.3f} V per tap.\n")

    def read_all(order):
        out = {}
        for channel in order:
            try:
                out[channel] = read_input(nidaqmx, mode, channel) * daq.voltage_multiplier
            except nidaqmx.DaqError as exc:
                print(f"  {channel:18s} could not be read: {exc}")
        return out

    up, down = read_all(inputs), read_all(list(reversed(inputs)))
    floating = floating_inputs(up, down, step)
    readings = {ch: (up[ch] + down[ch]) / 2 for ch in inputs
                if ch in up and ch in down and ch not in floating}
    per_channel, per_tap = match_taps(readings, top_volts, series)

    configured = {c: k for k, c in enumerate(daq.voltage_channels, start=1)}
    for channel in inputs:
        if channel not in up or channel not in down:
            continue
        volts = (up[channel] + down[channel]) / 2
        tap = None if channel in floating else per_channel[channel]
        found = ("floating" if channel in floating
                 else {None: "", 0: "ground (0 V)"}.get(tap, f"tap {tap}"))
        want = f"configured as tap {configured[channel]}" if channel in configured else ""
        flag = "  <-- MISMATCH" if channel in configured and tap != configured[channel] else ""
        print(f"  {channel:18s} {volts:8.3f} V  {found:13s} {want}{flag}")

    print()
    problems = 0
    for k, chans in per_tap.items():
        if not chans:
            print(f"Tap {k}: no input reads it. Check the wire and the terminal.")
            problems += 1
        elif len(chans) > 1:
            print(f"Tap {k}: read by {', '.join(chans)}. Only one is wired; an unwired "
                  f"input can copy the one read before it.")
            problems += 1
    if not any(tap == 0 for tap in per_channel.values()):
        print("No input reads 0 V. That is fine if the ground is only on COM.")

    suggestion = suggested_channels(per_tap)
    if suggestion == list(daq.voltage_channels):
        print("voltage_channels in rig_config.json matches the wiring.")
        return 0
    if suggestion:
        print("voltage_channels in rig_config.json does NOT match the wiring. To match:")
        print('    "voltage_channels": [' + ", ".join(f'"{c}"' for c in suggestion) + "],")
    elif not problems:
        print("Could not work out a full tap map.")
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python tools/tap_map_check.py TOP_OF_STACK_VOLTS")
    sys.exit(main(float(sys.argv[1])))
