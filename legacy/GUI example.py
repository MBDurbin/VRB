import time
import threading
import math
import random
import tkinter as tk

# ================= CONFIGURATION =================
RESISTOR_RESOLUTION = 0.25
MAX_RESISTANCE = 63.75
MAX_SAFE_TEMP = 55.0

# ================= SHARED DATA & LOCKS =================
data_lock = threading.Lock()
temp_data_lock = threading.Lock()
stop_event = threading.Event()

# Initialize temperatures to a realistic starting ambient of 25.0 C
battery_temps = [[25.0] * 8 for _ in range(6)]

system_state = {
    'lap': 1,
    'time': 0.0,
    'velocity_mph': 0.0,
    'resistance': MAX_RESISTANCE,
    'amps': 0.0,
    'power_kw': 0.0,
    'max_temp': 25.0,
    'status': "STANDBY",
    'voltage': 43.5
}


# ================= SIMULATION THREADS =================

def demo_daq_worker():
    """Simulates the NI-DAQ reading current spikes based on speed"""
    while not stop_event.is_set():
        with data_lock:
            # Fake current that loosely follows the speed, plus some noise
            base_amps = system_state['velocity_mph'] * 1.5
            noise = random.uniform(-2.0, 2.0)
            current = max(0.0, base_amps + noise)

            system_state['amps'] = current
            system_state['power_kw'] = (current * system_state['voltage']) / 1000.0
        time.sleep(0.1)


def demo_temp_worker():
    """Simulates 48 sensors heating up, with the center cells getting hotter faster"""
    while not stop_event.is_set():
        with temp_data_lock:
            for b in range(6):
                for i in range(8):
                    # Base heating rate
                    heat_rate = random.uniform(0.01, 0.05)

                    # Make buses 2, 3, 4 (center of pack) heat up faster to show a hotspot
                    if 2 <= b <= 4:
                        heat_rate += 0.03

                    battery_temps[b][i] += heat_rate
        time.sleep(0.1)  # 10Hz update


def demo_physics_worker():
    """Simulates the 1Hz CSV reading loop and physics math"""
    global system_state

    with data_lock:
        system_state['status'] = "DEMO RUNNING"
        voltage = system_state['voltage']

    sim_time = 0.0

    while not stop_event.is_set():
        # 1. Verify Safety Limit First
        with data_lock:
            current_max_temp = system_state['max_temp']

        if current_max_temp >= MAX_SAFE_TEMP:
            print("!!! SIMULATED HARD KILL SENT !!!")
            with data_lock:
                system_state['status'] = "KILLED - OVERTEMP"
            stop_event.set()  # Stop the simulation
            return

        # 2. Simulate Speed (Sine wave fluctuating between 10mph and 60mph)
        sim_time += 1.0
        speed_mph = 35.0 + 25.0 * math.sin(sim_time / 10.0)
        velocity_ms = speed_mph * 0.44704

        # 3. Physics Math
        f_downforce = 0.5 * 1.2255 * abs(-1.0) * 2.224 * (velocity_ms ** 2)
        f_drag = 0.5 * 1.2255 * 0.6 * 2.224 * (velocity_ms ** 2)
        normal_force = (260.0 * 9.81) + f_downforce
        req_power = (0.015 * normal_force + f_drag) * velocity_ms

        req_r = MAX_RESISTANCE if req_power <= 0 else min(MAX_RESISTANCE, ((voltage * 9) ** 2) / req_power)

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


