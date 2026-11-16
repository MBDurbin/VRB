"""
current_and_temp_recorder.py -- watch the LEM current and the 1 ohm resistor's
temperature, and record them to a CSV.

Bench tool, run directly:

    python tools/current_and_temp_recorder.py

A window with the two live readings, a plot of each, and a Record button.
Record starts a new CSV in logs/, named current_temp_<date>_<time>.csv with the
Name box added if it is filled in; pressing it again closes the file. Every row
goes to disk as it is read, so a crash or a pulled plug loses nothing already
recorded.

    --bank N      another bank's thermocouple instead of the 1 ohm (1-4)

Columns: Timestamp, Elapsed_s (from the first recorded row), LEM_V, Current_A,
Bank<N>_Temp_C. Current_A is left empty while the LEM signal is off scale, and
the temperature while the thermocouple reads open: an empty field reads back as
NaN in pandas and plots as a gap.

Reads the same two channels, the same way, as tools/current_and_temp_check.py,
from rig_config.json. Reads only; it never writes to the DAQ or the config.

Close main_v2.py first. Two programs cannot hold the same DAQ module, and the
rig's own reader does not come back if it loses one.
"""

import argparse
import csv
import math
import os
import re
import sys
import threading
import time
from collections import deque, namedtuple
from datetime import datetime

import nidaqmx
import numpy as np
import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(TOOLS_DIR)
sys.path.insert(0, PROJECT_DIR)
sys.path.insert(0, TOOLS_DIR)
import current_and_temp_check as check
import theme
from gui_layout import MetricCard, apply_theme
from rig_config import RigConfig

# Where recordings go: with the rig's own logs, which git ignores.
LOG_DIR = os.path.join(PROJECT_DIR, "logs")

DEFAULT_PERIOD_S = 0.5
# Shorter than about 0.1 s the rows stop being evenly spaced: a sample waits
# while the window redraws. Each row carries the time it was really taken, and
# the Sample rate readout shows the rate really reached.
MIN_PERIOD_S = 0.05
MAX_PERIOD_S = 60.0

# Points kept on the plots: 2.8 hours at the default period. The CSV has no limit.
PLOT_POINTS = 20000

# How often the readouts and plots are redrawn. The sampling and the CSV do not
# wait on it.
REFRESH_MS = 200

# Samples the "Sample rate" readout is averaged over.
RATE_SAMPLES = 20

# One reading of both channels. `time` is time.time() when it was taken.
Sample = namedtuple("Sample", "time volts amps temp_c")


def csv_row(sample, started, daq):
    """One CSV row. A reading that is not a measurement is left empty.

    LEM_V is always written: it is what the input read, on scale or not.
    """
    stamp = datetime.fromtimestamp(sample.time).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return [
        stamp,
        f"{sample.time - started:.3f}",
        f"{sample.volts:.5f}",
        f"{sample.amps:.3f}" if check.lem_on_scale(sample.volts, daq) else "",
        f"{sample.temp_c:.2f}" if check.is_temperature(sample.temp_c) else "",
    ]


def log_path(name, when, log_dir=LOG_DIR):
    """The file a recording started at `when` goes to. `name` is optional."""
    stem = "current_temp_" + time.strftime("%Y%m%d_%H%M%S", time.localtime(when))
    safe = re.sub(r"[^A-Za-z0-9.-]+", "_", name).strip("._")
    return os.path.join(log_dir, f"{stem}_{safe}.csv" if safe else f"{stem}.csv")


