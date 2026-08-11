import serial
import nidaqmx
import time
import threading
import csv
import os
import tkinter as tk
from tkinter import messagebox

# ================= CONFIGURATION =================
RESISTOR_SERIAL_PORT = 'COM5'
RESISTOR_BAUD_RATE = 9600

TEMP_SERIAL_PORT = 'COM6'
TEMP_BAUD_RATE = 115200

CSV_FILENAME = r"C:\Users\Durbi\PycharmProjects\Resistor Bank Master\FSAE - ETS - Speed and Time 1 Lap.csv"

CURRENT_CHANNEL = "cDAQ1Mod8/ai0"

VOLTAGE_CHANNELS = [
    "cDAQ1Mod8/ai1", "cDAQ1Mod8/ai2", "cDAQ1Mod8/ai3", # 3 channels
    "cDAQ1Mod7/ai0", "cDAQ1Mod7/ai1", "cDAQ1Mod7/ai2", "cDAQ1Mod7/ai3", # 4 channels
    "cDAQ1Mod6/ai0", "cDAQ1Mod6/ai1", "cDAQ1Mod6/ai2", "cDAQ1Mod6/ai3", # 4 channels
    "cDAQ1Mod5/ai0"  # 1 channel (12th and highest voltage)
]

# Voltage divider: (100k + 10k) / 10k = 11.0 multiplier
VOLTAGE_MULTIPLIER = 11.0

RESISTOR_RESOLUTION = 0.25
MAX_RESISTANCE = 63.75
MAX_SAFE_TEMP = 55.0

# ================= SHARED DATA & LOCKS =================
serial_lock = threading.Lock()
data_lock = threading.Lock()
temp_data_lock = threading.Lock()
stop_event = threading.Event()

# Global Data Stores
battery_temps = [[0.0] * 8 for _ in range(6)]

# System state dictionary for the UI to read safely
system_state = {
    'lap': 1,
    'time': 0.0,
    'velocity_mph': 0.0,
    'resistance': MAX_RESISTANCE,
    'amps': 0.0,
    'power_kw': 0.0,
    'max_temp': 0.0,
    'status': "STANDBY",
    'voltage': 43.5,  # Pack voltage (Updates automatically from DAQ)
    'cell_voltages': [0.0] * 12  # Individual 12s voltages
}


# ================= BACKGROUND THREADS =================

def heartbeat_worker(ser):
    """COM5: Keeps the Resistor Arduino alive"""
    while not stop_event.is_set():
        with serial_lock:
            try:
                ser.write(b"alive\n")
            except:
                pass
        time.sleep(1.0)


def daq_worker():
    """NI-DAQ: Reads Current and 12 Voltages"""
    try:
        with nidaqmx.Task() as task:
            # 1. Add Current Channel First (Index 0 in read array)
            task.ai_channels.add_ai_voltage_chan(CURRENT_CHANNEL, min_val=-10.0, max_val=10.0)

            # 2. Add the 12 Voltage Channels (Indices 1 through 12)
            for ch in VOLTAGE_CHANNELS:
                task.ai_channels.add_ai_voltage_chan(ch, min_val=-10.0, max_val=10.0)

            while not stop_event.is_set():
                # task.read() returns a list when multiple channels are configured
                data = task.read()

                # Parse current
                current = data[0] * 100.0  # 1V = 100A

                # Parse and Calculate Voltages
                raw_daq_voltages = data[1:]
                actual_cumulative_voltages = [v * VOLTAGE_MULTIPLIER for v in raw_daq_voltages]

                cell_voltages = [0.0] * 12
                # First cell is just the first cumulative reading
                cell_voltages[0] = actual_cumulative_voltages[0]

                # Subsequent cells are the difference between current and previous tap
                for i in range(1, 12):
                    cell_voltages[i] = actual_cumulative_voltages[i] - actual_cumulative_voltages[i - 1]

                # Pack voltage is the highest tap
                total_pack_voltage = actual_cumulative_voltages[-1]

                with data_lock:
                    system_state['amps'] = current
                    system_state['voltage'] = total_pack_voltage
                    system_state['cell_voltages'] = cell_voltages
                    system_state['power_kw'] = (current * total_pack_voltage) / 1000.0
                time.sleep(0.1)
    except nidaqmx.DaqError as e:
        print(f"\n[DAQ ERROR]: {e}")
        stop_event.set()


def temp_worker():
    """COM6: Reads 48 Temperatures"""
    try:
        temp_ser = serial.Serial(TEMP_SERIAL_PORT, TEMP_BAUD_RATE, timeout=1)
        time.sleep(2)
        while not stop_event.is_set():
            if temp_ser.in_waiting > 0:
                line = temp_ser.readline().decode('utf-8', errors='ignore').strip()
                data = line.split(',')
                if len(data) == 9:
                    try:
                        bus_idx = int(data[0]) - 1
                        with temp_data_lock:
                            for i in range(8):
                                if data[i + 1] != "ERR":
                                    battery_temps[bus_idx][i] = float(data[i + 1])
                    except ValueError:
                        pass
    except Exception as e:
        print(f"\n[TEMP ERROR]: {e}")
    finally:
        if 'temp_ser' in locals() and temp_ser.is_open: temp_ser.close()


