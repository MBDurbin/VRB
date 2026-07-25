import sys
import csv
import time
import os
from queue import Empty, Full
from multiprocessing import Queue, Event
from PyQt6 import QtWidgets, QtCore
import pyqtgraph as pg


# ================= COMMAND DISPATCH =================

def send_command_nonblocking(cmd_queue, cmd):
    """Push a command to the logic process without ever blocking the caller.

    gui_cmd_queue is bounded, and a plain .put() blocks when it is full -- which
    would freeze the whole UI, E-STOP button included, exactly when the operator
    needs it most. If the queue is backed up we discard the oldest command to make
    room, since the commands that realistically pile up (SET_LIMITS) are
    latest-wins anyway. A queued STOP is never discarded.

    Returns True if the command was enqueued, False if it had to be dropped.
    """
    try:
        cmd_queue.put_nowait(cmd)
        return True
    except Full:
        pass

    try:
        stale = cmd_queue.get_nowait()
    except Empty:
        stale = None

    if stale == "STOP" and cmd != "STOP":
        # A pending E-STOP outranks whatever we were trying to send. Put it back
        # and drop the new command instead.
        try:
            cmd_queue.put_nowait(stale)
        except Full:
            pass
        return False

    try:
        cmd_queue.put_nowait(cmd)
        return True
    except Full:
        return False


