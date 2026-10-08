import time
from PyQt6 import QtWidgets, QtCore
from multiprocessing import Queue

import theme
from queue_util import put_latest
from rig_config import RigConfig


class SILSimulatorWindow(QtWidgets.QWidget):
    def __init__(self, telemetry_queue: Queue, config: RigConfig = None):
        super().__init__()
        self.telemetry_queue = telemetry_queue

        self.setWindowTitle("SIL Plant Model")
        self.resize(430, 380)

        # The pack and wiring the rig is configured for, so desk tests of the
        # current, sag and bank-power trips run against the installed cells. These
        # were hardcoded for the Molicel P45B (45 mOhm pack) and stayed that way
        # after the move to the RS50 (12 mOhm), so every sag figure was 3-4x the
        # real one. Read once at startup; restart to pick up a new pack.
        config = config if config is not None else RigConfig.load()
        pack, daq = config.pack, config.daq
        self.series_count = pack.series_count
        self.pack_min_v = pack.min_voltage
        self.pack_ir = pack.resistance_ohm
        self.cell_min_v = pack.cell_min_voltage
        self.cell_max_v = pack.cell_max_voltage
        # The same layout the DAQ would publish, so the logic's count checks see
        # a complete packet.
        self.temp_buses = daq.temp_bus_count
        self.sensors_per_bus = daq.sensors_per_bus
        self.n_banks = len(daq.resistor_tc_channels)

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

        # Bank 1 resistor temperature. Bank 1 carries the peak duty, so it is the
        # one to drive past its trip; banks 2-4 sit at room temperature.
        self.lbl_res_temp = self._slider_label("Bank 1 resistor  30 °C")
        self.slider_res_temp = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_res_temp.setRange(20, 350)
        self.slider_res_temp.setValue(30)
        self.slider_res_temp.valueChanged.connect(
            lambda v: self.lbl_res_temp.setText(f"Bank 1 resistor  {v} °C"))
        layout.addWidget(self.lbl_res_temp)
        layout.addWidget(self.slider_res_temp)

        # Pack OCV Slider -- lets the operator walk the pack down to exercise the
        # UNDERVOLTAGE trip. Sag alone cannot reach it from a full pack: at the
        # slider's 250 A a 12 mOhm module drops only 3 V. Cutoff to full charge.
        top = int(round(self.cell_max_v * 100))
        self.lbl_ocv = self._slider_label(
            f"Pack OCV        {self.cell_max_v:.2f} V/cell  ·  "
            f"{self.cell_max_v * self.series_count:.1f} V")
        self.slider_ocv = QtWidgets.QSlider(QtCore.Qt.Orientation.Horizontal)
        self.slider_ocv.setRange(int(round(self.cell_min_v * 100)), top)  # V/cell x 100
        self.slider_ocv.setValue(top)
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
        sim_voltage = max(self.pack_min_v, sim_voltage)  # Hard floor at the cell cutoff

        # Generate stable fake cell voltages
        fake_cells = [(sim_voltage / self.series_count)] * self.series_count
        fake_temps = [[sim_temp] * self.sensors_per_bus for _ in range(self.temp_buses)]
        # Bank 1 from its slider; any other thermocoupled banks at room temperature.
        bank_temps = ([float(self.slider_res_temp.value())] + [30.0] * (self.n_banks - 1)
                      if self.n_banks else [])

        hw_fault = self.chk_fault.isChecked()

        fake_data = {
            'voltage': sim_voltage,
            'amps': sim_amps,
            'max_temp': sim_temp,
            'cell_voltages': fake_cells,
            'temperatures': fake_temps,
            'power_kw': (sim_voltage * sim_amps) / 1000.0,
            # SIL data is generated fresh every tick. The fault toggle below drops
            # temp_arduino instead, which is what exercises the stale/lost checks.
            'temp_age_s': 0.0,
            'temp_sensor_ages_s': [[0.0] * len(bus) for bus in fake_temps],
            'resistor_temps': bank_temps,
            'resistor_temp_ages_s': [0.0] * len(bank_temps),
            'hardware_status': {
                'ni_daq': not hw_fault,
                'temp_arduino': not hw_fault,
                'res_arduino': True
            }
        }

        # Newest packet wins, and never blocks the Qt thread: the put() that was
        # here could wait on a queue the logic process had not yet drained.
        put_latest(self.telemetry_queue, fake_data)