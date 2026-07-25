import time
from PyQt6 import QtWidgets, QtCore
from multiprocessing import Queue


class SILSimulatorWindow(QtWidgets.QWidget):
    def __init__(self, telemetry_queue: Queue):
        super().__init__()
        self.telemetry_queue = telemetry_queue

        self.setWindowTitle("SIL Jailbreak - Plant Model")
        self.resize(400, 250)
        self.setStyleSheet("background-color: #2b2b2b; color: white; font-size: 14px;")

        # Physics Constants for P45B 12S4P (Molicel INR-21700-P45B v1.2)
        #   4.2 V/cell charge * 12S      = 50.4 V full
        #   2.5 V/cell cutoff * 12S      = 30.0 V floor
        #   15 mohm DC/cell / 4P * 12S   = 45 mohm pack IR
        self.series_count = 12
        self.pack_max_v = 50.4
        self.pack_min_v = 30.0
        self.pack_ir = 0.045

        self.init_ui()

        # 20Hz Telemetry Injector Loop
        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.inject_telemetry)
        self.timer.start(50)

    def init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)

        title = QtWidgets.QLabel("SOFTWARE-IN-THE-LOOP (SIL) ACTIVE")
        title.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("color: #00ff00; font-weight: bold; font-size: 16px; padding-bottom: 10px;")
        layout.addWidget(title)

        # Amps Slider
        self.lbl_amps = QtWidgets.QLabel("Simulated Load: 0.0 A")
        self.slider_amps = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_amps.setRange(0, 2500)  # 0 to 250.0 A (scaled by 10)
        self.slider_amps.valueChanged.connect(lambda v: self.lbl_amps.setText(f"Simulated Load: {v / 10.0:.1f} A"))
        layout.addWidget(self.lbl_amps)
        layout.addWidget(self.slider_amps)

        # Temp Slider
        self.lbl_temp = QtWidgets.QLabel("Simulated Max Temp: 25.0 °C")
        self.slider_temp = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_temp.setRange(200, 1000)  # 20.0 to 100.0 C (scaled by 10)
        self.slider_temp.setValue(250)
        self.slider_temp.valueChanged.connect(lambda v: self.lbl_temp.setText(f"Simulated Max Temp: {v / 10.0:.1f} °C"))
        layout.addWidget(self.lbl_temp)
        layout.addWidget(self.slider_temp)

        # Pack OCV Slider -- lets the operator walk the pack down to exercise the
        # UNDERVOLTAGE trip. Without this the rig floors at 39.15 V (max sag at
        # 250 A) and the trip can never be reached on a desk test.
        self.lbl_ocv = QtWidgets.QLabel("Simulated Pack OCV: 4.20 V/cell (50.4 V)")
        self.slider_ocv = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_ocv.setRange(250, 420)  # 2.50 to 4.20 V/cell (scaled by 100)
        self.slider_ocv.setValue(420)
        self.slider_ocv.valueChanged.connect(
            lambda v: self.lbl_ocv.setText(
                f"Simulated Pack OCV: {v / 100.0:.2f} V/cell ({(v / 100.0) * self.series_count:.1f} V)"))
        layout.addWidget(self.lbl_ocv)
        layout.addWidget(self.slider_ocv)

        # Hardware Fault Toggle
        self.chk_fault = QtWidgets.QCheckBox("Trigger Hardware Interlock Fault")
        self.chk_fault.setStyleSheet("color: #ff4444; font-weight: bold; margin-top: 15px;")
        layout.addWidget(self.chk_fault)

    def inject_telemetry(self):
        """Builds a fake telemetry packet and shoves it into the main queue."""
        sim_amps = self.slider_amps.value() / 10.0
        sim_temp = self.slider_temp.value() / 10.0

        # Calculate realistic voltage sag off the operator-set open-circuit voltage
        sim_ocv = (self.slider_ocv.value() / 100.0) * self.series_count
        sim_voltage = sim_ocv - (sim_amps * self.pack_ir)
        sim_voltage = max(self.pack_min_v, sim_voltage)  # Hard floor at 2.5 V/cell

        # Generate stable fake cell voltages
        fake_cells = [(sim_voltage / self.series_count)] * self.series_count
        fake_temps = [[sim_temp] * 12 for _ in range(4)]

        hw_fault = self.chk_fault.isChecked()

        fake_data = {
            'voltage': sim_voltage,
            'amps': sim_amps,
            'max_temp': sim_temp,
            'cell_voltages': fake_cells,
            'temperatures': fake_temps,
            'power_kw': (sim_voltage * sim_amps) / 1000.0,
            'hardware_status': {
                'ni_daq': not hw_fault,
                'temp_arduino': not hw_fault,
                'res_arduino': True
            }
        }

        # Overwrite queue to keep it fresh
        if self.telemetry_queue.full():
            try:
                self.telemetry_queue.get_nowait()
            except:
                pass
        self.telemetry_queue.put(fake_data)