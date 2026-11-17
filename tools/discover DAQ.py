import nidaqmx
import nidaqmx.system


def find_channels():
    system = nidaqmx.system.System.local()

    if not system.devices:
        print("No NI-DAQ devices found! Check your USB/Ethernet connection.")
        return

    print("=== Available DAQ Channels ===")
    for device in system.devices:
        # The model and slot say what each channel name means: an NI 9215 has
        # four differential inputs (ai0-ai3, each an AI+ and AI- pair) on a
        # 10-terminal block, so a terminal number is not a channel number.
        try:
            model = device.product_type
        except nidaqmx.DaqError:
            model = "unknown model"
        try:
            slot = f"slot {device.compact_daq_slot_num}"
        except nidaqmx.DaqError:
            slot = "not in a cDAQ slot"
        print(f"\nDevice/Module Name: {device.name}  ({model}, {slot})")
        print("Analog Input Channels:")

        # This will list every AI channel this specific device has
        for ai_chan in device.ai_physical_chans:
            try:
                configs = ", ".join(c.name for c in ai_chan.ai_term_cfgs)
            except nidaqmx.DaqError:
                configs = "?"
            print(f"  '{ai_chan.name}'  terminal configs: {configs}")


if __name__ == "__main__":
    find_channels()
