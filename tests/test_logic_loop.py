"""
The real logic loop, driven through its queues, for behaviour the pure-function
tests cannot reach: what ARM does, and what reaches the resistor controller,
when the NI-DAQ is missing or is lost mid-run.

No hardware. The resistor controller is a fake that records what is written to
it, the DAQ is a thread feeding packets, and the config is the built-in
defaults rather than whatever rig_config.json holds. Each test takes about a
second.
"""
import csv
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
        'sampled_at': time.time(),
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
        self.ohms = None        # the ladder setting in circuit; None with the contactor open
        self._lock = threading.Lock()

    def write(self, data):
        if self.fail:
            raise serial.SerialException("WriteFile failed")
        with self._lock:
            self.writes.append(bytes(data))
        # What the firmware would do with it: a command closes the contactor
        # onto that setting, KILL opens it.
        if is_resistance_command(data):
            self.ohms = cl.decode_word(data.decode().strip()) * cl.RESISTOR_RESOLUTION
        elif data.strip() == b"KILL":
            self.ohms = None

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
        # The current the DAQ reports. None follows the bank: the packet's 46 V
        # across whatever the controller was last sent, and nothing once it is
        # killed. A number holds the reading there whatever the ladder is doing,
        # which is how the tests put a fault in front of the trips.
        self.amps = None
        self.readings = {}      # fields to overwrite in every packet the DAQ sends
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
            amps = self.amps
            if amps is None:
                ohms = self.resistor.ohms
                amps = 46.0 / ohms if ohms else 0.0
            packet = daq_packet(self.ni_daq, amps)
            packet.update(self.readings)
            self.daq_q.put(packet)
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
def rig(monkeypatch, tmp_path):
    # Trips go to this test's folder, never the rig's own logs/trip_log.csv.
    monkeypatch.setattr(cl, "TRIP_LOG_PATH", str(tmp_path / "trip_log.csv"))
    r = Rig(monkeypatch)
    r.trip_log = tmp_path / "trip_log.csv"
    yield r
    r.close()


def test_arm_is_refused_without_the_ni_daq(rig, capsys):
    rig.ni_daq = False
    rig.start()
    rig.wait_for("IDLE")

    rig.cmd_q.put(cl.transition_command("ARM"))
    time.sleep(0.5)

    # Refused, not armed into a fault: no RESET needed once the DAQ is back.
    assert rig.state == "IDLE"
    assert "ARMED" not in rig.seen and "FAULT" not in rig.seen
    assert "Cannot ARM: NI-DAQ OFFLINE" in capsys.readouterr().out

    rig.ni_daq = True
    time.sleep(0.2)
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")


