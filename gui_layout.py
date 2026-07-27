import sys
import csv
import time
import os
from dataclasses import fields
from queue import Empty, Full
from multiprocessing import Queue, Event

from rig_config import (
    RigConfig, VehicleParams, PackConfig, DaqConfig,
    VEHICLE_FIELD_LABELS, PACK_FIELD_LABELS, DAQ_FIELD_LABELS, field_label,
)
from control_logic import DEFAULT_LAP_CSV
import theme
from PyQt6 import QtWidgets, QtCore
import pyqtgraph as pg


class MetricCard(QtWidgets.QFrame):
    """A captioned numeric readout.

    The value renders monospaced so the card does not reflow as digits change --
    a proportional font makes live telemetry visibly twitch.
    """

    def __init__(self, caption, initial="--", colour=None):
        super().__init__()
        self.setProperty("variant", "card")
        # Equal, non-shrinking widths. Captions must stay static for this to
        # hold: a caption that grows with its value makes the card reflow mid-run.
        self.setMinimumWidth(150)
        self.setSizePolicy(QtWidgets.QSizePolicy.Policy.Expanding,
                           QtWidgets.QSizePolicy.Policy.Fixed)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(theme.GAP_MD, theme.GAP_SM, theme.GAP_MD, theme.GAP_SM)
        layout.setSpacing(2)

        self.caption = QtWidgets.QLabel(caption.upper())
        self.caption.setProperty("variant", "caption")

        self.value = QtWidgets.QLabel(initial)
        self.value.setStyleSheet(
            f"font-family: {theme.FONT_MONO}; font-size: 21px; font-weight: 700; "
            f"color: {colour or theme.TEXT}; background: transparent; border: none;"
        )

        layout.addWidget(self.caption)
        layout.addWidget(self.value)

    def set_value(self, text, colour=None):
        self.value.setText(text)
        if colour:
            self.value.setStyleSheet(
                f"font-family: {theme.FONT_MONO}; font-size: 21px; font-weight: 700; "
                f"color: {colour}; background: transparent; border: none;"
            )


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
        self.resize(780, 720)

        self.config = config
        self.vehicle_widgets = {}
        self.pack_widgets = {}
        self.daq_widgets = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(theme.GAP_LG, theme.GAP_LG, theme.GAP_LG, theme.GAP_LG)
        layout.setSpacing(theme.GAP_MD)

        heading = QtWidgets.QLabel("RIG CONFIGURATION")
        heading.setProperty("variant", "section")
        layout.addWidget(heading)

        intro = QtWidgets.QLabel(
            "Retarget this rig for a different car or a different cell. Pack limits "
            "are derived from the per-cell datasheet values below, so enter what your "
            "cell is rated for and the pack numbers follow automatically."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.SIZE_SMALL}px;")
        layout.addWidget(intro)

        tabs = QtWidgets.QTabWidget()
        tabs.addTab(self._build_vehicle_tab(), "Vehicle")
        tabs.addTab(self._build_pack_tab(), "Battery Pack")
        tabs.addTab(self._build_daq_tab(), "DAQ / Sensors")
        # The tab area absorbs spare height: the derived and warning panels have
        # large text sizeHints and would otherwise squeeze the form down to a few
        # visible rows.
        layout.addWidget(tabs, 1)

        self.lbl_derived = QtWidgets.QLabel()
        self.lbl_derived.setWordWrap(True)
        self.lbl_derived.setStyleSheet(
            f"background-color: {theme.SURFACE}; border: 1px solid {theme.BORDER_SUBTLE}; "
            f"border-radius: {theme.RADIUS}px; padding: {theme.GAP_MD}px; "
            f"font-family: {theme.FONT_MONO}; font-size: 11px; color: {theme.TEXT_DIM};"
        )
        layout.addWidget(self.lbl_derived)

        self.lbl_warnings = QtWidgets.QLabel()
        self.lbl_warnings.setWordWrap(True)
        self.lbl_warnings.setStyleSheet(
            f"background-color: rgba(248, 81, 73, 0.10); border: 1px solid {theme.DANGER_DIM}; "
            f"border-radius: {theme.RADIUS}px; color: {theme.DANGER}; "
            f"padding: {theme.GAP_MD}px; font-size: {theme.SIZE_SMALL}px;"
        )
        self.lbl_warnings.setVisible(False)
        layout.addWidget(self.lbl_warnings)

        buttons = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Save
            | QtWidgets.QDialogButtonBox.StandardButton.Cancel
            | QtWidgets.QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.button(
            QtWidgets.QDialogButtonBox.StandardButton.Save
        ).setProperty("variant", "success")

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
        if isinstance(value, list):
            # Channel lists edit as comma-separated text; order is meaningful
            # (channel i reads the cumulative tap for series group i).
            w = QtWidgets.QLineEdit(", ".join(str(v) for v in value))
            w.setProperty('is_list', True)
            w.editingFinished.connect(self.refresh_derived)
            return w

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

        return w

    def _build_form(self, instance, labels, widget_store):
        container = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(container)
        form.setContentsMargins(theme.GAP_LG, theme.GAP_LG, theme.GAP_LG, theme.GAP_LG)
        form.setSpacing(theme.GAP_MD)
        form.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight |
                               QtCore.Qt.AlignmentFlag.AlignVCenter)

        for f in fields(instance):
            value = getattr(instance, f.name)
            label_text, unit = field_label(f.name, labels)

            label = QtWidgets.QLabel(
                f"{label_text}"
                + (f" <span style='color:{theme.TEXT_MUTED}; font-size:10px;'>{unit}</span>"
                   if unit else "")
            )
            label.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.SIZE_SMALL}px;")

            editor = self._make_editor(value)
            widget_store[f.name] = editor
            form.addRow(label, editor)

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(container)
        return scroll

    def _build_vehicle_tab(self):
        return self._build_form(self.config.vehicle, VEHICLE_FIELD_LABELS, self.vehicle_widgets)

    def _build_pack_tab(self):
        return self._build_form(self.config.pack, PACK_FIELD_LABELS, self.pack_widgets)

    def _build_daq_tab(self):
        return self._build_form(self.config.daq, DAQ_FIELD_LABELS, self.daq_widgets)

    # --- reading values back ---------------------------------------------

    def _read_widget(self, widget):
        if isinstance(widget, QtWidgets.QCheckBox):
            return widget.isChecked()
        if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return widget.value()
        if widget.property('is_list'):
            return [part.strip() for part in widget.text().split(',') if part.strip()]
        return widget.text()

    def collect(self) -> RigConfig:
        """Build a RigConfig from the current widget values."""
        vehicle = VehicleParams(**{
            name: self._read_widget(w) for name, w in self.vehicle_widgets.items()
        })
        pack = PackConfig(**{
            name: self._read_widget(w) for name, w in self.pack_widgets.items()
        })
        daq = DaqConfig(**{
            name: self._read_widget(w) for name, w in self.daq_widgets.items()
        })

        limits = self.config.limits
        limits.apply_pack_derivation(pack)
        return RigConfig(vehicle=vehicle, pack=pack, limits=limits, daq=daq)

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
            f"WIRING            {candidate.daq.channel_count} voltage channels "
            f"for {pack.series_count}S  |  "
            f"{candidate.daq.temp_bus_count}x{candidate.daq.sensors_per_bus} = "
            f"{candidate.daq.sensor_count} thermistors for {pack.cell_count} cells\n"
            f"\n"
            f"RESULTING TRIPS   {candidate.limits.max_amps:.0f} A "
            f"(+{candidate.limits.amp_buffer:.0f} buffer)  |  "
            f"{candidate.limits.max_temp:.0f} C  |  "
            f"{candidate.limits.min_volts:.1f} V"
        )

        warnings = candidate.validate()
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
        for name, w in self.daq_widgets.items():
            self._write_widget(w, getattr(defaults.daq, name))
        self.refresh_derived()

    def _write_widget(self, widget, value):
        if isinstance(widget, QtWidgets.QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            widget.setValue(value)
        elif widget.property('is_list'):
            widget.setText(", ".join(str(v) for v in value))
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

        self.setWindowTitle(f"{series_count}S{parallel_count}P Thermal Map")
        self.resize(min(1600, 62 * series_count + 80), 82 * parallel_count + 80)

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(theme.GAP_LG, theme.GAP_MD, theme.GAP_LG, theme.GAP_LG)
        main_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)

        self.CELL_SIZE = 52
        self.RADIUS = self.CELL_SIZE // 2

        caption = QtWidgets.QLabel(
            f"CELL TEMPERATURES · {series_count}S × {parallel_count}P")
        caption.setProperty("variant", "section")
        main_layout.addWidget(caption)

        label_layout = QtWidgets.QHBoxLayout()
        label_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
        label_layout.setContentsMargins(0, 0, 0, theme.GAP_XS)
        label_layout.setSpacing(3)

        for i in range(series_count):
            lbl = QtWidgets.QLabel(f"S{i + 1}")
            lbl.setFixedSize(self.CELL_SIZE, 18)
            lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color: {theme.TEXT_MUTED}; font-weight: 700; font-size: 10px; "
                f"letter-spacing: 0.5px;")
            label_layout.addWidget(lbl)

        main_layout.addLayout(label_layout)

        self.cell_labels = [[None for _ in range(series_count)] for _ in range(parallel_count)]

        for row in range(parallel_count):
            row_layout = QtWidgets.QHBoxLayout()
            row_layout.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(3)

            for col in range(self.series_count):
                lbl = QtWidgets.QLabel("--")
                lbl.setFixedSize(self.CELL_SIZE, self.CELL_SIZE)
                lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
                lbl.setStyleSheet(self._cell_style(theme.SURFACE_HIGH, theme.TEXT_MUTED))

                row_layout.addWidget(lbl)
                self.cell_labels[row][col] = lbl

            main_layout.addLayout(row_layout)

        swatches = "".join(
            f"<span style='color:{hexcode}'>■</span>" for _, hexcode in theme.HEAT_STOPS)
        legend = QtWidgets.QLabel(f"cool &nbsp;{swatches}&nbsp; at limit")
        legend.setStyleSheet(f"color: {theme.TEXT_MUTED}; font-size: 11px;")
        main_layout.addSpacing(theme.GAP_SM)
        main_layout.addWidget(legend)

    def _cell_style(self, bg, fg):
        return (
            f"background-color: {bg}; color: {fg}; font-family: {theme.FONT_MONO}; "
            f"font-weight: 700; font-size: 12px; border-radius: {theme.RADIUS_SM}px; "
            f"border: 1px solid {theme.BORDER_SUBTLE};"
        )

    def update_temps(self, temp_array, current_max_safe_temp):
        flat_temps = []
        for bus in temp_array:
            flat_temps.extend(bus)

        for col in range(self.series_count):
            for row in range(self.parallel_count):
                sensor_idx = (col * self.parallel_count) + row

                if sensor_idx < len(flat_temps):
                    t = flat_temps[sensor_idx]
                    colour, ratio = theme.heat_colour(t, current_max_safe_temp)
                    # Keep text legible across the whole ramp: dark on the bright
                    # mid-range, white at both cool and hot extremes.
                    text_colour = "#ffffff" if (ratio < 0.35 or ratio > 0.75) else "#14171c"

                    self.cell_labels[row][col].setText(f"{t:.0f}")
                    self.cell_labels[row][col].setStyleSheet(
                        self._cell_style(colour, text_colour))


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
        main_layout.setContentsMargins(theme.GAP_LG, theme.GAP_MD, theme.GAP_LG, theme.GAP_MD)
        main_layout.setSpacing(theme.GAP_MD)

        # ---------- ROW 1: STATE BANNER + LIVE METRICS ----------
        # Status and controls are separated into their own rows. Interleaving
        # them, as this previously did across 14 widgets in a single row, buries
        # the state banner and the E-STOP among the routine controls.
        status_row = QtWidgets.QHBoxLayout()
        status_row.setSpacing(theme.GAP_SM)

        self.lbl_fsm_state = QtWidgets.QLabel("DISCONNECTED")
        self.lbl_fsm_state.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_fsm_state.setMinimumWidth(300)
        self.lbl_fsm_state.setStyleSheet(theme.fsm_style("DISCONNECTED"))

        self.card_voltage = MetricCard("Pack Voltage", "--- V", theme.TRACE_VOLTAGE)
        self.card_current = MetricCard("Current", "--- A", theme.TRACE_CURRENT)
        self.card_power = MetricCard("Power", "--- kW", theme.TEXT)
        self.card_temp = MetricCard("Max Temp", "--- °C", theme.TEXT)
        self.card_soc = MetricCard("Charge", "--- %", theme.SUCCESS)
        self.card_lap = MetricCard("Lap", "-- / --", theme.TEXT_DIM)

        status_row.addWidget(self.lbl_fsm_state, stretch=3)
        for card in (self.card_voltage, self.card_current, self.card_power,
                     self.card_temp, self.card_soc, self.card_lap):
            status_row.addWidget(card, stretch=2)
        main_layout.addLayout(status_row)

        # ---------- ROW 2: CONTROLS ----------
        control_row = QtWidgets.QHBoxLayout()
        control_row.setSpacing(theme.GAP_SM)

        self.btn_config = QtWidgets.QPushButton("Configure")
        self.btn_config.setToolTip("Set up this rig for a different car or a different cell")
        self.btn_config.clicked.connect(self.open_config_dialog)

        self.btn_load_csv = QtWidgets.QPushButton("Load Profile")
        self.btn_load_csv.setToolTip("Load a lap telemetry CSV")
        self.btn_load_csv.clicked.connect(self.load_csv_dialog)

        lbl_laps = QtWidgets.QLabel("LAPS")
        lbl_laps.setProperty("variant", "caption")

        self.spin_laps = QtWidgets.QSpinBox()
        self.spin_laps.setRange(1, 999)
        self.spin_laps.setValue(1)
        self.spin_laps.setKeyboardTracking(False)
        self.spin_laps.setFixedWidth(78)

        self.btn_arm = QtWidgets.QPushButton("Arm")
        self.btn_arm.setProperty("variant", "warning")
        self.btn_arm.clicked.connect(lambda: self.send_command("ARM"))

        self.btn_run = QtWidgets.QPushButton("Run")
        self.btn_run.setProperty("variant", "success")
        self.btn_run.clicked.connect(lambda: self.send_command(("RUN", self.spin_laps.value())))

        self.btn_stop = QtWidgets.QPushButton("E-STOP")
        self.btn_stop.setProperty("variant", "danger")
        self.btn_stop.setToolTip("Immediately shed all load and latch a fault")
        self.btn_stop.clicked.connect(lambda: self.send_command("STOP"))

        self.btn_reset = QtWidgets.QPushButton("Reset")
        self.btn_reset.setToolTip("Clear a latched fault and return to idle")
        self.btn_reset.clicked.connect(lambda: self.send_command("RESET"))

        self.btn_heatmap = QtWidgets.QPushButton("Thermal Map")
        self.btn_heatmap.clicked.connect(self.toggle_heatmap)

        self.btn_record = QtWidgets.QPushButton("Record")
        self.btn_record.setCheckable(True)
        self.btn_record.clicked.connect(self.toggle_logging)

        control_row.addWidget(self.btn_config)
        control_row.addWidget(self.btn_load_csv)
        control_row.addWidget(self._divider())
        control_row.addWidget(lbl_laps)
        control_row.addWidget(self.spin_laps)
        control_row.addWidget(self.btn_arm)
        control_row.addWidget(self.btn_run)
        control_row.addStretch()
        # E-STOP sits alone on the right, clear of the routine controls, so it
        # cannot be hit by accident while reaching for Run or Reset.
        control_row.addWidget(self.btn_stop)
        control_row.addWidget(self.btn_reset)
        control_row.addWidget(self._divider())
        control_row.addWidget(self.btn_heatmap)
        control_row.addWidget(self.btn_record)
        main_layout.addLayout(control_row)

        # ---------- ROW 3: CONTEXT STRIP ----------
        sub_top_layout = QtWidgets.QHBoxLayout()
        sub_top_layout.setSpacing(theme.GAP_MD)

        self.lbl_active_csv = QtWidgets.QLabel(f"Profile: {DEFAULT_LAP_CSV}")
        self.lbl_active_csv.setProperty("variant", "mono")
        self.lbl_active_csv.setStyleSheet(f"color: {theme.TEXT_DIM};")

        # Always-visible statement of which car and which pack is loaded, so an
        # operator can never be unsure what the rig is currently modelling.
        self.lbl_active_config = QtWidgets.QLabel()
        self.lbl_active_config.setProperty("variant", "mono")
        self.lbl_active_config.setStyleSheet(f"color: {theme.ACCENT};")
        self.refresh_config_label()

        sub_top_layout.addWidget(self.lbl_active_csv)
        sub_top_layout.addWidget(self._divider())
        sub_top_layout.addWidget(self.lbl_active_config)
        sub_top_layout.addStretch()
        main_layout.addLayout(sub_top_layout)

        # 2. STATUS PILLS
        hw_layout = QtWidgets.QHBoxLayout()
        hw_layout.setContentsMargins(0, 0, 0, theme.GAP_XS)
        hw_layout.setSpacing(theme.GAP_SM)

        hw_label = QtWidgets.QLabel("HARDWARE LINKS")
        hw_label.setProperty("variant", "caption")

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

        pg.setConfigOptions(antialias=True, background=theme.SURFACE, foreground=theme.TEXT_DIM)
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.plot_widget.setStyleSheet(
            f"border: 1px solid {theme.BORDER_SUBTLE}; border-radius: {theme.RADIUS}px;")
        graph_layout.addWidget(self.plot_widget)

        title_css = {'color': theme.TEXT, 'size': '11pt', 'bold': True}
        grid_alpha = 0.15  # subtle: the trace should dominate, not the grid

        # Voltage Plot
        self.p_voltage = self.plot_widget.addPlot(title="Pack Voltage")
        self.p_voltage.setTitle("Pack Voltage", **title_css)
        self.p_voltage.showGrid(x=True, y=True, alpha=grid_alpha)
        self.p_voltage.setMouseEnabled(x=False, y=True)
        self.p_voltage.getAxis('left').setLabel("Volts", color=theme.TEXT_MUTED)
        self.curve_voltage = self.p_voltage.plot(pen=pg.mkPen(theme.TRACE_VOLTAGE, width=2))

        self.warn_line_v = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(theme.WARNING, width=1, style=QtCore.Qt.PenStyle.DashLine))
        self.warn_line_v.setValue(self.lim_v_warn)
        self.crit_line_v = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(theme.DANGER, width=2, style=QtCore.Qt.PenStyle.DashLine))
        self.crit_line_v.setValue(self.lim_v_crit)
        self.p_voltage.addItem(self.warn_line_v)
        self.p_voltage.addItem(self.crit_line_v)

        self.plot_widget.nextRow()

        # Current Plot
        self.p_current = self.plot_widget.addPlot(title="Current Draw")
        self.p_current.setTitle("Current Draw", **title_css)
        self.p_current.showGrid(x=True, y=True, alpha=grid_alpha)
        self.p_current.setMouseEnabled(x=False, y=True)
        self.p_current.getAxis('left').setLabel("Amps", color=theme.TEXT_MUTED)
        self.p_current.getAxis('bottom').setLabel("Elapsed (s)", color=theme.TEXT_MUTED)
        self.curve_current = self.p_current.plot(pen=pg.mkPen(theme.TRACE_CURRENT, width=2))

        self.crit_line_c = pg.InfiniteLine(
            angle=0, pen=pg.mkPen(theme.DANGER, width=2, style=QtCore.Qt.PenStyle.DashLine))
        self.crit_line_c.setValue(self.lim_c_crit)
        self.p_current.addItem(self.crit_line_c)

        mid_layout.addLayout(graph_layout, stretch=4)

        # Right Sidebar: Cell Voltages & Live Threshold Controls
        sidebar = QtWidgets.QWidget()
        sidebar.setFixedWidth(268)
        cell_layout = QtWidgets.QVBoxLayout(sidebar)
        cell_layout.setContentsMargins(0, 0, 0, 0)
        cell_layout.setSpacing(theme.GAP_SM)

        cells_card = QtWidgets.QFrame()
        cells_card.setProperty("variant", "card")
        cells_inner = QtWidgets.QVBoxLayout(cells_card)
        cells_inner.setContentsMargins(theme.GAP_MD, theme.GAP_MD, theme.GAP_MD, theme.GAP_MD)
        cells_inner.setSpacing(3)

        cell_label = QtWidgets.QLabel(f"{self.config.pack.series_count}S CELL VOLTAGES")
        cell_label.setProperty("variant", "section")
        cells_inner.addWidget(cell_label)

        self.lbl_cells = []
        for i in range(self.config.pack.series_count):
            lbl = QtWidgets.QLabel(f"{i + 1:>2}   ----  V")
            lbl.setStyleSheet(
                f"font-family: {theme.FONT_MONO}; font-size: {theme.SIZE_SMALL}px; "
                f"color: {theme.TEXT}; background: transparent; border: none;")
            self.lbl_cells.append(lbl)
            cells_inner.addWidget(lbl)

        self.lbl_delta_v = QtWidgets.QLabel("ΔV   0.000 V")
        self.lbl_delta_v.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.lbl_delta_v.setStyleSheet(theme.delta_v_style(0.0))
        cells_inner.addSpacing(theme.GAP_XS)
        cells_inner.addWidget(self.lbl_delta_v)
        cell_layout.addWidget(cells_card)

        # --- LIVE SIDEBAR CONTROLS SECTION ---
        limits_card = QtWidgets.QFrame()
        limits_card.setProperty("variant", "card")
        limits_inner = QtWidgets.QVBoxLayout(limits_card)
        limits_inner.setContentsMargins(theme.GAP_MD, theme.GAP_MD, theme.GAP_MD, theme.GAP_MD)
        limits_inner.setSpacing(theme.GAP_SM)

        limits_label = QtWidgets.QLabel("SAFETY THRESHOLDS")
        limits_label.setProperty("variant", "section")
        limits_inner.addWidget(limits_label)

        form_layout = QtWidgets.QFormLayout()
        form_layout.setContentsMargins(0, 0, 0, 0)
        form_layout.setSpacing(theme.GAP_SM)
        form_layout.setLabelAlignment(QtCore.Qt.AlignmentFlag.AlignRight |
                                      QtCore.Qt.AlignmentFlag.AlignVCenter)

        self.sb_v_warn = self.create_sidebar_spinbox(self.lim_v_warn, 0.0, 1000.0)
        self.sb_v_crit = self.create_sidebar_spinbox(self.lim_v_crit, 0.0, 1000.0)
        self.sb_c_crit = self.create_sidebar_spinbox(self.lim_c_crit, 0.0, 2000.0)
        self.sb_c_buffer = self.create_sidebar_spinbox(self.lim_c_buffer, 0.0, 100.0)

        self.sb_v_warn.setToolTip("Display-only line on the voltage plot. Does not trip a fault.")
        self.sb_v_crit.setToolTip("Undervoltage trip point. Keep above the pack's absolute cutoff "
                                  "to leave room for IR sag under load.")
        self.sb_c_crit.setToolTip("Continuous operating limit. The E-STOP fires at this plus the buffer.")
        self.sb_c_buffer.setToolTip("Headroom above the operating limit before the over-current trip fires.")

        self.chk_derate = QtWidgets.QCheckBox("Thermal derate")
        self.chk_derate.setChecked(self.derate_enabled)
        self.chk_derate.setToolTip("Progressively reduce the current limit between "
                                   "derate start and max temp.")

        self.sb_t_derate = self.create_sidebar_spinbox(self.lim_t_derate, 0.0, 200.0)
        self.sb_t_crit = self.create_sidebar_spinbox(self.lim_t_crit, 0.0, 200.0)

        form_layout.addRow(self._field_label("V warn", "display only"), self.sb_v_warn)
        form_layout.addRow(self._field_label("V crit", "trip"), self.sb_v_crit)
        form_layout.addRow(self._field_label("Max current", "A"), self.sb_c_crit)
        form_layout.addRow(self._field_label("E-stop buffer", "A"), self.sb_c_buffer)
        form_layout.addRow(self._field_label("Derate start", "°C"), self.sb_t_derate)
        form_layout.addRow(self._field_label("Max temp", "°C"), self.sb_t_crit)

        limits_inner.addLayout(form_layout)
        limits_inner.addWidget(self.chk_derate)
        cell_layout.addWidget(limits_card)

        self.sb_v_warn.valueChanged.connect(self.handle_limit_change)
        self.sb_v_crit.valueChanged.connect(self.handle_limit_change)
        self.sb_c_crit.valueChanged.connect(self.handle_limit_change)
        self.sb_c_buffer.valueChanged.connect(self.handle_limit_change)
        self.sb_t_crit.valueChanged.connect(self.handle_limit_change)
        self.sb_t_derate.valueChanged.connect(self.handle_limit_change)
        self.chk_derate.stateChanged.connect(self.handle_limit_change)

        cell_layout.addStretch()
        mid_layout.addWidget(sidebar)
        main_layout.addLayout(mid_layout)

    def send_command(self, cmd):
        return send_command_nonblocking(self.gui_cmd_queue, cmd)

    def _divider(self):
        """Thin vertical rule used to group related controls."""
        line = QtWidgets.QFrame()
        line.setFrameShape(QtWidgets.QFrame.Shape.VLine)
        line.setStyleSheet(f"color: {theme.BORDER}; background-color: {theme.BORDER}; max-width: 1px;")
        return line

    def _field_label(self, name, unit):
        """Sidebar form label: name in body text, unit in muted small text."""
        lbl = QtWidgets.QLabel(f"{name} <span style='color:{theme.TEXT_MUTED}; font-size:10px;'>{unit}</span>")
        lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: {theme.SIZE_SMALL}px;")
        return lbl

    def create_status_pill(self, text):
        lbl = QtWidgets.QLabel(f"○  {text}")
        lbl.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        lbl.setStyleSheet(theme.pill_style(False))
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
        sb.setFixedWidth(92)
        return sb

    def update_status_pill(self, lbl, name, is_connected):
        # Filled dot for a live link, hollow for a dead one -- readable without
        # relying on the colour alone.
        lbl.setText(f"{'●' if is_connected else '○'}  {name}")
        lbl.setStyleSheet(theme.pill_style(is_connected))

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

        daq_changed = new_config.daq != self.config.daq

        warnings = new_config.validate()
        if warnings:
            body = "\n".join(f"  - {w}" for w in warnings)
            confirm = QtWidgets.QMessageBox.warning(
                self, "Configuration problems",
                f"This configuration has problems:\n\n{body}\n\n"
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

        if daq_changed:
            # The DAQ process builds its nidaqmx task and sensor buffers once at
            # startup, so channel and sensor-layout changes cannot take effect
            # live. Say so plainly rather than letting the operator believe a
            # rewiring change is already active.
            QtWidgets.QMessageBox.information(
                self, "Restart required",
                "DAQ channel or sensor settings changed.\n\n"
                "These are applied when the DAQ process starts, so restart the "
                "application for them to take effect. Vehicle, pack and limit "
                "changes are already live.")

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
            self.btn_heatmap.setText("Thermal Map")
            theme.restyle(self.btn_heatmap, None)
        else:
            self.heatmap_window.show()
            self.btn_heatmap.setText("Hide Thermal Map")
            theme.restyle(self.btn_heatmap, "primary")

    def toggle_logging(self):
        if self.btn_record.isChecked():
            self.is_logging = True
            self.btn_record.setText("● Recording")

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
            self.btn_record.setText("Record")
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

            fsm_state = latest_data.get('fsm_state', 'DISCONNECTED')
            self.lbl_fsm_state.setStyleSheet(theme.fsm_style(fsm_state))
            self.lbl_fsm_state.setText(fsm_state)

            cur_lap = latest_data.get('current_lap', 0)
            tot_laps = latest_data.get('total_laps', 0)
            if fsm_state == "RUNNING":
                self.card_lap.set_value(f"{cur_lap} / {tot_laps}", theme.SUCCESS)
            else:
                self.card_lap.set_value("-- / --", theme.TEXT_DIM)

            self.card_voltage.set_value(f"{volts:.1f} V")
            self.card_current.set_value(f"{amps:.1f} A")
            self.card_power.set_value(f"{latest_data.get('power_kw', 0.0):.2f} kW")

            # Temperature and SOC change colour as they approach their limits, so
            # a glance at the card is enough to know whether the pack is happy.
            temp_colour = theme.TEXT
            if max_t >= self.lim_t_crit:
                temp_colour = theme.DANGER
            elif max_t >= self.lim_t_derate:
                temp_colour = theme.WARNING
            self.card_temp.set_value(f"{max_t:.1f} °C", temp_colour)

            soc_colour = theme.SUCCESS
            if true_soc <= 10.0:
                soc_colour = theme.DANGER
            elif true_soc <= 25.0:
                soc_colour = theme.WARNING
            self.card_soc.set_value(f"{true_soc:.0f} %", soc_colour)
            self.card_soc.setToolTip(f"{rem_ah:.2f} Ah remaining of "
                                     f"{self.config.pack.capacity_ah:.1f} Ah nameplate")

            valid_cells = [c for c in cells if c > 1.0]
            # zip() bounds the loop to whichever is shorter: a pack configured
            # with a different series count than the DAQ reports must not raise.
            for i, (lbl, cell_v) in enumerate(zip(self.lbl_cells, cells)):
                lbl.setText(f"{i + 1:>2}   {cell_v:5.2f} V")

            delta_v = (max(valid_cells) - min(valid_cells)) if valid_cells else 0.0
            self.lbl_delta_v.setText(f"ΔV   {delta_v:.3f} V")
            self.lbl_delta_v.setStyleSheet(theme.delta_v_style(delta_v))

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


def apply_theme(app):
    """Apply the shared look. Call once per QApplication, before showing windows."""
    app.setStyle("Fusion")
    app.setStyleSheet(theme.app_stylesheet())


def run_gui_process(telemetry_queue: Queue, gui_cmd_queue: Queue, stop_event: Event):
    app = QtWidgets.QApplication(sys.argv)
    apply_theme(app)
    window = TelemetryGUI(telemetry_queue, gui_cmd_queue, stop_event)
    window.show()
    sys.exit(app.exec())