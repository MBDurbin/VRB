import serial
import time
import threading

# ================= CONFIGURATION =================
# REPLACE 'COM3' with your actual port (e.g., '/dev/ttyUSB0' on Linux/Mac)
SERIAL_PORT = 'COM3'
BAUD_RATE = 9600

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

        print("System Running. Press Ctrl+C to stop.")

        while True:
            # --- FLOWCHART STEP: "Current Voltage & Velocity Data" ---
            # In a real system, these would read from sensors.
            # Here, we ask for manual input for testing.
            try:
                v_in = float(input("\nEnter Velocity (m/s): "))
                volts_in = float(input("Enter Module Voltage (V): "))
            except ValueError:
                print("Invalid number.")
                continue

            # --- FLOWCHART STEP: Calculate Resistance ---
            req_r = calculate_required_resistance(v_in, volts_in)
            print(f"  -> Calc Power Load: {(volts_in ** 2) / req_r:.2f} W")
            print(f"  -> Target Resistance: {req_r:.4f} Ohms")

            # --- FLOWCHART STEP: Convert to Binary ---
            bin_str = resistance_to_binary(req_r)

            # --- SAFETY CHECK (Matches Arduino Logic) ---
            # If we calculated 0 resistance (Short Circuit), force it to safe limit
            if bin_str == "00000000":
                print("  -> WARNING: 0 Resistance Calc. Forcing 0.25 Safe Mode.")
                bin_str = "00000001"

            print(f"  -> Sending Binary: {bin_str}")

            # --- FLOWCHART STEP: Serial Signal to Arduino ---
            # We encode string to bytes and add newline '\n' because
            # Arduino uses readStringUntil('\n')
            with serial_lock:
                ser.write((bin_str + '\n').encode('utf-8'))

    except KeyboardInterrupt:
        print("\nStopping System...")
        stop_heartbeat.set()
        hb_thread.join()
        ser.close()
        print("Disconnected.")


if __name__ == "__main__":
    main()