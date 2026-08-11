import unittest
from unittest.mock import MagicMock, patch
import threading
import time

# IMPORT YOUR MAIN SCRIPT
# Assumes your main script is named 'loadbank_controller.py'
# If it is named something else, change this line!
import Master_Code_v1 as app


class TestPhysicsEngine(unittest.TestCase):
    """Tests the math and logic without touching hardware."""

    def test_resistance_calculation(self):
        """
        Verify the physics formula: R = V^2 / P
        We will use simple numbers to verify the math is correct.
        """
        # Fake inputs
        velocity = 20.0  # m/s
        voltage = 48.0  # Volts

        # 1. Calculate Expected Power based on the constants in your main script
        # crr=0.015, mass=300, g=9.81 -> F_roll = 44.145 N
        # rho=1.225, cd=0.7, area=1.0 -> F_drag = 0.5 * 1.225 * 0.7 * 1.0 * (20^2) = 171.5 N
        # Total Force = 215.645 N
        # Power = Force * Velocity = 215.645 * 20 = 4312.9 Watts

        # 2. Calculate Expected Resistance
        # R = V^2 / P = (48^2) / 4312.9 = 2304 / 4312.9 = ~0.5342 Ohms

        result_ohms = app.calculate_required_resistance(velocity, voltage)

        print(f"\n[Physics Test] Input: {velocity}m/s, {voltage}V -> Output: {result_ohms:.4f} Ohms")

        # Assert the result is close enough (within 1%)
        self.assertAlmostEqual(result_ohms, 0.5342, places=3)

    def test_binary_conversion(self):
        """
        Verify that Ohms are correctly converted to 8-bit binary strings.
        Resolution = 0.25 Ohms.
        """
        # Test Case 1: 1.0 Ohm
        # 1.0 / 0.25 = 4 steps -> Binary 4 is '00000100'
        self.assertEqual(app.resistance_to_binary(1.0), "00000100")

        # Test Case 2: 2.5 Ohms
        # 2.5 / 0.25 = 10 steps -> Binary 10 is '00001010'
        self.assertEqual(app.resistance_to_binary(2.5), "00001010")

        # Test Case 3: Max Limit (64 Ohms)
        # Should clamp to 11111111 (255 steps)
        self.assertEqual(app.resistance_to_binary(100.0), "11111111")

        # Test Case 4: Min Limit (0 Ohms)
        # Should clamp to 00000001 (1 step) to prevent short circuit
        self.assertEqual(app.resistance_to_binary(0.0), "00000001")
        print("\n[Binary Test] All conversions passed.")


class TestSystemIntegration(unittest.TestCase):
    """Tests the threading and Serial communication logic."""

    @patch('serial.Serial')  # This mocks the serial library
    def test_heartbeat_thread(self, mock_serial):
        """
        Verifies that the background thread actually sends 'alive'
        without blocking the main program.
        """
        # Setup the mock
        mock_ser_instance = MagicMock()
        stop_event = threading.Event()

        # Start the heartbeat
        print("\n[Thread Test] Starting Heartbeat Monitor...")
        t = threading.Thread(target=app.heartbeat_worker, args=(mock_ser_instance, stop_event))
        t.start()

        # Let it run for 2.5 seconds (should send 'alive' ~2 times)
        time.sleep(2.5)

        # Stop the thread
        stop_event.set()
        t.join()

        # Verify 'alive\n' was written at least twice
        # The arg passed to write() must be b"alive\n"
        call_count = mock_ser_instance.write.call_count
        print(f"[Thread Test] Heartbeat sent {call_count} times.")
        self.assertGreaterEqual(call_count, 2)
        mock_ser_instance.write.assert_called_with(b"alive\n")


if __name__ == '__main__':
    # Save the user's provided code into a file so we can import it
    # This block is just for the user's convenience to run this test file directly
    import os

    if not os.path.exists("loadbank_controller.py"):
        print("CRITICAL ERROR: Please save your main code as 'loadbank_controller.py' first!")
    else:
        unittest.main()