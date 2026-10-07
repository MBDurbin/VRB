import serial
import time
import threading

# ================= CONFIGURATION =================
RESISTOR_SERIAL_PORT = 'COM5'
RESISTOR_BAUD_RATE = 9600

RESISTOR_RESOLUTION = 0.25
MAX_RESISTANCE = 63.75

# Global flags
stop_event = threading.Event()
serial_lock = threading.Lock()


# ================= BACKGROUND THREAD =================

def heartbeat_worker(ser):
    """COM5: Keeps the Resistor Arduino alive in the background"""
    while not stop_event.is_set():
        with serial_lock:
            try:
                ser.write(b"alive\n")
            except:
                pass
        time.sleep(1.0)


# ================= MAIN LOGIC =================

def main():
    # --- Initialize Hardware ---
    try:
        ser = serial.Serial(RESISTOR_SERIAL_PORT, RESISTOR_BAUD_RATE, timeout=1)
        time.sleep(2)  # Give the Arduino time to reset on connection
        print("Connected to Resistor Bank Arduino on COM5.")
    except Exception as e:
        print(f"FAILED TO OPEN RESISTOR BANK: {e}")
        return

    # --- Start Heartbeat Thread ---
    threading.Thread(target=heartbeat_worker, args=(ser,), daemon=True).start()

    print("\n--- Manual Resistor Control ---")
    print(f"Valid range: {RESISTOR_RESOLUTION} to {MAX_RESISTANCE} Ohms")
    print("Type 'q' to safely shut down and quit.\n")

    try:
        while True:
            # 1. Wait for user input
            user_input = input("Enter desired resistance (Ohms): ").strip().lower()


            if user_input == 'q':
                break

            try:
                target_r = float(user_input)
            except ValueError:
                print("Invalid input. Please enter a number.")
                continue

            # 2. Clamp input to hardware limits
            target_r = max(RESISTOR_RESOLUTION, min(MAX_RESISTANCE, target_r))

            # 3. Math for the closest physical step
            steps = int(round(target_r / RESISTOR_RESOLUTION))
            bin_str = format(steps, '08b')[::-1]

            actual_r = steps * RESISTOR_RESOLUTION

            # 4. Command Hardware
            with serial_lock:
                ser.write((bin_str + '\n').encode('utf-8'))

            print(f" >> Hardware Set to: {actual_r} Ω (Command Sent: {bin_str})\n")

    except KeyboardInterrupt:
        print("\nManual interrupt received (Ctrl+C).")
    finally:
        # Cleanup on shutdown
        stop_event.set()
        if 'ser' in locals() and ser.is_open:
            try:
                print("Sending KILL command to Arduino...")
                with serial_lock:
                    ser.write(b"KILL\n")
                    time.sleep(0.05)
                    ser.write(b"00000000\n")
                ser.close()
            except Exception as e:
                print(f"Error closing port: {e}")
        print("System Shutdown Complete.")


if __name__ == "__main__":
    main()