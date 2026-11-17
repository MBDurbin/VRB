"""
The host's resistance commands, run through the real resistor-controller firmware.

arduino/resistor_bank_controller/resistor_bank_controller.ino is compiled for the
PC against a fake Arduino core (tests/firmware_harness/) and fed exactly what
send_binary_command writes. The relays it leaves set are then compared with the
banks control_logic believes it put in circuit.

This is the check that would have caught the bit-order bug: the host wrote the
word least significant bank first while the firmware reads the last character
as bank 1, so a 32 ohm command switched in 0.25 ohm alone. Every single-bank
command is tested on its own, because an all-ones word reads the same either way
round and cannot catch a reversal. Every one of the 255 settings is tested too.

What it cannot check is the relay board's wiring: that pin 12 really bypasses
the 32 ohm bank. That is the bench meter's job (tools/bench_relay_check.py).

Needs a C++ compiler (g++, clang++ or c++ on PATH); skipped without one.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import control_logic as cl

HARNESS = os.path.join(ROOT, "tests", "firmware_harness")
SKETCH_DIR = os.path.join(ROOT, "arduino", "resistor_bank_controller")

MAIN_RELAY_PIN = 4
FIRST_PIN = 4               # the driver reports pins 4-12
HIGH, LOW = 1, 0
RELAY_OPEN = LOW            # an open bypass relay leaves its bank IN circuit
RELAY_CLOSE = HIGH


@pytest.fixture(scope="module")
def firmware(tmp_path_factory):
    compiler = next((c for c in ("g++", "clang++", "c++") if shutil.which(c)), None)
    if compiler is None:
        pytest.skip("no C++ compiler to build the firmware with")
    exe = str(tmp_path_factory.mktemp("firmware") / "resistor_bank")
    subprocess.run([compiler, "-std=c++11", "-I", HARNESS, "-I", SKETCH_DIR,
                    os.path.join(HARNESS, "resistor_bank_driver.cpp"), "-o", exe],
                   check=True, capture_output=True, text=True)
    return exe


def run(firmware, lines):
    """Feed the firmware one message per line. Returns the pin levels after each."""
    out = subprocess.run([firmware], input="".join(l + "\n" for l in lines),
                         capture_output=True, text=True, check=True, timeout=30).stdout
    states = []
    for line in out.splitlines():
        if line.startswith("PINS "):
            levels = [int(v) for v in line.split()[1:]]
            states.append({FIRST_PIN + i: v for i, v in enumerate(levels)})
    assert len(states) == len(lines)
    return states


def command(steps):
    """Exactly what the rig writes for `steps`."""
    class Capture:
        def write(self, data):
            self.data = data
    port = Capture()
    assert cl.send_binary_command(port, steps)
    return port.data.decode().rstrip("\n")


def banks_in_circuit(pins):
    """Bank numbers whose bypass relay the firmware left open."""
    return sorted(bank for bank, pin in enumerate(cl.BANK_RELAY_PIN, start=1)
                  if pins[pin] == RELAY_OPEN)


def ohms(banks):
    return sum(cl.BANK_RESISTANCE_OHM[b - 1] for b in banks)


def expected_banks(steps):
    return [k + 1 for k in cl.banks_in_circuit(steps)]


@pytest.mark.parametrize("bank", range(1, 9))
def test_each_bank_alone_switches_only_that_bank(firmware, bank):
    steps = 2 ** (bank - 1)
    pins = run(firmware, [command(steps)])[-1]
    assert pins[MAIN_RELAY_PIN] == RELAY_CLOSE
    assert banks_in_circuit(pins) == [bank]
    assert ohms(banks_in_circuit(pins)) == steps * cl.RESISTOR_RESOLUTION


def test_32_ohm_does_not_switch_in_the_0_25_ohm_bank(firmware):
    # The reported failure: 32 ohm arriving as 00000001, ~202 A at 50.4 V.
    pins = run(firmware, [command(128)])[-1]
    assert pins[cl.BANK_RELAY_PIN[0]] == RELAY_CLOSE    # bank 1 bypassed
    assert ohms(banks_in_circuit(pins)) == 32.0


def test_every_setting_puts_exactly_its_banks_in_circuit(firmware):
    # One firmware run stepping through all 255 settings in turn, so every
    # transition between neighbouring settings is exercised too.
    settings = list(range(1, 256))
    states = run(firmware, [command(s) for s in settings])
    wrong = [(s, banks_in_circuit(p)) for s, p in zip(settings, states)
             if banks_in_circuit(p) != expected_banks(s) or p[MAIN_RELAY_PIN] != RELAY_CLOSE]
    assert wrong == []


def test_every_setting_from_a_cold_start(firmware):
    # From power-up and from just after a KILL, the paths a run actually starts on.
    for s in (1, 2, 64, 128, 255):
        assert banks_in_circuit(run(firmware, [command(s)])[-1]) == expected_banks(s)
        assert banks_in_circuit(run(firmware, [command(255), "KILL", command(s)])[-1]) \
            == expected_banks(s)


def test_all_zeros_is_refused_and_moves_nothing(firmware):
    # Every bank bypassed is a dead short. It used to be turned into bank 1
    # alone, the heaviest load there is. From cold the contactor stays open.
    pins = run(firmware, ["0" * 8])[-1]
    assert pins[MAIN_RELAY_PIN] == RELAY_OPEN
    assert banks_in_circuit(pins) == list(range(1, 9))

    # Mid-run it leaves the setting in circuit exactly as it was.
    pins = run(firmware, [command(128), "0" * 8])[-1]
    assert pins[MAIN_RELAY_PIN] == RELAY_CLOSE
    assert banks_in_circuit(pins) == [8]


def test_all_zeros_does_not_feed_the_watchdog(firmware):
    # A refused line is not control traffic: the bank still sheds 2 s after
    # the last real command, however many arrive.
    pins = run(firmware, [command(128), "@wait 1500", "0" * 8, "@wait 600"])[-1]
    assert pins[MAIN_RELAY_PIN] == RELAY_OPEN


def test_kill_opens_the_main_relay_and_puts_every_bank_in_circuit(firmware):
    pins = run(firmware, [command(128), "KILL"])[-1]
    assert pins[MAIN_RELAY_PIN] == RELAY_OPEN
    assert banks_in_circuit(pins) == list(range(1, 9))


class TestEncoding:
    """The host side of the mapping, without the firmware."""

    def test_word_reads_as_binary_steps(self):
        assert cl.encode_steps(128) == "10000000"
        assert cl.encode_steps(1) == "00000001"
        assert all(cl.encode_steps(s) == format(s, "08b") for s in range(1, 256))

    def test_decode_inverts_encode(self):
        assert all(cl.decode_word(cl.encode_steps(s)) == s for s in range(1, 256))

    def test_mapping_covers_every_bank_once(self):
        assert sorted(cl.COMMAND_BANKS) == list(range(1, 9))
        assert len(set(cl.BANK_RELAY_PIN)) == 8
        assert MAIN_RELAY_PIN not in cl.BANK_RELAY_PIN

    def test_a_setting_outside_the_ladder_is_the_lightest_load_never_the_heaviest(self):
        # Nothing asks for one. If something ever does, it gets every bank in
        # circuit, 63.75 ohm, and never the all-zero word the firmware refuses.
        for steps in (0, -1, 256, 1000):
            assert cl.encode_steps(steps) == "11111111"
        assert "00000000" not in {cl.encode_steps(s) for s in range(-5, 300)}


def sketch_source():
    with open(os.path.join(SKETCH_DIR, "resistor_bank_controller.ino"), encoding="utf-8") as fh:
        return fh.read()


def sketch_int(source, name):
    """The value of `const int|long NAME = <number>;` in the sketch."""
    match = re.search(rf"const\s+(?:int|long)\s+{name}\s*=\s*(\d+)\s*;", source)
    assert match, f"{name} not found in the sketch"
    return int(match.group(1))


class TestSketchAgainstHost:
    """The sketch's own tables, read from its source, against the host's.

    The tests above compile and run the firmware, and skip on a machine with no
    C++ compiler, which is how the pin 10 and 11 swap went untested on the
    laptop it was made on. These need nothing but the file, so they always run.
    They check the tables agree; only the compiled tests check what the sketch
    does with them.
    """

    def test_every_command_character_drives_the_pin_the_host_expects(self):
        source = sketch_source()
        match = re.search(r"otherRelayPins\[\]\s*=\s*\{([^}]*)\}", source)
        assert match, "otherRelayPins not found in the sketch"
        other_pins = [int(p) for p in match.group(1).split(",")]
        # The sketch drives characters 0..6 onto otherRelayPins in order, and
        # the last character onto BANK_1_RELAY.
        sketch_pins = other_pins + [sketch_int(source, "BANK_1_RELAY")]
        host_pins = [cl.BANK_RELAY_PIN[bank - 1] for bank in cl.COMMAND_BANKS]
        assert sketch_pins == host_pins

    def test_the_main_contactor_pin_and_the_command_length(self):
        source = sketch_source()
        assert sketch_int(source, "RELAY_MAIN_PIN") == MAIN_RELAY_PIN
        assert re.search(r"COMMAND_LENGTH\s*=\s*numOtherRelays\s*\+\s*1\s*;", source)
        assert len(cl.COMMAND_BANKS) == 8

    def test_an_open_relay_is_a_low_pin(self):
        # '1' in the word means the bypass relay open and the bank in circuit.
        source = sketch_source()
        assert re.search(r"RELAY_OPEN\s*=\s*LOW\s*;", source)
        assert re.search(r"RELAY_CLOSE\s*=\s*HIGH\s*;", source)

    def test_the_heartbeat_fits_inside_the_sketchs_watchdog(self):
        timeout_s = sketch_int(sketch_source(), "TIMEOUT_LIMIT") / 1000.0
        assert cl.HEARTBEAT_INTERVAL_S * 3 < timeout_s

    def test_the_sketch_answers_the_lines_the_host_sends(self):
        source = sketch_source()
        for line in ('"?WHOAMI"', '"RESISTOR_CTRL"', '"alive"', '"KILL"'):
            assert line in source, line
        assert cl.keepalive_message("RUNNING") == b"alive\n"
        assert cl.keepalive_message("IDLE") == b"KILL\n"

    def test_only_alive_and_a_command_feed_the_watchdog(self):
        # KILL is the keep-alive outside a run. If the sketch ever fed its
        # watchdog on KILL, the watchdog would never lapse in FAULT.
        source = sketch_source()
        kill_branch = re.search(r'incomingData == "KILL".*?\{(.*?)\}', source, re.S)
        assert kill_branch and "feedWatchdog" not in kill_branch.group(1)
        # Two calls in loop(): for "alive", and for a valid command.
        assert len(re.findall(r"^\s+feedWatchdog\(\);", source, re.M)) == 2

    def test_all_zeros_is_refused_before_the_watchdog_is_fed(self):
        source = sketch_source()
        refused = source.index("All zeros would short the bank")
        fed = source.index("feedWatchdog();", source.index("isValidCommand(incomingData)"))
        assert refused < fed
