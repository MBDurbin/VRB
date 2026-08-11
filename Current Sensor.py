import nidaqmx
import time

# NOTE: You will likely need to change "cDAQ1Mod1" to match your specific hardware name
# You can find the exact name in the NI MAX software under "Devices and Interfaces"
CHANNEL_NAME = "cDAQ1Mod8/ai0"


def read_current_sensor():
    try:
        # Create a DAQmx Task
        with nidaqmx.Task() as task:

            # Configure the channel for Analog Input (Voltage)
            # The NI 9215 has a standard range of +/- 10 Volts
            task.ai_channels.add_ai_voltage_chan(
                CHANNEL_NAME,
                min_val=-10.0,
                max_val=10.0
            )

            print(f"Connecting to {CHANNEL_NAME}...")
            print("Reading data. Press Ctrl+C to stop.\n")
            print("-" * 50)

            # Continuously read from the DAQ
            while True:
                # Read a single voltage sample
                raw_voltage = task.read()

                # Scale the voltage to Amperage
                # The LEM HAC 400-S typically outputs +/- 4V for +/- 400A
                # Therefore, the scaling factor is 100 Amps per 1 Volt
                current_amps = raw_voltage * 100.0

                # Print the formatted results
                print(f"Raw Signal: {raw_voltage:>7.4f} V  |  Calculated Current: {current_amps:>7.2f} A")

                # Small delay to keep the terminal readable
                time.sleep(0.2)

    except nidaqmx.DaqError as e:
        print("\n[DAQ Error]")
        print(e)
        print("\nTroubleshooting: Double-check your CHANNEL_NAME in NI MAX.")
    except KeyboardInterrupt:
        print("\n\nData acquisition stopped by user.")


if __name__ == "__main__":
    read_current_sensor()