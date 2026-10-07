"""Desk check: can the host's real serial traffic chatter the main relay?

No Arduino, DAQ or battery needed. Runs control_logic's actual logic loop against
a fake resistor controller that timestamps every line written to it, then
replays those lines through a model of the firmware's rules in
arduino/resistor_bank_controller:

  * the main relay closes on a valid resistance command (exactly 8 binary digits)
  * it opens on KILL, or when neither "alive" nor a valid command has arrived for
    more than 2 s -- the watchdog
  * nothing else (noise, a malformed command, ?WHOAMI) moves a relay or resets
    the watchdog

A mid-run open of the main relay breaks full load current, and the next command
re-closes it milliseconds later. That cycle wears the contactor and kicks the
system's inductance. A healthy run closes the relay once and opens it once.

    python tools/relay_timing_check.py

Takes about 20 s; the scenarios run in parallel. Prints PASS or FAIL for each and
exits non-zero if any fail.

The firmware rules below are a MODEL, kept in step with the sketch by hand. If
TIMEOUT_LIMIT, the command length, or which messages feed the watchdog change
there, change WATCHDOG_S, COMMAND_LENGTH and classify() here to match. This
checks the host's traffic against those rules; it cannot prove the flashed
firmware follows them. The bench check in arduino/README.md does that.
"""

import contextlib
import io
import multiprocessing
import os
import queue
import sys
import tempfile
import threading
import time
from dataclasses import dataclass

# The rig's modules live one folder up. At module level so the scenario
# processes, which re-import this file, can find them too.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mirrors arduino/resistor_bank_controller: TIMEOUT_LIMIT = 2000 ms, and
# COMMAND_LENGTH = numOtherRelays + 1.
WATCHDOG_S = 2.0
COMMAND_LENGTH = 8

# Longest allowed gap between watchdog-feeding messages while the main relay is
# closed. The heartbeat runs every 0.5 s, so ~0.5 s is expected; past 1 s, half
# the watchdog's margin is gone.
MAX_FEED_GAP_S = 1.0

# How long the host-hang scenario keeps junk arriving after the host goes quiet.
# Comfortably longer than the watchdog, so a pass proves junk did not hold it off.
JUNK_S = 4.0


@dataclass
class Scenario:
    name: str
    description: str
    row_dt: float               # seconds between lap-profile rows
    rows: int
    laps: int
    expect_open: str            # how the main relay should finally open
    corrupt_cmd: int = None     # 1-based resistance command to mangle on the wire
    hang_after_s: float = None  # host stops writing this long into the run


SCENARIOS = [
    Scenario("normal", "1 s rows like the shipped profile, 2 laps with a lap wrap",
             row_dt=1.0, rows=6, laps=2, expect_open="KILL"),
    Scenario("slow rows", "rows 2 s apart, which used to starve the watchdog between rows",
             row_dt=2.0, rows=5, laps=1, expect_open="KILL"),
    Scenario("corrupt", "one resistance command mangled on the wire mid-run",
             row_dt=1.0, rows=8, laps=1, expect_open="KILL", corrupt_cmd=4),
    Scenario("host hang", "host stops mid-run while junk keeps arriving on the line",
             row_dt=1.0, rows=10, laps=1, expect_open="watchdog", hang_after_s=4.5),
]


def healthy_packet():
    """A DAQ packet that passes every safety check once armed."""
    return {
        'amps': 50.0, 'voltage': 46.0, 'cell_voltages': [3.85] * 12, 'power_kw': 2.3,
        'temperatures': [[30.0] * 8 for _ in range(6)], 'max_temp': 30.0,
        'temp_age_s': 0.05, 'temp_sensor_ages_s': [[0.05] * 8 for _ in range(6)],
        'resistor_temps': [40.0] * 4, 'resistor_temp_ages_s': [0.05] * 4,
        'hardware_status': {'temp_arduino': True, 'ni_daq': True, 'res_arduino': False},
    }


def run_scenario(sc):
    """Drive the real logic loop through ARM and RUN. Returns what reached the wire.

    Runs in its own process: it patches control_logic's resistor discovery, which
    would otherwise leak between scenarios running side by side.
    """
    import pandas as pd
    import control_logic as cl

    writes = []                 # (monotonic time, bytes) as the Arduino would see them
    hung = threading.Event()
    sent_cmds = [0]

    def is_command(text):
        return len(text) == COMMAND_LENGTH and set(text) <= {"0", "1"}

    class FakeResistor:
        is_open = True

        def write(self, data):
            if hung.is_set():
                return          # the host is hung: nothing it writes reaches the wire
            if is_command(data.decode(errors="replace").strip()):
                sent_cmds[0] += 1
                if sent_cmds[0] == sc.corrupt_cmd:
                    data = b"10\x8311x0\n"
            writes.append((time.monotonic(), bytes(data)))

        def close(self):
            self.is_open = False

    cl.auto_detect_resistor = lambda discovery_lock=None: FakeResistor()

    daq_q, tel_q, cmd_q = queue.Queue(maxsize=5), queue.Queue(maxsize=50), queue.Queue(maxsize=10)
    stop = threading.Event()

    def feed_daq():
        while not stop.is_set():
            if daq_q.full():
                with contextlib.suppress(queue.Empty):
                    daq_q.get_nowait()
            daq_q.put(healthy_packet())
            time.sleep(0.1)

    def drain_telemetry():
        while not stop.is_set():
            with contextlib.suppress(queue.Empty):
                tel_q.get(timeout=0.2)

    log = io.StringIO()
    t_hang = None
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(log):
        # The shipped lap's speeds, re-timed to this scenario's row spacing.
        profile = pd.read_csv(cl.CSV_FILENAME).head(sc.rows).copy()
        profile["Time (s)"] = [sc.row_dt * i for i in range(len(profile))]
        csv_path = os.path.join(tmp, "profile.csv")
        profile.to_csv(csv_path, index=False)

        for target in (feed_daq, drain_telemetry):
            threading.Thread(target=target, daemon=True).start()
        logic = threading.Thread(target=cl.run_logic_process,
                                 args=(daq_q, tel_q, cmd_q, stop), daemon=True)
        logic.start()

        time.sleep(2.0)                     # DISCONNECTED -> IDLE
        cmd_q.put(("LOAD_CSV", csv_path))
        time.sleep(0.5)
        cmd_q.put("ARM")
        time.sleep(1.0)
        t_run = time.monotonic()
        cmd_q.put(("RUN", sc.laps))

        if sc.hang_after_s is not None:
            time.sleep(sc.hang_after_s)
            hung.set()
            t_hang = time.monotonic()
            while time.monotonic() - t_hang < JUNK_S:
                writes.append((time.monotonic(), b"#noise\n"))
                time.sleep(0.1)
        else:
            time.sleep(sc.row_dt * sc.rows * sc.laps + 2.0)

        stop.set()
        logic.join(5)

    return {'writes': writes, 't_run': t_run, 't_hang': t_hang, 'log': log.getvalue()}


