"""
test_tc.py

Thermocouple test code for type K thermocouples using NI DAQ
"""

import nidaqmx
from nidaqmx.constants import TemperatureUnits, ThermocoupleType

with nidaqmx.Task() as task:
    task.ai_channels.add_ai_thrmcpl_chan(
        "cDAQ1Mod3/ai0:1",    # Mod# is the module slot number, ai#:# is the TC plug numbers (inclusive)
        thermocouple_type=ThermocoupleType.K,
        units=TemperatureUnits.DEG_C
    )
    
    # Read
    temps = task.read()  # Reads a single sample from all of the TC's in °C
    
    for i, temp in enumerate(temps):
        print(f"Channel ai{i}: {temp:.2f} °C")