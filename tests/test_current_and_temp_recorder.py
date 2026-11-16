"""
tools/current_and_temp_recorder.py without a DAQ: what a recording holds, that
it reaches the disk row by row, and that the window starts, stops and survives
a lost DAQ. Runs the real widgets on Qt's offscreen platform; no window appears.
"""
import csv
import importlib.util
import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import nidaqmx
import pytest
from PyQt6 import QtWidgets

from rig_config import DaqConfig

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "tools", "current_and_temp_recorder.py")
spec = importlib.util.spec_from_file_location("current_and_temp_recorder", TOOL)
rec = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rec)

Sample = rec.Sample
DAQ = DaqConfig()
T0 = time.mktime((2026, 11, 9, 14, 30, 0, 0, 0, -1))


def read_csv(path):
    with open(path, newline="") as f:
        return list(csv.reader(f))


# ================= WHAT A ROW HOLDS =================

def test_a_row_holds_the_time_the_signal_the_current_and_the_temperature():
    row = rec.csv_row(Sample(T0 + 1.5, 0.3, 30.0, 87.456), T0, DAQ)
    assert row == ["2026-11-09 14:30:01.500", "1.500", "0.30000", "30.000", "87.46"]


def test_an_off_scale_lem_leaves_the_current_empty_but_keeps_the_signal():
    # The input at its rail, as on the bench: -1044 A is not a current.
    row = rec.csv_row(Sample(T0, -10.4372, -1043.72, 20.8), T0, DAQ)
    assert row[2:] == ["-10.43720", "", "20.80"]


def test_an_open_thermocouple_leaves_the_temperature_empty():
    row = rec.csv_row(Sample(T0, 0.3, 30.0, 2284.0), T0, DAQ)
    assert row[3:] == ["30.000", ""]


def test_the_file_is_named_for_when_it_started_and_what_it_was(tmp_path):
    assert rec.log_path("", T0, str(tmp_path)) == os.path.join(
        str(tmp_path), "current_temp_20261109_143000.csv")
    assert os.path.basename(rec.log_path(" 30 A / hold: run#2 ", T0, str(tmp_path))) == (
        "current_temp_20261109_143000_30_A_hold_run_2.csv")


# ================= THE RECORDING =================

def test_each_row_is_on_disk_before_the_recording_stops(tmp_path):
    log = rec.CsvLog(DAQ, bank=3)
    path = str(tmp_path / "run.csv")
    log.start(path)
    log.write(Sample(T0 + 5.0, 0.1, 10.0, 25.0))
    log.write(Sample(T0 + 5.5, 0.2, 20.0, 26.0))

    rows = read_csv(path)                       # still recording
    assert rows[0] == ["Timestamp", "Elapsed_s", "LEM_V", "Current_A", "Bank3_Temp_C"]
    assert [r[1] for r in rows[1:]] == ["0.000", "0.500"]    # from the first row
    assert log.recording and log.rows == 2

    log.stop()
    assert not log.recording
    log.write(Sample(T0 + 6.0, 0.3, 30.0, 27.0))             # not recorded
    assert len(read_csv(path)) == 3


def test_a_recording_never_writes_over_an_earlier_one(tmp_path):
    path = tmp_path / "run.csv"
    path.write_text("earlier data\n")
    log = rec.CsvLog(DAQ, bank=3)
    with pytest.raises(FileExistsError):
        log.start(str(path))
    assert path.read_text() == "earlier data\n" and not log.recording


def test_a_file_that_stops_taking_rows_ends_the_recording_and_says_why(tmp_path):
    log = rec.CsvLog(DAQ, bank=3)
    log.start(str(tmp_path / "run.csv"))
    log.write(Sample(T0, 0.1, 10.0, 25.0))

    class Full:
        def writerow(self, row):
            raise OSError("No space left on device")

    log._writer = Full()
    log.write(Sample(T0 + 0.5, 0.1, 10.0, 25.0))            # must not raise
    assert not log.recording and log.rows == 1
    assert "No space left" in log.error


# ================= THE SAMPLING THREAD =================

class FakeTask:
    """Stands in for nidaqmx.Task: 0.3 V from the LEM, 87.5 C from the bank."""
    start_error = None
    read_error = None

    def __init__(self):
        self.kind = None
        self.ai_channels = self

    def add_ai_voltage_chan(self, channel, **kwargs):
        self.kind = "volts"

    def add_ai_thrmcpl_chan(self, channel, **kwargs):
        self.kind = "tc"
        return type("Chan", (), {})()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def start(self):
        if FakeTask.start_error:
            raise FakeTask.start_error

    def read(self):
        if FakeTask.read_error:
            raise FakeTask.read_error
        return 0.3 if self.kind == "volts" else 87.5


