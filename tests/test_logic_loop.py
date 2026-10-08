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
import serial

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import control_logic as cl
from rig_config import RigConfig


def daq_packet(ni_daq, amps=50.0):
    """A healthy rig with the DAQ up, or what hardware_manager sends without it."""
    packet = {
        'amps': amps, 'voltage': 46.0, 'cell_voltages': [3.85] * 12, 'power_kw': 2.3,
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
        self.fail = False       # set to make every write raise, like a pulled cable
        self.writes = []
        self._lock = threading.Lock()

    def write(self, data):
        if self.fail:
            raise serial.SerialException("WriteFile failed")
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
        # Discovery finds the controller while it is open. Once the logic closes
        # a failed handle it stays gone, as an unplugged board would.
        monkeypatch.setattr(cl, "auto_detect_resistor",
                            lambda discovery_lock=None:
                            self.resistor if self.resistor.is_open else None)
        # What the logic process loads at startup. Replace before start().
        self.config = RigConfig.defaults()
        monkeypatch.setattr(RigConfig, "load", staticmethod(lambda *a, **k: self.config))

        self.ni_daq = True
        self.amps = 50.0
        self.daq_q = queue.Queue(maxsize=5)
        self.tel_q = queue.Queue(maxsize=50)
        self.cmd_q = queue.Queue(maxsize=10)
        self.stop = threading.Event()
        self.estop = threading.Event()   # the GUI's E-STOP signal
        self.seen = []          # every fsm_state forwarded, in order
        self.last = None        # the latest packet forwarded
        self.crash = None       # what run_logic_process raised, if anything
        self.threads = []

    def _logic(self):
        try:
            cl.run_logic_process(self.daq_q, self.tel_q, self.cmd_q, self.stop,
                                 None, self.estop)
        except Exception as exc:
            self.crash = exc

    def _feed(self):
        while not self.stop.is_set():
            if self.daq_q.full():
                try:
                    self.daq_q.get_nowait()
                except queue.Empty:
                    pass
            self.daq_q.put(daq_packet(self.ni_daq, self.amps))
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
            threading.Thread(target=self._logic, daemon=True),
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


def test_bank_over_its_own_rating_trips_once_the_setting_settles(rig, monkeypatch, capsys):
    # Force 0.5 ohm, bank 2 alone, and feed a current that puts it at 5.8 kW:
    # past its 5.6 kW (140%) though nowhere near the 11.2 kW ladder total.
    monkeypatch.setattr(cl, "command_steps", lambda *a, **k: 2)
    rig.amps = 5800.0 / 46.0
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")
    rig.cmd_q.put(("RUN", 1))
    rig.wait_for("RUNNING")

    deadline = time.monotonic() + 3.0
    while not any(is_resistance_command(w) for w in rig.resistor.written()):
        assert time.monotonic() < deadline, "the run never commanded the bank"
        time.sleep(0.01)
    commanded_at = time.monotonic()

    rig.wait_for("FAULT")
    # Held off until the relays and the DAQ have caught up with the setting.
    assert time.monotonic() - commanded_at >= cl.BANK_SETTLE_S - 0.1
    out = capsys.readouterr().out
    assert "BANK OVERPOWER ALARM! Killing Load." in out
    assert "Bank 2 at 5800 W (limit 5600 W)" in out


def test_sidebar_switch_to_rated_power_reaches_the_trip(rig, monkeypatch, capsys):
    # 4.2 kW in bank 2: inside 140% of its 4 kW, outside 100%.
    monkeypatch.setattr(cl, "command_steps", lambda *a, **k: 2)
    rig.amps = 4200.0 / 46.0
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")
    rig.cmd_q.put(("RUN", 1))
    rig.wait_for("RUNNING")
    time.sleep(cl.BANK_SETTLE_S + 0.5)
    assert rig.state == "RUNNING"

    rated = RigConfig.defaults().limits
    rated.bank_rated_power_only = True
    rig.cmd_q.put(("SET_LIMITS", rated.to_command_dict()))
    rig.wait_for("FAULT")
    assert "Bank 2 at 4200 W (limit 4000 W)" in capsys.readouterr().out


def test_estop_gets_through_a_jammed_command_queue(rig, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")
    rig.cmd_q.put(("RUN", 1))
    rig.wait_for("RUNNING")

    # Fill the command queue the way spinbox spam would, then press E-STOP.
    limits = RigConfig.defaults().limits.to_command_dict()
    while not rig.cmd_q.full():
        rig.cmd_q.put_nowait(("SET_LIMITS", limits))
    before = len(rig.resistor.written())
    pressed = time.monotonic()
    rig.estop.set()

    rig.wait_for("FAULT")
    assert time.monotonic() - pressed < 0.5
    assert b"KILL\n" in rig.resistor.written()[before:]
    assert not rig.estop.is_set()           # taken, so one press acts once
    assert capsys.readouterr().out.count("EMERGENCY STOP triggered via GUI.") == 1


class DrainedBeforeGet(queue.Queue):
    """Reports full, as it was a moment ago, though the GUI has since emptied
    it: the race between full() and a blocking get()."""
    def full(self):
        return True


def test_loop_keeps_running_when_the_gui_drains_telemetry_mid_publish(rig):
    # The old `if full(): get()` waited forever on this queue, freezing every
    # trip, the E-STOP and the heartbeat. Nothing past the first packet ran.
    rig.tel_q = DrainedBeforeGet(maxsize=50)
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")
    rig.estop.set()
    rig.wait_for("FAULT")


# ================= RESISTOR CONTROLLER LINK =================

def run_to_running(rig):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")
    rig.cmd_q.put(("RUN", 1))
    rig.wait_for("RUNNING")


def test_failed_write_mid_run_latches_a_fault_and_drops_the_link(rig, capsys):
    run_to_running(rig)
    rig.resistor.fail = True            # cable pulled: every write raises
    rig.wait_for("FAULT", timeout=2.0)

    time.sleep(0.5)
    # Latched, not DISCONNECTED, and the GUI is told the controller is gone.
    assert rig.state == "FAULT"
    assert rig.last['hardware_status']['res_arduino'] is False
    assert not rig.resistor.is_open     # handle invalidated, discovery re-armed
    assert rig.crash is None and rig.threads[2].is_alive()
    assert "RESISTOR LINK LOST ALARM!" in capsys.readouterr().out


def test_port_closed_mid_run_latches_a_fault_not_disconnected(rig, capsys):
    # This used to drop to DISCONNECTED, and a reconnect then went straight
    # back to IDLE with no fault on record.
    run_to_running(rig)
    rig.resistor.close()
    rig.wait_for("FAULT", timeout=2.0)
    time.sleep(0.5)
    assert rig.state == "FAULT"
    assert "link lost (port closed)" in capsys.readouterr().out


def test_failed_kill_does_not_crash_the_loop(rig, capsys):
    # The KILL writes were unguarded: a dead link raised out of the E-STOP
    # handler and took the whole logic process down.
    run_to_running(rig)
    rig.resistor.fail = True
    rig.estop.set()
    rig.wait_for("FAULT", timeout=2.0)

    count = len(rig.seen)
    time.sleep(0.5)
    assert rig.crash is None and rig.threads[2].is_alive()
    assert len(rig.seen) > count                # still publishing telemetry
    assert "Resistor controller link lost" in capsys.readouterr().out


def test_heartbeat_failure_in_idle_disconnects_without_a_fault(rig):
    # Nothing is loaded in IDLE, so a lost link is not-connected, not a fault.
    rig.start()
    rig.wait_for("IDLE")
    rig.resistor.fail = True
    rig.wait_for("DISCONNECTED", timeout=2.0)
    assert "FAULT" not in rig.seen


def test_unexpected_error_still_kills_the_load_and_tells_the_gui(rig, monkeypatch, capsys):
    # An error in the loop used to skip the cleanup after it: no KILL, port left
    # open, and the GUI showing the last state it had been sent.
    def broken(*args, **kwargs):
        raise RuntimeError("physics blew up")
    monkeypatch.setattr(cl, "compute_required_power", broken)

    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    rig.wait_for("ARMED")
    rig.cmd_q.put(("RUN", 1))

    deadline = time.monotonic() + 3.0
    while rig.crash is None:
        assert time.monotonic() < deadline, "the loop never hit the error"
        time.sleep(0.02)

    assert isinstance(rig.crash, RuntimeError)
    assert rig.resistor.written()[-1] == b"KILL\n"
    assert not rig.resistor.is_open
    rig.wait_for("FAULT", timeout=1.0)
    out = capsys.readouterr().out
    assert "LOGIC CRITICAL ERROR" in out
    assert "Process cleanly shutdown" not in out
