"""
Current Sensor.py -- the current transducer's reading, streamed to the console.

Bench script, run directly:

    python "tools/Current Sensor.py"

Prints the raw volts on the current input and the amps they scale to, five
times a second, until Ctrl+C. The channel and the amps-per-volt scale come
from rig_config.json, so it reads what the rig reads. It used to name its own
channel, cDAQ1Mod8/ai0, which stopped being the current input when the chassis
was rewired in October 2026: the sensor is on the NI 9215 in slot 7.

tools/current_and_temp_check.py reads the same input beside a bank
thermocouple, and tools/current_and_temp_recorder.py plots and records both.

Reads only. Close main_v2.py first: two programs cannot hold the same DAQ
module, and the rig's own reader does not come back if it loses one.
"""

import os
import sys
import time

import nidaqmx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from rig_config import RigConfig


def read_current_sensor():
    daq = RigConfig.load().daq
    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                daq.current_channel, min_val=daq.ai_min_volts, max_val=daq.ai_max_volts)

            print(f"Reading {daq.current_channel} at {daq.current_amps_per_volt:g} A/V. "
                  f"Ctrl+C to stop.\n")
            print("-" * 50)

            while True:
                raw_voltage = task.read()
                current_amps = raw_voltage * daq.current_amps_per_volt
                print(f"Raw Signal: {raw_voltage:>7.4f} V  |  "
                      f"Calculated Current: {current_amps:>7.2f} A")
                time.sleep(0.2)

    except nidaqmx.DaqError as e:
        print("\n[DAQ Error]")
        print(e)
        print("\nCheck current_channel in rig_config.json against "
              'python "tools/discover DAQ.py".')
    except KeyboardInterrupt:
        print("\n\nData acquisition stopped by user.")


if __name__ == "__main__":
    read_current_sensor()
