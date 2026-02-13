import serial
import time
import threading
import csv
import os

# ================= CONFIGURATION =================
# REPLACE 'COM3' with your actual port (e.g., '/dev/ttyUSB0' on Linux/Mac)
SERIAL_PORT = 'COM3'
BAUD_RATE = 9600
CSV_FILENAME = r"C:\Users\Durbi\PycharmProjects\Resistor Bank Master\FSAE - ETS - Speed and Time 1 Lap.csv"

# SYSTEM CONSTANTS (Based on your "1st bit = 1/4, 8th bit = 32" rule)
# This assumes an 8-bit resolution where the LSB (Last bit) is 0.25 Ohms
RESISTOR_RESOLUTION = 0.25  # The value of the smallest bit
MAX_RESISTANCE = 32 + 16 + 8 + 4 + 2 + 1 + 0.5 + 0.25  # ~63.75 Ohms


# ================= SAFETY HEARTBEAT =================

# Create a "token". Only the thread holding this token can write to Serial.
serial_lock = threading.Lock()

def heartbeat_worker(ser, stop_event):
    while not stop_event.is_set():
        # "acquire()" waits until the lock is free, then takes it.
        # "with" automatically releases the lock when the block finishes.
        with serial_lock:
            try:
                ser.write(b"alive\n")
            except:
                pass
        time.sleep(1.0)


# ================= PHYSICS ENGINE =================
def calculate_required_resistance(velocity, voltage):
    """
    FLOWCHART STEP: "Calculate Required Resistance... based off velocity"

    NOTE: You need to insert your specific vehicle loads here.
    Current Logic: P = Force * Velocity
    """

    # 1. Calculate Required Power (Simulated Physics)
    # P_load = (Rolling Resistance + Aerodynamic Drag) * Velocity
    # Example constants (You should calibrate these for your project)
    mass = 300  # kg
    g = 9.81
    crr = 0.015  # Rolling resistance coeff
    rho = 1.225
    cd = 0.7
    area = 1.0

    # Force Calculation
    f_roll = crr * mass * g
    f_drag = 0.5 * rho * cd * area * (velocity ** 2)
    total_force = f_roll + f_drag

    required_power = total_force * velocity

    # Avoid division by zero if car is stopped
    if required_power <= 0:
        return MAX_RESISTANCE  # Max resistance = Min Power/Current

    # 2. Calculate Resistance needed to dissipate that power
    # P = V^2 / R  --->  R = V^2 / P
    resistance = ((voltage * 9) ** 2) / required_power

    return resistance


# ================= BINARY CONVERTER =================
def resistance_to_binary(target_ohms):
    """
    FLOWCHART STEP: "Convert to nearest binary"
    Logic:
    1. Divide resistance by resolution (0.25) to get 'steps'.
    2. Convert 'steps' to an 8-bit binary string.
    """

    # Clamp the resistance to what our bank can physically handle
    if target_ohms > MAX_RESISTANCE:
        target_ohms = MAX_RESISTANCE
    if target_ohms < RESISTOR_RESOLUTION:
        target_ohms = RESISTOR_RESOLUTION  # Prevent 0 (Short Circuit)

    # Calculate number of 0.25 ohm "steps" needed
    # Example: 5.75 ohms / 0.25 = 23 steps
    steps = int(round(target_ohms / RESISTOR_RESOLUTION))

    # Convert to 8-bit binary string (e.g., 23 -> "00010111")
    # '08b' means: Format as Binary, pad with Zeros, 8 digits long
    binary_string = format(steps, '08b')

    return binary_string[::-1]


# ================= MAIN APP =================
def main():
    print("--- Master Controller Starting ---")

    # Check if CSV exists
    if not os.path.exists(CSV_FILENAME):
        print(f"ERROR: Could not find '{CSV_FILENAME}'")
        return

    try:
        # Open Serial Port
        ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
        time.sleep(2)  # Wait for Arduino to reset
        print(f"Connected to {SERIAL_PORT}")

        # Start Heartbeat in background thread
        stop_heartbeat = threading.Event()
        hb_thread = threading.Thread(target=heartbeat_worker, args=(ser, stop_heartbeat))
        hb_thread.daemon = True
        hb_thread.start()

        # 3. Get Constant Battery Voltage (since CSV doesn't have it)
        try:
            battery_voltage = float(input("\nEnter Battery Voltage (V) for this run: "))
        except ValueError:
            battery_voltage = 43.5  # Default fallback
            print("Invalid voltage. Defaulting to 400V.")

        print(f"\nStarting Simulation from: {CSV_FILENAME}")
        print("Press Ctrl+C to abort.")
        time.sleep(1)
        i=0
        while i<11:
            i += 1
            print(f"--------LAP {i}/11--------")
            # 4. Read and Replay CSV
            with open(CSV_FILENAME, 'r') as csvfile:
                reader = csv.DictReader(csvfile)

                start_time = time.time()

                for row in reader:
                    # --- A. Parse CSV Data ---
                    try:
                        # CSV Headers: 'YouTube Time', 'Time (s)', 'Speed (mph)'
                        sim_time = float(row['Time (s)'])
                        speed_mph = float(row['Speed (mph)'])
                    except (ValueError, KeyError):
                        continue  # Skip bad rows

                    # --- B. Conversions ---
                    # Convert mph to m/s
                    velocity_ms = speed_mph * 0.44704

                    # --- C. Physics & Logic ---
                    req_r = calculate_required_resistance(velocity_ms, battery_voltage)
                    bin_str = resistance_to_binary(req_r)

                    # Safety Fix: Prevent pure 0
                    if bin_str == "00000000":
                        bin_str = "00000001"

                    # --- D. Send to Arduino ---
                    # (Removed the duplicate write command from previous version)
                    with serial_lock:
                        ser.write((bin_str + '\n').encode('utf-8'))

                    # --- E. Console Feedback ---
                    print(
                        f"T={sim_time:>3}s | Spd={speed_mph:>3.0f}mph | Pwr={(battery_voltage ** 2) / req_r:>5.0f}W | R={req_r:>5.2f}Ω | Bits={bin_str}")

                    # --- F. Real-time Sync ---
                    # This logic keeps the script synced with Sim Time, regardless of calculation lag
                    # 'Time (s)' in your CSV increments by 1.

                    # If you want it to run exactly at the speed of the CSV:
                    time_to_wait = 1.0  # Your CSV seems to be 1Hz
                    time.sleep(time_to_wait)

    except KeyboardInterrupt:
        print("\nStopping System...")
    finally:
        if 'stop_heartbeat' in locals():
            stop_heartbeat.set()
            hb_thread.join()
        if 'ser' in locals() and ser.is_open:
            ser.close()
        print("Disconnected.")


if __name__ == "__main__":
    main()