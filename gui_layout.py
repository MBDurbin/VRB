import sys
import csv
import time
import os
from dataclasses import fields
from queue import Empty, Full
from multiprocessing import Queue, Event

from rig_config import (
    RigConfig, VehicleParams, PackConfig,
    VEHICLE_FIELD_LABELS, PACK_FIELD_LABELS, field_label,
)
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


# ================= CONFIGURATION DIALOG =================

class ConfigDialog(QtWidgets.QDialog):
    """Editor for the vehicle model and battery pack.

    Fields are generated from the dataclasses in rig_config, so adding a
    parameter there makes it appear here with no GUI changes needed. Pack limits
    are shown derived live from the per-cell datasheet values, which is the whole
    point: a future team enters their cell's numbers and the pack current,
    voltage and capacity limits follow, instead of being hand-computed.
    """

    def __init__(self, config: RigConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rig Configuration")
        self.resize(760, 700)
        self.setStyleSheet("background-color: #1e1e1e; color: white; font-size: 13px;")

        self.config = config
        self.vehicle_widgets = {}
        self.pack_widgets = {}

        layout = QtWidgets.QVBoxLayout(self)

        intro = QtWidgets.QLabel(
            "Retarget this rig for a different car or a different cell. Pack limits "
            "are derived from the per-cell datasheet values below, so enter what your "
            "cell is rated for and the pack numbers follow automatically."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #aaa; font-style: italic; padding-bottom: 8px;")
        layout.addWidget(intro)

        tabs = QtWidgets.QTabWidget()
        tabs.setStyleSheet(
            "QTabBar::tab { background: #333; color: white; padding: 8px 16px; }"
            "QTabBar::tab:selected { background: #0055ff; }"
        )
        tabs.addTab(self._build_vehicle_tab(), "Vehicle")
        tabs.addTab(self._build_pack_tab(), "Battery Pack")
        layout.addWidget(tabs)

        self.lbl_derived = QtWidgets.QLabel()
        self.lbl_derived.setWordWrap(True)
        self.lbl_derived.setStyleSheet(
            "background-color: #262626; border: 1px solid #444; padding: 10px; "
            "font-family: Consolas; font-size: 12px;"
        )
        layout.addWidget(self.lbl_derived)

        self.lbl_warnings = QtWidgets.QLabel()
        self.lbl_warnings.setWordWrap(True)
        self.lbl_warnings.setStyleSheet(
            "background-color: #3a1111; border: 1px solid #a33; color: #ff9999; "
            "padding: 10px; font-size: 12px;"
        )
        self.lbl_warnings.setVisible(False)
        layout.addWidget(self.lbl_warnings)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Save).setStyleSheet(
            "background-color: darkgreen; color: white; font-weight: bold; padding: 8px 20px;")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Cancel).setStyleSheet(
            "background-color: #555; color: white; padding: 8px 20px;")
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults).setStyleSheet(
            "background-color: #444; color: white; padding: 8px 20px;")

        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults
        ).clicked.connect(self.restore_defaults)
        layout.addWidget(buttons)

        self.refresh_derived()

    # --- tab construction -------------------------------------------------

    def _make_editor(self, value):
        """Build an input widget appropriate to the value's type."""
        if isinstance(value, bool):
            w = QtWidgets.QCheckBox()
            w.setChecked(value)
            w.stateChanged.connect(self.refresh_derived)
        elif isinstance(value, int):
            w = QtWidgets.QSpinBox()
            w.setRange(1, 999)
            w.setValue(value)
            w.setKeyboardTracking(False)
            w.valueChanged.connect(self.refresh_derived)
        elif isinstance(value, float):
            w = QtWidgets.QDoubleSpinBox()
            w.setDecimals(4)
            # Wide enough for any plausible parameter; negative allowed so Cl can
            # express downforce with the usual sign convention.
            w.setRange(-10000.0, 100000.0)
            w.setValue(value)
            w.setKeyboardTracking(False)
            w.valueChanged.connect(self.refresh_derived)
        else:
            w = QtWidgets.QLineEdit(str(value))
            w.editingFinished.connect(self.refresh_derived)

        w.setStyleSheet(
            "background-color: #333; color: white; border: 1px solid #555; "
            "padding: 4px; font-family: Consolas;"
        )
        return w

    def _build_form(self, instance, labels, widget_store):
        container = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(container)
        form.setContentsMargins(15, 15, 15, 15)
        form.setSpacing(8)

        for f in fields(instance):
            value = getattr(instance, f.name)
            label_text, unit = field_label(f.name, labels)
            if unit:
                label_text = f"{label_text}  [{unit}]"

            editor = self._make_editor(value)
            widget_store[f.name] = editor
            form.addRow(label_text + ":", editor)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        scroll.setStyleSheet("border: none;")
        return scroll

    def _build_vehicle_tab(self):
        return self._build_form(self.config.vehicle, VEHICLE_FIELD_LABELS, self.vehicle_widgets)

    def _build_pack_tab(self):
        return self._build_form(self.config.pack, PACK_FIELD_LABELS, self.pack_widgets)

    # --- reading values back ---------------------------------------------

    def _read_widget(self, widget):
        if isinstance(widget, QtWidgets.QCheckBox):
            return widget.isChecked()
        if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return widget.value()
        return widget.text()

    def collect(self) -> RigConfig:
        """Build a RigConfig from the current widget values."""
        vehicle = VehicleParams(**{
            name: self._read_widget(w) for name, w in self.vehicle_widgets.items()
        })
        pack = PackConfig(**{
            name: self._read_widget(w) for name, w in self.pack_widgets.items()
        })

        limits = self.config.limits
        limits.apply_pack_derivation(pack)
        return RigConfig(vehicle=vehicle, pack=pack, limits=limits)

    # --- live feedback ----------------------------------------------------

    def refresh_derived(self):
        try:
            candidate = self.collect()
        except Exception as exc:
            self.lbl_derived.setText(f"Invalid configuration: {exc}")
            return

        pack, vehicle = candidate.pack, candidate.vehicle
        sag = pack.sag_volts(pack.max_current_a)

        self.lbl_derived.setText(
            f"DERIVED PACK      {pack.series_count}S{pack.parallel_count}P "
            f"= {pack.cell_count} cells\n"
            f"  Capacity        {pack.capacity_ah:.2f} Ah "
            f"({'minimum' if pack.use_minimum_capacity else 'typical'} cell capacity)\n"
            f"  Voltage         {pack.min_voltage:.1f} V cutoff  ->  "
            f"{pack.nominal_voltage:.1f} V nominal  ->  {pack.max_voltage:.1f} V full\n"
            f"  Max current     {pack.max_current_a:.0f} A "
            f"({pack.cell_max_continuous_a:.0f} A/cell x {pack.parallel_count}P)\n"
            f"  Internal R      {pack.resistance_ohm * 1000:.1f} mOhm  "
            f"-> {sag:.1f} V sag at {pack.max_current_a:.0f} A\n"
            f"  Energy          {pack.energy_wh:.0f} Wh\n"
            f"\n"
            f"DERIVED VEHICLE\n"
            f"  Total mass      {vehicle.total_mass_kg:.1f} kg "
            f"({vehicle.total_mass_kg * vehicle.rotational_mass_factor:.1f} kg effective "
            f"under acceleration)\n"
            f"\n"
            f"RESULTING TRIPS   {candidate.limits.max_amps:.0f} A "
            f"(+{candidate.limits.amp_buffer:.0f} buffer)  |  "
            f"{candidate.limits.max_temp:.0f} C  |  "
            f"{candidate.limits.min_volts:.1f} V"
        )

        warnings = candidate.limits.exceedances(pack)
        if warnings:
            self.lbl_warnings.setText("WARNING\n" + "\n".join(f"  - {w}" for w in warnings))
            self.lbl_warnings.setVisible(True)
        else:
            self.lbl_warnings.setVisible(False)

    def restore_defaults(self):
        confirm = QtWidgets.QMessageBox.question(
            self, "Restore defaults?",
            "Reset the vehicle and pack to the shipped Molicel P45B 12S4P defaults?\n\n"
            "This discards your car's parameters.",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        defaults = RigConfig.defaults()
        for name, w in self.vehicle_widgets.items():
            self._write_widget(w, getattr(defaults.vehicle, name))
        for name, w in self.pack_widgets.items():
            self._write_widget(w, getattr(defaults.pack, name))
        self.refresh_derived()

    def _write_widget(self, widget, value):
        if isinstance(widget, QtWidgets.QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            widget.setValue(value)
        else:
            widget.setText(str(value))


# ================= HEATMAP WINDOW =================
class HeatmapWindow(QtWidgets.QWidget):
    def __init__(self, series_count=12, parallel_count=4):
        super().__init__()
        # Grid follows the configured pack topology rather than a hardcoded
        # 12S4P, so a future team's pack draws correctly.
        self.series_count = series_count
        self.parallel_count = parallel_count

        self.setWindowTitle(f"{series_count}S{parallel_count}P Battery Thermal Map")
        self.resize(min(1600, 70 * series_count + 60), 90 * parallel_count)
        self.setStyleSheet("background-color: #121212;")

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.CELL_SIZE = 55
        self.RADIUS = self.CELL_SIZE // 2

        label_layout = QtWidgets.QHBoxLayout()
        label_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        label_layout.setContentsMargins(15, 0, 0, 10)

        for i in range(series_count):
            lbl = QtWidgets.QLabel(f"S{i + 1}")
            lbl.setFixedSize(self.CELL_SIZE, 20)
            lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet("color: #888; font-weight: bold; font-size: 14px;")
            label_layout.addWidget(lbl)

        main_layout.addLayout(label_layout)

        self.cell_labels = [[None for _ in range(series_count)] for _ in range(parallel_count)]

        for row in range(parallel_count):
            row_layout = QtWidgets.QHBoxLayout()
            row_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
            row_layout.setContentsMargins(0, 0, 0, 0)

            if row % 2 != 0:
                row_layout.addSpacing(self.RADIUS)

            for col in range(self.series_count):
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

        for col in range(self.series_count):
            for row in range(self.parallel_count):
                sensor_idx = (col * self.parallel_count) + row

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

        # Vehicle, pack and safety limits all come from rig_config.json, falling
        # back to the P45B 12S4P defaults. Editable from the Configure dialog so
        # a future team retargets the rig without touching source.
        self.config = RigConfig.load()

        # V Crit trips a fault; V Warn is a display-only line on the plot.
        self.lim_v_warn = self.config.limits.warn_volts
        self.lim_v_crit = self.config.limits.min_volts
        self.lim_c_crit = self.config.limits.max_amps
        self.lim_c_buffer = self.config.limits.amp_buffer
        self.lim_t_crit = self.config.limits.max_temp

        # Derate Variables
        self.derate_enabled = self.config.limits.derate_enabled
        self.lim_t_derate = self.config.limits.derate_start

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
        self.btn_config = QtWidgets.QPushButton("⚙ CONFIGURE")
        self.btn_config.setToolTip("Set up this rig for a different car or a different cell")
        self.btn_config.setStyleSheet(
            "background-color: #444; color: white; font-weight: bold; font-size: 14px; padding: 10px;")
        self.btn_config.clicked.connect(self.open_config_dialog)

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
        top_layout.addWidget(self.btn_config)
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

        # Always-visible statement of which car and which pack is loaded, so an
        # operator can never be unsure what the rig is currently modelling.
        self.lbl_active_config = QtWidgets.QLabel()
        self.lbl_active_config.setStyleSheet("color: #66aaff; font-size: 14px; margin-left: 20px;")
        self.refresh_config_label()

        self.lbl_lap_progress = QtWidgets.QLabel("Lap: -- / --")
        self.lbl_lap_progress.setStyleSheet("color: #0f0; font-weight: bold; font-size: 18px; margin-right: 20px;")

        sub_top_layout.addWidget(self.lbl_active_csv)
        sub_top_layout.addWidget(self.lbl_active_config)
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
        cell_label = QtWidgets.QLabel(f"{self.config.pack.series_count}S Cell Voltages")
        cell_label.setStyleSheet("font-size: 16px; font-weight: bold; color: white;")
        cell_layout.addWidget(cell_label)

        self.lbl_cells = []
        for i in range(self.config.pack.series_count):
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

    def refresh_config_label(self):
        pack = self.config.pack
        vehicle = self.config.vehicle
        self.lbl_active_config.setText(
            f"Car: {vehicle.total_mass_kg:.0f} kg  |  "
            f"Pack: {pack.cell_model} {pack.series_count}S{pack.parallel_count}P "
            f"({pack.capacity_ah:.1f} Ah, {pack.max_current_a:.0f} A)"
        )

    def open_config_dialog(self):
        dialog = ConfigDialog(self.config, self)
        if dialog.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return

        try:
            new_config = dialog.collect()
        except Exception as exc:
            QtWidgets.QMessageBox.critical(
                self, "Invalid configuration", f"Could not apply configuration:\n\n{exc}")
            return

        warnings = new_config.limits.exceedances(new_config.pack)
        if warnings:
            body = "\n".join(f"  - {w}" for w in warnings)
            confirm = QtWidgets.QMessageBox.warning(
                self, "Limits exceed cell ratings",
                f"This configuration exceeds what the cells are rated for:\n\n{body}\n\n"
                "Apply anyway?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if confirm != QtWidgets.QMessageBox.StandardButton.Yes:
                return

        self.config = new_config
        self.apply_config_to_widgets()
        self.refresh_config_label()

        try:
            path = self.config.save()
            print(f"[GUI] Configuration saved to {path}")
        except Exception as exc:
            QtWidgets.QMessageBox.warning(
                self, "Could not save",
                f"Configuration applied for this session but not saved to disk:\n\n{exc}")

        self.send_command(("SET_CONFIG", self.config.to_dict()))

    def apply_config_to_widgets(self):
        """Push config-derived limits into the sidebar spinboxes.

        Signals are blocked so repopulating the boxes does not emit a burst of
        SET_LIMITS commands that would race the SET_CONFIG we are about to send.
        """
        limits = self.config.limits
        pairs = [
            (self.sb_v_warn, limits.warn_volts),
            (self.sb_v_crit, limits.min_volts),
            (self.sb_c_crit, limits.max_amps),
            (self.sb_c_buffer, limits.amp_buffer),
            (self.sb_t_crit, limits.max_temp),
            (self.sb_t_derate, limits.derate_start),
        ]
        for widget, value in pairs:
            widget.blockSignals(True)
            widget.setValue(value)
            widget.blockSignals(False)

        self.chk_derate.blockSignals(True)
        self.chk_derate.setChecked(limits.derate_enabled)
        self.chk_derate.blockSignals(False)

        self.lim_v_warn = limits.warn_volts
        self.lim_v_crit = limits.min_volts
        self.lim_c_crit = limits.max_amps
        self.lim_c_buffer = limits.amp_buffer
        self.lim_t_crit = limits.max_temp
        self.lim_t_derate = limits.derate_start
        self.derate_enabled = limits.derate_enabled

        self.warn_line_v.setValue(self.lim_v_warn)
        self.crit_line_v.setValue(self.lim_v_crit)
        self.crit_line_c.setValue(self.lim_c_crit)

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

        # Hand-edited limits are an intentional override, so stop re-deriving
        # them from the pack -- otherwise the next config load would silently
        # revert the operator's values.
        limits = self.config.limits
        limits.derive_from_pack = False
        limits.warn_volts = self.lim_v_warn
        limits.min_volts = self.lim_v_crit
        limits.max_amps = self.lim_c_crit
        limits.amp_buffer = self.lim_c_buffer
        limits.max_temp = self.lim_t_crit
        limits.derate_start = self.lim_t_derate
        limits.derate_enabled = self.derate_enabled

        self.send_command(("SET_LIMITS", limits.to_command_dict()))

    def toggle_heatmap(self):
        if self.heatmap_window is None:
            self.heatmap_window = HeatmapWindow(
                self.config.pack.series_count, self.config.pack.parallel_count)

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

            series = self.config.pack.series_count
            parallel = self.config.pack.parallel_count

            headers = ["Timestamp", "Pack_V", "Amps", "Power_kW", "Max_Temp", "SOC_Est", "FSM_State", "Target_Res"]
            headers.extend([f"Cell_{i + 1}_V" for i in range(series)])
            for s in range(1, series + 1):
                for p in range(1, parallel + 1):
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
            # zip() bounds the loop to whichever is shorter: a pack configured
            # with a different series count than the DAQ reports must not raise.
            for i, (lbl, cell_v) in enumerate(zip(self.lbl_cells, cells)):
                lbl.setText(f"Cell {i + 1:>2}: {cell_v:.2f} V")

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

                # Pad/truncate to the configured pack size so every row matches
                # the header width even if the hardware reports a different count.
                series = self.config.pack.series_count
                sensor_count = series * self.config.pack.parallel_count

                cell_row = [round(v, 3) for v in cells][:series]
                cell_row += [0.0] * (series - len(cell_row))
                row_data.extend(cell_row)

                flat_temps = []
                for bus in temps:
                    flat_temps.extend(bus)

                temp_row = [round(t, 1) for t in flat_temps][:sensor_count]
                temp_row += [0.0] * (sensor_count - len(temp_row))
                row_data.extend(temp_row)

                self.csv_writer.writerow(row_data)

    def closeEvent(self, event):
        self.stop_event.set()

        # Persist limits so hand-edited thresholds survive a restart. A failure
        # here must never block shutdown -- the E-STOP path runs through close.
        try:
            self.config.save()
        except Exception as exc:
            print(f"[GUI] Could not save configuration on exit: {exc}")

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