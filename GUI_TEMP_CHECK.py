import serial
import time
import threading
import tkinter as tk

# ================= CONFIGURATION =================
TEMP_SERIAL_PORT = 'COM6'  # Ensure this matches your Temp Arduino
TEMP_BAUD_RATE = 115200
MAX_SAFE_TEMP = 55.0

# ================= SHARED DATA & LOCKS =================
temp_data_lock = threading.Lock()
stop_event = threading.Event()

# Global Store: 6 buses, 8 sensors each
battery_temps = [[0.0] * 8 for _ in range(6)]
max_temp_val = 0.0


# ================= BACKGROUND TEMPERATURE THREAD =================
def temp_worker():
    """Reads 48 Temperatures from COM6"""
    global max_temp_val
    try:
        temp_ser = serial.Serial(TEMP_SERIAL_PORT, TEMP_BAUD_RATE, timeout=1)
        print(f"Connected to {TEMP_SERIAL_PORT}")
        time.sleep(2)  # Wait for Arduino reset

        while not stop_event.is_set():
            if temp_ser.in_waiting > 0:
                line = temp_ser.readline().decode('utf-8', errors='ignore').strip()
                # Expected format: bus_idx, t1, t2, t3, t4, t5, t6, t7, t8
                data = line.split(',')

                if len(data) == 9:
                    try:
                        bus_idx = int(data[0]) - 1  # Convert 1-6 to 0-5
                        if 0 <= bus_idx < 6:
                            with temp_data_lock:
                                for i in range(8):
                                    val = data[i + 1]
                                    if val != "ERR":
                                        battery_temps[bus_idx][i] = float(val)
                    except ValueError:
                        pass
    except Exception as e:
        print(f"\n[SERIAL ERROR]: {e}")
    finally:
        if 'temp_ser' in locals() and temp_ser.is_open:
            temp_ser.close()
            print("Serial Port Closed.")


# ================= GUI LOGIC =================
def get_color(temp):
    """Blue (Cold) -> Red (Hot)"""
    min_t = 20.0
    ratio = max(0.0, min(1.0, (temp - min_t) / (MAX_SAFE_TEMP - min_t)))
    r = int(ratio * 255)
    b = int((1.0 - ratio) * 255)
    return f"#{r:02x}00{b:02x}"


def update_gui(root, canvas, cell_rects, cell_texts, status_var):
    if stop_event.is_set():
        root.destroy()
        return

    with temp_data_lock:
        temps_snapshot = [row[:] for row in battery_temps]

    current_max = 0.0
    for b in range(6):
        for i in range(8):
            temp = temps_snapshot[b][i]
            if temp > current_max: current_max = temp

            # Grid mapping: 4 rows, 12 columns
            r = i % 4
            c = (b * 2) + (i // 4)

            color = get_color(temp)
            text_color = "white" if (temp < 25 or temp > MAX_SAFE_TEMP - 10) else "black"

            canvas.itemconfig(cell_rects[r][c], fill=color)
            canvas.itemconfig(cell_texts[r][c], text=f"{temp:.1f}", fill=text_color)

    status_var.set(f"MAX TEMP: {current_max:.1f}°C")
    root.after(100, update_gui, root, canvas, cell_rects, cell_texts, status_var)


# ================= MAIN =================
def main():
    threading.Thread(target=temp_worker, daemon=True).start()

    root = tk.Tk()
    root.title("Thermal Interface Test")
    root.geometry("900x450")
    root.configure(bg="#1e1e1e")

    # Header
    status_var = tk.StringVar(value="Waiting for Data...")
    header = tk.Label(root, textvariable=status_var, font=("Consolas", 20, "bold"), fg="white", bg="#1e1e1e")
    header.pack(pady=20)

    # Canvas
    canvas_frame = tk.Frame(root, bg="#1e1e1e")
    canvas_frame.pack(expand=True)

    CELL_W, CELL_H = 70, 60
    canvas = tk.Canvas(canvas_frame, width=CELL_W * 12, height=CELL_H * 4, bg="#333333", highlightthickness=0)
    canvas.pack()

    cell_rects = [[None] * 12 for _ in range(4)]
    cell_texts = [[None] * 12 for _ in range(4)]

    for r in range(4):
        for c in range(12):
            x0, y0 = c * CELL_W, r * CELL_H
            x1, y1 = x0 + CELL_W, y0 + CELL_H
            cell_rects[r][c] = canvas.create_rectangle(x0, y0, x1, y1, fill="blue", outline="#1e1e1e")
            cell_texts[r][c] = canvas.create_text(x0 + CELL_W / 2, y0 + CELL_H / 2, text="0.0", font=("Arial", 10),
                                                  fill="white")

    root.protocol("WM_DELETE_WINDOW", lambda: [stop_event.set(), root.destroy()])
    root.after(100, update_gui, root, canvas, cell_rects, cell_texts, status_var)
    root.mainloop()


if __name__ == "__main__":
    main()