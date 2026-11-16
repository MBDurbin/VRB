"""
current_and_temp_check.py -- the LEM current and the 1 ohm resistor's temperature, live.

Bench script, run directly:

    python tools/current_and_temp_check.py

Prints the LEM current sensor and the thermocouple on the 1 ohm resistor (bank
3, jack TC4) twice a second until Ctrl+C. Nothing else is read: no cell taps,
no cell temperatures, no Arduino.

    --bank N      another bank's thermocouple instead (1-4)
    --period S    seconds between lines (default 0.5)
    --count N     stop after N lines (default: run until Ctrl+C)

To watch the same two readings on a plot and record them to a CSV, run
tools/current_and_temp_recorder.py.

The channels, the amps-per-volt scale and the thermocouple settings come from
rig_config.json, so this reads what the rig reads. Reads only; it never writes
to the DAQ or the config.

Close main_v2.py first. Two programs cannot hold the same DAQ module, and the
rig's own reader does not come back if it loses one.
"""

import argparse
import math
import os
import sys
import time

import nidaqmx
from nidaqmx.constants import ADCTimingMode, CJCSource, TemperatureUnits, ThermocoupleType

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hardware_manager import TC_MAX_C, TC_MIN_C
from rig_config import RigConfig

# Bank 3 is the single 1 ohm TE2000 element (docs/hardware_topology.md).
ONE_OHM_BANK = 3

# The current on each line is the mean of this many back-to-back samples.
CURRENT_SAMPLES = 10


def tc_channel(daq, bank):
    """The DAQ channel of a bank's thermocouple. Banks count from 1."""
    channels = daq.resistor_tc_channels
    if not 1 <= bank <= len(channels):
        raise ValueError(f"Bank {bank} has no thermocouple: rig_config.json lists "
                         f"{len(channels)}, bank 1 first.")
    return channels[bank - 1]


def is_temperature(temp_c):
    """False for a reading the rig would not accept as a temperature.

    An empty or broken jack on the NI 9212 reads about 2284 C.
    """
    return math.isfinite(temp_c) and TC_MIN_C <= temp_c <= TC_MAX_C


def lem_on_scale(volts, daq):
    """False when the LEM input is past the range it is read on, so not a current.

    The 9215 input sat at -10.44 V, its rail, on the bench on 2026-11-09, which
    scales to -1044 A. Whatever the cause (sensor supply off, signal lead off),
    that number is not a measurement.
    """
    return math.isfinite(volts) and daq.ai_min_volts <= volts <= daq.ai_max_volts


def temperature_text(temp_c):
    """A thermocouple reading for the screen. One that is not a temperature says so."""
    if is_temperature(temp_c):
        return f"{temp_c:7.1f} °C"
    return f"open? (reads {temp_c:.0f} °C)"


def current_text(volts, amps, daq):
    """The current for the screen, or that the LEM signal is off scale."""
    if lem_on_scale(volts, daq):
        return f"{amps:10.2f}"
    return " off scale"


def start_tasks(current_task, tc_task, daq, channel):
    """Set up the LEM input and one thermocouple the way the rig does, and start both.

    Started once and left running: a task left unstarted is started and stopped
    by the driver around every read, ~110 ms a read against ~2 ms.
    """
    current_task.ai_channels.add_ai_voltage_chan(
        daq.current_channel, min_val=daq.ai_min_volts, max_val=daq.ai_max_volts)
    chan = tc_task.ai_channels.add_ai_thrmcpl_chan(
        channel, min_val=TC_MIN_C, max_val=TC_MAX_C,
        units=TemperatureUnits.DEG_C,
        thermocouple_type=ThermocoupleType[daq.thermocouple_type],
        cjc_source=CJCSource[daq.resistor_tc_cjc_source])
    if daq.resistor_tc_adc_mode != "DEFAULT":
        chan.ai_adc_timing_mode = ADCTimingMode[daq.resistor_tc_adc_mode]
    current_task.start()
    tc_task.start()


def read_once(current_task, tc_task, amps_per_volt, samples=CURRENT_SAMPLES):
    """One line's readings: (LEM volts, amps, bank temperature in C)."""
    volts = sum(current_task.read() for _ in range(samples)) / samples
    return volts, volts * amps_per_volt, tc_task.read()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Print the LEM current and one resistor bank's temperature.")
    parser.add_argument("--bank", type=int, default=ONE_OHM_BANK,
                        help=f"bank whose thermocouple to read (default {ONE_OHM_BANK}, the 1 ohm)")
    parser.add_argument("--period", type=float, default=0.5,
                        help="seconds between lines (default 0.5)")
    parser.add_argument("--count", type=int, default=None,
                        help="stop after this many lines (default: until Ctrl+C)")
    args = parser.parse_args(argv)
    if not args.period > 0:
        parser.error("--period must be more than 0")

    daq = RigConfig.load().daq
    try:
        channel = tc_channel(daq, args.bank)
    except ValueError as exc:
        parser.error(str(exc))

    try:
        with nidaqmx.Task() as current_task, nidaqmx.Task() as tc_task:
            start_tasks(current_task, tc_task, daq, channel)

            print(f"LEM on {daq.current_channel} at {daq.current_amps_per_volt:g} A/V. "
                  f"Bank {args.bank} thermocouple (type {daq.thermocouple_type}) on {channel}.")
            print("Ctrl+C to stop.\n")
            print(f"{'time s':>8}  {'LEM V':>8}  {'current A':>10}  bank {args.bank}")

            start = time.monotonic()
            line = 0
            while args.count is None or line < args.count:
                volts, amps, temp_c = read_once(
                    current_task, tc_task, daq.current_amps_per_volt)
                print(f"{time.monotonic() - start:8.1f}  {volts:8.4f}  "
                      f"{current_text(volts, amps, daq)}  {temperature_text(temp_c)}",
                      flush=True)
                line += 1
                # Timed from the start, so the lines do not drift later.
                time.sleep(max(0.0, start + line * args.period - time.monotonic()))
    except nidaqmx.DaqError as exc:
        print(f"\n[DAQ ERROR] {exc}")
        print("Is the chassis plugged in and powered, and main_v2.py closed?")
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
