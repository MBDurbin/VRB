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


def test_zero_steps_is_bank_1_alone_on_both_sides(firmware):
    # Host and firmware each turn zero into 0.25 ohm; both must pick bank 1.
    assert command(0) == command(1)
    assert banks_in_circuit(run(firmware, ["0" * 8])[-1]) == [1]


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