@pytest.fixture
def fake_daq(monkeypatch):
    FakeTask.start_error = FakeTask.read_error = None
    monkeypatch.setattr(rec.nidaqmx, "Task", FakeTask)
    return FakeTask


def run_sampler(log, period_s=0.005, until=3):
    samples, errors = [], []
    sampler = rec.Sampler(DAQ, "cDAQ1Mod6/ai4", period_s, log, samples.append, errors.append)
    sampler.start()
    deadline = time.monotonic() + 5.0
    while len(samples) < until and not errors and time.monotonic() < deadline:
        time.sleep(0.005)
    sampler.stop()
    sampler.join(timeout=5.0)
    assert not sampler.is_alive()
    return samples, errors


def test_the_sampler_records_each_sample_and_hands_it_on(fake_daq, tmp_path):
    log = rec.CsvLog(DAQ, bank=3)
    log.start(str(tmp_path / "run.csv"))
    samples, errors = run_sampler(log)
    log.stop()

    assert errors == [] and len(samples) >= 3
    assert samples[0].volts == pytest.approx(0.3)
    assert samples[0].amps == pytest.approx(30.0)
    assert samples[0].temp_c == 87.5
    rows = read_csv(log.path)
    assert len(rows) - 1 == log.rows == len(samples)
    assert rows[1][2:] == ["0.30000", "30.000", "87.50"]


def test_a_daq_that_will_not_start_is_reported_and_the_thread_ends(fake_daq):
    fake_daq.start_error = nidaqmx.DaqError("Device not found", -200220)
    samples, errors = run_sampler(rec.CsvLog(DAQ, bank=3))
    assert samples == [] and len(errors) == 1 and "Device not found" in errors[0]


def test_a_daq_lost_mid_run_is_reported(fake_daq):
    log = rec.CsvLog(DAQ, bank=3)
    samples, errors = [], []
    sampler = rec.Sampler(DAQ, "cDAQ1Mod6/ai4", 0.005, log, samples.append, errors.append)
    sampler.start()
    deadline = time.monotonic() + 5.0
    while not samples and time.monotonic() < deadline:
        time.sleep(0.005)
    fake_daq.read_error = nidaqmx.DaqError("Device removed", -88709)
    sampler.join(timeout=5.0)
    assert not sampler.is_alive() and samples and "Device removed" in errors[0]


# ================= THE WINDOW =================

class IdleSampler:
    """A sampler that reads nothing; the tests hand the window its samples."""
    made = []

    def __init__(self, daq, channel, period_s, log, on_sample, on_error):
        self.channel, self.period_s, self.log = channel, period_s, log
        self.started = self.stopped = False
        IdleSampler.made.append(self)

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def join(self, timeout=None):
        pass


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    IdleSampler.made = []
    win = rec.RecorderWindow(DAQ, 3, sampler_factory=IdleSampler, log_dir=str(tmp_path))
    win.timer.stop()        # the tests call refresh() themselves
    yield win
    win.close()
    win.deleteLater()


def feed(window, sample):
    """What happens to a sample: logged and handed over, then shown at the next refresh."""
    window.log.write(sample)
    window.on_sample(sample)
    window.refresh()


def test_it_opens_reading_the_1_ohm_bank_and_not_recording(window):
    sampler, = IdleSampler.made
    assert sampler.started and sampler.channel == "cDAQ1Mod6/ai4"
    assert sampler.period_s == rec.DEFAULT_PERIOD_S
    assert not window.log.recording and not window.btn_record.isChecked()


def test_the_readouts_follow_the_samples(window):
    feed(window, Sample(T0, 0.3, 30.0, 87.46))
    assert window.card_current.value.text().strip() == "30.00 A"
    assert window.card_volts.value.text().strip() == "0.3000 V"
    assert window.card_temp.value.text().strip() == "87.5 °C"
    assert "Not recording" in window.lbl_status.text()