def classify(data):
    """Which firmware branch a line takes. Mirrors loop() in the sketch."""
    text = data.decode(errors="replace").strip()
    if text == "alive":
        return "alive"
    if text in ("KILL", "kill"):
        return "kill"
    if text == "?WHOAMI":
        return "handshake"
    if len(text) == COMMAND_LENGTH and set(text) <= {"0", "1"}:
        return "command"
    return "rejected"


def replay(writes):
    """Main-relay transitions under the firmware's rules, plus the longest gap
    between watchdog-feeding messages while the relay was closed."""
    closed, last_feed, longest_gap = False, None, 0.0
    transitions = []
    events = sorted(writes)
    # A sentinel after the last line, so a watchdog that would fire once the
    # traffic stops is still seen.
    end = (events[-1][0] if events else 0.0) + WATCHDOG_S + 1.0
    for t, data in events + [(end, None)]:
        if closed and last_feed is not None and t - last_feed > WATCHDOG_S:
            closed = False
            transitions.append((last_feed + WATCHDOG_S, "watchdog"))
        if data is None:
            break
        kind = classify(data)
        if kind in ("alive", "command"):
            if closed and last_feed is not None:
                longest_gap = max(longest_gap, t - last_feed)
            last_feed = t
        if kind == "kill" and closed:
            closed = False
            transitions.append((t, "KILL"))
        elif kind == "command" and not closed:
            closed = True
            transitions.append((t, "CLOSE"))
    return transitions, longest_gap


def evaluate(sc, result):
    """Returns (passed, summary lines, failure reasons)."""
    transitions, gap = replay(result['writes'])
    kinds = [classify(d) for _, d in result['writes']]
    t0 = result['t_run']

    problems = []
    names = [what for _, what in transitions]
    if names != ["CLOSE", sc.expect_open]:
        problems.append(f"expected the main relay to close once and open once by "
                        f"{sc.expect_open}; got {names or 'no transitions'}")
    if gap > MAX_FEED_GAP_S:
        problems.append(f"watchdog went {gap:.2f} s unfed while the main relay was closed "
                        f"(limit {MAX_FEED_GAP_S:.1f} s, firmware trips at {WATCHDOG_S:.1f} s)")
    if sc.corrupt_cmd and kinds.count("rejected") != 1:
        problems.append(f"expected exactly 1 rejected line, saw {kinds.count('rejected')}")
    if sc.hang_after_s is not None and names[-1:] == ["watchdog"]:
        opened = transitions[-1][0]
        if opened > result['t_hang'] + JUNK_S:
            problems.append("watchdog only fired after the junk stopped: junk held it off")

    summary = [
        "main relay: " + (", ".join(f"{'CLOSE' if w == 'CLOSE' else 'OPEN by ' + w} "
                                    f"at {t - t0:+.2f} s" for t, w in transitions) or "never closed"),
        f"longest watchdog gap while closed {gap:.2f} s | sent {kinds.count('command')} commands, "
        f"{kinds.count('alive')} alive, {kinds.count('kill')} KILL, "
        f"{kinds.count('rejected')} rejected",
    ]
    return not problems, summary, problems


def main():
    print(f"Running {len(SCENARIOS)} scenarios in parallel (about 20 s)...\n")
    with multiprocessing.Pool(len(SCENARIOS)) as pool:
        results = pool.map(run_scenario, SCENARIOS)

    failures = 0
    for sc, result in zip(SCENARIOS, results):
        passed, summary, problems = evaluate(sc, result)
        print(f"{'PASS' if passed else 'FAIL'}  {sc.name}: {sc.description}")
        for line in summary:
            print(f"      {line}")
        for problem in problems:
            print(f"      !! {problem}")
        if not passed:
            failures += 1
            print("      logic process output:")
            for line in result['log'].strip().splitlines()[-15:]:
                print(f"        {line}")
        print()

    print("All scenarios passed." if not failures else f"{failures} scenario(s) FAILED.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
