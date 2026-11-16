"""
tools/current_and_temp_check.py without a DAQ: which channels it opens, how it
scales the LEM reading, and what it prints for a thermocouple that is not there.
"""
import importlib.util
import os
import types

import nidaqmx
import pytest

import control_logic
from rig_config import DaqConfig

TOOL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "tools", "current_and_temp_check.py")
spec = importlib.util.spec_from_file_location("current_and_temp_check", TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


class FakeChannels:
    def __init__(self, task):
        self.task = task

    def add_ai_voltage_chan(self, channel, **kwargs):
        self.task.kind, self.task.channel, self.task.kwargs = "volts", channel, kwargs

    def add_ai_thrmcpl_chan(self, channel, **kwargs):
        self.task.kind, self.task.channel, self.task.kwargs = "tc", channel, kwargs
        return self.task.chan


class FakeTask:
    """Stands in for nidaqmx.Task: a 0.3 V LEM signal, an 87.5 C thermocouple."""
    made = []
    start_error = None

    def __init__(self):
        self.kind = self.channel = self.kwargs = None
        self.chan = type("Chan", (), {})()
        self.ai_channels = FakeChannels(self)
        self.started = self.closed = False
        self.reads = 0
        FakeTask.made.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def start(self):
        if FakeTask.start_error:
            raise FakeTask.start_error
        self.started = True

    def read(self):
        self.reads += 1
        return 0.3 if self.kind == "volts" else 87.5


@pytest.fixture
def fake_daq(monkeypatch):
    FakeTask.made, FakeTask.start_error = [], None
    monkeypatch.setattr(tool.nidaqmx, "Task", FakeTask)
    # The default wiring, whatever rig_config.json says today.
    config = types.SimpleNamespace(daq=DaqConfig())
    monkeypatch.setattr(tool, "RigConfig",
                        types.SimpleNamespace(load=lambda: config))
    return FakeTask


def test_the_default_bank_is_the_1_ohm_one():
    assert control_logic.BANK_RESISTANCE_OHM[tool.ONE_OHM_BANK - 1] == 1.0


def test_bank_3_is_on_jack_tc4():
    assert tool.tc_channel(DaqConfig(), 3) == "cDAQ1Mod6/ai4"


@pytest.mark.parametrize("bank", [0, 5, -1])
def test_a_bank_without_a_thermocouple_is_refused(bank):
    with pytest.raises(ValueError, match="no thermocouple"):
        tool.tc_channel(DaqConfig(), bank)


def test_the_current_is_the_mean_of_the_samples_scaled_to_amps():
    class Ramp:
        values = iter([0.10, 0.20, 0.30, 0.40])

        def read(self):
            return next(self.values)

    class Tc:
        def read(self):
            return 41.0

    volts, amps, temp = tool.read_once(Ramp(), Tc(), 100.0, samples=4)
    assert volts == pytest.approx(0.25)
    assert amps == pytest.approx(25.0)
    assert temp == 41.0


def test_a_real_temperature_is_shown_as_one():
    assert tool.temperature_text(87.46).strip() == "87.5 °C"


@pytest.mark.parametrize("reading", [2284.0, float("nan"), float("inf"), -200.0])
def test_a_reading_that_is_not_a_temperature_is_called_open(reading):
    assert tool.temperature_text(reading).startswith("open?")


def test_a_lem_input_at_its_rail_is_called_off_scale_not_a_thousand_amps():
    # What the 9215 read on the bench with the sensor not driving it.
    assert not tool.lem_on_scale(-10.4372, DaqConfig())
    assert tool.current_text(-10.4372, -1043.72, DaqConfig()).strip() == "off scale"
    assert tool.current_text(0.4016, 40.16, DaqConfig()).strip() == "40.16"
    assert not tool.lem_on_scale(float("nan"), DaqConfig())


def test_it_reads_the_lem_and_the_1_ohm_thermocouple_and_nothing_else(fake_daq, capsys):
    assert tool.main(["--count", "2", "--period", "0.001"]) == 0

    current, tc = fake_daq.made
    assert (current.kind, current.channel) == ("volts", "cDAQ1Mod7/ai0")
    assert (tc.kind, tc.channel) == ("tc", "cDAQ1Mod6/ai4")
    # The thermocouple is set up as the rig sets it up, not left at the
    # driver's fixed 25 C cold junction and 0-100 C range.
    assert tc.kwargs["cjc_source"] == nidaqmx.constants.CJCSource.BUILT_IN
    assert tc.kwargs["thermocouple_type"] == nidaqmx.constants.ThermocoupleType.K
    assert tc.kwargs["max_val"] == tool.TC_MAX_C
    assert tc.chan.ai_adc_timing_mode == nidaqmx.constants.ADCTimingMode.HIGH_SPEED
    assert current.started and tc.started and current.closed and tc.closed
    assert tc.reads == 2 and current.reads == 2 * tool.CURRENT_SAMPLES

    rows = [line.split() for line in capsys.readouterr().out.splitlines()
            if line.strip().startswith("0.")]
    assert len(rows) == 2
    assert rows[0][1:] == ["0.3000", "30.00", "87.5", "°C"]


def test_another_bank_can_be_asked_for(fake_daq):
    assert tool.main(["--bank", "1", "--count", "1", "--period", "0.001"]) == 0
    assert fake_daq.made[1].channel == "cDAQ1Mod6/ai0"


def test_a_bank_with_no_thermocouple_stops_before_the_daq_is_touched(fake_daq):
    with pytest.raises(SystemExit):
        tool.main(["--bank", "5"])
    assert fake_daq.made == []


def test_a_daq_that_will_not_start_is_reported_not_raised(fake_daq, capsys):
    fake_daq.start_error = nidaqmx.DaqError("Device not found", -200220)
    assert tool.main(["--count", "1"]) == 1
    out = capsys.readouterr().out
    assert "[DAQ ERROR]" in out and "main_v2.py" in out
    assert all(task.closed for task in fake_daq.made)
