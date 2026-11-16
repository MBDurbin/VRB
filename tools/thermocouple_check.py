"""
thermocouple_check.py  (was tests/test_tc.py)

Thermocouple test code for type K thermocouples using NI DAQ
"""

import os
import sys

import nidaqmx
from nidaqmx.constants import CJCSource, TemperatureUnits, ThermocoupleType

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from hardware_manager import TC_MAX_C, TC_MIN_C
from rig_config import RigConfig

# Bench script, run directly: python tools/thermocouple_check.py
# The guard keeps a plain import from opening the DAQ. It used to sit in tests/
# as test_tc.py, where pytest imported it and, without cDAQ1Mod3 present, the
# import failed and halted the whole suite.
#
# Reads the bank thermocouples rig_config.json lists, bank 1 first, with the
# same type and cold junction the rig uses. It used to read cDAQ1Mod3/ai0:1,
# but the banks sit on every other jack of the TB-9212 (TC0, TC2, TC4, TC6).
if __name__ == "__main__":
    daq = RigConfig.load().daq
    with nidaqmx.Task() as task:
        for channel in daq.resistor_tc_channels:
            task.ai_channels.add_ai_thrmcpl_chan(
                channel, min_val=TC_MIN_C, max_val=TC_MAX_C,
                thermocouple_type=ThermocoupleType[daq.thermocouple_type],
                units=TemperatureUnits.DEG_C,
                cjc_source=CJCSource[daq.resistor_tc_cjc_source],
            )

        # Read
        temps = task.read()  # Reads a single sample from all of the TC's in °C
        if not isinstance(temps, list):
            temps = [temps]

        for bank, (channel, temp) in enumerate(zip(daq.resistor_tc_channels, temps), start=1):
            print(f"Bank {bank} ({channel}): {temp:.2f} °C")
