"""
The real logic loop, driven through its queues, for behaviour the pure-function
tests cannot reach: what ARM does, and what reaches the resistor controller,
when the NI-DAQ is missing or is lost mid-run.

No hardware. The resistor controller is a fake that records what is written to
it, the DAQ is a thread feeding packets, and the config is the built-in
defaults rather than whatever rig_config.json holds. Each test takes about a
second.
"""
import os
import queue
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import control_logic as cl
from rig_config import RigConfig


def daq_packet(ni_daq):
    """A healthy rig with the DAQ up, or what hardware_manager sends without it."""
    packet = {
        'amps': 50.0, 'voltage': 46.0, 'cell_voltages': [3.85] * 12, 'power_kw': 2.3,
        'temperatures': [[30.0] * 8 for _ in range(6)], 'max_temp': 30.0,
        'temp_age_s': 0.05, 'temp_sensor_ages_s': [[0.05] * 8 for _ in range(6)],
        'resistor_temps': [40.0] * 4, 'resistor_temp_ages_s': [0.05] * 4,
        'hardware_status': {'temp_arduino': True, 'ni_daq': ni_daq, 'res_arduino': False},
    }
    if not ni_daq:
        packet.update(amps=0.0, voltage=0.0, cell_voltages=[], power_kw=0.0)
    return packet


def is_resistance_command(data):
    text = data.decode(errors="replace").strip()
    return len(text) == 8 and set(text) <= {"0", "1"}


class FakeResistor:
    def __init__(self):
        self.is_open = True
        self.writes = []
        self._lock = threading.Lock()

    def write(self, data):
        with self._lock:
            self.writes.append(bytes(data))

    def written(self):
        with self._lock:
            return list(self.writes)

    def close(self):
        self.is_open = False


class Rig:
    """run_logic_process on a thread, with a DAQ feeder and a telemetry tap."""

    def __init__(self, monkeypatch):
        self.resistor = FakeResistor()
        monkeypatch.setattr(cl, "auto_detect_resistor",
                            lambda discovery_lock=None: self.resistor)
        # What the logic process loads at startup. Replace before start().
        self.config = RigConfig.defaults()
        monkeypatch.setattr(RigConfig, "load", staticmethod(lambda *a, **k: self.config))

        self.ni_daq = True
        self.daq_q = queue.Queue(maxsize=5)
        self.tel_q = queue.Queue(maxsize=50)
        self.cmd_q = queue.Queue(maxsize=10)
        self.stop = threading.Event()
        self.seen = []          # every fsm_state forwarded, in order
        self.last = None        # the latest packet forwarded
        self.threads = []

    def _feed(self):
        while not self.stop.is_set():
            if self.daq_q.full():
                try:
                    self.daq_q.get_nowait()
                except queue.Empty:
                    pass
            self.daq_q.put(daq_packet(self.ni_daq))
            time.sleep(0.05)

    def _tap(self):
        while not self.stop.is_set():
            try:
                self.last = self.tel_q.get(timeout=0.1)
                self.seen.append(self.last['fsm_state'])
            except queue.Empty:
                pass

    def start(self):
        self.threads = [
            threading.Thread(target=self._feed, daemon=True),
            threading.Thread(target=self._tap, daemon=True),
            threading.Thread(target=cl.run_logic_process,
                             args=(self.daq_q, self.tel_q, self.cmd_q, self.stop),
                             daemon=True),
        ]
        for t in self.threads:
            t.start()

    @property
    def state(self):
        return self.seen[-1] if self.seen else None

    def wait_for(self, state, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.state == state:
                return
            time.sleep(0.02)
        raise AssertionError(f"never reached {state}; last {self.state}")

    def close(self):
        self.stop.set()
        for t in self.threads:
            t.join(timeout=5)


@pytest.fixture
def rig(monkeypatch):
    r = Rig(monkeypatch)
    yield r
    r.close()


def test_arm_is_refused_without_the_ni_daq(rig, capsys):
    rig.ni_daq = False
    rig.start()
    rig.wait_for("IDLE")

    rig.cmd_q.put("ARM")
    time.sleep(0.5)

    # Refused, not armed into a fault: no RESET needed once the DAQ is back.
    assert rig.state == "IDLE"
    assert "ARMED" not in rig.seen and "FAULT" not in rig.seen
    assert "Cannot ARM: NI-DAQ OFFLINE" in capsys.readouterr().out

    rig.ni_daq = True
    time.sleep(0.2)
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")


def test_ni_daq_lost_mid_run_faults_and_kills_the_load(rig, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")
    rig.cmd_q.put(("RUN", 1))
    rig.wait_for("RUNNING")

    deadline = time.monotonic() + 3.0
    while not any(is_resistance_command(w) for w in rig.resistor.written()):
        assert time.monotonic() < deadline, "the run never commanded the bank"
        time.sleep(0.02)

    before = len(rig.resistor.written())
    lost_at = time.monotonic()
    rig.ni_daq = False
    rig.wait_for("FAULT")
    # Well inside the 1 s DAQ DATA STALE timeout: this is the NI-DAQ check.
    assert time.monotonic() - lost_at < 0.8

    after = rig.resistor.written()[before:]
    assert b"KILL\n" in after
    kill = after.index(b"KILL\n")
    assert not any(is_resistance_command(w) for w in after[kill:])
    assert "NI-DAQ OFFLINE ALARM! Killing Load." in capsys.readouterr().out


def eleven_taps():
    """The default config with one cell's voltage tap missing from the list."""
    cfg = RigConfig.defaults()
    cfg.daq.voltage_channels = cfg.daq.voltage_channels[:11]
    return cfg


def test_arm_is_refused_while_the_config_leaves_a_cell_unwatched(rig, capsys):
    rig.config = eleven_taps()
    rig.start()
    rig.wait_for("IDLE")

    rig.cmd_q.put("ARM")
    time.sleep(0.5)

    assert rig.state == "IDLE"
    assert "ARMED" not in rig.seen and "FAULT" not in rig.seen
    assert "Cannot ARM: 11 voltage channels configured but the pack is 12S" \
        in capsys.readouterr().out
    # The GUI is told why, not just the console.
    assert any("11 voltage channels" in r for r in rig.last['arm_refusals'])


def test_fixing_the_wiring_in_the_gui_waits_for_a_restart(rig, capsys):
    # The DAQ process keeps the channel list it started with, so a corrected
    # list in the Configure dialog does not make those cells measured yet.
    rig.config = eleven_taps()
    rig.start()
    rig.wait_for("IDLE")

    rig.cmd_q.put(("SET_CONFIG", RigConfig.defaults().to_dict()))
    time.sleep(0.3)
    rig.cmd_q.put("ARM")
    time.sleep(0.5)

    assert rig.state == "IDLE"
    out = capsys.readouterr().out
    assert "DAQ settings changed. They apply on restart" in out
    assert "Cannot ARM: 11 voltage channels" in out


def test_config_change_while_armed_faults_and_kills_the_load(rig, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")

    # Reconfigured for a 14S module while armed; the DAQ still reads 12 taps.
    fourteen_s = RigConfig.defaults()
    fourteen_s.pack.series_count = 14
    before = len(rig.resistor.written())
    rig.cmd_q.put(("SET_CONFIG", fourteen_s.to_dict()))
    rig.wait_for("FAULT")

    assert b"KILL\n" in rig.resistor.written()[before:]
    out = capsys.readouterr().out
    assert "CONFIG FAULT ALARM! Killing Load." in out
    assert "12 voltage channels configured but the pack is 14S" in out