# ================= HEATMAP WINDOW =================
class HeatmapWindow(QtWidgets.QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("12S4P Battery Thermal Map")
        self.resize(850, 350)
        self.setStyleSheet("background-color: #121212;")

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.CELL_SIZE = 55
        self.RADIUS = self.CELL_SIZE // 2

        label_layout = QtWidgets.QHBoxLayout()
        label_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        label_layout.setContentsMargins(15, 0, 0, 10)

        for i in range(12):
            lbl = QtWidgets.QLabel(f"S{i + 1}")
            lbl.setFixedSize(self.CELL_SIZE, 20)
            lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color: #888; font-weight: bold; font-size: 14px;")
            label_layout.addWidget(lbl)

        main_layout.addLayout(label_layout)

        self.cell_labels = [[None for _ in range(12)] for _ in range(4)]

        for row in range(4):
            row_layout = QtWidgets.QHBoxLayout()
            row_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
            row_layout.setContentsMargins(0, 0, 0, 0)

            if row % 2 != 0:
                row_layout.addSpacing(self.RADIUS)

            for col in range(12):
                lbl = QtWidgets.QLabel("0.0")
                lbl.setFixedSize(self.CELL_SIZE, self.CELL_SIZE)
                lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

                lbl.setStyleSheet(
                    f"background-color: blue; color: white; "
                    f"font-weight: bold; font-size: 13px; "
                    f"border-radius: {self.RADIUS}px; border: 2px solid #222;"
                )

                row_layout.addWidget(lbl)
                self.cell_labels[row][col] = lbl

            main_layout.addLayout(row_layout)
            main_layout.addSpacing(-10)

    def get_color(self, temp, max_t):
        min_t = 20.0
        ratio = max(0.0, min(1.0, (temp - min_t) / (max_t - min_t)))
        r = int(ratio * 255)
        b = int((1.0 - ratio) * 255)
        return f"#{r:02x}00{b:02x}"

    def update_temps(self, temp_array, current_max_safe_temp):
        flat_temps = []
        for bus in temp_array:
            flat_temps.extend(bus)

        for col in range(12):
            for row in range(4):
                sensor_idx = (col * 4) + row

                if sensor_idx < len(flat_temps):
                    t = flat_temps[sensor_idx]
                    color = self.get_color(t, current_max_safe_temp)
                    text_color = "white" if (t < 25 or t > (current_max_safe_temp - 10)) else "black"

                    self.cell_labels[row][col].setText(f"{t:.1f}")
                    self.cell_labels[row][col].setStyleSheet(
                        f"background-color: {color}; color: {text_color}; "
                        f"font-weight: bold; font-size: 13px; "
                        f"border-radius: {self.RADIUS}px; border: 2px solid #222;"
                    )


# ================= MAIN TELEMETRY GUI =================
class TelemetryGUI(QtWidgets.QMainWindow):
    def __init__(self, telemetry_queue: Queue, gui_cmd_queue: Queue, stop_event: Event):
        super().__init__()
        self.queue = telemetry_queue
        self.gui_cmd_queue = gui_cmd_queue
        self.stop_event = stop_event

        self.is_logging = False
        self.csv_file = None
        self.csv_writer = None
        self.heatmap_window = None

        # Default Safety Thresholds -- Molicel INR-21700-P45B v1.2, 12S4P.
        # V Crit trips a fault; V Warn is a display-only line on the plot.
        self.lim_v_warn = 38.0
        self.lim_v_crit = 36.0   # 3.0 V/cell, above the 2.5 V/cell (30.0 V) cutoff
        self.lim_c_crit = 180.0  # 45 A/cell continuous * 4P
        self.lim_c_buffer = 5.0
        self.lim_t_crit = 60.0   # datasheet discharge range tops out at 60 C

        # Derate Variables
        self.derate_enabled = False
        self.lim_t_derate = 55.0

        # Dynamic lists for continuous scrolling data
        self.time_data = []
        self.current_data = []
        self.voltage_data = []
        self.start_time = time.time()

        self.init_ui()

        self.timer = QtCore.QTimer()
        self.timer.timeout.connect(self.update_gui)
        self.timer.start(50)

    def init_ui(self):
        self.setWindowTitle("FSAE Resistor Bank Telemetry")
        self.resize(1450, 850)

        central_widget = QtWidgets.QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QtWidgets.QVBoxLayout(central_widget)

        # 1. TOP BAR (Critical Metrics & Controls)
        top_layout = QtWidgets.QHBoxLayout()

        self.lbl_fsm_state = QtWidgets.QLabel("DISCONNECTED")
        self.lbl_fsm_state.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_fsm_state.setStyleSheet(
            "background-color: #333; color: #777; font-size: 24px; font-weight: bold; border-radius: 5px; padding: 10px;")

        self.lbl_pack_v = self.create_metric_label("0.0 V", "yellow")
        self.lbl_current = self.create_metric_label("0.0 A", "cyan")
        self.lbl_soc = self.create_metric_label("SOC: 0.0% (0.0 Ah)", "green")

        # Lap Control Section
        lbl_laps = QtWidgets.QLabel("Target Laps:")
        lbl_laps.setStyleSheet("color: white; font-weight: bold; font-size: 14px; margin-left: 10px;")

        self.spin_laps = QtWidgets.QSpinBox()
        self.spin_laps.setRange(1, 999)
        self.spin_laps.setValue(1)
        self.spin_laps.setStyleSheet(
            "background-color: #333; color: white; font-size: 16px; font-weight: bold; padding: 5px; border: 1px solid #555;")

        # FSM Command Buttons
        self.btn_load_csv = QtWidgets.QPushButton("📂 LOAD CSV")
        self.btn_load_csv.setStyleSheet(
            "background-color: #444; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        self.btn_load_csv.clicked.connect(self.load_csv_dialog)

        self.btn_arm = QtWidgets.QPushButton("ARM")
        self.btn_arm.setStyleSheet(
            "background-color: darkorange; color: black; font-weight: bold; font-size: 14px; padding: 10px;")
        self.btn_arm.clicked.connect(lambda: self.send_command("ARM"))

        self.btn_run = QtWidgets.QPushButton("▶ RUN")
        self.btn_run.setStyleSheet(
            "background-color: darkgreen; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        self.btn_run.clicked.connect(lambda: self.send_command(("RUN", self.spin_laps.value())))

        self.btn_stop = QtWidgets.QPushButton("🛑 E-STOP")
        self.btn_stop.setStyleSheet(
            "background-color: red; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        self.btn_stop.clicked.connect(lambda: self.send_command("STOP"))

        self.btn_reset = QtWidgets.QPushButton("RESET")
        self.btn_reset.setStyleSheet(
            "background-color: #555; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        self.btn_reset.clicked.connect(lambda: self.send_command("RESET"))

        self.btn_heatmap = QtWidgets.QPushButton("SHOW HEATMAP")
        self.btn_heatmap.setStyleSheet(
            "background-color: #0055ff; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        self.btn_heatmap.clicked.connect(self.toggle_heatmap)

        self.btn_record = QtWidgets.QPushButton("START RECORDING")
        self.btn_record.setCheckable(True)
        self.btn_record.setStyleSheet(
            "background-color: darkred; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        self.btn_record.clicked.connect(self.toggle_logging)

        # Assemble Top Row
        top_layout.addWidget(self.lbl_fsm_state, stretch=2)
        top_layout.addWidget(self.lbl_pack_v)
        top_layout.addWidget(self.lbl_current)
        top_layout.addWidget(self.lbl_soc)
        top_layout.addWidget(self.btn_load_csv)
        top_layout.addWidget(lbl_laps)
        top_layout.addWidget(self.spin_laps)
        top_layout.addWidget(self.btn_arm)
        top_layout.addWidget(self.btn_run)
        top_layout.addWidget(self.btn_stop)
        top_layout.addWidget(self.btn_reset)
        top_layout.addWidget(self.btn_heatmap)
        top_layout.addWidget(self.btn_record)
        main_layout.addLayout(top_layout)

        # 1B. SUB-TOP BAR (File Name and Lap Progress)
        sub_top_layout = QtWidgets.QHBoxLayout()
        self.lbl_active_csv = QtWidgets.QLabel("Profile: Default (Speed and Time 1 Lap.csv)")
        self.lbl_active_csv.setStyleSheet("color: #aaa; font-style: italic; font-size: 14px; margin-left: 10px;")

        self.lbl_lap_progress = QtWidgets.QLabel("Lap: -- / --")
        self.lbl_lap_progress.setStyleSheet("color: #0f0; font-weight: bold; font-size: 18px; margin-right: 20px;")

        sub_top_layout.addWidget(self.lbl_active_csv)
        sub_top_layout.addStretch()
        sub_top_layout.addWidget(self.lbl_lap_progress)
        main_layout.addLayout(sub_top_layout)

        # 2. STATUS PILLS
        hw_layout = QtWidgets.QHBoxLayout()
        hw_layout.setContentsMargins(0, 0, 0, 10)

        hw_label = QtWidgets.QLabel("HARDWARE LINKS:")
        hw_label.setStyleSheet("color: #888; font-weight: bold; font-size: 14px; margin-right: 10px;")

        self.lbl_stat_daq = self.create_status_pill("NI DAQ")
        self.lbl_stat_temp = self.create_status_pill("TEMP SENSOR")
        self.lbl_stat_res = self.create_status_pill("RESISTOR CTRL")

        hw_layout.addWidget(hw_label)
        hw_layout.addWidget(self.lbl_stat_daq)
        hw_layout.addWidget(self.lbl_stat_temp)
        hw_layout.addWidget(self.lbl_stat_res)
        hw_layout.addStretch()
        main_layout.addLayout(hw_layout)

        # 3. MIDDLE (Graphs & Cells Sidebar)
        mid_layout = QtWidgets.QHBoxLayout()
        graph_layout = QtWidgets.QVBoxLayout()

        pg.setConfigOptions(antialias=True, background='#121212', foreground='w')
        self.plot_widget = pg.GraphicsLayoutWidget()
        graph_layout.addWidget(self.plot_widget)

        # Voltage Plot
        self.p_voltage = self.plot_widget.addPlot(title="Pack Voltage (V)")
        self.p_voltage.showGrid(x=True, y=True, alpha=0.3)
        self.p_voltage.setMouseEnabled(x=False, y=True)
        self.curve_voltage = self.p_voltage.plot(pen=pg.mkPen('y', width=2))

        self.warn_line_v = pg.InfiniteLine(angle=0, pen=pg.mkPen('y', width=2, style=QtCore.Qt.PenStyle.DashLine))
        self.warn_line_v.setValue(self.lim_v_warn)
        self.crit_line_v = pg.InfiniteLine(angle=0, pen=pg.mkPen('r', width=2, style=QtCore.Qt.PenStyle.DashLine))
        self.crit_line_v.setValue(self.lim_v_crit)
        self.p_voltage.addItem(self.warn_line_v)
        self.p_voltage.addItem(self.crit_line_v)

        self.plot_widget.nextRow()

        # Current Plot
        self.p_current = self.plot_widget.addPlot(title="Current Draw (A)")
        self.p_current.showGrid(x=True, y=True, alpha=0.3)
        self.p_current.setMouseEnabled(x=False, y=True)
        self.curve_current = self.p_current.plot(pen=pg.mkPen('c', width=2))

        self.crit_line_c = pg.InfiniteLine(angle=0, pen=pg.mkPen('r', width=2, style=QtCore.Qt.PenStyle.DashLine))
        self.crit_line_c.setValue(self.lim_c_crit)
        self.p_current.addItem(self.crit_line_c)

        mid_layout.addLayout(graph_layout, stretch=4)

        # Right Sidebar: Cell Voltages & Live Threshold Controls
        cell_layout = QtWidgets.QVBoxLayout()
        cell_label = QtWidgets.QLabel("12S Cell Voltages")
        cell_label.setStyleSheet("font-size: 16px; font-weight: bold; color: white;")
        cell_layout.addWidget(cell_label)

        self.lbl_cells = []
        for i in range(12):
            lbl = QtWidgets.QLabel(f"Cell {i + 1:>2}: 0.00 V")
            lbl.setStyleSheet("font-family: Consolas; font-size: 14px;")
            self.lbl_cells.append(lbl)
            cell_layout.addWidget(lbl)

        self.lbl_delta_v = QtWidgets.QLabel("ΔV: 0.00 V")
        self.lbl_delta_v.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_delta_v.setStyleSheet(
            "font-size: 18px; font-weight: bold; background-color: #222; margin-top: 10px; padding: 5px;")
        cell_layout.addWidget(self.lbl_delta_v)

        # --- LIVE SIDEBAR CONTROLS SECTION ---
        limits_label = QtWidgets.QLabel("Safety Thresholds")
        limits_label.setStyleSheet("font-size: 16px; font-weight: bold; margin-top: 20px; color: #ffaa00;")
        cell_layout.addWidget(limits_label)

        form_layout = QtWidgets.QFormLayout()
        form_layout.setContentsMargins(0, 5, 0, 5)

        self.sb_v_warn = self.create_sidebar_spinbox(self.lim_v_warn, 0.0, 100.0)
        self.sb_v_crit = self.create_sidebar_spinbox(self.lim_v_crit, 0.0, 100.0)
        self.sb_c_crit = self.create_sidebar_spinbox(self.lim_c_crit, 0.0, 500.0)
        self.sb_c_buffer = self.create_sidebar_spinbox(self.lim_c_buffer, 0.0, 50.0)

        self.chk_derate = QtWidgets.QCheckBox("Enable Thermal Derate")
        self.chk_derate.setStyleSheet("color: white; font-size: 13px; font-weight: bold;")
        self.chk_derate.setChecked(self.derate_enabled)

        self.sb_t_derate = self.create_sidebar_spinbox(self.lim_t_derate, 20.0, 150.0)
        self.sb_t_crit = self.create_sidebar_spinbox(self.lim_t_crit, 20.0, 150.0)

        form_layout.addRow("V Warn [V] (display):", self.sb_v_warn)
        form_layout.addRow("V Crit [V] (trip):", self.sb_v_crit)
        form_layout.addRow("Max Amp [A]:", self.sb_c_crit)
        form_layout.addRow("E-Stop Buffer [A]:", self.sb_c_buffer)
        form_layout.addRow(self.chk_derate)
        form_layout.addRow("Derate Start [°C]:", self.sb_t_derate)
        form_layout.addRow("Max Temp [°C]:", self.sb_t_crit)

        limits_container = QtWidgets.QWidget()
        limits_container.setLayout(form_layout)
        limits_container.setStyleSheet("color: white; font-size: 13px; font-weight: bold;")
        cell_layout.addWidget(limits_container)

        self.sb_v_warn.valueChanged.connect(self.handle_limit_change)
        self.sb_v_crit.valueChanged.connect(self.handle_limit_change)
        self.sb_c_crit.valueChanged.connect(self.handle_limit_change)
        self.sb_c_buffer.valueChanged.connect(self.handle_limit_change)
        self.sb_t_crit.valueChanged.connect(self.handle_limit_change)
        self.sb_t_derate.valueChanged.connect(self.handle_limit_change)
        self.chk_derate.stateChanged.connect(self.handle_limit_change)

        cell_layout.addStretch()
        mid_layout.addLayout(cell_layout, stretch=1)
        main_layout.addLayout(mid_layout)

    def send_command(self, cmd):
        return send_command_nonblocking(self.gui_cmd_queue, cmd)

    def create_metric_label(self, text, color):
        lbl = QtWidgets.QLabel(text)
        lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            f"font-size: 18px; font-weight: bold; color: {color}; background-color: #222; border-radius: 5px; padding: 5px;")
        return lbl

    def create_status_pill(self, text):
        lbl = QtWidgets.QLabel(f"{text}: OFFLINE")
        lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(
            "background-color: darkred; color: white; font-weight: bold; font-size: 12px; border-radius: 4px; padding: 4px 10px; margin-right: 5px;")
        return lbl

    def create_sidebar_spinbox(self, val, min_val, max_val):
        sb = QtWidgets.QDoubleSpinBox()
        sb.setRange(min_val, max_val)
        sb.setValue(val)
        sb.setSingleStep(1.0)
        # Without this, valueChanged fires on every keystroke: typing "100" into
        # Max Temp emits 1 -> 10 -> 100, and the intermediate 1.0 is pushed to the
        # logic process as the live safety limit, instantly tripping OVERTEMP.
        # Off means the value is only committed on Enter/arrows/focus-out.
        sb.setKeyboardTracking(False)
        sb.setStyleSheet(
            "background-color: #333; color: white; border: 1px solid #555; padding: 2px; font-size: 13px; font-family: Consolas;")
        return sb

    def update_status_pill(self, lbl, name, is_connected):
        if is_connected:
            lbl.setText(f"{name}: ONLINE")
            lbl.setStyleSheet(
                "background-color: green; color: white; font-weight: bold; font-size: 12px; border-radius: 4px; padding: 4px 10px; margin-right: 5px;")
        else:
            lbl.setText(f"{name}: OFFLINE")
            lbl.setStyleSheet(
                "background-color: darkred; color: white; font-weight: bold; font-size: 12px; border-radius: 4px; padding: 4px 10px; margin-right: 5px;")

    def load_csv_dialog(self):
        filepath, _ = QtWidgets.QFileDialog.getOpenFileName(
            self,
            "Select Lap Telemetry CSV",
            "",
            "CSV Files (*.csv)"
        )
        if filepath:
            self.send_command(("LOAD_CSV", filepath))
            filename = os.path.basename(filepath)
            self.lbl_active_csv.setText(f"Profile: {filename}")

    def handle_limit_change(self):
        self.lim_v_warn = self.sb_v_warn.value()
        self.lim_v_crit = self.sb_v_crit.value()
        self.lim_c_crit = self.sb_c_crit.value()
        self.lim_c_buffer = self.sb_c_buffer.value()
        self.lim_t_crit = self.sb_t_crit.value()
        self.lim_t_derate = self.sb_t_derate.value()
        self.derate_enabled = self.chk_derate.isChecked()

        self.warn_line_v.setValue(self.lim_v_warn)
        self.crit_line_v.setValue(self.lim_v_crit)
        self.crit_line_c.setValue(self.lim_c_crit)

        limits_dict = {
            'max_amps': self.lim_c_crit,
            'amp_buffer': self.lim_c_buffer,
            'max_temp': self.lim_t_crit,
            'min_volts': self.lim_v_crit,
            'derate_en': self.derate_enabled,
            'derate_start': self.lim_t_derate
        }
        self.send_command(("SET_LIMITS", limits_dict))

    def toggle_heatmap(self):
        if self.heatmap_window is None:
            self.heatmap_window = HeatmapWindow()

        if self.heatmap_window.isVisible():
            self.heatmap_window.hide()
            self.btn_heatmap.setText("SHOW HEATMAP")
            self.btn_heatmap.setStyleSheet(
                "background-color: #0055ff; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        else:
            self.heatmap_window.show()
            self.btn_heatmap.setText("HIDE HEATMAP")
            self.btn_heatmap.setStyleSheet(
                "background-color: #555; color: white; font-weight: bold; font-size: 14px; padding: 10px;")

    def toggle_logging(self):
        if self.btn_record.isChecked():
            self.is_logging = True
            self.btn_record.setText("STOP RECORDING")
            self.btn_record.setStyleSheet(
                "background-color: red; color: white; font-weight: bold; font-size: 14px; padding: 10px;")

            filename = f"telemetry_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            self.csv_file = open(filename, 'w', newline='')
            self.csv_writer = csv.writer(self.csv_file)

            headers = ["Timestamp", "Pack_V", "Amps", "Power_kW", "Max_Temp", "SOC_Est", "FSM_State", "Target_Res"]
            headers.extend([f"Cell_{i + 1}_V" for i in range(12)])
            for s in range(1, 13):
                for p in range(1, 5):
                    headers.append(f"S{s}_T{p}")

            self.csv_writer.writerow(headers)
        else:
            self.is_logging = False
            self.btn_record.setText("START RECORDING")
            self.btn_record.setStyleSheet(
                "background-color: darkred; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
            if self.csv_file:
                self.csv_file.close()

    def update_gui(self):
        if self.stop_event.is_set():
            self.close()
            return

        latest_data = None
        while not self.queue.empty():
            try:
                latest_data = self.queue.get_nowait()
            except Empty:
                break

        if latest_data:
            volts = latest_data['voltage']
            amps = latest_data['amps']
            max_t = latest_data['max_temp']
            cells = latest_data['cell_voltages']
            temps = latest_data.get('temperatures', [])

            # Pull Coulomb tracking metrics
            true_soc = latest_data.get('true_soc', 0.0)
            rem_ah = latest_data.get('remaining_ah', 0.0)

            hw_status = latest_data.get('hardware_status', {})
            self.update_status_pill(self.lbl_stat_daq, "NI DAQ", hw_status.get('ni_daq', False))
            self.update_status_pill(self.lbl_stat_temp, "TEMP SENSOR", hw_status.get('temp_arduino', False))
            self.update_status_pill(self.lbl_stat_res, "RESISTOR CTRL", hw_status.get('res_arduino', False))

            fsm_state = latest_data.get('fsm_state', 'WAITING/MONITORING')
            if "FAULT" in fsm_state or "KILLED" in fsm_state:
                self.lbl_fsm_state.setStyleSheet(
                    "background-color: red; color: white; font-size: 24px; font-weight: bold; border-radius: 5px;")
            elif fsm_state == "ARMED":
                self.lbl_fsm_state.setStyleSheet(
                    "background-color: #ffaa00; color: black; font-size: 24px; font-weight: bold; border-radius: 5px;")
            elif fsm_state == "RUNNING":
                self.lbl_fsm_state.setStyleSheet(
                    "background-color: #00aa00; color: white; font-size: 24px; font-weight: bold; border-radius: 5px;")
            else:
                self.lbl_fsm_state.setStyleSheet(
                    "background-color: #333; color: #0f0; font-size: 24px; font-weight: bold; border-radius: 5px;")
            self.lbl_fsm_state.setText(f"STATE: {fsm_state}")

            cur_lap = latest_data.get('current_lap', 0)
            tot_laps = latest_data.get('total_laps', 0)
            if fsm_state == "RUNNING":
                self.lbl_lap_progress.setText(f"Lap: {cur_lap} / {tot_laps}")
            else:
                self.lbl_lap_progress.setText("Lap: -- / --")

            self.lbl_pack_v.setText(f"{volts:.1f} V")
            self.lbl_current.setText(f"{amps:.1f} A")

            # Apply Coulomb Counting update to UI label
            self.lbl_soc.setText(f"SOC: {true_soc:.1f}% ({rem_ah:.1f} Ah)")

            valid_cells = [c for c in cells if c > 1.0]
            for i, cell_v in enumerate(cells):
                self.lbl_cells[i].setText(f"Cell {i + 1:>2}: {cell_v:.2f} V")

            delta_v = (max(valid_cells) - min(valid_cells)) if valid_cells else 0.0
            self.lbl_delta_v.setText(f"ΔV: {delta_v:.3f} V")
            if delta_v > 0.3:
                self.lbl_delta_v.setStyleSheet(
                    "font-size: 18px; font-weight: bold; background-color: red; color: white; margin-top: 10px; padding: 5px;")
            elif delta_v > 0.15:
                self.lbl_delta_v.setStyleSheet(
                    "font-size: 18px; font-weight: bold; background-color: #ffaa00; color: black; margin-top: 10px; padding: 5px;")
            else:
                self.lbl_delta_v.setStyleSheet(
                    "font-size: 18px; font-weight: bold; background-color: #222; color: #0f0; margin-top: 10px; padding: 5px;")

            if self.heatmap_window and self.heatmap_window.isVisible() and temps:
                self.heatmap_window.update_temps(temps, self.lim_t_crit)

            current_time = time.time() - self.start_time
            self.time_data.append(current_time)
            self.voltage_data.append(volts)
            self.current_data.append(amps)

            if len(self.time_data) > 36000:
                self.time_data.pop(0)
                self.voltage_data.pop(0)
                self.current_data.pop(0)

            self.curve_voltage.setData(self.time_data, self.voltage_data)
            self.curve_current.setData(self.time_data, self.current_data)

            if self.time_data[-1] > 0:
                self.p_voltage.setXRange(self.time_data[0], self.time_data[-1], padding=0)
                self.p_current.setXRange(self.time_data[0], self.time_data[-1], padding=0)

            if self.is_logging and self.csv_writer:
                row_data = [
                    round(current_time, 3), round(volts, 2), round(amps, 2),
                    round(latest_data.get('power_kw', 0), 2), round(max_t, 2),
                    round(true_soc, 1), fsm_state, round(latest_data.get('target_resistance', 0.0), 2)
                ]

                row_data.extend([round(v, 3) for v in cells])
                flat_temps = []
                for bus in temps:
                    flat_temps.extend(bus)

                if len(flat_temps) == 48:
                    row_data.extend([round(t, 1) for t in flat_temps])
                else:
                    row_data.extend([0.0] * 48)

                self.csv_writer.writerow(row_data)

    def closeEvent(self, event):
        self.stop_event.set()
        if self.csv_file:
            self.csv_file.close()
        if self.heatmap_window:
            self.heatmap_window.close()
        event.accept()


def run_gui_process(telemetry_queue: Queue, gui_cmd_queue: Queue, stop_event: Event):
    app = QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    window = TelemetryGUI(telemetry_queue, gui_cmd_queue, stop_event)
    window.show()
    sys.exit(app.exec())