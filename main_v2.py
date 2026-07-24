import sys
import serial
import serial.tools.list_ports
import time
from multiprocessing import Queue, Event, Process

# Import modules
from gui_layout import run_gui_process
from control_logic import run_logic_process
from hardware_manager import run_daq_process
from sil_simulator import SILSimulatorWindow
from PyQt6 import QtWidgets


def detect_sil_key():
    """Sweeps COM ports looking for the Arduino Dongle."""
    print("Scanning for Hardware Keys...")
    ports = serial.tools.list_ports.comports()
    for port in ports:
        try:
            ser = serial.Serial(port.device, 115200, timeout=2)
            time.sleep(2)  # Wait for Arduino reset
            ser.reset_input_buffer() #Clear the data we got when the arduino reset
            ser.write(b"?WHOAMI\n") #ping the device to see if it is the SIL key
            response = ser.readline().decode('utf-8').strip()
            ser.close()

            if response == "SIL_KEY":
                print(f"--> SIL KEY UNLOCKED on {port.device}")
                return True
        except Exception:
            pass
    return False


if __name__ == '__main__':
    telemetry_queue = Queue(maxsize=10) # FSM/Controller to GUI
    gui_cmd_queue = Queue(maxsize=10) #GUI to Logic
    daq_queue = Queue(maxsize=10) #houses voltages, temps, and current
    stop_event = Event() #Kill Switch

    # 1. Start the FSM Control Logic Process
    logic_process = Process(target=run_logic_process, args=(daq_queue, telemetry_queue, gui_cmd_queue, stop_event)) #pakage queues and send to logic in seperate CPU
    logic_process.start() #initialize ^

    # 2. Check for the Developer Dongle
    is_sil_mode = detect_sil_key() # are we in SIL mode?

    if is_sil_mode:
        # --- DESKTOP SIL MODE ---
        # Note: We run the SIL window in the same process as the Main GUI to keep PyQt6 happy
        app = QtWidgets.QApplication(sys.argv)
        app.setStyle("Fusion")

        # To link the SIL to the logic, we route the SIL telemetry to both queues
        # (Since SIL replaces the DAQ entirely)
        sil_window = SILSimulatorWindow(daq_queue)
        sil_window.show()

        from gui_layout import TelemetryGUI

        main_gui = TelemetryGUI(telemetry_queue, gui_cmd_queue, stop_event)
        main_gui.show() #Pull up the telemetry view on the GUI

        sys.exit(app.exec()) #wait for input from the GUI

    else:
        # --- PHYSICAL HARDWARE MODE ---
        print("Starting physical DAQ hardware...")
        daq_process = Process(target=run_daq_process, args=(daq_queue, stop_event))
        daq_process.start()

        # Start the Main GUI in its own process
        gui_process = Process(target=run_gui_process, args=(telemetry_queue, gui_cmd_queue, stop_event)) #run on its own CPU core
        gui_process.start()

        gui_process.join()
        stop_event.set()
        logic_process.join()
        daq_process.join()