def update_gui(root, metrics_vars, canvas, cell_rects, cell_texts):
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

    # Check if killed for flashing red text
    if state_snapshot['status'] == "KILLED - OVERTEMP":
        metrics_vars['status'].set("!!! SYSTEM HALTED !!!")
    else:
        metrics_vars['status'].set(f"Status: {state_snapshot['status']}")

    metrics_vars['max_t'].set(f"Max Temp: {state_snapshot['max_temp']:>4.1f} °C")

    # 3. Update 4x12 Heatmap Grid
    max_t = 0.0
    for b in range(6):
        for i in range(8):
            temp = temps_snapshot[b][i]
            if temp > max_t: max_t = temp

            # Map index to GUI row/col
            r = i % 4
            c = (b * 2) + (i // 4)

            color = get_color(temp)
            text_color = "white" if (temp < 28 or temp > MAX_SAFE_TEMP - 10) else "black"

            canvas.itemconfig(cell_rects[r][c], fill=color)
            canvas.itemconfig(cell_texts[r][c], text=f"{temp:.1f}", fill=text_color)

    # Update max temp in state for the physics thread to check
    with data_lock:
        system_state['max_temp'] = max_t

    # Trigger next UI refresh in 100ms (10 FPS)
    root.after(100, update_gui, root, metrics_vars, canvas, cell_rects, cell_texts)


def on_close():
    stop_event.set()


def main():
    battery_v = input("Enter Battery Voltage (V) for this demo [43.5]: ")
    system_state['voltage'] = float(battery_v) if battery_v.strip() else 43.5

    # --- Start Simulation Threads ---
    threading.Thread(target=demo_daq_worker, daemon=True).start()
    threading.Thread(target=demo_temp_worker, daemon=True).start()
    physics_th = threading.Thread(target=demo_physics_worker, daemon=True)
    physics_th.start()

    # --- Build the Tkinter GUI ---
    root = tk.Tk()
    root.title("FSAE Telemetry Demo (No Hardware Required)")
    root.geometry("1000x500")
    root.configure(bg="#1e1e1e")
    root.protocol("WM_DELETE_WINDOW", on_close)

    # 1. Top Metrics Bar
    top_frame = tk.Frame(root, bg="#1e1e1e")
    top_frame.pack(fill="x", pady=15, padx=20)

    m_vars = {k: tk.StringVar() for k in ['lap', 'time', 'spd', 'res', 'amp', 'pwr', 'max_t', 'status']}

    font_large = ("Consolas", 16, "bold")
    font_med = ("Consolas", 14)

    tk.Label(top_frame, textvariable=m_vars['status'], font=font_large, fg="#00ff00", bg="#1e1e1e", width=20,
             anchor="w").grid(row=0, column=0)
    tk.Label(top_frame, textvariable=m_vars['lap'], font=font_med, fg="white", bg="#1e1e1e", width=15).grid(row=0,
                                                                                                            column=1)
    tk.Label(top_frame, textvariable=m_vars['time'], font=font_med, fg="white", bg="#1e1e1e", width=15).grid(row=0,
                                                                                                             column=2)
    tk.Label(top_frame, textvariable=m_vars['spd'], font=font_med, fg="cyan", bg="#1e1e1e", width=15).grid(row=0,
                                                                                                           column=3)

    tk.Label(top_frame, textvariable=m_vars['max_t'], font=font_large, fg="#ff4444", bg="#1e1e1e", width=20,
             anchor="w").grid(row=1, column=0)
    tk.Label(top_frame, textvariable=m_vars['res'], font=font_med, fg="yellow", bg="#1e1e1e", width=15).grid(row=1,
                                                                                                             column=1)
    tk.Label(top_frame, textvariable=m_vars['amp'], font=font_med, fg="#ffaa00", bg="#1e1e1e", width=15).grid(row=1,
                                                                                                              column=2)
    tk.Label(top_frame, textvariable=m_vars['pwr'], font=font_med, fg="#ffaa00", bg="#1e1e1e", width=15).grid(row=1,
                                                                                                              column=3)

    # 2. Heatmap Canvas
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
    root.after(100, update_gui, root, m_vars, canvas, cell_rects, cell_texts)
    root.mainloop()

    stop_event.set()
    print("\nDemo Shutdown Complete.")


if __name__ == "__main__":
    main()"