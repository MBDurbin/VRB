import time
from PyQt6 import QtWidgets, QtCore
from multiprocessing import Queue

import theme


class SILSimulatorWindow(QtWidgets.QWidget):
    def __init__(self, telemetry_queue: Queue):
        super().__init__()
        self.telemetry_queue = telemetry_queue

        self.setWindowTitle("SIL Plant Model")
        self.resize(430, 330)

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

    def _slider_label(self, text):
        lbl = QtWidgets.QLabel(text)
        lbl.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: {theme.SIZE_SMALL}px; "
            f"color: {theme.TEXT};")
        return lbl

    def init_ui(self):
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(theme.GAP_LG, theme.GAP_LG, theme.GAP_LG, theme.GAP_LG)
        layout.setSpacing(theme.GAP_SM)

        title = QtWidgets.QLabel("SOFTWARE-IN-THE-LOOP · PLANT MODEL")
        title.setProperty("variant", "section")
        title.setStyleSheet(
            f"color: {theme.WARNING}; font-size: {theme.SIZE_CAPTION}px; font-weight: 700; "
            f"letter-spacing: 1.2px;")
        layout.addWidget(title)

        subtitle = QtWidgets.QLabel(
            "Simulated data is driving the rig. No physical DAQ is being read.")
        subtitle.setWordWrap(True)
        subtitle.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        layout.addWidget(subtitle)
        layout.addSpacing(theme.GAP_SM)

        # Amps Slider
        self.lbl_amps = self._slider_label("Load            0.0 A")
        self.slider_amps = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_amps.setRange(0, 2500)  # 0 to 250.0 A (scaled by 10)
        self.slider_amps.valueChanged.connect(
            lambda v: self.lbl_amps.setText(f"Load            {v / 10.0:.1f} A"))
        layout.addWidget(self.lbl_amps)
        layout.addWidget(self.slider_amps)

        # Temp Slider
        self.lbl_temp = self._slider_label("Max cell temp   25.0 °C")
        self.slider_temp = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_temp.setRange(200, 1000)  # 20.0 to 100.0 C (scaled by 10)
        self.slider_temp.setValue(250)
        self.slider_temp.valueChanged.connect(
            lambda v: self.lbl_temp.setText(f"Max cell temp   {v / 10.0:.1f} °C"))
        layout.addWidget(self.lbl_temp)
        layout.addWidget(self.slider_temp)

        # Pack OCV Slider -- lets the operator walk the pack down to exercise the
        # UNDERVOLTAGE trip. Without this the rig floors at 39.15 V (max sag at
        # 250 A) and the trip can never be reached on a desk test.
        self.lbl_ocv = self._slider_label("Pack OCV        4.20 V/cell  ·  50.4 V")
        self.slider_ocv = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_ocv.setRange(250, 420)  # 2.50 to 4.20 V/cell (scaled by 100)
        self.slider_ocv.setValue(420)
        self.slider_ocv.valueChanged.connect(
            lambda v: self.lbl_ocv.setText(
                f"Pack OCV        {v / 100.0:.2f} V/cell  ·  "
                f"{(v / 100.0) * self.series_count:.1f} V"))
        layout.addWidget(self.lbl_ocv)
        layout.addWidget(self.slider_ocv)

        layout.addSpacing(theme.GAP_MD)

        # Hardware Fault Toggle
        self.chk_fault = QtWidgets.QCheckBox("Simulate hardware interlock fault")
        self.chk_fault.setStyleSheet(f"color: {theme.DANGER}; font-weight: 600;")
        layout.addWidget(self.chk_fault)
        layout.addStretch()

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