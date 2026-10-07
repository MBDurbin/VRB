# import serial
# import time
#
# # ================= CONFIGURATION =================
# # REPLACE 'COM3' WITH YOUR ACTUAL PORT (e.g., '/dev/ttyUSB0' on Linux/Mac)
# SERIAL_PORT = 'COM3'
# BAUD_RATE = 9600 #must match baud rate of the arduino
#
#
# def read_arduino_response(ser):
#     """Reads all lines waiting in the buffer from Arduino."""
#     time.sleep(0.1)  # Give Arduino a moment to print
#     while ser.in_waiting > 0:
#         try:
#             line = ser.readline().decode('utf-8').strip()
#             if line:
#                 print(f"[Arduino]: {line}")
#         except:
#             pass
#
#
# def run_test():
#     try:
#         print(f"Connecting to {SERIAL_PORT}...")
#         arduino = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)
#         time.sleep(2)  # Wait for Arduino to reset
#         print("Connection established.\n")
#
#         # Clear initial "Ready" message
#         read_arduino_response(arduino)
#
#         # --- TEST 1: Standard Valid Signal ---
#         print("\n--- TEST 1: Sending Valid Binary '10101010' ---")
#         print("(Expect: New State Received -> Relays Changed -> 0.25 logic applied)")
#         arduino.write(b"10101010\n")
#         time.sleep(1)
#         read_arduino_response(arduino)
#
#         # --- TEST 2: Safety Interlock (All Zeros) ---
#         print("\n--- TEST 2: Sending All Zeros '00000000' ---")
#         print("(Expect: SAFETY ACTION trigger -> Signal modified to end in '1')")
#         arduino.write(b"00000000\n")
#         time.sleep(1)
#         read_arduino_response(arduino)
#
#         # --- TEST 3: Heartbeat ---
#         print("\n--- TEST 3: Sending Heartbeat 'alive' ---")
#         print("(Expect: NO state change messages. Silent update.)")
#         arduino.write(b"alive\n")
#         time.sleep(1)
#         if arduino.in_waiting == 0:
#             print("[Success]: Arduino remained silent (Correct behavior).")
#         else:
#             read_arduino_response(arduino)
#
#         # --- TEST 4: Invalid Data ---
#         print("\n--- TEST 4: Sending Invalid Data 'hello' ---")
#         print("(Expect: Error: Not a binary signal)")
#         arduino.write(b"hello\n")
#         time.sleep(1)
#         read_arduino_response(arduino)
#
#         # --- TEST 5: Watchdog Timer ---
#         print("\n--- TEST 5: Watchdog Timeout (Waiting 3 seconds...) ---")
#         print("(Expect: No Signal: System Reset / All Off)")
#         time.sleep(3)  # Wait longer than the 2000ms limit
#         # We need to send a character or check buffer to trigger the loop print in some cases,
#         # but the Arduino loop runs constantly, so it should auto-print.
#         read_arduino_response(arduino)
#
#         arduino.close()
#         print("\n--- Test Complete ---")
#
#     except serial.SerialException:
#         print(f"Error: Could not open {SERIAL_PORT}. Check your connection.")
#     except Exception as e:
#         print(f"An error occurred: {e}")
#
#
# if __name__ == "__main__":
#     run_test()

import serial
import time

# ================= CONFIGURATION =================
# CHECK YOUR PORT! (e.g., 'COM3' for Windows, '/dev/ttyUSB0' for Linux/Mac)
SERIAL_PORT = 'COM3'
BAUD_RATE = 9600


def read_arduino_response(ser, label="Arduino"):
    """Reads and prints anything the Arduino sent back."""
    time.sleep(0.1)
    while ser.in_waiting > 0:
        try:
            line = ser.readline().decode('utf-8').strip()
            if line:
                print(f"[{label}]: {line}")
        except:
            pass


def run_test():
    try:
        print(f"Connecting to {SERIAL_PORT}...")
        arduino = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=2)
        time.sleep(2)  # Wait for reboot
        print("Connection established. Clearing buffer...\n")
        read_arduino_response(arduino)

        # Generate patterns to test each bit position.
        # We have 8 bits total (7 "other" relays + 1 "main/0.25" relay logic at the end).
        # We want to walk a '1' across this string: "10000000", "01000000", etc.
        test_patterns = []
        for i in range(8):
            # Create a string of 8 zeros
            chars = ['0'] * 8
            # Set one position to '1'
            chars[i] = '1'
            test_patterns.append("".join(chars))

        # --- MAIN TEST LOOP ---
        for index, pattern in enumerate(test_patterns):
            print(f"\n================ TEST ROUND {index + 1} ================")

            # # STEP 1: SAFETY CHECK (Send 00000000)
            # print("1. Sending SAFETY CHECK ('00000000')...")
            # print("   (Expect: 'SAFETY ACTION: All-Zero detected')")
            # arduino.write(b"00000000\n")
            # time.sleep(0.5)
            # read_arduino_response(arduino, "Safety")

            # STEP 2: ACTIVATE RELAY
            print(f"2. Switching to Pattern '{pattern}'...")
            arduino.write(pattern.encode() + b"\n")
            time.sleep(0.5)
            read_arduino_response(arduino, "Switch")

            # STEP 3: SEND ALIVE (Hold for 5 seconds)
            print("3. Holding state (Sending 'alive' for 5 seconds)...")
            # We send 'alive' 10 times with 0.5s delay = 5 seconds total
            for tick in range(10):
                arduino.write(b"alive\n")
                time.sleep(0.5)

                # Check if Arduino complains (it shouldn't)
                if arduino.in_waiting > 0:
                    read_arduino_response(arduino, "Error?")
                else:
                    # Print a dot to show we are waiting
                    print(".", end="", flush=True)
            print("\n   Done holding.")

        # --- CLEANUP ---
        print("\n================ TESTS COMPLETE ================")
        print("Triggering Watchdog Shutdown (waiting 3s)...")
        time.sleep(3)
        read_arduino_response(arduino, "Shutdown")
        arduino.close()

    except serial.SerialException:
        print(f"Error: Could not open {SERIAL_PORT}. Check your connection.")
    except Exception as e:
        print(f"An error occurred: {e}")


if __name__ == "__main__":
    run_test()