def test_ni_daq_lost_mid_run_faults_and_kills_the_load(rig, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    rig.cmd_q.put(cl.transition_command("RUN", 1))
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

    rig.cmd_q.put(cl.transition_command("ARM"))
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
    rig.cmd_q.put(cl.transition_command("ARM"))
    time.sleep(0.5)

    assert rig.state == "IDLE"
    out = capsys.readouterr().out
    assert "DAQ settings changed. They apply on restart" in out
    assert "Cannot ARM: 11 voltage channels" in out


def test_config_change_while_armed_faults_and_kills_the_load(rig, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(cl.transition_command("ARM"))
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
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    rig.cmd_q.put(cl.transition_command("RUN", 1))
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
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    rig.cmd_q.put(cl.transition_command("RUN", 1))
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
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    rig.cmd_q.put(cl.transition_command("RUN", 1))
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
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    rig.estop.set()
    rig.wait_for("FAULT")


# ================= RESISTOR CONTROLLER LINK =================

def run_to_running(rig):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    rig.cmd_q.put(cl.transition_command("RUN", 1))
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


def test_only_a_running_rig_is_sent_alive(rig):
    # Anywhere but RUNNING the keep-alive is KILL, which does not feed the
    # Arduino's watchdog. "alive" in IDLE used to hold the watchdog off, so one
    # lost KILL at the end of a run left the contactor closed.
    rig.start()
    rig.wait_for("IDLE")
    time.sleep(1.3)
    idle = rig.resistor.written()
    assert idle.count(b"KILL\n") >= 2 and b"alive\n" not in idle

    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    time.sleep(0.7)
    assert b"alive\n" not in rig.resistor.written()

    rig.cmd_q.put(cl.transition_command("RUN", 1))
    rig.wait_for("RUNNING")
    since = len(rig.resistor.written())
    time.sleep(1.3)
    running = rig.resistor.written()[since:]
    assert running.count(b"alive\n") >= 2 and b"KILL\n" not in running

    rig.estop.set()
    rig.wait_for("FAULT")
    since = len(rig.resistor.written())
    time.sleep(1.3)
    faulted = rig.resistor.written()[since:]
    assert faulted.count(b"KILL\n") >= 2 and b"alive\n" not in faulted


def test_the_kill_is_repeated_after_a_run_ends(rig, tmp_path):
    profile = tmp_path / "short.csv"
    profile.write_text(f"Time (s),{cl.RESISTANCE_COLUMN}\n0,63.75\n0.3,63.75\n")
    rig.amps = 0.5
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(("LOAD_CSV", str(profile)))
    arm_and_run(rig)
    rig.wait_for("IDLE", timeout=4.0)

    since = len(rig.resistor.written())
    time.sleep(1.3)
    after = rig.resistor.written()[since:]
    assert after.count(b"KILL\n") >= 2 and b"alive\n" not in after


def test_a_trip_latches_with_no_controller_connected(rig, capsys):
    # DISCONNECTED used to swallow every trip: nothing shown and nothing logged
    # with every cell at 95 C, because the rig was still looking for a board.
    rig.resistor.is_open = False            # discovery never finds it
    rig.start()
    rig.wait_for("DISCONNECTED")
    rig.readings = {'max_temp': 95.0, 'temperatures': [[95.0] * 8 for _ in range(6)]}
    rig.wait_for("FAULT", timeout=1.0)

    time.sleep(0.2)
    assert rig.last['fault_reason'] == "OVERTEMP"
    (row,) = read_trip_log(rig)
    # No controller, so there was no KILL to send: blank, not "yes".
    assert (row['Reason'], row['State'], row['Kill_Sent']) == ("OVERTEMP", "DISCONNECTED", "")
    assert "OVERTEMP ALARM!" in capsys.readouterr().out

    # RESET clears it once the cells are cool again.
    rig.readings = {}
    time.sleep(0.2)
    rig.cmd_q.put(cl.transition_command("RESET"))
    rig.wait_for("DISCONNECTED", timeout=1.0)


def test_a_missing_temperature_board_does_not_fault_a_disconnected_rig(rig):
    # Only the trips that apply in every state can fire before the rig is up.
    rig.resistor.is_open = False
    rig.readings = {'voltage': 0.0, 'cell_voltages': [],
                    'hardware_status': {'temp_arduino': False, 'ni_daq': False,
                                        'res_arduino': False}}
    rig.start()
    rig.wait_for("DISCONNECTED")
    time.sleep(0.6)
    assert set(rig.seen) == {"DISCONNECTED"}


def test_a_profile_is_not_swapped_under_a_run(rig, tmp_path, capsys):
    other = tmp_path / "other.csv"
    other.write_text(f"Time (s),{cl.RESISTANCE_COLUMN}\n0,63.75\n1,63.75\n")
    run_to_running(rig)
    assert rig.last['profile_name'] == cl.DEFAULT_LAP_CSV

    rig.cmd_q.put(("LOAD_CSV", str(other)))
    time.sleep(0.5)
    assert "Ignoring profile change while RUNNING" in capsys.readouterr().out
    assert rig.last['profile_name'] == cl.DEFAULT_LAP_CSV and rig.state == "RUNNING"

    rig.estop.set()
    rig.wait_for("FAULT")
    rig.cmd_q.put(("LOAD_CSV", str(other)))
    deadline = time.monotonic() + 1.0
    while rig.last['profile_name'] != "other.csv":
        assert time.monotonic() < deadline, rig.last['profile_name']
        time.sleep(0.02)
    assert rig.last['profile_rows'] == 2


def test_a_profile_that_fails_to_load_leaves_the_old_one_in_force(rig, tmp_path, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(("LOAD_CSV", str(tmp_path / "missing.csv")))
    time.sleep(0.4)
    assert "Failed to load new CSV" in capsys.readouterr().out
    assert rig.last['profile_name'] == cl.DEFAULT_LAP_CSV and rig.last['profile_rows'] == 68


def test_a_run_needs_a_whole_lap_count(rig, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    for bad in (0, None, 2.5):
        rig.cmd_q.put(cl.transition_command("RUN", bad))
    time.sleep(0.5)
    assert rig.state == "ARMED" and rig.crash is None
    assert capsys.readouterr().out.count("Cannot RUN: laps must be a whole number") == 3


def test_a_config_that_cannot_be_used_changes_nothing(rig, capsys):
    # It used to be reported as rejected after its pack and limits were in force.
    rig.start()
    rig.wait_for("IDLE")
    before = (rig.last['active_settings'], rig.last['active_limits'])

    broken = RigConfig.defaults().to_dict()
    broken['pack']['parallel_count'] = 0        # a module with no capacity
    broken['limits']['max_temp'] = 40.0
    rig.cmd_q.put(("SET_CONFIG", broken))
    time.sleep(0.5)

    assert "Rejected bad config, nothing changed" in capsys.readouterr().out
    assert (rig.last['active_settings'], rig.last['active_limits']) == before
    assert rig.crash is None and rig.threads[2].is_alive()


def test_unexpected_error_still_kills_the_load_and_tells_the_gui(rig, monkeypatch, capsys):
    # An error in the loop used to skip the cleanup after it: no KILL, port left
    # open, and the GUI showing the last state it had been sent.
    def broken(*args, **kwargs):
        raise RuntimeError("physics blew up")
    monkeypatch.setattr(cl, "compute_required_power", broken)

    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    rig.cmd_q.put(cl.transition_command("RUN", 1))

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


# ================= MEASUREMENT INTEGRITY =================

def test_nan_current_mid_run_faults_and_kills_the_load(rig, capsys):
    run_to_running(rig)
    before = len(rig.resistor.written())
    rig.amps = float('nan')
    rig.wait_for("FAULT", timeout=1.0)

    assert b"KILL\n" in rig.resistor.written()[before:]
    out = capsys.readouterr().out
    assert "INVALID READING ALARM! Killing Load." in out
    assert "current reads nan" in out


# ================= SIDEBAR LIMITS =================

def sidebar_limits(**changes):
    """The SET_LIMITS payload the GUI sidebar sends after an edit."""
    limits = RigConfig.defaults().limits
    limits.derive_from_pack = False
    for name, value in changes.items():
        setattr(limits, name, value)
    return ("SET_LIMITS", limits.to_command_dict())


def test_sidebar_trip_past_the_cell_rating_refuses_arm(rig, capsys):
    # The sidebar used to bypass validation: a 2000 A over-current trip armed.
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(sidebar_limits(max_amps=2000.0))
    time.sleep(0.3)
    rig.cmd_q.put(cl.transition_command("ARM"))
    time.sleep(0.5)

    assert rig.state == "IDLE" and "ARMED" not in rig.seen
    assert "Cannot ARM: Over-current trip at 2005.0 A exceeds" in capsys.readouterr().out
    assert any("Over-current trip" in r for r in rig.last['arm_refusals'])


def test_sidebar_edit_past_a_rating_while_running_faults(rig, capsys):
    run_to_running(rig)
    before = len(rig.resistor.written())
    rig.cmd_q.put(sidebar_limits(min_volts=0.0))
    rig.wait_for("FAULT", timeout=1.0)

    assert b"KILL\n" in rig.resistor.written()[before:]
    out = capsys.readouterr().out
    assert "CONFIG FAULT ALARM!" in out
    assert "Undervoltage trip 0.0 V is below" in out


# ================= SETTINGS IN FORCE =================

def test_controller_reports_the_settings_in_force(rig):
    rig.start()
    rig.wait_for("IDLE")
    assert rig.last['active_settings'] == RigConfig.defaults().settings_fingerprint()
    assert rig.last['active_limits'] == RigConfig.defaults().limits.to_command_dict()

    rig.cmd_q.put(sidebar_limits(max_temp=60.0))
    deadline = time.monotonic() + 1.0
    while rig.last['active_limits']['max_temp'] != 60.0:
        assert time.monotonic() < deadline, "the new limit was never reported"
        time.sleep(0.02)


def test_config_rejected_mid_run_stays_visibly_not_in_force(rig, capsys):
    # The controller ignores a new config while RUNNING. What it reports keeps
    # the old one, so the GUI can see its own copy is not in force.
    from gui_layout import settings_mismatch

    run_to_running(rig)
    cooler = RigConfig.defaults()
    cooler.pack.cell_max_temp_c = 60.0
    cooler.limits.apply_pack_derivation(cooler.pack)
    rig.cmd_q.put(("SET_CONFIG", cooler.to_dict()))
    time.sleep(0.5)

    assert "Ignoring config change while RUNNING" in capsys.readouterr().out
    assert rig.last['active_settings'] == RigConfig.defaults().settings_fingerprint()
    mismatch = settings_mismatch(cooler, rig.last)
    assert any(line.startswith("Max temp: shown 60.0, in force 80.0") for line in mismatch)
    assert any("previous configuration" in line for line in mismatch)


def test_config_accepted_in_idle_shows_no_mismatch(rig):
    # The common path must not raise a false alarm once the change lands.
    from gui_layout import settings_mismatch

    rig.start()
    rig.wait_for("IDLE")
    cooler = RigConfig.defaults()
    cooler.pack.cell_max_temp_c = 60.0
    cooler.limits.apply_pack_derivation(cooler.pack)
    rig.cmd_q.put(("SET_CONFIG", cooler.to_dict()))

    deadline = time.monotonic() + 1.0
    while settings_mismatch(cooler, rig.last):
        assert time.monotonic() < deadline, settings_mismatch(cooler, rig.last)
        time.sleep(0.02)


class GatedQueue(queue.Queue):
    """A DAQ queue the test can hold the logic loop at.

    With the gate shut, the loop parks in its DAQ get() at the top of the next
    pass, so commands and an E-STOP can be lined up for it to find together.
    """
    def __init__(self, maxsize):
        super().__init__(maxsize)
        self.gate = threading.Event()
        self.gate.set()
        self.parked = threading.Event()

    def get(self, block=True, timeout=None):
        if not self.gate.is_set():
            self.parked.set()
            self.gate.wait()
        return super().get(block, timeout)


def hold_loop(rig):
    rig.daq_q.parked.clear()
    rig.daq_q.gate.clear()
    assert rig.daq_q.parked.wait(2.0), "the loop never came round to its DAQ read"


def test_queued_reset_arm_run_cannot_undo_an_estop(rig, capsys):
    # The reported failure: RESET, ARM and RUN already waiting in the queue
    # were processed straight after the E-STOP, in the same pass, and the rig
    # was RUNNING again with the next resistance command reconnecting the load.
    rig.daq_q = GatedQueue(maxsize=5)
    run_to_running(rig)
    hold_loop(rig)

    for cmd in (cl.transition_command("RESET"), cl.transition_command("ARM"),
                cl.transition_command("RUN", 1)):
        rig.cmd_q.put(cmd)
    rig.estop.set()
    before = len(rig.resistor.written())
    fault_index = len(rig.seen)
    rig.daq_q.gate.set()

    rig.wait_for("FAULT")
    time.sleep(1.0)
    # RUNNING packets the loop published before it was held can still be in
    # tel_q when fault_index is taken, so the tap appends them after it. Once
    # FAULT is seen, nothing else may follow.
    first_fault = rig.seen.index("FAULT", fault_index)
    assert set(rig.seen[fault_index:first_fault]) <= {"RUNNING"}
    assert set(rig.seen[first_fault:]) == {"FAULT"}
    after = rig.resistor.written()[before:]
    assert b"KILL\n" in after
    assert not any(is_resistance_command(w) for w in after[after.index(b"KILL\n"):])
    out = capsys.readouterr().out
    for name in ("RESET", "ARM", "RUN"):
        assert f"Ignoring {name}: issued before the last stop or fault." in out


def test_reset_pressed_after_the_estop_clears_it(rig):
    run_to_running(rig)
    rig.estop.set()
    rig.wait_for("FAULT")
    rig.cmd_q.put(cl.transition_command("RESET"))
    rig.wait_for("IDLE")


def test_reset_pressed_before_a_fault_does_not_clear_it(rig):
    # Not only E-STOP: a trip is a stop the operator has not yet seen.
    rig.daq_q = GatedQueue(maxsize=5)
    run_to_running(rig)
    hold_loop(rig)
    early_reset = cl.transition_command("RESET")
    rig.resistor.fail = True            # the next write fails: link lost, FAULT
    rig.daq_q.gate.set()
    rig.wait_for("FAULT")
    rig.cmd_q.put(early_reset)
    time.sleep(0.5)
    assert rig.state == "FAULT"


def test_unstamped_transition_is_refused(rig, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put("ARM")
    time.sleep(0.5)
    assert rig.state == "IDLE"
    assert "Ignoring ARM: no issue time." in capsys.readouterr().out


# ================= TRIP LOG AND TRIP REASON =================

def read_trip_log(rig):
    with open(rig.trip_log, newline='', encoding='utf-8') as fh:
        return list(csv.DictReader(fh))


def test_a_trip_mid_run_is_logged_after_the_kill(rig, monkeypatch, capsys):
    run_to_running(rig)
    # What the controller had been sent when the log was written.
    sent_before_log = []
    real_append = cl.append_trip_log

    def spy(record, path=None):
        sent_before_log.append(rig.resistor.written())
        return real_append(record, path)
    monkeypatch.setattr(cl, "append_trip_log", spy)

    rig.amps = 400.0                        # past the 280 A trip
    rig.wait_for("FAULT")
    time.sleep(0.2)

    (row,) = read_trip_log(rig)
    assert (row['Reason'], row['State'], row['Kill_Sent']) == ("OVERCURRENT", "RUNNING", "yes")
    assert row['Detail'] == "Current 400.0 A (trip 280.0 A)"
    assert row['Amps'] == "400.0" and row['Module_V'] == "46.00"
    assert 0 <= float(row['Sample_To_Kill_ms']) < 1000
    assert b"KILL\n" in sent_before_log[0]
    assert "[LOGIC] Current 400.0 A (trip 280.0 A)" in capsys.readouterr().out


def test_the_trip_reason_reaches_the_gui_until_reset(rig):
    run_to_running(rig)
    rig.amps = 400.0
    rig.wait_for("FAULT")
    time.sleep(0.2)
    assert rig.last['fault_reason'] == "OVERCURRENT"
    assert rig.last['fault_detail'] == ["Current 400.0 A (trip 280.0 A)"]

    rig.amps = 50.0
    time.sleep(0.2)
    rig.cmd_q.put(cl.transition_command("RESET"))
    rig.wait_for("IDLE")
    assert rig.last['fault_reason'] is None and rig.last['fault_detail'] == []


def test_an_estop_is_logged_with_the_state_it_stopped(rig):
    run_to_running(rig)
    rig.estop.set()
    rig.wait_for("FAULT")
    time.sleep(0.2)
    (row,) = read_trip_log(rig)
    assert (row['Reason'], row['State'], row['Kill_Sent']) == ("E-STOP", "RUNNING", "yes")
    assert rig.last['fault_reason'] == "E-STOP"


def test_a_lost_controller_is_logged_as_no_kill_sent(rig):
    run_to_running(rig)
    rig.resistor.fail = True
    rig.wait_for("FAULT", timeout=2.0)
    time.sleep(0.2)
    (row,) = read_trip_log(rig)
    assert (row['Reason'], row['Kill_Sent']) == ("RESISTOR LINK LOST", "NO")


# ================= LOAD OVERRIDE AND BENCH PROFILES =================

def settings_sent(rig, since=0):
    """Every ladder setting written since write number `since`, in steps."""
    return [cl.decode_word(w.decode().strip()) for w in rig.resistor.written()[since:]
            if is_resistance_command(w)]


def wait_for_setting(rig, steps, since=0, timeout=2.0):
    deadline = time.monotonic() + timeout
    while steps not in settings_sent(rig, since):
        assert time.monotonic() < deadline, \
            f"never commanded {steps} steps; sent {settings_sent(rig, since)}"
        time.sleep(0.01)


def arm_and_run(rig):
    rig.cmd_q.put(cl.transition_command("ARM"))
    rig.wait_for("ARMED")
    rig.cmd_q.put(cl.transition_command("RUN", 1))
    rig.wait_for("RUNNING")


def test_an_override_set_before_run_holds_its_resistance(rig, monkeypatch):
    monkeypatch.setattr(cl, "command_steps", lambda *a, **k: 6)    # the profile: 1.5 ohm
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(("SET_OVERRIDE", "RESISTANCE", 1.0))
    arm_and_run(rig)

    wait_for_setting(rig, 4)
    time.sleep(1.5)                         # across at least one profile row
    assert set(settings_sent(rig)) == {4}
    assert rig.last['load_override'] == ("RESISTANCE", 1.0)


def test_an_override_changed_mid_run_reaches_the_bank_before_the_next_row(rig, monkeypatch):
    monkeypatch.setattr(cl, "command_steps", lambda *a, **k: 6)
    run_to_running(rig)
    wait_for_setting(rig, 6)

    since = len(rig.resistor.written())
    sent = time.monotonic()
    rig.cmd_q.put(("SET_OVERRIDE", "POWER", 46.0 ** 2 / 2.0))  # 2.0 ohm at 46 V
    wait_for_setting(rig, 8, since)
    assert time.monotonic() - sent < 0.5    # the shipped rows are 1 s apart

    # Handed back: the profile's own setting at once, not at the next row.
    since = len(rig.resistor.written())
    rig.cmd_q.put(("SET_OVERRIDE", None))
    wait_for_setting(rig, 6, since, timeout=0.5)


def test_an_override_is_held_to_the_current_limit(rig):
    rig.amps = 20.0
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(sidebar_limits(max_amps=40.0))          # floor 46 / 40 = 1.15 ohm
    rig.cmd_q.put(("SET_OVERRIDE", "RESISTANCE", 0.25))
    arm_and_run(rig)

    wait_for_setting(rig, 5)                # 1.25 ohm, the first step over the floor
    time.sleep(1.0)
    assert min(settings_sent(rig)) == 5


def test_a_malformed_override_changes_nothing(rig, capsys):
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(("SET_OVERRIDE", "RESISTANCE", 1.0))
    rig.cmd_q.put(("SET_OVERRIDE", "RESISTANCE", 0.0))
    time.sleep(0.5)
    assert rig.last['load_override'] == ("RESISTANCE", 1.0)
    assert "Ignoring load override" in capsys.readouterr().out


def test_a_bench_profile_sets_the_resistance_it_names(rig, tmp_path):
    profile = tmp_path / "bench.csv"
    profile.write_text(f"Time (s),{cl.RESISTANCE_COLUMN}\n0,63.75\n0.3,2\n0.6,63.75\n")
    # The fake DAQ's current does not follow the ladder. 50 A through 63.75 ohm
    # would rightly trip banks 7 and 8; keep it under every bank's share.
    rig.amps = 0.5
    rig.start()
    rig.wait_for("IDLE")
    rig.cmd_q.put(("LOAD_CSV", str(profile)))
    arm_and_run(rig)

    rig.wait_for("IDLE", timeout=4.0)       # the run ends with the profile
    assert settings_sent(rig) == [255, 8, 255]