def physics_worker(ser):
    """THE NEW THREAD: Reads CSV and calculates physics exactly at 1Hz"""
    global system_state

    with data_lock:
        system_state['status'] = "RUNNING"

    for lap in range(1, 12):
        if stop_event.is_set(): break

        with data_lock:
            system_state['lap'] = lap

        with open(CSV_FILENAME, 'r') as csvfile:
            reader = csv.DictReader(csvfile)
            for row in reader:
                if stop_event.is_set(): return

                # 1. Verify Safety Limit First
                with data_lock:
                    current_max_temp = system_state['max_temp']
                    voltage = system_state['voltage']  # Read live pack voltage

                if current_max_temp >= MAX_SAFE_TEMP:
                    with serial_lock:
                        ser.write(b"KILL\n")
                        time.sleep(0.05)
                        ser.write(b"00000000\n")
                    with data_lock:
                        system_state['status'] = "KILLED - OVERTEMP"
                    stop_event.set()
                    return

                # 2. Physics Math
                sim_time = float(row.get('Time (s)', 0))
                speed_mph = float(row.get('Speed (mph)', 0))
                velocity_ms = speed_mph * 0.44704

                f_downforce = 0.5 * 1.2255 * abs(-1.0) * 2.224 * (velocity_ms ** 2)
                f_drag = 0.5 * 1.2255 * 0.6 * 2.224 * (velocity_ms ** 2)
                normal_force = (260.0 * 9.81) + f_downforce
                req_power = (0.015 * normal_force + f_drag) * velocity_ms

                req_r = MAX_RESISTANCE if req_power <= 0 else min(MAX_RESISTANCE, ((voltage * 9) ** 2) / req_power)

                steps = int(round(max(RESISTOR_RESOLUTION, req_r) / RESISTOR_RESOLUTION))
                bin_str = format(steps, '08b')[::-1]
                if bin_str == "00000000": bin_str = "00000001"

                # 3. Command Hardware
                with serial_lock:
                    ser.write((bin_str + '\n').encode('utf-8'))

                # 4. Update UI State variables
                with data_lock:
                    system_state['time'] = sim_time
                    system_state['velocity_mph'] = speed_mph
                    system_state['resistance'] = req_r

                # 5. Perfect 1Hz loop timing
                time.sleep(1.0)


# ================= FAST TKINTER GUI =================

def get_color(temp):
    """Converts a temperature to a Blue->Purple->Red hex color code"""
    min_t = 20.0
    ratio = max(0.0, min(1.0, (temp - min_t) / (MAX_SAFE_TEMP - min_t)))
    r = int(ratio * 255)
    b = int((1.0 - ratio) * 255)
    return f"#{r:02x}00{b:02x}"


