"""
thermocouple_check.py  (was tests/test_tc.py)

Thermocouple test code for type K thermocouples using NI DAQ
"""

import nidaqmx
from nidaqmx.constants import TemperatureUnits, ThermocoupleType

# Bench script, run directly: python tools/thermocouple_check.py
# The guard keeps a plain import from opening the DAQ. It used to sit in tests/
# as test_tc.py, where pytest imported it and, without cDAQ1Mod3 present, the
# import failed and halted the whole suite.
if __name__ == "__main__":
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