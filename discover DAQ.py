import nidaqmx
import nidaqmx.system


def find_channels():
    system = nidaqmx.system.System.local()

    if not system.devices:
        print("No NI-DAQ devices found! Check your USB/Ethernet connection.")
        return

    print("=== Available DAQ Channels ===")
    for device in system.devices:
        print(f"\nDevice/Module Name: {device.name}")
        print("Analog Input Channels:")

        # This will list every AI channel this specific device has
        for ai_chan in device.ai_physical_chans:
            print(f"  '{ai_chan.name}'")


if __name__ == "__main__":
    find_channels()