def update_gui(root, metrics_vars, cell_volt_vars, canvas, cell_rects, cell_texts):
    """Runs at 10Hz on the main thread, purely reading data and painting pixels"""
    if stop_event.is_set() and system_state['status'] != "KILLED - OVERTEMP":
        root.destroy()
        return

    # 1. Get snapshot of data
    with temp_data_lock:
        temps_snapshot = [row[:] for row in battery_temps]

    with data_lock:
        state_snapshot = system_state.copy()

    # 2. Update Top Metrics Bar
    metrics_vars['lap'].set(f"Lap: {state_snapshot['lap']}/11")
    metrics_vars['time'].set(f"Time: {state_snapshot['time']:>5.1f}s")
    metrics_vars['spd'].set(f"Speed: {state_snapshot['velocity_mph']:>4.0f} mph")
    metrics_vars['res'].set(f"Res: {state_snapshot['resistance']:>5.2f} Ω")
    metrics_vars['amp'].set(f"Current: {state_snapshot['amps']:>6.1f} A")
    metrics_vars['pwr'].set(f"Power: {state_snapshot['power_kw']:>5.2f} kW")
    metrics_vars['status'].set(f"Status: {state_snapshot['status']}")
    metrics_vars['max_t'].set(f"Max Temp: {state_snapshot['max_temp']:>4.1f} °C")
    metrics_vars['pack_v'].set(f"Pack V: {state_snapshot['voltage']:>5.1f} V")

    # 3. Update Individual Cell Voltages
    for i in range(12):
        cell_volt_vars[i].set(f"S{i + 1}: {state_snapshot['cell_voltages'][i]:.2f}V")

    # 4. Update 4x12 Heatmap Grid
    max_t = 0.0
    for b in range(6):
        for i in range(8):
            temp = temps_snapshot[b][i]
            if temp > max_t: max_t = temp

            r = i % 4
            c = (b * 2) + (i // 4)

            color = get_color(temp)
            text_color = "white" if (temp < 25 or temp > MAX_SAFE_TEMP - 10) else "black"

            canvas.itemconfig(cell_rects[r][c], fill=color)
            canvas.itemconfig(cell_texts[r][c], text=f"{temp:.1f}", fill=text_color)

    with data_lock:
        system_state['max_temp'] = max_t

    root.after(100, update_gui, root, metrics_vars, cell_volt_vars, canvas, cell_rects, cell_texts)


def on_close():
    stop_event.set()


def main():
    # --- Initialize Hardware ---
    try:
        ser = serial.Serial(RESISTOR_SERIAL_PORT, RESISTOR_BAUD_RATE, timeout=1)
        time.sleep(2)
    except Exception as e:
        print(f"FAILED TO OPEN RESISTOR BANK: {e}")
        return

    # --- Start All Background Threads ---
    threading.Thread(target=heartbeat_worker, args=(ser,), daemon=True).start()
    threading.Thread(target=daq_worker, daemon=True).start()
    threading.Thread(target=temp_worker, daemon=True).start()
    physics_th = threading.Thread(target=physics_worker, args=(ser,), daemon=True)
    physics_th.start()

    # --- Build the Tkinter GUI ---
    root = tk.Tk()
    root.title("FSAE Telemetry & Resistor Bank Master")
    root.geometry("1000x550")
    root.configure(bg="#1e1e1e")
    root.protocol("WM_DELETE_WINDOW", on_close)

    # 1. Top Metrics Bar
    top_frame = tk.Frame(root, bg="#1e1e1e")
    top_frame.pack(fill="x", pady=10, padx=20)

    m_vars = {k: tk.StringVar() for k in ['lap', 'time', 'spd', 'res', 'amp', 'pwr', 'max_t', 'status', 'pack_v']}

    font_large = ("Consolas", 16, "bold")
    font_med = ("Consolas", 14)

    # Row 1
    tk.Label(top_frame, textvariable=m_vars['status'], font=font_large, fg="#00ff00", bg="#1e1e1e", width=20,
             anchor="w").grid(row=0, column=0)
    tk.Label(top_frame, textvariable=m_vars['lap'], font=font_med, fg="white", bg="#1e1e1e", width=15).grid(row=0,
                                                                                                            column=1)
    tk.Label(top_frame, textvariable=m_vars['time'], font=font_med, fg="white", bg="#1e1e1e", width=15).grid(row=0,
                                                                                                             column=2)
    tk.Label(top_frame, textvariable=m_vars['spd'], font=font_med, fg="cyan", bg="#1e1e1e", width=15).grid(row=0,
                                                                                                           column=3)

    # Row 2
    tk.Label(top_frame, textvariable=m_vars['max_t'], font=font_large, fg="#ff4444", bg="#1e1e1e", width=20,
             anchor="w").grid(row=1, column=0)
    tk.Label(top_frame, textvariable=m_vars['pack_v'], font=font_med, fg="yellow", bg="#1e1e1e", width=15).grid(row=1,
                                                                                                                column=1)
    tk.Label(top_frame, textvariable=m_vars['amp'], font=font_med, fg="#ffaa00", bg="#1e1e1e", width=15).grid(row=1,
                                                                                                              column=2)
    tk.Label(top_frame, textvariable=m_vars['pwr'], font=font_med, fg="#ffaa00", bg="#1e1e1e", width=15).grid(row=1,
                                                                                                              column=3)

    # 2. Voltage Cell Bar (New Frame)
    volt_frame = tk.Frame(root, bg="#2d2d2d", bd=2, relief="sunken")
    volt_frame.pack(fill="x", pady=5, padx=20)

    cell_volt_vars = [tk.StringVar() for _ in range(12)]
    for i in range(12):
        tk.Label(volt_frame, textvariable=cell_volt_vars[i], font=("Consolas", 11), fg="white", bg="#2d2d2d",
                 width=9).grid(row=0, column=i, padx=1, pady=2)

    # 3. Heatmap Canvas
    canvas_frame = tk.Frame(root, bg="#1e1e1e")
    canvas_frame.pack(expand=True)

    CELL_W, CELL_H = 70, 60
    canvas = tk.Canvas(canvas_frame, width=CELL_W * 12, height=CELL_H * 4, bg="#333333", highlightthickness=0)
    canvas.pack(pady=10)

    cell_rects = [[None] * 12 for _ in range(4)]
    cell_texts = [[None] * 12 for _ in range(4)]

    for r in range(4):
        for c in range(12):
            x0, y0 = c * CELL_W, r * CELL_H
            x1, y1 = x0 + CELL_W, y0 + CELL_H
            cell_rects[r][c] = canvas.create_rectangle(x0, y0, x1, y1, fill="blue", outline="#1e1e1e", width=2)
            cell_texts[r][c] = canvas.create_text(x0 + CELL_W / 2, y0 + CELL_H / 2, text="0.0",
                                                  font=("Arial", 12, "bold"), fill="white")

    # Start the UI loop
    root.after(100, update_gui, root, m_vars, cell_volt_vars, canvas, cell_rects, cell_texts)
    root.mainloop()

    # Cleanup
    stop_event.set()
    if 'ser' in locals() and ser.is_open:
        try:
            ser.write(b"KILL\n")
            ser.write(b"00000000\n")
            ser.close()
        except:
            pass
    print("\nSystem Shutdown Complete.")


if __name__ == "__main__":
    main()