def test_a_sample_is_not_drawn_until_the_refresh(window):
    # Drawing as each sample arrived held up the sampling thread.
    window.on_sample(Sample(T0, 0.3, 30.0, 87.46))
    assert window.card_current.value.text() == "--"
    assert window.curve_current.getData()[0] is None
    window.refresh()
    assert window.card_current.value.text().strip() == "30.00 A"
    assert list(window.curve_current.getData()[1]) == [30.0]


def test_the_sample_rate_shown_is_the_rate_the_samples_arrived_at(window):
    for i in range(5):
        window.on_sample(Sample(T0 + 0.25 * i, 0.3, 30.0, 25.0))
    window.refresh()
    assert window.card_rate.value.text().strip() == "4.0 /s"


def test_a_gap_in_a_trace_is_kept_as_a_gap(window):
    for i, volts in enumerate([0.3, -10.4372, 0.3]):
        window.on_sample(Sample(T0 + i, volts, volts * 100.0, 25.0))
    window.refresh()
    amps = window.curve_current.getData()[1]
    assert amps[0] == 30.0 and amps[1] != amps[1] and amps[2] == 30.0   # NaN in the middle


def test_a_reading_that_is_not_a_measurement_is_named_not_shown(window):
    feed(window, Sample(T0, -10.4372, -1043.72, 2284.0))
    assert window.card_current.value.text() == "off scale"
    assert window.card_temp.value.text() == "open?"
    assert window.card_volts.value.text().strip() == "-10.4372 V"
    # Nothing to plot, and no all-NaN curve handed to pyqtgraph.
    assert window.curve_current.getData()[0] is None or len(window.curve_current.getData()[0]) == 0


def test_record_writes_a_csv_until_it_is_pressed_again(window, tmp_path):
    feed(window, Sample(T0, 0.1, 10.0, 25.0))               # before Record: not in the file
    window.edit_name.setText("hold 1 ohm")
    window.btn_record.click()
    assert window.log.recording and window.btn_record.isChecked()
    assert not window.spin_period.isEnabled() and not window.edit_name.isEnabled()

    feed(window, Sample(T0 + 1.0, 0.2, 20.0, 30.0))
    feed(window, Sample(T0 + 1.5, 0.3, 30.0, 35.0))
    assert window.card_rows.value.text() == "2 rows"

    window.btn_record.click()
    assert not window.log.recording and not window.btn_record.isChecked()
    assert window.spin_period.isEnabled() and window.edit_name.isEnabled()
    feed(window, Sample(T0 + 2.0, 0.4, 40.0, 40.0))         # after Stop: not in the file

    files = os.listdir(tmp_path)
    assert len(files) == 1 and files[0].endswith("_hold_1_ohm.csv")
    rows = read_csv(tmp_path / files[0])
    assert [r[3] for r in rows[1:]] == ["20.000", "30.000"]
    assert "Saved 2 rows" in window.lbl_status.text() and files[0] in window.lbl_status.text()


def test_the_sample_period_reaches_the_running_sampler(window):
    window.spin_period.setValue(0.1)
    assert IdleSampler.made[0].period_s == pytest.approx(0.1)


def test_a_lost_daq_stops_the_recording_keeps_the_file_and_offers_a_reconnect(window, tmp_path):
    window.show()
    window.btn_record.click()
    feed(window, Sample(T0, 0.3, 30.0, 87.5))
    window.on_daq_failed("Device removed\nStatus Code: -88709")

    assert not window.log.recording and not window.btn_record.isChecked()
    assert "DAQ error: Device removed" in window.lbl_status.text()
    assert "Saved 1 rows" in window.lbl_status.text()
    assert window.btn_reconnect.isVisible()
    path, = [tmp_path / f for f in os.listdir(tmp_path)]
    assert len(read_csv(path)) == 2

    window.btn_reconnect.click()
    assert len(IdleSampler.made) == 2 and IdleSampler.made[1].started
    assert not window.btn_reconnect.isVisible()


def test_a_recording_that_cannot_start_leaves_the_button_up(window, monkeypatch):
    def refuse(path):
        raise PermissionError("Access is denied")

    monkeypatch.setattr(window.log, "start", refuse)
    window.btn_record.click()
    assert not window.btn_record.isChecked() and window.btn_record.text() == "Record"
    assert "Could not start a recording" in window.lbl_status.text()


def test_closing_the_window_stops_the_sampler_and_closes_the_file(window):
    window.btn_record.click()
    feed(window, Sample(T0, 0.3, 30.0, 87.5))
    window.close()
    assert IdleSampler.made[0].stopped and not window.log.recording
