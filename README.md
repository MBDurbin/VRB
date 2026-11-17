# VRB — Variable Resistor Bank

A battery-module dyno for BYU FSAE. It discharges **one 12S4P module** through a
relay-switched binary resistor ladder (0.25–63.75 Ω), following a recorded lap
speed profile, so the module can be characterised under race loads without a car.

The design, the physics and the safety layers are explained in
[PROJECT_CONTEXT.md](PROJECT_CONTEXT.md). This page is the map.

## Running it

| What | Command |
|---|---|
| The rig | `python main_v2.py` |
| Desk test without hardware | plug in the SIL dongle, then `python main_v2.py`; follow [tests/SIL_CHECKLIST.md](tests/SIL_CHECKLIST.md) |
| Trip test on bench supplies, no battery | `python main_v2.py`; follow [tests/BENCH_CHECKLIST.md](tests/BENCH_CHECKLIST.md) |
| Unit tests | `python -m pytest` (fourteen of them compile the controller sketch for the PC and skip without `g++` or `clang++`; the sketch's pin table is checked either way) |
| Relay chatter check (no hardware) | `python tools/relay_timing_check.py` |
| Does a sketch still build | `arduino-cli compile --fqbn arduino:avr:uno arduino/resistor_bank_controller` (see [arduino/README.md](arduino/README.md)) |

First time on a machine: `python -m pip install -r requirements.txt`, and
install NI-DAQmx from ni.com. Python 3.10 or newer.

Run everything from this folder. `rig_config.json` holds the car, pack, wiring and
limits, and is edited from the GUI's **Configure** button. The resistor and
bus-bar trip temperatures are edited in the file itself.

**Esc is the E-STOP** from any of the rig's windows, as well as the button.

**Watch for amber beside the car and pack label.** `LIMITS HAND-SET` means the
trips are not the ones the pack gives, and `LOAD CHECK OFF` means the rig is not
checking the current against the ladder. Both are right for the bench-supply
tests, both are saved when the window closes, and neither should be showing
with a module on. The sidebar's two tick boxes clear them.

## What's where

| Path | What it is |
|---|---|
| `main_v2.py` | Entry point. Starts the DAQ, logic and GUI processes, or SIL mode if the dongle is present. |
| `control_logic.py` | The safety-critical process: state machine, every trip, lap physics, resistor commands. |
| `hardware_manager.py` | Reads the NI-DAQ (current, cell taps, resistor thermocouples) and the temperature Arduino. |
| `gui_layout.py`, `theme.py` | The PyQt6 interface. |
| `sil_simulator.py` | Desk-test plant model that stands in for the DAQ. |
| `rig_config.py`, `rig_config.json` | All configuration, and the file it is saved to. |
| `queue_util.py` | `put_latest()`: publishes DAQ packets and telemetry onto the bounded queues without ever blocking. |
| `profiles/` | Lap speed profiles (CSV), and `Bench - ...` profiles that set the resistance directly. The GUI's **Load Profile** dialog opens here. |
| `logs/` | Recordings from the GUI's **Record** button, and `trip_log.csv`: every stop and fault with the reading that caused it. Not tracked by git. |
| `tools/` | Bench and desk tools, run by hand: see below. |
| `analysis/` | Offline cooling analysis of the bank. Not used by the running rig. |
| `arduino/` | Firmware for the resistor controller and the temperature array. See its README. |
| `datasheets/` | The cell and resistor datasheets the limits come from. |
| `docs/` | Hardware topology, and `VRB_audit_bundle.xml`: a packaged copy of the code as it was at commit `84738f2` on 15 August 2026. It predates the October and November fixes; read the files themselves, not the bundle. |
| `tests/` | Unit tests, the SIL desk-test checklist and the bench trip-test checklist. |
| `legacy/` | Superseded prototypes, kept for reference only. Not maintained. |

### tools/

| Script | Use |
|---|---|
| `bench_relay_check.py` | Command resistances and meter each relay. Battery disconnected. |
| `relay_timing_check.py` | Replays the control loop's serial traffic against the firmware's relay rules. No hardware. |
| `thermocouple_check.py` | Reads the resistor thermocouples once (Bryce's bench test). |
| `discover DAQ.py` | Lists every NI-DAQ module (model and slot) and channel name, for filling in `rig_config.json`. |
| `tap_map_check.py` | With the taps powered, reads every input and reports which cell tap each carries, and whether `voltage_channels` matches. |
| `tap_calibrate.py` | With the taps powered, reads every tap, takes a multimeter reading for each, and prints the per-tap gains to paste into `rig_config.json`. Do this before trusting a per-cell voltage or resistance. |
| `current_and_temp_check.py` | Prints the current sensor and one bank's thermocouple, live. |
| `current_and_temp_recorder.py` | The same two readings on a plot, with a Record button that writes a CSV to `logs/`. |
| `dcir_from_log.py` | Reads a recording of `Bench - DC Resistance Pulses.csv` and prints the module's and each cell's DC resistance at 1 s and 10 s, step by step down the charge. No hardware. |
| `Current Sensor.py` | Streams the current transducer reading alone, on the channel `rig_config.json` names. |

### analysis/

`CFM Calculator.py` sizes the fan for the bank's worst case. `resistor_thermal.py`
models how hot the elements get over a full discharge. Both write their figures to
`analysis/plots/`.

### legacy/

The single-file v1 controller (`Master_Code_v1.py`) and its tests, the tkinter GUI
prototypes, and early serial bench scripts. The current rig replaced all of them.
Some still hold absolute paths from the machine they were written on.