class CsvLog:
    """One CSV recording at a time.

    write() is called from the sampling thread, start() and stop() from the
    window's, so all three take the lock.
    """

    def __init__(self, daq, bank):
        self._daq = daq
        self._bank = bank
        self._lock = threading.Lock()
        self._file = None
        self._writer = None
        self._started = None
        self.path = None        # the current or last recording
        self.rows = 0           # rows written to it
        self.error = None       # why it stopped on its own, if it did

    @property
    def recording(self):
        return self._file is not None

    def start(self, path):
        """Open a new recording. Raises OSError if the file cannot be created."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._lock:
            # "x": never write over an earlier recording.
            self._file = open(path, "x", newline="")
            self._writer = csv.writer(self._file)
            self._writer.writerow(["Timestamp", "Elapsed_s", "LEM_V", "Current_A",
                                   f"Bank{self._bank}_Temp_C"])
            self._file.flush()
            self._started = None
            self.path, self.rows, self.error = path, 0, None

    def write(self, sample):
        """Add one row if a recording is open, and put it on disk straight away."""
        with self._lock:
            if self._file is None:
                return
            if self._started is None:
                self._started = sample.time
            try:
                self._writer.writerow(csv_row(sample, self._started, self._daq))
                self._file.flush()
            except OSError as exc:
                # Disk full, drive gone. Stop here and say so, rather than let
                # the exception end the sampling thread.
                self.error = str(exc)
                self._close()
                return
            self.rows += 1

    def stop(self):
        with self._lock:
            self._close()

    def _close(self):
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
        self._file = self._writer = None


class Sampler(threading.Thread):
    """Reads both channels every period_s on a thread of its own.

    Off the window's thread so a slow or failed read cannot freeze it. Each
    sample goes to the log first and then to on_sample, so the recording does
    not wait on the window: dragging or resizing it holds up the display, not
    the CSV.
    """

    def __init__(self, daq, channel, period_s, log, on_sample, on_error):
        super().__init__(daemon=True)
        self._daq = daq
        self._channel = channel
        self._log = log
        self._on_sample = on_sample
        self._on_error = on_error
        self._halt = threading.Event()
        self.period_s = period_s    # may be changed while running

    def stop(self):
        self._halt.set()

    def run(self):
        try:
            with nidaqmx.Task() as current_task, nidaqmx.Task() as tc_task:
                check.start_tasks(current_task, tc_task, self._daq, self._channel)
                due = time.monotonic()
                while not self._halt.is_set():
                    volts, amps, temp_c = check.read_once(
                        current_task, tc_task, self._daq.current_amps_per_volt)
                    sample = Sample(time.time(), volts, amps, temp_c)
                    self._log.write(sample)
                    self._on_sample(sample)
                    # From when this one was due, so the rows do not drift
                    # later; from now if a read ran long, so they do not bunch.
                    due = max(due + self.period_s, time.monotonic())
                    self._halt.wait(due - time.monotonic())
        except Exception as exc:
            # Anything that ends the thread is shown in the window. Left
            # uncaught, the readouts would simply freeze at their last values.
            self._on_error(str(exc) or type(exc).__name__)


class RecorderWindow(QtWidgets.QMainWindow):
    # The sampling thread hands its results over through these, which Qt
    # delivers on the window's own thread.
    sample_ready = QtCore.pyqtSignal(object)
    daq_failed = QtCore.pyqtSignal(str)

    def __init__(self, daq, bank=check.ONE_OHM_BANK, sampler_factory=Sampler, log_dir=LOG_DIR):
        super().__init__()
        self.daq = daq
        self.bank = bank
        self.channel = check.tc_channel(daq, bank)
        self.log_dir = log_dir
        self.log = CsvLog(daq, bank)
        self.sampler = None
        self._sampler_factory = sampler_factory

        self._connected = False     # a sample has arrived since the sampler started
        self._plot_start = None     # time.time() at the plots' zero
        self._times = deque(maxlen=PLOT_POINTS)
        self._amps = deque(maxlen=PLOT_POINTS)
        self._temps = deque(maxlen=PLOT_POINTS)
        self._latest = None         # newest sample not yet on the readouts
        self._recent = deque(maxlen=RATE_SAMPLES)   # arrival times, for the rate

        bank_label = f"Bank {bank}" + (" (1 Ω)" if bank == check.ONE_OHM_BANK else "")
        self.setWindowTitle(f"LEM current and {bank_label} temperature")
        self.resize(900, 680)
        self._build(bank_label)

        self.sample_ready.connect(self.on_sample)
        self.daq_failed.connect(self.on_daq_failed)
        self.timer = QtCore.QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(REFRESH_MS)
        self.start_sampler()

    # ---------------------------------------------------------------- layout --

    def _build(self, bank_label):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        root = QtWidgets.QVBoxLayout(central)
        root.setContentsMargins(theme.GAP_LG, theme.GAP_LG, theme.GAP_LG, theme.GAP_LG)
        root.setSpacing(theme.GAP_MD)

        header = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(f"LEM CURRENT  ·  {bank_label.upper()} TEMPERATURE")
        title.setProperty("variant", "section")
        self.lbl_daq = QtWidgets.QLabel("NI-DAQ")
        self.lbl_daq.setStyleSheet(theme.pill_style(False))
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.lbl_daq)
        root.addLayout(header)

        cards = QtWidgets.QHBoxLayout()
        cards.setSpacing(theme.GAP_MD)
        self.card_current = MetricCard("Current", "--", theme.TRACE_CURRENT)
        self.card_volts = MetricCard("LEM signal")
        self.card_temp = MetricCard(f"{bank_label} temperature", "--", theme.TRACE_VOLTAGE)
        self.card_rate = MetricCard("Sample rate")
        self.card_rows = MetricCard("Recorded")
        for card in (self.card_current, self.card_volts, self.card_temp,
                     self.card_rate, self.card_rows):
            cards.addWidget(card)
        root.addLayout(cards)

        pg.setConfigOptions(antialias=True, background=theme.SURFACE, foreground=theme.TEXT_DIM)
        self.plot_widget = pg.GraphicsLayoutWidget()
        self.plot_widget.setStyleSheet(
            f"border: 1px solid {theme.BORDER_SUBTLE}; border-radius: {theme.RADIUS}px;")
        title_css = {'color': theme.TEXT, 'size': '11pt', 'bold': True}

        # The traces are 1 px wide on purpose. Qt draws a wider antialiased
        # line far more slowly: 6000 points took 0.46 s a redraw at 2 px and
        # 20000 took 2 s, against under 0.1 s at 1 px, and sampling stalls for
        # as long as a redraw runs.
        self.p_current = self.plot_widget.addPlot()
        self.p_current.setTitle("Current", **title_css)
        self.p_current.showGrid(x=True, y=True, alpha=0.15)
        self.p_current.getAxis('left').setLabel("Amps", color=theme.TEXT_MUTED)
        self.curve_current = self.p_current.plot(pen=pg.mkPen(theme.TRACE_CURRENT, width=1))

        self.plot_widget.nextRow()
        self.p_temp = self.plot_widget.addPlot()
        self.p_temp.setTitle(f"{bank_label} temperature", **title_css)
        self.p_temp.showGrid(x=True, y=True, alpha=0.15)
        self.p_temp.getAxis('left').setLabel("°C", color=theme.TEXT_MUTED)
        self.p_temp.getAxis('bottom').setLabel("Elapsed (s)", color=theme.TEXT_MUTED)
        self.curve_temp = self.p_temp.plot(pen=pg.mkPen(theme.TRACE_VOLTAGE, width=1))
        self.p_temp.setXLink(self.p_current)
        root.addWidget(self.plot_widget, stretch=1)

        controls = QtWidgets.QHBoxLayout()
        controls.setSpacing(theme.GAP_SM)
        self.spin_period = QtWidgets.QDoubleSpinBox()
        self.spin_period.setRange(MIN_PERIOD_S, MAX_PERIOD_S)
        self.spin_period.setDecimals(2)
        self.spin_period.setSingleStep(0.1)
        self.spin_period.setSuffix(" s")
        self.spin_period.setValue(DEFAULT_PERIOD_S)
        self.spin_period.setToolTip("Time between samples. Fixed while recording.")
        self.spin_period.valueChanged.connect(self.set_period)

        self.edit_name = QtWidgets.QLineEdit()
        self.edit_name.setPlaceholderText("optional, added to the file name")
        self.edit_name.setMinimumWidth(220)

        self.btn_record = QtWidgets.QPushButton("Record")
        self.btn_record.setCheckable(True)
        self.btn_record.setMinimumWidth(150)
        self.btn_record.setToolTip("Start a new CSV in the logs folder. Press again to stop.")
        self.btn_record.clicked.connect(self.toggle_record)

        self.btn_clear = QtWidgets.QPushButton("Clear plots")
        self.btn_clear.clicked.connect(self.clear_plots)
        self.btn_folder = QtWidgets.QPushButton("Open logs folder")
        self.btn_folder.clicked.connect(self.open_log_folder)
        self.btn_reconnect = QtWidgets.QPushButton("Reconnect DAQ")
        self.btn_reconnect.setProperty("variant", "warning")
        self.btn_reconnect.clicked.connect(self.start_sampler)
        self.btn_reconnect.hide()

        controls.addWidget(QtWidgets.QLabel("Sample every"))
        controls.addWidget(self.spin_period)
        controls.addSpacing(theme.GAP_MD)
        controls.addWidget(QtWidgets.QLabel("Name"))
        controls.addWidget(self.edit_name, stretch=1)
        controls.addWidget(self.btn_record)
        controls.addWidget(self.btn_clear)
        controls.addWidget(self.btn_folder)
        controls.addWidget(self.btn_reconnect)
        root.addLayout(controls)

        self.lbl_status = QtWidgets.QLabel()
        self.lbl_status.setProperty("variant", "mono")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setTextInteractionFlags(
            QtCore.Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(self.lbl_status)

    def set_status(self, text, colour=None, detail=""):
        self.lbl_status.setText(text)
        self.lbl_status.setToolTip(detail)
        self.lbl_status.setStyleSheet(f"color: {colour};" if colour else "")

    # -------------------------------------------------------------- sampling --

    def start_sampler(self):
        self.btn_reconnect.hide()
        self.set_status(f"Connecting: LEM on {self.daq.current_channel}, "
                        f"thermocouple on {self.channel}...")
        self.sampler = self._sampler_factory(
            self.daq, self.channel, self.spin_period.value(), self.log,
            self.sample_ready.emit, self.daq_failed.emit)
        self.sampler.start()

    def set_period(self, seconds):
        if self.sampler is not None:
            self.sampler.period_s = seconds

    def on_sample(self, sample):
        """Keep a sample for the next refresh(). Nothing is drawn here.

        Drawing on every sample starved the sampling thread: Python runs one
        thread at a time, and a plot redraw is slow. On the bench on
        2026-11-09, asked for a short period, rows came 0.13 s apart with the
        plots nearly empty and 0.21 s apart a quarter of an hour later.
        """
        if self._plot_start is None:
            self._plot_start = sample.time
        # NaN for a reading that is not a measurement, so it plots as a gap.
        self._times.append(sample.time - self._plot_start)
        self._amps.append(sample.amps if check.lem_on_scale(sample.volts, self.daq)
                          else math.nan)
        self._temps.append(sample.temp_c if check.is_temperature(sample.temp_c)
                           else math.nan)
        self._recent.append(sample.time)
        self._latest = sample

    def refresh(self):
        """Bring the readouts and plots up to date. Runs on a timer, REFRESH_MS apart."""
        if self.log.error:
            self._recording_ended(f"Recording stopped, the file could not be written: "
                                  f"{self.log.error}.", theme.DANGER)
            self.log.error = None
        elif self.log.recording:
            self.card_rows.set_value(f"{self.log.rows} rows", theme.DANGER)

        sample, self._latest = self._latest, None
        if sample is None:
            return
        if not self._connected:
            self._connected = True
            self.lbl_daq.setStyleSheet(theme.pill_style(True))
            if not self.log.recording:
                self.set_status(f"Reading: LEM on {self.daq.current_channel} at "
                                f"{self.daq.current_amps_per_volt:g} A/V, type "
                                f"{self.daq.thermocouple_type} thermocouple on {self.channel}. "
                                f"Not recording.")

        if check.lem_on_scale(sample.volts, self.daq):
            self.card_current.set_value(f"{sample.amps:8.2f} A", theme.TRACE_CURRENT)
        else:
            self.card_current.set_value("off scale", theme.WARNING)
        self.card_volts.set_value(f"{sample.volts:8.4f} V")
        if check.is_temperature(sample.temp_c):
            self.card_temp.set_value(f"{sample.temp_c:6.1f} °C", theme.TRACE_VOLTAGE)
        else:
            self.card_temp.set_value("open?", theme.WARNING)
        # The rate the samples really arrive at, which is what the CSV gets.
        # Slower than asked for means the reads take longer than the period.
        if len(self._recent) > 1 and self._recent[-1] > self._recent[0]:
            rate = (len(self._recent) - 1) / (self._recent[-1] - self._recent[0])
            self.card_rate.set_value(f"{rate:5.1f} /s")

        self._draw(self.curve_current, self._amps)
        self._draw(self.curve_temp, self._temps)

    def _draw(self, curve, values):
        y = np.fromiter(values, dtype=float, count=len(values))
        if np.isfinite(y).any():
            x = np.fromiter(self._times, dtype=float, count=len(self._times))
            curve.setData(x, y, connect="finite")
        else:
            curve.setData([], [])

    def clear_plots(self):
        for series in (self._times, self._amps, self._temps):
            series.clear()
        self.curve_current.setData([], [])
        self.curve_temp.setData([], [])
        # The time axis restarts at the next sample.
        self._plot_start = None

    def on_daq_failed(self, message):
        self.sampler = None
        self._connected = False
        self._recent.clear()
        self.card_rate.set_value("--")
        self.lbl_daq.setStyleSheet(theme.pill_style(False))
        self.btn_reconnect.show()
        first_line = message.strip().splitlines()[0] if message.strip() else "unknown error"
        text = f"DAQ error: {first_line} Is the chassis plugged in and main_v2.py closed?"
        if self.log.recording:
            self.log.stop()
            text += f" Recording stopped: {self._saved_text()}"
            self._recording_controls(False)
        self.set_status(text, theme.DANGER, detail=message)

    # ------------------------------------------------------------- recording --

    def toggle_record(self, checked):
        if checked:
            path = log_path(self.edit_name.text(), time.time(), self.log_dir)
            try:
                self.log.start(path)
            except OSError as exc:
                self._recording_controls(False)
                self.set_status(f"Could not start a recording: {exc}", theme.DANGER)
                return
            self._recording_controls(True)
            self.card_rows.set_value("0 rows", theme.DANGER)
            self.set_status(f"Recording to {path}", theme.DANGER)
        else:
            self.log.stop()
            self._recording_ended(self._saved_text())

    def _saved_text(self):
        return f"Saved {self.log.rows} rows to {self.log.path}"

    def _recording_ended(self, text, colour=None):
        self._recording_controls(False)
        self.set_status(text, colour)

    def _recording_controls(self, recording):
        self.btn_record.setChecked(recording)
        self.btn_record.setText("■  Stop recording" if recording else "Record")
        # One file, one sample rate, one name.
        self.spin_period.setEnabled(not recording)
        self.edit_name.setEnabled(not recording)
        if not recording and self.log.path:
            self.card_rows.set_value(f"{self.log.rows} rows", theme.TEXT)

    def open_log_folder(self):
        os.makedirs(self.log_dir, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(self.log_dir))

    def closeEvent(self, event):
        self.timer.stop()
        if self.sampler is not None:
            self.sampler.stop()
            self.sampler.join(timeout=2.0)
        self.log.stop()
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Watch and record the LEM current and one resistor bank's temperature.")
    parser.add_argument("--bank", type=int, default=check.ONE_OHM_BANK,
                        help=f"bank whose thermocouple to read "
                             f"(default {check.ONE_OHM_BANK}, the 1 ohm)")
    args = parser.parse_args(argv)

    daq = RigConfig.load().daq
    try:
        check.tc_channel(daq, args.bank)
    except ValueError as exc:
        parser.error(str(exc))

    app = QtWidgets.QApplication(sys.argv[:1])
    apply_theme(app)
    window = RecorderWindow(daq, args.bank)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
